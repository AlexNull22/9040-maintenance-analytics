"""生成六张完全合成、可重现的维护分析CSV表，不调用分析引擎。"""
from __future__ import annotations

import argparse
import csv
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import tempfile


AS_OF = date(2026, 10, 19)
TABLES = {
    "9040_通知.csv": ["notification_id", "equipment_id", "created_date", "description", "failure_code", "cause_code", "action_code", "channel", "entry_minutes"],
    "9040_工单.csv": ["order_id", "equipment_id", "notification_id", "status", "created_date", "due_date", "completed_date", "labor_hours", "cost"],
    "9040_设备.csv": ["equipment_id", "description", "location", "criticality", "active"],
    "9040_计划.csv": ["plan_id", "equipment_id", "plan_type", "interval_days", "last_done", "next_due", "active"],
    "9040_测量.csv": ["measurement_id", "equipment_id", "measured_date", "value", "lower_limit", "upper_limit", "unit"],
    "9040_录入耗时.csv": ["entry_id", "task_type", "channel", "minutes"],
}


def _rows():
    equipment = []
    for number in range(1, 21):
        equipment.append({"equipment_id": f"EQ{number:03d}", "description": "" if number == 1 else f"合成示例设备{number:03d}",
                          "location": "" if number == 2 else f"合成区域{(number - 1) % 4 + 1}",
                          "criticality": "Z" if number == 3 else ("A", "B", "C")[(number - 1) % 3],
                          "active": "false" if number in (19, 20) else "true"})
    notifications = []
    for number in range(1, 101):
        mobile = number % 2 == 1
        notifications.append({"notification_id": f"N{number:04d}", "equipment_id": f"EQ{(number - 1) % 20 + 1:03d}",
                              "created_date": (AS_OF - timedelta(days=number * 7 % 45)).isoformat(),
                              "description": "" if number % 25 == 0 else f"合成示例通知{number:04d}：例行巡检",
                              "failure_code": "" if number % 10 == 0 else f"F{number % 4 + 1:02d}",
                              "cause_code": "" if number % 15 == 0 else f"C{number % 3 + 1:02d}",
                              "action_code": "" if number % 20 == 0 else f"A{number % 5 + 1:02d}",
                              "channel": "mobile" if mobile else "desktop", "entry_minutes": str(3 + number % 3 if mobile else 8 + number % 3)})
    orders = []
    for number in range(1, 81):
        age = number * 11 % 45
        created = AS_OF - timedelta(days=age)
        status = ("open", "in_progress", "closed")[(number - 1) % 3]
        orders.append({"order_id": f"O{number:04d}", "equipment_id": f"EQ{(number - 1) % 20 + 1:03d}",
                       "notification_id": f"N{number:04d}", "status": status, "created_date": created.isoformat(),
                       "due_date": (created + timedelta(days=7)).isoformat(),
                       "completed_date": (created + timedelta(days=min(2, age))).isoformat() if status == "closed" else "",
                       "labor_hours": format(Decimal("1.25") * (number % 6 + 1), ".2f"),
                       "cost": format(Decimal(45) * (number % 9 + 1) + Decimal("0.50"), ".2f")})
    plans = []
    offsets = (-14, -7, 0, 1, 3, 7, 8, 14)
    for number in range(1, 21):
        due = AS_OF + timedelta(days=offsets[(number - 1) % len(offsets)])
        last = due - timedelta(days=31 if number == 3 else 30)
        plans.append({"plan_id": f"P{number:03d}", "equipment_id": "EQ_UNKNOWN" if number == 20 else f"EQ{number:03d}",
                      "plan_type": "合成周期保养", "interval_days": "30", "last_done": last.isoformat(),
                      "next_due": due.isoformat(), "active": "false" if number in (8, 16) else "true"})
    measurements = []
    special = {4: "40.0", 6: "5.0", 19: "99.0", 23: "35.0", 24: "20.0", 25: "5.0", 26: "10.0", 27: "30.0"}
    for number in range(1, 31):
        measurements.append({"measurement_id": f"M{number:03d}", "equipment_id": f"EQ{(number - 1) % 20 + 1:03d}",
                             "measured_date": (AS_OF - timedelta(days=2 if number <= 20 else 0)).isoformat(),
                             "value": special.get(number, f"{17 + number % 7}.0"), "lower_limit": "10.0",
                             "upper_limit": "30.0", "unit": "degC"})
    timing = []
    for number in range(1, 41):
        task = "notification_entry" if number <= 20 else "order_entry" if number <= 36 else "desktop_only_task"
        mobile = number % 2 == 1 and number <= 36
        minutes = (3 if task == "notification_entry" else 4) + number % 3 if mobile else (8 if task == "notification_entry" else 10) + number % 3
        timing.append({"entry_id": f"T{number:03d}", "task_type": task, "channel": "mobile" if mobile else "desktop", "minutes": str(minutes)})
    return {"9040_通知.csv": notifications, "9040_工单.csv": orders, "9040_设备.csv": equipment,
            "9040_计划.csv": plans, "9040_测量.csv": measurements, "9040_录入耗时.csv": timing}


def generate_samples(output_dir):
    """生成到新建或空目录；已存在的任何文件均不覆盖。返回行数及源哈希。"""
    output = Path(output_dir).absolute()
    if output.is_symlink() or (output.exists() and (not output.is_dir() or any(output.iterdir()))):
        raise ValueError("output directory must be new or empty")
    output.parent.mkdir(parents=True, exist_ok=True)
    sources = []
    with tempfile.TemporaryDirectory(prefix="9040_生成暂存_", dir=output.parent) as stage_name:
        stage = Path(stage_name)
        for filename, rows in _rows().items():
            path = stage / filename
            with path.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=TABLES[filename])
                writer.writeheader()
                writer.writerows(rows)
            sources.append({"filename": filename, "rows": len(rows), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        if output.exists():
            output.rmdir()
        os.replace(stage, output)
    return {"synthetic": True, "as_of": AS_OF.isoformat(), "sources": sources}


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成六张完全合成的维护分析CSV样例")
    parser.add_argument("--output", default=str(Path(__file__).resolve().parent / "9040_示例输入"),
                        help="新建或空的输出目录，默认脚本目录下9040_示例输入")
    args = parser.parse_args(argv)
    try:
        result = generate_samples(args.output)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Error: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
