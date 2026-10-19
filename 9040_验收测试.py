"""维护分析的独立业务验收。仅用标准库，所有样本和输出位于临时目录。"""
from __future__ import annotations

import copy
import csv
from datetime import date, timedelta
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


def _module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


engine = _module("maintenance_9040", "9040_维护分析.py")
samples = _module("maintenance_samples_9040", "9040_生成示例.py")
AS_OF = "2026-10-19"
HEADERS = {
    "9040_通知.csv": ["notification_id", "equipment_id", "created_date", "description", "failure_code", "cause_code", "action_code", "channel", "entry_minutes"],
    "9040_工单.csv": ["order_id", "equipment_id", "notification_id", "status", "created_date", "due_date", "completed_date", "labor_hours", "cost"],
    "9040_设备.csv": ["equipment_id", "description", "location", "criticality", "active"],
    "9040_计划.csv": ["plan_id", "equipment_id", "plan_type", "interval_days", "last_done", "next_due", "active"],
    "9040_测量.csv": ["measurement_id", "equipment_id", "measured_date", "value", "lower_limit", "upper_limit", "unit"],
    "9040_录入耗时.csv": ["entry_id", "task_type", "channel", "minutes"],
}


class MaintenanceAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="9040_验收_")
        self.root = Path(self.temporary.name)
        self.input = self.root / "9040_输入"
        self.input.mkdir()
        self.addCleanup(self.temporary.cleanup)
        self.base = {name: [] for name in HEADERS}
        self.base["9040_设备.csv"] = [self.equipment("E1")]
        self.base["9040_通知.csv"] = [self.notification("N1")]
        self.tables = copy.deepcopy(self.base)
        self.write_all()

    @staticmethod
    def digest(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    @staticmethod
    def equipment(identifier, **changes):
        return {"equipment_id": identifier, "description": "合成设备", "location": "合成区域", "criticality": "A", "active": "true", **changes}

    @staticmethod
    def notification(identifier, **changes):
        return {"notification_id": identifier, "equipment_id": "E1", "created_date": AS_OF,
                "description": "合成通知", "failure_code": "F1", "cause_code": "C1", "action_code": "A1",
                "channel": "mobile", "entry_minutes": "3.50", **changes}

    @staticmethod
    def order(identifier, **changes):
        return {"order_id": identifier, "equipment_id": "E1", "notification_id": "N1", "status": "open",
                "created_date": AS_OF, "due_date": AS_OF, "completed_date": "", "labor_hours": "1.00", "cost": "10.00", **changes}

    @staticmethod
    def plan(identifier, due, **changes):
        due = date.fromisoformat(due)
        return {"plan_id": identifier, "equipment_id": "E1", "plan_type": "合成保养", "interval_days": "30",
                "last_done": (due - timedelta(days=30)).isoformat(), "next_due": due.isoformat(), "active": "true", **changes}

    @staticmethod
    def measurement(identifier, equipment_id="E1", **changes):
        return {"measurement_id": identifier, "equipment_id": equipment_id, "measured_date": AS_OF,
                "value": "20", "lower_limit": "10", "upper_limit": "30", "unit": "degC", **changes}

    @staticmethod
    def timing(identifier, task, channel, minutes):
        return {"entry_id": identifier, "task_type": task, "channel": channel, "minutes": minutes}

    def write_all(self):
        for filename, rows in self.tables.items():
            self.write_table(filename, rows)

    def write_table(self, filename, rows, headers=None):
        self.tables[filename] = rows
        with (self.input / filename).open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=HEADERS[filename] if headers is None else headers)
            writer.writeheader()
            writer.writerows(rows)

    def hashes(self):
        return {path.name: self.digest(path) for path in self.input.iterdir() if path.is_file()}

    def analyze(self):
        report = engine.analyze(self.input, AS_OF)
        self.assertEqual(report["as_of"], AS_OF)
        self.assertEqual(set(report["ideas"]), {"1", "2", "3", "4", "5"})
        return report

    def assert_rejected(self, output_name="9040_拒绝输出"):
        before = self.hashes()
        with self.assertRaises(engine.WorkflowError) as caught:
            engine.analyze(self.input, AS_OF)
        self.assertTrue(str(caught.exception))
        output = self.root / output_name
        with self.assertRaises(engine.WorkflowError):
            engine.run_all(self.input, output, AS_OF)
        self.assertFalse(output.exists() and any(output.iterdir()), "失败运行留下部分输出")
        self.assertEqual(before, self.hashes(), "失败运行修改了输入")

    def test_notification_completeness_uses_four_equal_weights_and_trims_blanks(self):
        self.write_table("9040_通知.csv", [
            self.notification("N1", failure_code=" F1 "),
            self.notification("N2", description="  "),
            self.notification("N3", failure_code="", cause_code=""),
            self.notification("N4", description="", failure_code="", cause_code="", action_code=""),
        ])
        idea = self.analyze()["ideas"]["1"]
        self.assertEqual(idea["summary"]["records"], 4)
        self.assertEqual(idea["summary"]["average_score"], "56.25")
        self.assertEqual(idea["summary"]["complete_records"], 1)
        rows = {row["notification_id"]: row for row in idea["rows"]}
        self.assertEqual({key: row["score"] for key, row in rows.items()}, {"N1": 100, "N2": 75, "N3": 50, "N4": 0})
        self.assertEqual(set(rows["N3"]["missing_fields"]), {"failure_code", "cause_code"})

    def test_no_notifications_has_no_fabricated_average(self):
        self.write_table("9040_通知.csv", [])
        idea = self.analyze()["ideas"]["1"]
        self.assertEqual({key: idea["summary"][key] for key in ("records", "average_score", "complete_records")},
                         {"records": 0, "average_score": None, "complete_records": 0})
        self.assertEqual(idea["rows"], [])

    def test_seven_day_cohort_boundaries_and_overdue_backlog_scope(self):
        self.write_table("9040_工单.csv", [
            self.order("OLD", created_date="2026-10-12", due_date="2026-10-18", labor_hours="99", cost="99"),
            self.order("FIRST", created_date="2026-10-13", due_date="2026-10-18", labor_hours="1.25", cost="10.10"),
            self.order("TODAY", status="in_progress", due_date=AS_OF, labor_hours="2.50", cost="20.20"),
            self.order("CLOSED", status="closed", due_date="2026-10-17", completed_date=AS_OF, labor_hours="3.25", cost="30.30"),
            self.order("FUTURE", created_date="2026-10-20", due_date="2026-10-21", labor_hours="100", cost="100"),
        ])
        idea = self.analyze()["ideas"]["2"]
        summary = idea["summary"]
        self.assertEqual(summary["total_created"], 3)
        self.assertEqual(summary["status_counts"], {"open": 1, "in_progress": 1, "closed": 1})
        self.assertEqual((summary["labor_hours"], summary["cost"]), ("7.00", "60.60"))
        self.assertEqual(summary["overdue_open"], 2)
        rows = idea["rows"]
        self.assertEqual([row["date"] for row in rows], [f"2026-10-{day:02d}" for day in range(13, 20)])
        self.assertEqual(sum(row["orders_created"] for row in rows), 3)
        self.assertEqual((rows[0]["orders_created"], rows[0]["open_overdue"], rows[0]["cost"]), (1, 1, "10.10"))
        self.assertEqual((rows[-1]["orders_created"], rows[-1]["open_overdue"], rows[-1]["cost"]), (2, 0, "50.50"))

    def test_equipment_quality_problems_are_reported_without_schema_rejection(self):
        self.write_table("9040_设备.csv", [self.equipment("E1", description=" ", location="", criticality="Z")])
        idea = self.analyze()["ideas"]["3"]
        self.assertEqual(idea["summary"]["issue_count"], 3)
        self.assertEqual(len(idea["rows"]), 3)
        self.assertTrue(all(row["equipment_id"] == "E1" for row in idea["rows"]))
        self.assertTrue(all(row["rule"] and row["detail"] for row in idea["rows"]))

    def test_unknown_equipment_and_invalid_plan_intervals_are_domain_issues(self):
        self.write_table("9040_计划.csv", [
            self.plan("UNKNOWN", AS_OF, equipment_id="E_UNKNOWN"),
            self.plan("ZERO", AS_OF, interval_days="0", last_done=AS_OF),
            self.plan("MISMATCH", "2026-10-20", last_done="2026-09-19"),
        ])
        idea = self.analyze()["ideas"]["3"]
        self.assertEqual(idea["summary"]["issue_count"], 3)
        self.assertEqual({row["record_id"] for row in idea["rows"]}, {"UNKNOWN", "ZERO", "MISMATCH"})

    def test_task_comparison_means_medians_and_missing_group(self):
        self.write_table("9040_录入耗时.csv", [
            self.timing("T1", "A", "mobile", "2"), self.timing("T2", "A", "mobile", "4"),
            self.timing("T3", "A", "desktop", "6"), self.timing("T4", "A", "desktop", "10"),
            self.timing("T5", "B", "mobile", "1"),
        ])
        idea = self.analyze()["ideas"]["4"]
        self.assertEqual((idea["summary"]["comparable_task_types"], idea["summary"]["task_types"]), (1, 2))
        rows = {row["task_type"]: row for row in idea["rows"]}
        first, missing = rows["A"], rows["B"]
        self.assertEqual((first["mobile_count"], first["desktop_count"]), (2, 2))
        self.assertEqual((first["mobile_mean"], first["mobile_median"], first["desktop_mean"], first["desktop_median"]), ("3.00", "3.00", "8.00", "8.00"))
        self.assertEqual(first["estimated_per_entry_saving"], "5.00")
        self.assertTrue(first["comparable"])
        self.assertFalse(missing["comparable"])
        self.assertEqual((missing["mobile_count"], missing["desktop_count"]), (1, 0))
        self.assertIsNone(missing["desktop_mean"])
        self.assertIsNone(missing["desktop_median"])
        self.assertIsNone(missing["estimated_per_entry_saving"])

    def test_mobile_slower_than_desktop_keeps_negative_saving(self):
        self.write_table("9040_录入耗时.csv", [self.timing("T1", "A", "mobile", "3"), self.timing("T2", "A", "desktop", "1")])
        row = self.analyze()["ideas"]["4"]["rows"][0]
        self.assertEqual(row["estimated_per_entry_saving"], "-2.00")

    def test_plan_due_boundaries_and_active_flags(self):
        self.write_table("9040_设备.csv", [self.equipment("E1"), self.equipment("E2", active="false")])
        self.write_table("9040_计划.csv", [
            self.plan("OVERDUE", "2026-10-18"), self.plan("TODAY", AS_OF), self.plan("DAY1", "2026-10-20"),
            self.plan("DAY7", "2026-10-26"), self.plan("DAY8", "2026-10-27"),
            self.plan("INACTIVE_PLAN", "2026-10-17", active="false"), self.plan("INACTIVE_EQUIPMENT", "2026-10-16", equipment_id="E2"),
        ])
        idea = self.analyze()["ideas"]["5"]
        self.assertEqual({key: idea["summary"][key] for key in ("overdue", "due_today", "next_7_days", "measurement_alerts")},
                         {"overdue": 1, "due_today": 1, "next_7_days": 2, "measurement_alerts": 0})
        self.assertEqual({row["reference_id"]: row["status"] for row in idea["rows"]}, {"OVERDUE": "overdue", "TODAY": "due_today", "DAY1": "next_7_days", "DAY7": "next_7_days"})

    def test_measurements_use_latest_as_of_date_and_last_row_on_ties(self):
        self.write_table("9040_设备.csv", [self.equipment(f"E{number}", active="false" if number == 5 else "true") for number in range(1, 6)])
        self.write_table("9040_测量.csv", [
            self.measurement("M1", "E1", measured_date="2026-10-18", value="99"), self.measurement("M2", "E1", value="20"),
            self.measurement("M3", "E2", value="5"), self.measurement("M4", "E2", measured_date="2026-10-20", value="20"),
            self.measurement("M5", "E3", value="20"), self.measurement("M6", "E3", value="35"),
            self.measurement("M7", "E4", value="35"), self.measurement("M8", "E4", value="20"), self.measurement("M9", "E5", value="99"),
        ])
        idea = self.analyze()["ideas"]["5"]
        self.assertEqual(idea["summary"]["measurement_alerts"], 2)
        self.assertEqual({row["reference_id"] for row in idea["rows"] if row["type"] == "measurement"}, {"M3", "M6"})

    def test_equal_measurement_limits_and_absent_limits_are_not_alerts(self):
        self.write_table("9040_设备.csv", [self.equipment(f"E{number}") for number in range(1, 6)])
        self.write_table("9040_测量.csv", [self.measurement("M1", "E1", value="10"), self.measurement("M2", "E2", value="30"),
                                              self.measurement("M3", "E3", value="5", lower_limit=""),
                                              self.measurement("M4", "E4", value="35", upper_limit=""),
                                              self.measurement("M5", "E5", value="100", lower_limit="", upper_limit="")])
        self.assertEqual(self.analyze()["ideas"]["5"]["summary"]["measurement_alerts"], 0)

    def test_reversed_measurement_limits_are_domain_issue_not_false_alert(self):
        self.write_table("9040_测量.csv", [self.measurement("M1", lower_limit="30", upper_limit="10")])
        report = self.analyze()
        self.assertEqual(report["ideas"]["3"]["summary"]["issue_count"], 1)
        self.assertEqual(report["ideas"]["5"]["summary"]["measurement_alerts"], 0)

    def test_analyze_is_read_only_and_published_sources_are_hash_auditable(self):
        before = self.hashes()
        initial_files = {str(path.relative_to(self.root)) for path in self.root.rglob("*")}
        analysis = self.analyze()
        self.assertEqual(initial_files, {str(path.relative_to(self.root)) for path in self.root.rglob("*")})
        self.assertEqual({row["filename"]: row["sha256"] for row in analysis["sources"]}, before)
        output = self.root / "9040_输出"
        report = engine.run_all(self.input, output, AS_OF)
        self.assertEqual(report["ideas"], analysis["ideas"])
        self.assertEqual(set(report["outputs"]), {"json", "html", "1", "2", "3", "4", "5"})
        for filename in report["outputs"].values():
            self.assertEqual(Path(filename).name, filename)
            self.assertTrue(filename.startswith("9040_"))
            self.assertTrue((output / filename).is_file())
        saved = json.loads((output / report["outputs"]["json"]).read_text(encoding="utf-8-sig"))
        self.assertEqual(saved["ideas"], report["ideas"])
        self.assertNotIn(str(self.root), (output / report["outputs"]["json"]).read_text(encoding="utf-8-sig"))
        self.assertEqual(self.hashes(), before)

    def test_existing_output_is_preserved(self):
        before = self.hashes()
        output = self.root / "9040_已有输出"
        output.mkdir()
        sentinel = output / "9040_原有文件.txt"
        sentinel.write_text("keep unchanged", encoding="utf-8")
        with self.assertRaises(engine.WorkflowError):
            engine.run_all(self.input, output, AS_OF)
        self.assertEqual([path.name for path in output.iterdir()], [sentinel.name])
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep unchanged")
        self.assertEqual(self.hashes(), before)

    def test_missing_file_and_wrong_schema_are_rejected(self):
        (self.input / "9040_计划.csv").unlink()
        self.assert_rejected("9040_缺表拒绝")
        self.write_all()
        self.write_table("9040_计划.csv", [], headers=HEADERS["9040_计划.csv"][:-1])
        self.assert_rejected("9040_表头拒绝")

    def test_duplicate_primary_ids_are_rejected(self):
        self.write_table("9040_设备.csv", [self.equipment("E1"), self.equipment("E1")])
        self.assert_rejected()

    def test_invalid_numbers_dates_booleans_and_enums_are_rejected(self):
        cases = [
            ("9040_通知.csv", [self.notification("N1", entry_minutes="NaN")]),
            ("9040_通知.csv", [self.notification("N1", entry_minutes="-1")]),
            ("9040_通知.csv", [self.notification("N1", created_date="10/19/2026")]),
            ("9040_通知.csv", [self.notification("N1", channel="phone")]),
            ("9040_设备.csv", [self.equipment("E1", active="yes")]),
            ("9040_工单.csv", [self.order("O1", status="pending")]),
            ("9040_工单.csv", [self.order("O1", cost="not money")]),
        ]
        for index, (filename, rows) in enumerate(cases):
            with self.subTest(filename=filename, index=index):
                self.tables = copy.deepcopy(self.base)
                self.write_all()
                self.write_table(filename, rows)
                self.assert_rejected(f"9040_类型拒绝_{index}")

    def test_invalid_as_of_dates_are_rejected_without_outputs(self):
        before = self.hashes()
        for index, as_of in enumerate(("2026-02-30", "10/19/2026", "2026-1-2")):
            with self.subTest(as_of=as_of):
                output = self.root / f"9040_截止日拒绝_{index}"
                with self.assertRaises(engine.WorkflowError):
                    engine.run_all(self.input, output, as_of)
                self.assertFalse(output.exists())
        self.assertEqual(self.hashes(), before)

    def test_synthetic_generator_is_reproducible_and_runs_all_five_ideas(self):
        left, right = self.root / "9040_第一批", self.root / "9040_第二批"
        first, second = samples.generate_samples(left), samples.generate_samples(right)
        self.assertEqual(first, second)
        self.assertTrue(first["synthetic"])
        self.assertEqual({row["filename"]: row["rows"] for row in first["sources"]}, {
            "9040_通知.csv": 100, "9040_工单.csv": 80, "9040_设备.csv": 20,
            "9040_计划.csv": 20, "9040_测量.csv": 30, "9040_录入耗时.csv": 40,
        })
        self.assertEqual({path.name for path in left.iterdir()}, set(HEADERS))
        for filename in HEADERS:
            self.assertEqual((left / filename).read_bytes(), (right / filename).read_bytes())
            self.assertTrue((left / filename).read_bytes().startswith(b"\xef\xbb\xbf"))
        report = engine.run_all(left, self.root / "9040_示例全流程", AS_OF)
        self.assertEqual(report["ideas"]["1"]["summary"]["records"], 100)
        self.assertEqual(report["ideas"]["4"]["summary"]["comparable_task_types"], 2)
        self.assertGreaterEqual(report["ideas"]["3"]["summary"]["issue_count"], 5)
        self.assertEqual({row["filename"]: row["sha256"] for row in report["sources"]},
                         {row["filename"]: row["sha256"] for row in first["sources"]})
        with self.assertRaises(ValueError):
            samples.generate_samples(left)


if __name__ == "__main__":
    unittest.main(verbosity=2)
