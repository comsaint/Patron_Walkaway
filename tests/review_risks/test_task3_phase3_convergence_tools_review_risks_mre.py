"""Task 3 / Phase 3 convergence tools review risks -> minimal reproducible tests.

Scope (tests-only; do not modify production):
- trainer/scripts/task3_phase3_compare_alerts.py
- trainer/scripts/task3_phase3_compare_p95.py
- doc/task3_phase3_convergence_validation.md
"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from trainer.scripts.task3_phase3_compare_alerts import compare_alerts
from trainer.scripts.task3_phase3_compare_p95 import compare_logs


class TestRisk1NullVsZeroMaskedByCoalesce(unittest.TestCase):
    """Risk #1: NULL vs 0.0 may be masked by COALESCE in numeric drift SQL."""

    def _init_db(self, path: Path, rows: list[tuple[str, object, object]]) -> None:
        conn = sqlite3.connect(str(path))
        try:
            conn.execute(
                """
                CREATE TABLE alerts (
                    bet_id TEXT PRIMARY KEY,
                    score REAL,
                    margin REAL
                )
                """
            )
            conn.executemany(
                "INSERT INTO alerts(bet_id, score, margin) VALUES (?, ?, ?)",
                rows,
            )
            conn.commit()
        finally:
            conn.close()

    def test_null_vs_zero_currently_reports_zero_score_diff(self) -> None:
        tmp = Path("tmp_test_task3_phase3_null_vs_zero")
        tmp.mkdir(exist_ok=True)
        baseline = tmp / "baseline.db"
        candidate = tmp / "candidate.db"
        try:
            self._init_db(baseline, [("b1", None, 0.1)])
            self._init_db(candidate, [("b1", 0.0, 0.1)])
            report = compare_alerts(baseline, candidate, score_tol=1e-12, margin_tol=1e-12)
            drift = report["numeric_drift"]
            self.assertEqual(drift["max_abs_score_diff"], 0.0)
            self.assertEqual(drift["score_diff_rows_over_tolerance"], 0)
        finally:
            for p in (baseline, candidate):
                if p.exists():
                    p.unlink()
            if tmp.exists():
                tmp.rmdir()


class TestRisk2MissingAlertsTableErrorSurface(unittest.TestCase):
    """Risk #2: missing alerts table currently raises generic sqlite OperationalError."""

    def test_missing_alerts_table_raises_sqlite_operational_error(self) -> None:
        tmp = Path("tmp_test_task3_phase3_missing_alerts")
        tmp.mkdir(exist_ok=True)
        baseline = tmp / "baseline.db"
        candidate = tmp / "candidate.db"
        try:
            sqlite3.connect(str(baseline)).close()
            sqlite3.connect(str(candidate)).close()
            with self.assertRaises(sqlite3.OperationalError):
                compare_alerts(baseline, candidate, score_tol=1e-6, margin_tol=1e-6)
        finally:
            for p in (baseline, candidate):
                if p.exists():
                    p.unlink()
            if tmp.exists():
                tmp.rmdir()


class TestRisk3P95CountUsesLineCountNotReportedN(unittest.TestCase):
    """Risk #3: baseline_count/candidate_count count matched lines, not reported n."""

    def test_count_is_number_of_stage_matches(self) -> None:
        tmp = Path("tmp_test_task3_phase3_p95_count")
        tmp.mkdir(exist_ok=True)
        baseline = tmp / "baseline.log"
        candidate = tmp / "candidate.log"
        try:
            baseline.write_text(
                "\n".join(
                    [
                        "x [scorer][perf] top_hotspots: feature_engineering=1.0s (p50=1.0s, p95=2.0s, n=999)",
                        "x [scorer][perf] top_hotspots: feature_engineering=1.1s (p50=1.0s, p95=2.1s, n=1)",
                    ]
                ),
                encoding="utf-8",
            )
            candidate.write_text(
                "x [scorer][perf] top_hotspots: feature_engineering=0.9s (p50=0.8s, p95=1.9s, n=500)",
                encoding="utf-8",
            )
            report = compare_logs(baseline, candidate)
            self.assertEqual(report["feature_engineering"]["baseline_count"], 2.0)
            self.assertEqual(report["feature_engineering"]["candidate_count"], 1.0)
        finally:
            for p in (baseline, candidate):
                if p.exists():
                    p.unlink()
            if tmp.exists():
                tmp.rmdir()


class TestRisk4RunbookNoExplicitStopControl(unittest.TestCase):
    """Risk #4: runbook command example does not include explicit stop control."""

    def test_runbook_does_not_mention_once_flag(self) -> None:
        root = Path(__file__).resolve().parents[2]
        runbook = root / "doc" / "task3_phase3_convergence_validation.md"
        text = runbook.read_text(encoding="utf-8")
        self.assertIn("python -m trainer.serving.scorer --log-level INFO", text)
        self.assertNotIn("--once", text)


class TestRisk5StageRegexDoesNotMatchHyphenatedStage(unittest.TestCase):
    """Risk #5: stage regex currently does not parse hyphenated stage names."""

    def test_hyphenated_stage_name_is_parsed_as_truncated_suffix(self) -> None:
        tmp = Path("tmp_test_task3_phase3_stage_regex")
        tmp.mkdir(exist_ok=True)
        baseline = tmp / "baseline.log"
        candidate = tmp / "candidate.log"
        try:
            baseline.write_text(
                "x [scorer][perf] top_hotspots: api-query=1.0s (p50=1.0s, p95=1.2s, n=20)",
                encoding="utf-8",
            )
            candidate.write_text(
                "x [scorer][perf] top_hotspots: api-query=0.8s (p50=0.7s, p95=1.0s, n=20)",
                encoding="utf-8",
            )
            report = compare_logs(baseline, candidate)
            self.assertNotIn("api-query", report)
            self.assertIn("query", report)
        finally:
            for p in (baseline, candidate):
                if p.exists():
                    p.unlink()
            if tmp.exists():
                tmp.rmdir()


if __name__ == "__main__":
    unittest.main()
