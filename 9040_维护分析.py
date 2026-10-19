"""Five read-only maintenance analyses using synthetic CSV exports. Standard library only."""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import os
import re
import shutil
import sys
import tempfile
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from pathlib import Path
from statistics import median

TABLES = {
    "notifications": ("9040_通知.csv", "notification_id,equipment_id,created_date,description,failure_code,cause_code,action_code,channel,entry_minutes"),
    "orders": ("9040_工单.csv", "order_id,equipment_id,notification_id,status,created_date,due_date,completed_date,labor_hours,cost"),
    "equipment": ("9040_设备.csv", "equipment_id,description,location,criticality,active"),
    "plans": ("9040_计划.csv", "plan_id,equipment_id,plan_type,interval_days,last_done,next_due,active"),
    "measurements": ("9040_测量.csv", "measurement_id,equipment_id,measured_date,value,lower_limit,upper_limit,unit"),
    "entry_times": ("9040_录入耗时.csv", "entry_id,task_type,channel,minutes"),
}
NAMES = {"1": "9040_质量评分.csv", "2": "9040_维护周报.csv", "3": "9040_主数据问题.csv", "4": "9040_录入耗时对比.csv", "5": "9040_逾期预警.csv", "json": "9040_分析结果.json", "html": "9040_运行报告.html"}
HEADERS = {
    "1": "notification_id,score,missing_fields",
    "2": "date,orders_created,open_overdue,open,in_progress,closed,labor_hours,cost",
    "3": "table,record_id,equipment_id,rule,detail",
    "4": "task_type,mobile_count,desktop_count,mobile_mean,mobile_median,desktop_mean,desktop_median,comparable,estimated_per_entry_saving",
    "5": "type,equipment_id,reference_id,date,status,detail",
}
TITLES = {"1": "Notification completeness", "2": "Seven-day maintenance report", "3": "Master data checks", "4": "Mobile and desktop entry times", "5": "Due plans and measurement alerts"}


class WorkflowError(Exception):
    def __init__(self, message, issues=None):
        self.issues = list(issues or [])
        super().__init__(message + ("\n" + "\n".join("- " + x for x in self.issues) if self.issues else ""))


def iso(value):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("expected YYYY-MM-DD")
    return date.fromisoformat(value)


def number(value):
    try:
        result = Decimal(value)
        if not result.is_finite() or len(result.as_tuple().digits) > 50 or abs(result.as_tuple().exponent) > 30:
            raise ValueError("expected a finite number with at most 50 digits and 30 decimal places")
        return result
    except InvalidOperation as exc:
        raise ValueError("expected a decimal number") from exc


def public(value):
    if isinstance(value, dict):
        return {key: public(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [public(item) for item in value]
    if isinstance(value, Decimal):
        return format(value, "f")
    return value.isoformat() if isinstance(value, date) else value


def rounded(value):
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if value is not None else None


def read_data(folder):
    data, sources, issues = {}, [], []
    for key, (filename, schema) in TABLES.items():
        path, columns = folder / filename, schema.split(",")
        rows, ids = [], set()
        try:
            if path.is_symlink():
                raise ValueError("symbolic-link input files are not accepted")
            payload = path.read_bytes()
            reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig"), newline=""), strict=True)
            if not reader.fieldnames or len(reader.fieldnames) != len(columns) or set(reader.fieldnames) != set(columns):
                raise ValueError("columns must be exactly " + schema)
            for index, row in enumerate(reader, 2):
                label = f"{filename} row {index}"
                try:
                    if None in row or any(v is None for v in row.values()):
                        raise ValueError("wrong number of CSV fields")
                    row = {name: value.strip() for name, value in row.items()}
                    identity = row[columns[0]]
                    if not identity or identity in ids:
                        raise ValueError("primary ID is empty or duplicated: " + identity)
                    ids.add(identity)
                    for name in ("created_date", "due_date", "completed_date", "last_done", "next_due", "measured_date"):
                        if name in row:
                            row[name] = iso(row[name]) if row[name] else None
                            if row[name] is None and name != "completed_date":
                                raise ValueError(name + " is required")
                    for name in ("entry_minutes", "labor_hours", "cost", "value", "lower_limit", "upper_limit", "minutes"):
                        if name in row:
                            row[name] = number(row[name]) if row[name] or name not in {"lower_limit", "upper_limit"} else None
                            if name in {"entry_minutes", "labor_hours", "cost", "minutes"} and row[name] < 0:
                                raise ValueError(name + " cannot be negative")
                    if "interval_days" in row:
                        if not re.fullmatch(r"[+-]?\d+", row["interval_days"]):
                            raise ValueError("interval_days must be an integer")
                        row["interval_days"] = int(row["interval_days"])
                    if "active" in row:
                        if row["active"].lower() not in {"true", "false"}:
                            raise ValueError("active must be true or false")
                        row["active"] = row["active"].lower() == "true"
                    if "channel" in row and row["channel"] not in {"mobile", "desktop"}:
                        raise ValueError("channel must be mobile or desktop")
                    if "status" in row and row["status"] not in {"open", "in_progress", "closed"}:
                        raise ValueError("status must be open, in_progress, or closed")
                    if key == "entry_times" and not row["task_type"]:
                        raise ValueError("task_type is required")
                    rows.append(row)
                except (ValueError, OverflowError) as exc:
                    issues.append(label + ": " + str(exc))
            sources.append({"filename": filename, "sha256": hashlib.sha256(payload).hexdigest(), "rows": len(rows)})
            data[key] = rows
        except (OSError, UnicodeError, ValueError, csv.Error) as exc:
            issues.append(filename + ": " + str(exc))
    if issues:
        raise WorkflowError("Input validation failed. No reports were written.", issues)
    return data, sources


def analyze(data_dir, as_of="2026-10-19"):
    """Read and validate six CSV files; return five analyses without writing files."""
    try:
        today = iso(as_of)
    except (ValueError, TypeError) as exc:
        raise WorkflowError("Invalid as_of date; use YYYY-MM-DD.") from exc
    folder = Path(data_dir).resolve()
    if not folder.is_dir():
        raise WorkflowError("Input directory does not exist.")
    data, sources = read_data(folder)
    with localcontext() as context:
        context.prec = 120
        return public(build_analyses(data, sources, today))


def build_analyses(data, sources, today):
    ideas = {key: {"title": title, "summary": {}, "rows": []} for key, title in TITLES.items()}
    checks = ["description", "failure_code", "cause_code", "action_code"]
    for row in data["notifications"]:
        missing = [name for name in checks if not row[name]]
        ideas["1"]["rows"].append({"notification_id": row["notification_id"], "score": 100 - 25 * len(missing), "missing_fields": missing})
    scores = ideas["1"]["rows"]
    ideas["1"]["summary"] = {"records": len(scores), "average_score": rounded(Decimal(sum(r["score"] for r in scores)) / len(scores)) if scores else None, "complete_records": sum(r["score"] == 100 for r in scores), "checks": checks}
    start = today - timedelta(days=6)
    week = [r for r in data["orders"] if start <= r["created_date"] <= today]
    overdue = lambda row: row["status"] != "closed" and row["due_date"] < today and row["created_date"] <= today
    for offset in range(7):
        day = start + timedelta(days=offset)
        group = [r for r in week if r["created_date"] == day]
        ideas["2"]["rows"].append({"date": day, "orders_created": len(group), "open_overdue": sum(overdue(r) for r in group), **{status: sum(r["status"] == status for r in group) for status in ("open", "in_progress", "closed")}, "labor_hours": sum((r["labor_hours"] for r in group), Decimal(0)), "cost": sum((r["cost"] for r in group), Decimal(0))})
    ideas["2"]["summary"] = {"window_start": start, "window_end": today, "total_created": len(week), "status_counts": {status: sum(r["status"] == status for r in week) for status in ("open", "in_progress", "closed")}, "overdue_open": sum(overdue(r) for r in data["orders"]), "overdue_scope": "All orders created on or before as_of; due_date < as_of; status != closed. Daily rows use their created-date cohort.", "labor_hours": sum((r["labor_hours"] for r in week), Decimal(0)), "cost": sum((r["cost"] for r in week), Decimal(0))}
    equipment = {r["equipment_id"]: r for r in data["equipment"]}
    def issue(table, row, rule, detail):
        ideas["3"]["rows"].append({"table": table, "record_id": row[TABLES[table][1].split(",")[0]], "equipment_id": row.get("equipment_id", ""), "rule": rule, "detail": detail})
    for row in data["equipment"]:
        for name in ("description", "location"):
            if not row[name]:
                issue("equipment", row, "missing_" + name, name + " is empty")
        if row["criticality"] not in {"A", "B", "C"}:
            issue("equipment", row, "invalid_criticality", "criticality must be A, B, or C")
    for table in ("notifications", "orders", "plans", "measurements"):
        for row in data[table]:
            if row["equipment_id"] not in equipment:
                issue(table, row, "unknown_equipment", "equipment reference does not exist")
    for row in data["plans"]:
        if row["interval_days"] <= 0:
            issue("plans", row, "nonpositive_interval", "interval_days must be positive")
        else:
            try:
                expected = row["last_done"] + timedelta(days=row["interval_days"])
                if row["next_due"] != expected:
                    issue("plans", row, "next_due_mismatch", "Expected next_due " + expected.isoformat())
            except OverflowError:
                issue("plans", row, "interval_out_of_range", "Computed next_due is outside the supported calendar")
    for row in data["measurements"]:
        if row["lower_limit"] is not None and row["upper_limit"] is not None and row["lower_limit"] > row["upper_limit"]:
            issue("measurements", row, "invalid_limits", "lower_limit exceeds upper_limit")
    ideas["3"]["summary"] = {"issue_count": len(ideas["3"]["rows"]), "by_rule": dict(Counter(r["rule"] for r in ideas["3"]["rows"]))}
    for task in sorted({r["task_type"] for r in data["entry_times"]}):
        group = {channel: [r["minutes"] for r in data["entry_times"] if r["task_type"] == task and r["channel"] == channel] for channel in ("mobile", "desktop")}
        row = {"task_type": task}
        means = {}
        for channel, samples in group.items():
            means[channel] = sum(samples, Decimal(0)) / len(samples) if samples else None
            row.update({channel + "_count": len(samples), channel + "_mean": rounded(means[channel]), channel + "_median": rounded(median(samples)) if samples else None})
        row["comparable"] = all(group.values())
        row["estimated_per_entry_saving"] = rounded(means["desktop"] - means["mobile"]) if row["comparable"] else None
        ideas["4"]["rows"].append(row)
    ideas["4"]["summary"] = {"task_types": len(ideas["4"]["rows"]), "comparable_task_types": sum(r["comparable"] for r in ideas["4"]["rows"]), "interpretation": "Synthetic observations only. Desktop mean minus mobile mean in minutes; no causal or real-world savings inference. Unpaired task types are not comparable."}
    active = {key for key, row in equipment.items() if row["active"]}
    for row in data["plans"]:
        if not row["active"] or row["equipment_id"] not in active:
            continue
        due = row["next_due"]
        status = "overdue" if due < today else "due_today" if due == today else "next_7_days" if due <= today + timedelta(days=7) else None
        if status:
            ideas["5"]["rows"].append({"type": "plan", "equipment_id": row["equipment_id"], "reference_id": row["plan_id"], "date": due, "status": status, "detail": row["plan_type"]})
    latest = {}
    for row in data["measurements"]:
        key = row["equipment_id"]
        if key in active and row["measured_date"] <= today and (key not in latest or row["measured_date"] >= latest[key]["measured_date"]):
            latest[key] = row
    for key, row in sorted(latest.items()):
        lower, upper, value = row["lower_limit"], row["upper_limit"], row["value"]
        if lower is not None and upper is not None and lower > upper:
            continue
        if (lower is not None and value < lower) or (upper is not None and value > upper):
            ideas["5"]["rows"].append({"type": "measurement", "equipment_id": key, "reference_id": row["measurement_id"], "date": row["measured_date"], "status": "out_of_range", "detail": f"{value} {row['unit']}; limits {lower if lower is not None else 'unbounded'} to {upper if upper is not None else 'unbounded'}"})
    ideas["5"]["rows"].sort(key=lambda row: (row["date"], row["equipment_id"], row["reference_id"]))
    ideas["5"]["summary"] = {**{status: sum(r["status"] == status for r in ideas["5"]["rows"]) for status in ("overdue", "due_today", "next_7_days")}, "measurement_alerts": sum(r["type"] == "measurement" for r in ideas["5"]["rows"]), "measurement_policy": "Latest measurement on/before as_of per active equipment; same-date ties use the last CSV row. Limit equality is normal."}
    return {"schema_version": 1, "as_of": today, "read_only": True, "data_mode": "synthetic_demo", "sources": sources, "selected_ideas": list(TITLES), "ideas": ideas}


def safe_csv(value):
    if isinstance(value, list):
        value = "; ".join(str(item) for item in value)
    text = "" if value is None else str(value)
    return "'" + text if text.lstrip(" \t\r\n\ufeff").startswith(("=", "+", "-", "@")) else text


def write_html(path, report):
    esc = lambda value: html.escape(str(value if value is not None else "Not available"), quote=True)
    ideas = report["ideas"]
    metrics = [("COMPLETENESS", str(ideas["1"]["summary"]["average_score"] or "N/A") + "%", "Average of four required fields"), ("ORDERS THIS WEEK", ideas["2"]["summary"]["total_created"], "Created in the inclusive 7-day window"), ("MASTER DATA ISSUES", ideas["3"]["summary"]["issue_count"], "Business rules requiring review"), ("COMPARABLE TASK TYPES", ideas["4"]["summary"]["comparable_task_types"], "Both mobile and desktop samples"), ("OVERDUE ACTIVE PLANS", ideas["5"]["summary"]["overdue"], "Due before the reporting date")]
    cards = "".join(f'<article class="card"><span>{esc(label)}</span><strong>{esc(value)}</strong><small>{esc(note)}</small></article>' for label, value, note in metrics)
    modules = []
    for key, idea in ideas.items():
        summary = "".join(f'<div class="stat"><span>{esc(name.replace("_", " ").title())}</span><b>{esc(json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value)}</b></div>' for name, value in idea["summary"].items() if name not in {"checks", "interpretation", "measurement_policy", "overdue_scope"})
        headers = HEADERS[key].split(",")
        rows = "".join("<tr>" + "".join(f'<td>{esc("; ".join(str(v) for v in row.get(column, [])) if isinstance(row.get(column), list) else row.get(column))}</td>' for column in headers) + "</tr>" for row in idea["rows"][:12])
        extra = f'<p class="muted">Showing {min(12, len(idea["rows"]))} of {len(idea["rows"])} rows. Full detail is in the CSV and JSON reports.</p>'
        policy = idea["summary"].get("interpretation", idea["summary"].get("measurement_policy", idea["summary"].get("overdue_scope", "")))
        modules.append(f'<section id="idea{key}"><div class="section-title"><span class="number">0{key}</span><h2>{esc(idea["title"])}</h2><span class="readonly">READ ONLY</span></div><div class="stats">{summary}</div>{f"<p class=policy>{esc(policy)}</p>" if policy else ""}<div class="table"><table><thead><tr>{"".join(f"<th>{esc(c.replace(chr(95), chr(32)))}</th>" for c in headers)}</tr></thead><tbody>{rows or f"<tr><td colspan={len(headers)}>No records for this analysis.</td></tr>"}</tbody></table></div>{extra}</section>')
    document = '''<!doctype html><html lang="en" translate="no"><meta charset="utf-8"><meta name="google" content="notranslate"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Maintenance analytics demo</title><style>
*{box-sizing:border-box}body{margin:0;background:#f0f4f7;color:#18364a;font:14px/1.5 system-ui,Segoe UI,sans-serif}main{max-width:1400px;margin:auto;padding:30px 26px}header{background:#143549;color:white;padding:29px 32px;border-radius:14px}.eyebrow{color:#86dbca;letter-spacing:.16em;font-size:11px;font-weight:750}h1{font-size:34px;line-height:1.15;margin:11px 0}header p{color:#d0dfe7;margin-bottom:0}.cards{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin:22px 0}.card{background:white;border:1px solid #d9e4eb;border-radius:11px;padding:18px 16px}.card span{font-size:10px;font-weight:700;letter-spacing:.07em;color:#647d8f}.card strong{display:block;font-size:30px;color:#087e78;margin:6px 0}.card small{display:block;color:#6a8090;font-size:11px}.notice{background:#e4f2ee;border-left:4px solid #168979;padding:14px 18px;border-radius:5px;font-size:13px}nav{display:flex;gap:18px;flex-wrap:wrap;margin:18px 0}a{color:#087d79;text-decoration:none}section{background:white;border:1px solid #dae5ec;border-radius:12px;padding:22px;margin-top:20px}.section-title{display:flex;align-items:center;gap:12px}.number{background:#e6f3f0;color:#087d79;padding:5px 9px;border-radius:7px;font-size:12px;font-weight:700}h2{font-size:20px;margin:0}.readonly{margin-left:auto;color:#63828f;font-size:10px;letter-spacing:.1em}.stats{display:flex;flex-wrap:wrap;gap:22px;margin:20px 0}.stat{max-width:100%}.stat span{display:block;color:#6b8292;font-size:11px}.stat b{font-size:15px;overflow-wrap:anywhere}.table{overflow:auto;border:1px solid #e0e8ee;border-radius:7px}table{border-collapse:collapse;width:100%;text-align:left}th{background:#f0f5f8;color:#526f83;font-size:11px;text-transform:uppercase;white-space:nowrap}th,td{padding:10px 12px;border-bottom:1px solid #e7edf2}td{font-size:12px;vertical-align:top;overflow-wrap:anywhere;max-width:360px}.muted,.policy{color:#6b8292;font-size:12px}.policy{background:#f5f8fa;padding:11px 14px;border-radius:6px}footer{color:#698091;font-size:11px;margin:24px 0}.hash{font-family:ui-monospace,Consolas,monospace;overflow-wrap:anywhere}@media(max-width:850px){.cards{grid-template-columns:repeat(2,1fr)}main{padding:15px}header{padding:22px}h1{font-size:28px}}@media print{body{background:white}main{max-width:none;padding:0}header{background:white!important;color:#18364a;border:1px solid #a9bac7}header p{color:#526f83}nav{display:none}section{break-inside:avoid}.table{overflow:visible}th,td{padding:6px;font-size:9px}.cards{gap:8px}.card strong{font-size:23px}a{color:inherit}}
</style><main><header><div class="eyebrow">MAINTENANCE ANALYTICS / FIVE WORKING IDEAS</div><h1>From synthetic exports to maintenance decisions</h1><p>Reporting date: ASOF · Six local CSV sources · Five read-only analyses</p></header><div class="cards">CARDS</div><div class="notice"><b>Independent synthetic-data demonstration.</b> No live SAP connection, credentials, SAP table mapping, or system changes. All five analyses read local CSV files only. Entry-time differences are simulated observations, not evidence of real-world savings or causation.</div><nav><a href="#idea1">01 Completeness</a><a href="#idea2">02 Weekly report</a><a href="#idea3">03 Master data</a><a href="#idea4">04 Entry times</a><a href="#idea5">05 Alerts</a></nav>MODULES<footer><b>Input integrity:</b> HASHES<br>Output CSV modules selected: SELECTED. Input files are unchanged. Decimal values are retained as exact strings in JSON; averages are rounded to two decimal places. This report is self-contained and works offline.</footer></main></html>'''
    replacements = {"ASOF": esc(report["as_of"]), "CARDS": cards, "MODULES": "".join(modules), "HASHES": "<br>".join(f'{esc(row["filename"])} — <span class="hash">{row["sha256"]}</span>' for row in report["sources"]), "SELECTED": esc(", ".join(report["selected_ideas"]))}
    Path(path).write_text(re.sub(r"\b(ASOF|CARDS|MODULES|HASHES|SELECTED)\b", lambda m: replacements[m[0]], document), encoding="utf-8")


def publish(report, data_dir, output_dir, selected):
    source, raw = Path(data_dir).resolve(), Path(output_dir).absolute()
    if raw.is_symlink():
        raise WorkflowError("Output directory cannot be a symbolic link.")
    output = raw.resolve()
    if source == output or source in output.parents or output in source.parents:
        raise WorkflowError("Input and output directories must not overlap or contain one another.")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise WorkflowError("Output directory must be new or empty; existing files are never overwritten.")
    report = {**report, "selected_ideas": list(selected), "outputs": {key: NAMES[key] for key in [*selected, "json", "html"]}}
    stage = None
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix="9040_分析暂存_", dir=output.parent))
        for key in selected:
            with (stage / NAMES[key]).open("w", encoding="utf-8-sig", newline="") as stream:
                fields = HEADERS[key].split(",")
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                for row in report["ideas"][key]["rows"]:
                    writer.writerow({name: safe_csv(row.get(name)) for name in fields})
        (stage / NAMES["json"]).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        write_html(stage / NAMES["html"], report)
        for item in report["sources"]:
            if hashlib.sha256((source / item["filename"]).read_bytes()).hexdigest() != item["sha256"]:
                raise WorkflowError("An input file changed during analysis; no reports were published.")
        if output.exists():
            output.rmdir()
        os.replace(stage, output)
        stage = None
        return report
    except WorkflowError:
        raise
    except Exception as exc:
        raise WorkflowError(f"Report publication failed; no formal output files were published: {exc}") from exc
    finally:
        if stage is not None and stage.parent.resolve() == output.parent and stage.name.startswith("9040_分析暂存_"):
            shutil.rmtree(stage)


def run_all(data_dir, output_dir, as_of="2026-10-19"):
    return publish(analyze(data_dir, as_of), data_dir, output_dir, list(TITLES))


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Five read-only maintenance analyses using synthetic CSV data.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--as-of", default="2026-10-19")
    parser.add_argument("--idea", choices=["all", *TITLES], default="all")
    args = parser.parse_args(argv)
    try:
        result = publish(analyze(args.input, args.as_of), args.input, args.output, list(TITLES) if args.idea == "all" else [args.idea])
        print(json.dumps({"as_of": result["as_of"], "selected_ideas": result["selected_ideas"], "outputs": result["outputs"]}, ensure_ascii=False, indent=2))
        return 0
    except WorkflowError as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print("ERROR: Unexpected failure: " + str(exc), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
