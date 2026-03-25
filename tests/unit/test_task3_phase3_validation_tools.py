from __future__ import annotations

import sqlite3
from pathlib import Path

from trainer.scripts.task3_phase3_compare_alerts import compare_alerts
from trainer.scripts.task3_phase3_compare_p95 import compare_logs


def test_compare_p95_parses_and_computes_improvement(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.log"
    candidate = tmp_path / "candidate.log"

    baseline.write_text(
        "\n".join(
            [
                "2026-03-24 DEBUG [scorer][perf] top_hotspots: feature_engineering=1.000s (p50=1.000s, p95=1.200s, n=20); sqlite=0.400s (p50=0.300s, p95=0.500s, n=20)",
                "2026-03-24 DEBUG [scorer][perf] top_hotspots: feature_engineering=1.100s (p50=1.000s, p95=1.300s, n=21)",
            ]
        ),
        encoding="utf-8",
    )
    candidate.write_text(
        "\n".join(
            [
                "2026-03-24 DEBUG [scorer][perf] top_hotspots: feature_engineering=0.800s (p50=0.700s, p95=0.900s, n=20); sqlite=0.300s (p50=0.250s, p95=0.350s, n=20)",
                "2026-03-24 DEBUG [scorer][perf] top_hotspots: feature_engineering=0.850s (p50=0.750s, p95=1.000s, n=21)",
            ]
        ),
        encoding="utf-8",
    )

    report = compare_logs(baseline, candidate)
    assert "feature_engineering" in report
    assert report["feature_engineering"]["baseline_median_p95_sec"] > report["feature_engineering"][
        "candidate_median_p95_sec"
    ]
    assert report["feature_engineering"]["p95_improvement_pct"] > 0.0
    assert "sqlite" in report


def _init_alerts_db(db_path: Path, rows: list[tuple[str, float, float]]) -> None:
    conn = sqlite3.connect(str(db_path))
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


def test_compare_alerts_reports_schema_set_and_numeric_drift(tmp_path: Path) -> None:
    baseline_db = tmp_path / "baseline_state.db"
    candidate_db = tmp_path / "candidate_state.db"

    _init_alerts_db(
        baseline_db,
        [
            ("b1", 0.80, 0.10),
            ("b2", 0.30, -0.20),
            ("b3", 0.60, 0.01),
        ],
    )
    _init_alerts_db(
        candidate_db,
        [
            ("b1", 0.8000001, 0.1000001),
            ("b2", 0.35, -0.19),
            ("b4", 0.20, -0.30),
        ],
    )

    report = compare_alerts(
        baseline_db,
        candidate_db,
        score_tol=1e-4,
        margin_tol=1e-4,
    )
    schema = report["schema"]
    alerts = report["alerts"]
    drift = report["numeric_drift"]

    assert schema["baseline_only_columns"] == []
    assert schema["candidate_only_columns"] == []
    assert alerts["baseline_only_bet_ids"] == 1
    assert alerts["candidate_only_bet_ids"] == 1
    assert alerts["intersection_count"] == 2
    assert drift["score_diff_rows_over_tolerance"] == 1
    assert drift["margin_diff_rows_over_tolerance"] == 1
