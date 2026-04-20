"""W1-B2 slice_contract pure logic (PLAN §7) — precision uplift orchestrator."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[2]
_ORCH = _ROOT / "investigations" / "precision_uplift_recall_1pct" / "orchestrator"
if str(_ORCH) not in sys.path:
    sys.path.insert(0, str(_ORCH))

import slice_contract  # noqa: E402


def _base_profile(**kwargs: object) -> dict:
    p = {
        "active_days_30d": 5,
        "theo_win_sum_30d": 100.0,
        "turnover_sum_30d": 200.0,
        "days_since_first_session": 20,
    }
    p.update(kwargs)
    return p


def test_build_slice_contract_bundle_happy_path() -> None:
    spec = {
        "T0": "2026-01-10T00:00:00+08:00",
        "recall_score_threshold": 0.5,
        "min_slice_n": 2,
        "profiles": {
            "c1": _base_profile(),
            "c2": _base_profile(
                active_days_30d=2,
                theo_win_sum_30d=40.0,
                turnover_sum_30d=50.0,
                days_since_first_session=5,
            ),
        },
        "eval_rows": [
            {
                "canonical_id": "c1",
                "decision_ts": "2026-01-05T12:00:00+08:00",
                "table_id": "T99",
                "score": 0.9,
                "label": 1,
            },
            {
                "canonical_id": "c1",
                "decision_ts": "2026-01-06T12:00:00+08:00",
                "table_id": "T99",
                "score": 0.2,
                "label": 0,
            },
            {
                "canonical_id": "c2",
                "decision_ts": "2026-01-07T08:00:00+08:00",
                "table_id": "",
                "score": 0.8,
                "label": 0,
            },
            {
                "canonical_id": "c2",
                "decision_ts": "2026-01-07T09:00:00+08:00",
                "score": 0.6,
                "label": 1,
            },
        ],
    }
    out = slice_contract.build_slice_contract_bundle(spec)
    assert out["slice_data_incomplete"] is False
    assert out["slice_contract_violations"] == []
    assert len(out["row_annotations"]) == 4
    assert len(out["top_drag_slices"]) >= 1
    dims = {x["dimension"] for x in out["top_drag_slices"]}
    assert "eval_date" in dims or "table_id" in dims


def test_missing_profile_marks_incomplete() -> None:
    spec = {
        "T0": "2026-01-10T00:00:00+08:00",
        "profiles": {},
        "eval_rows": [
            {
                "canonical_id": "cx",
                "decision_ts": "2026-01-05T12:00:00+08:00",
                "score": 0.9,
                "label": 0,
            },
        ],
    }
    out = slice_contract.build_slice_contract_bundle(spec)
    assert out["slice_data_incomplete"] is True
    assert "T0_no_profile" in out["blocking_profile_codes"]


def test_profile_assertion_active_days_zero() -> None:
    spec = {
        "T0": "2026-01-10T00:00:00+08:00",
        "profiles": {"c1": _base_profile(active_days_30d=0)},
        "eval_rows": [
            {
                "canonical_id": "c1",
                "decision_ts": "2026-01-05T12:00:00+08:00",
                "score": 0.9,
                "label": 0,
            },
        ],
    }
    out = slice_contract.build_slice_contract_bundle(spec)
    assert out["slice_data_incomplete"] is True
    assert any("active_days" in str(v) for v in out["blocking_profile_codes"])


def test_eval_date_hkt_naive_assumes_hk() -> None:
    assert slice_contract.eval_date_hkt("2026-03-01T00:00:00") == "2026-03-01"


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (0, "T0_seg"),
        (7, "T0_seg"),
        (8, "T1"),
        (30, "T1"),
        (31, "T2"),
        (90, "T2"),
        (91, "T3"),
    ],
)
def test_tenure_bucket_boundaries(days: float, expected: str) -> None:
    assert slice_contract.tenure_bucket_days(days) == expected


def test_collect_phase1_artifacts_merges_inline_slice_contract(tmp_path: Path) -> None:
    """When cfg contains ``slice_contract.eval_rows``, collector attaches ``slice_contract``."""
    import json
    import sqlite3

    import collectors

    repo = tmp_path / "repo"
    repo.mkdir()
    orch = tmp_path / "orch"
    run_id = "run_slice_inline"
    logs = orch / "state" / run_id / "logs"
    logs.mkdir(parents=True)
    metrics_dir = repo / "trainer" / "out_backtest"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "backtest_metrics.json").write_text("{}", encoding="utf-8")
    (logs / "r1_r6.stdout.log").write_text(json.dumps({"ok": True}), encoding="utf-8")

    state_db = repo / "state.db"
    conn = sqlite3.connect(state_db)
    conn.execute(
        "CREATE TABLE validation_results (bet_id TEXT, alert_ts TEXT, validated_at TEXT, result INT)"
    )
    conn.commit()
    conn.close()

    cfg = {
        "model_dir": "trainer/models",
        "state_db_path": str(state_db),
        "prediction_log_db_path": str(repo / "pred.db"),
        "window": {"start_ts": "2026-01-01T00:00:00+08:00", "end_ts": "2026-01-08T00:00:00+08:00"},
        "thresholds": {},
        "backtest_metrics_path": "trainer/out_backtest/backtest_metrics.json",
        "slice_contract": {
            "T0": "2026-01-10T00:00:00+08:00",
            "recall_score_threshold": 0.5,
            "min_slice_n": 1,
            "profiles": {"c1": _base_profile()},
            "eval_rows": [
                {
                    "canonical_id": "c1",
                    "decision_ts": "2026-01-05T12:00:00+08:00",
                    "table_id": "T1",
                    "score": 0.9,
                    "label": 1,
                },
            ],
        },
    }
    bundle = collectors.collect_phase1_artifacts(
        run_id, cfg, repo_root=repo, orchestrator_dir=orch
    )
    assert "slice_contract" in bundle
    sc = bundle["slice_contract"]
    assert isinstance(sc, dict)
    assert sc.get("slice_data_incomplete") is False
    assert len(sc.get("row_annotations") or []) == 1
    assert str(sc.get("slice_contract_version") or "").strip() != ""
    ph = str(sc.get("slice_contract_plan_hash_sha256") or "").strip()
    if ph:
        assert len(ph) == 64


def test_collect_phase1_injects_recall_threshold_from_r1_when_omitted(tmp_path: Path) -> None:
    """If config omits ``recall_score_threshold``, use R1 ``threshold_at_target`` when present."""
    import json
    import sqlite3

    import collectors

    repo = tmp_path / "repo"
    repo.mkdir()
    orch = tmp_path / "orch"
    run_id = "run_slice_r1_thr"
    logs = orch / "state" / run_id / "logs"
    logs.mkdir(parents=True)
    metrics_dir = repo / "trainer" / "out_backtest"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "backtest_metrics.json").write_text("{}", encoding="utf-8")
    r1 = {
        "evaluate": {
            "precision_at_recall_target": {
                "target_recall": 0.01,
                "precision_at_target_recall": 0.4,
                "threshold_at_target": 0.95,
            }
        }
    }
    (logs / "r1_r6.stdout.log").write_text(json.dumps(r1), encoding="utf-8")

    state_db = repo / "state.db"
    conn = sqlite3.connect(state_db)
    conn.execute(
        "CREATE TABLE validation_results (bet_id TEXT, alert_ts TEXT, validated_at TEXT, result INT)"
    )
    conn.commit()
    conn.close()

    cfg = {
        "model_dir": "trainer/models",
        "state_db_path": str(state_db),
        "prediction_log_db_path": str(repo / "pred.db"),
        "window": {"start_ts": "2026-01-01T00:00:00+08:00", "end_ts": "2026-01-08T00:00:00+08:00"},
        "thresholds": {},
        "backtest_metrics_path": "trainer/out_backtest/backtest_metrics.json",
        "slice_contract": {
            "T0": "2026-01-10T00:00:00+08:00",
            "min_slice_n": 1,
            "profiles": {"c1": _base_profile()},
            "eval_rows": [
                {
                    "canonical_id": "c1",
                    "decision_ts": "2026-01-05T12:00:00+08:00",
                    "table_id": "T1",
                    "score": 0.8,
                    "label": 1,
                },
            ],
        },
    }
    bundle = collectors.collect_phase1_artifacts(
        run_id, cfg, repo_root=repo, orchestrator_dir=orch
    )
    sc = bundle["slice_contract"]
    assert isinstance(sc, dict)
    ra = sc.get("row_annotations") or []
    assert len(ra) == 1
    assert ra[0].get("is_alert") is False


def test_collect_phase1_inline_recall_threshold_overrides_r1(tmp_path: Path) -> None:
    """Explicit ``recall_score_threshold`` in config must not be replaced by R1."""
    import json
    import sqlite3

    import collectors

    repo = tmp_path / "repo"
    repo.mkdir()
    orch = tmp_path / "orch"
    run_id = "run_slice_inline_thr"
    logs = orch / "state" / run_id / "logs"
    logs.mkdir(parents=True)
    metrics_dir = repo / "trainer" / "out_backtest"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "backtest_metrics.json").write_text("{}", encoding="utf-8")
    (logs / "r1_r6.stdout.log").write_text(
        json.dumps(
            {
                "evaluate": {
                    "precision_at_recall_target": {"threshold_at_target": 0.99},
                }
            }
        ),
        encoding="utf-8",
    )

    state_db = repo / "state.db"
    conn = sqlite3.connect(state_db)
    conn.execute(
        "CREATE TABLE validation_results (bet_id TEXT, alert_ts TEXT, validated_at TEXT, result INT)"
    )
    conn.commit()
    conn.close()

    cfg = {
        "model_dir": "trainer/models",
        "state_db_path": str(state_db),
        "prediction_log_db_path": str(repo / "pred.db"),
        "window": {"start_ts": "2026-01-01T00:00:00+08:00", "end_ts": "2026-01-08T00:00:00+08:00"},
        "thresholds": {},
        "backtest_metrics_path": "trainer/out_backtest/backtest_metrics.json",
        "slice_contract": {
            "T0": "2026-01-10T00:00:00+08:00",
            "recall_score_threshold": 0.5,
            "min_slice_n": 1,
            "profiles": {"c1": _base_profile()},
            "eval_rows": [
                {
                    "canonical_id": "c1",
                    "decision_ts": "2026-01-05T12:00:00+08:00",
                    "table_id": "T1",
                    "score": 0.8,
                    "label": 1,
                },
            ],
        },
    }
    bundle = collectors.collect_phase1_artifacts(
        run_id, cfg, repo_root=repo, orchestrator_dir=orch
    )
    ra = (bundle.get("slice_contract") or {}).get("row_annotations") or []
    assert len(ra) == 1
    assert ra[0].get("is_alert") is True


def test_collect_phase1_injects_recall_threshold_from_backtest_when_r1_missing(
    tmp_path: Path,
) -> None:
    """When R1 has no ``threshold_at_target``, use ``backtest_metrics`` recall-1% threshold."""
    import json
    import sqlite3

    import collectors

    repo = tmp_path / "repo"
    repo.mkdir()
    orch = tmp_path / "orch"
    run_id = "run_slice_bt_thr"
    logs = orch / "state" / run_id / "logs"
    logs.mkdir(parents=True)
    metrics_dir = repo / "trainer" / "out_backtest"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "backtest_metrics.json").write_text(
        json.dumps({"model_default": {"threshold_at_recall_0.01": 0.91}}),
        encoding="utf-8",
    )
    (logs / "r1_r6.stdout.log").write_text(
        json.dumps({"evaluate": {"n": 1}}),
        encoding="utf-8",
    )

    state_db = repo / "state.db"
    conn = sqlite3.connect(state_db)
    conn.execute(
        "CREATE TABLE validation_results (bet_id TEXT, alert_ts TEXT, validated_at TEXT, result INT)"
    )
    conn.commit()
    conn.close()

    cfg = {
        "model_dir": "trainer/models",
        "state_db_path": str(state_db),
        "prediction_log_db_path": str(repo / "pred.db"),
        "window": {"start_ts": "2026-01-01T00:00:00+08:00", "end_ts": "2026-01-08T00:00:00+08:00"},
        "thresholds": {},
        "backtest_metrics_path": "trainer/out_backtest/backtest_metrics.json",
        "slice_contract": {
            "T0": "2026-01-10T00:00:00+08:00",
            "min_slice_n": 1,
            "profiles": {"c1": _base_profile()},
            "eval_rows": [
                {
                    "canonical_id": "c1",
                    "decision_ts": "2026-01-05T12:00:00+08:00",
                    "table_id": "T1",
                    "score": 0.8,
                    "label": 1,
                },
            ],
        },
    }
    bundle = collectors.collect_phase1_artifacts(
        run_id, cfg, repo_root=repo, orchestrator_dir=orch
    )
    ra = (bundle.get("slice_contract") or {}).get("row_annotations") or []
    assert len(ra) == 1
    assert ra[0].get("is_alert") is False


def test_collect_phase1_r1_threshold_preferred_over_backtest(tmp_path: Path) -> None:
    """R1 ``threshold_at_target`` wins when both R1 and backtest_metrics supply values."""
    import json
    import sqlite3

    import collectors

    repo = tmp_path / "repo"
    repo.mkdir()
    orch = tmp_path / "orch"
    run_id = "run_slice_r1_wins"
    logs = orch / "state" / run_id / "logs"
    logs.mkdir(parents=True)
    metrics_dir = repo / "trainer" / "out_backtest"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "backtest_metrics.json").write_text(
        json.dumps({"model_default": {"threshold_at_recall_0.01": 0.1}}),
        encoding="utf-8",
    )
    (logs / "r1_r6.stdout.log").write_text(
        json.dumps(
            {
                "evaluate": {
                    "precision_at_recall_target": {"threshold_at_target": 0.95},
                }
            }
        ),
        encoding="utf-8",
    )

    state_db = repo / "state.db"
    conn = sqlite3.connect(state_db)
    conn.execute(
        "CREATE TABLE validation_results (bet_id TEXT, alert_ts TEXT, validated_at TEXT, result INT)"
    )
    conn.commit()
    conn.close()

    cfg = {
        "model_dir": "trainer/models",
        "state_db_path": str(state_db),
        "prediction_log_db_path": str(repo / "pred.db"),
        "window": {"start_ts": "2026-01-01T00:00:00+08:00", "end_ts": "2026-01-08T00:00:00+08:00"},
        "thresholds": {},
        "backtest_metrics_path": "trainer/out_backtest/backtest_metrics.json",
        "slice_contract": {
            "T0": "2026-01-10T00:00:00+08:00",
            "min_slice_n": 1,
            "profiles": {"c1": _base_profile()},
            "eval_rows": [
                {
                    "canonical_id": "c1",
                    "decision_ts": "2026-01-05T12:00:00+08:00",
                    "table_id": "T1",
                    "score": 0.8,
                    "label": 1,
                },
            ],
        },
    }
    bundle = collectors.collect_phase1_artifacts(
        run_id, cfg, repo_root=repo, orchestrator_dir=orch
    )
    ra = (bundle.get("slice_contract") or {}).get("row_annotations") or []
    assert len(ra) == 1
    assert ra[0].get("is_alert") is False


def test_collect_phase1_auto_eval_rows_from_prediction_log_sqlite(tmp_path: Path) -> None:
    """Auto-build ``eval_rows`` from prediction_log+validation_results when enabled."""
    import json
    import sqlite3

    import collectors

    repo = tmp_path / "repo"
    repo.mkdir()
    orch = tmp_path / "orch"
    run_id = "run_slice_auto_eval_rows"
    logs = orch / "state" / run_id / "logs"
    logs.mkdir(parents=True)
    metrics_dir = repo / "trainer" / "out_backtest"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "backtest_metrics.json").write_text("{}", encoding="utf-8")
    (logs / "r1_r6.stdout.log").write_text(json.dumps({"ok": True}), encoding="utf-8")

    pred_db = repo / "pred.db"
    with sqlite3.connect(pred_db) as conn_pred:
        conn_pred.execute(
            """
            CREATE TABLE prediction_log (
                scored_at TEXT,
                bet_id TEXT,
                canonical_id TEXT,
                table_id TEXT,
                score REAL,
                is_rated_obs INT
            )
            """
        )
        conn_pred.execute(
            "INSERT INTO prediction_log VALUES (?,?,?,?,?,?)",
            ("2026-01-05T12:00:00+08:00", "b1", "c1", "T1", 0.9, 1),
        )
        conn_pred.execute(
            "INSERT INTO prediction_log VALUES (?,?,?,?,?,?)",
            ("2026-01-05T13:00:00+08:00", "b2", "c2", "T2", 0.2, 1),
        )
        conn_pred.execute(
            "INSERT INTO prediction_log VALUES (?,?,?,?,?,?)",
            ("2026-01-05T14:00:00+08:00", "b3", "c3", "T3", 0.8, 0),
        )
        conn_pred.commit()

    state_db = repo / "state.db"
    with sqlite3.connect(state_db) as conn_state:
        conn_state.execute(
            "CREATE TABLE validation_results (bet_id TEXT, alert_ts TEXT, validated_at TEXT, result INT)"
        )
        conn_state.execute(
            "INSERT INTO validation_results VALUES (?,?,?,?)",
            ("b1", "2026-01-05T12:00:00+08:00", "2026-01-06T00:00:00+08:00", 1),
        )
        conn_state.execute(
            "INSERT INTO validation_results VALUES (?,?,?,?)",
            ("b2", "2026-01-05T13:00:00+08:00", "2026-01-06T00:00:00+08:00", 0),
        )
        conn_state.commit()

    cfg = {
        "model_dir": "trainer/models",
        "state_db_path": str(state_db),
        "prediction_log_db_path": str(pred_db),
        "window": {
            "start_ts": "2026-01-01T00:00:00+08:00",
            "end_ts": "2026-01-08T00:00:00+08:00",
        },
        "thresholds": {},
        "backtest_metrics_path": "trainer/out_backtest/backtest_metrics.json",
        "slice_contract": {
            "T0": "2026-01-10T00:00:00+08:00",
            "recall_score_threshold": 0.5,
            "min_slice_n": 1,
            "auto_eval_rows_from_prediction_log": True,
            "profiles": {
                "c1": _base_profile(),
                "c2": _base_profile(active_days_30d=7, theo_win_sum_30d=77.0),
            },
        },
    }
    bundle = collectors.collect_phase1_artifacts(
        run_id, cfg, repo_root=repo, orchestrator_dir=orch
    )
    sc = bundle.get("slice_contract") or {}
    ra = sc.get("row_annotations") or []
    assert len(ra) == 2
    assert sc.get("slice_data_incomplete") is False


def test_collect_phase1_auto_profiles_from_state_db_sqlite(tmp_path: Path) -> None:
    """Auto-load profiles from state DB ``player_profile`` for eval_rows canonical IDs."""
    import json
    import sqlite3

    import collectors

    repo = tmp_path / "repo"
    repo.mkdir()
    orch = tmp_path / "orch"
    run_id = "run_slice_auto_profiles"
    logs = orch / "state" / run_id / "logs"
    logs.mkdir(parents=True)
    metrics_dir = repo / "trainer" / "out_backtest"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "backtest_metrics.json").write_text("{}", encoding="utf-8")
    (logs / "r1_r6.stdout.log").write_text(json.dumps({"ok": True}), encoding="utf-8")

    state_db = repo / "state.db"
    with sqlite3.connect(state_db) as conn:
        conn.execute(
            "CREATE TABLE validation_results (bet_id TEXT, alert_ts TEXT, validated_at TEXT, result INT)"
        )
        conn.execute(
            """
            CREATE TABLE player_profile (
                canonical_id TEXT,
                theo_win_sum_30d REAL,
                active_days_30d INT,
                turnover_sum_30d REAL,
                days_since_first_session INT
            )
            """
        )
        conn.execute(
            "INSERT INTO player_profile VALUES (?,?,?,?,?)",
            ("c1", 100.0, 5, 200.0, 20),
        )
        conn.execute(
            "INSERT INTO player_profile VALUES (?,?,?,?,?)",
            ("c2", 80.0, 4, 120.0, 12),
        )
        conn.commit()

    cfg = {
        "model_dir": "trainer/models",
        "state_db_path": str(state_db),
        "prediction_log_db_path": str(repo / "pred.db"),
        "window": {
            "start_ts": "2026-01-01T00:00:00+08:00",
            "end_ts": "2026-01-08T00:00:00+08:00",
        },
        "thresholds": {},
        "backtest_metrics_path": "trainer/out_backtest/backtest_metrics.json",
        "slice_contract": {
            "T0": "2026-01-10T00:00:00+08:00",
            "recall_score_threshold": 0.5,
            "min_slice_n": 1,
            "auto_profiles_from_state_db": True,
            "asof_mode": "WARN_ONLY",
            "eval_rows": [
                {
                    "canonical_id": "c1",
                    "decision_ts": "2026-01-05T12:00:00+08:00",
                    "table_id": "T1",
                    "score": 0.9,
                    "label": 1,
                },
                {
                    "canonical_id": "c2",
                    "decision_ts": "2026-01-05T13:00:00+08:00",
                    "table_id": "T2",
                    "score": 0.1,
                    "label": 0,
                },
            ],
        },
    }
    bundle = collectors.collect_phase1_artifacts(
        run_id, cfg, repo_root=repo, orchestrator_dir=orch
    )
    sc = bundle.get("slice_contract") or {}
    assert sc.get("slice_data_incomplete") is False
    assert len(sc.get("row_annotations") or []) == 2


def test_collect_phase1_auto_profiles_missing_asof_strict_marks_incomplete(
    tmp_path: Path,
) -> None:
    """STRICT mode: missing ``as_of_ts`` evidence should force incomplete + strict code."""
    import json
    import sqlite3

    import collectors

    repo = tmp_path / "repo"
    repo.mkdir()
    orch = tmp_path / "orch"
    run_id = "run_slice_auto_profiles_strict_asof_missing"
    logs = orch / "state" / run_id / "logs"
    logs.mkdir(parents=True)
    metrics_dir = repo / "trainer" / "out_backtest"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "backtest_metrics.json").write_text("{}", encoding="utf-8")
    (logs / "r1_r6.stdout.log").write_text(json.dumps({"ok": True}), encoding="utf-8")

    state_db = repo / "state.db"
    with sqlite3.connect(state_db) as conn:
        conn.execute(
            "CREATE TABLE validation_results (bet_id TEXT, alert_ts TEXT, validated_at TEXT, result INT)"
        )
        conn.execute(
            """
            CREATE TABLE player_profile (
                canonical_id TEXT,
                theo_win_sum_30d REAL,
                active_days_30d INT,
                turnover_sum_30d REAL,
                days_since_first_session INT
            )
            """
        )
        conn.execute(
            "INSERT INTO player_profile VALUES (?,?,?,?,?)",
            ("c1", 100.0, 5, 200.0, 20),
        )
        conn.commit()

    cfg = {
        "model_dir": "trainer/models",
        "state_db_path": str(state_db),
        "prediction_log_db_path": str(repo / "pred.db"),
        "window": {
            "start_ts": "2026-01-01T00:00:00+08:00",
            "end_ts": "2026-01-08T00:00:00+08:00",
        },
        "thresholds": {},
        "backtest_metrics_path": "trainer/out_backtest/backtest_metrics.json",
        "slice_contract": {
            "T0": "2026-01-10T00:00:00+08:00",
            "recall_score_threshold": 0.5,
            "min_slice_n": 1,
            "auto_profiles_from_state_db": True,
            "asof_mode": "STRICT",
            "eval_rows": [
                {
                    "canonical_id": "c1",
                    "decision_ts": "2026-01-05T12:00:00+08:00",
                    "table_id": "T1",
                    "score": 0.9,
                    "label": 1,
                },
            ],
        },
    }
    bundle = collectors.collect_phase1_artifacts(
        run_id, cfg, repo_root=repo, orchestrator_dir=orch
    )
    sc = bundle.get("slice_contract") or {}
    assert sc.get("slice_data_incomplete") is True
    assert "asof_contract_unavailable_strict" in (sc.get("blocking_profile_codes") or [])


def test_collect_phase1_auto_profiles_prefers_asof_row_at_or_before_t0(
    tmp_path: Path,
) -> None:
    """When ``as_of_ts`` exists, auto profile loader should pick nearest row <= T0."""
    import json
    import sqlite3

    import collectors

    repo = tmp_path / "repo"
    repo.mkdir()
    orch = tmp_path / "orch"
    run_id = "run_slice_auto_profiles_asof"
    logs = orch / "state" / run_id / "logs"
    logs.mkdir(parents=True)
    metrics_dir = repo / "trainer" / "out_backtest"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "backtest_metrics.json").write_text("{}", encoding="utf-8")
    (logs / "r1_r6.stdout.log").write_text(json.dumps({"ok": True}), encoding="utf-8")

    state_db = repo / "state.db"
    with sqlite3.connect(state_db) as conn:
        conn.execute(
            "CREATE TABLE validation_results (bet_id TEXT, alert_ts TEXT, validated_at TEXT, result INT)"
        )
        conn.execute(
            """
            CREATE TABLE player_profile (
                canonical_id TEXT,
                theo_win_sum_30d REAL,
                active_days_30d INT,
                turnover_sum_30d REAL,
                days_since_first_session INT,
                as_of_ts TEXT
            )
            """
        )
        # older row (<= T0) is intentionally invalid; newer row (>T0) is valid.
        conn.execute(
            "INSERT INTO player_profile VALUES (?,?,?,?,?,?)",
            ("c1", 100.0, 0, 200.0, 20, "2026-01-01T00:00:00+08:00"),
        )
        conn.execute(
            "INSERT INTO player_profile VALUES (?,?,?,?,?,?)",
            ("c1", 120.0, 5, 210.0, 25, "2026-02-01T00:00:00+08:00"),
        )
        conn.commit()

    cfg = {
        "model_dir": "trainer/models",
        "state_db_path": str(state_db),
        "prediction_log_db_path": str(repo / "pred.db"),
        "window": {
            "start_ts": "2026-01-01T00:00:00+08:00",
            "end_ts": "2026-01-08T00:00:00+08:00",
        },
        "thresholds": {},
        "backtest_metrics_path": "trainer/out_backtest/backtest_metrics.json",
        "slice_contract": {
            "T0": "2026-01-15T00:00:00+08:00",
            "recall_score_threshold": 0.5,
            "min_slice_n": 1,
            "auto_profiles_from_state_db": True,
            "eval_rows": [
                {
                    "canonical_id": "c1",
                    "decision_ts": "2026-01-05T12:00:00+08:00",
                    "table_id": "T1",
                    "score": 0.9,
                    "label": 1,
                },
            ],
        },
    }
    bundle = collectors.collect_phase1_artifacts(
        run_id, cfg, repo_root=repo, orchestrator_dir=orch
    )
    sc = bundle.get("slice_contract") or {}
    assert sc.get("slice_data_incomplete") is True
    assert "active_days_30d_lt_1_or_invalid" in (sc.get("blocking_profile_codes") or [])


def test_collect_phase1_profiles_fallback_to_parquet_when_state_db_infeasible(
    tmp_path: Path,
) -> None:
    """State DB profile failure should trigger Parquet fallback (state_db remains primary)."""
    import json
    import sqlite3

    import collectors

    repo = tmp_path / "repo"
    repo.mkdir()
    orch = tmp_path / "orch"
    run_id = "run_slice_profiles_fallback"
    logs = orch / "state" / run_id / "logs"
    logs.mkdir(parents=True)
    metrics_dir = repo / "trainer" / "out_backtest"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "backtest_metrics.json").write_text("{}", encoding="utf-8")
    (logs / "r1_r6.stdout.log").write_text(json.dumps({"ok": True}), encoding="utf-8")

    state_db = repo / "state.db"
    with sqlite3.connect(state_db) as conn:
        conn.execute(
            "CREATE TABLE validation_results (bet_id TEXT, alert_ts TEXT, validated_at TEXT, result INT)"
        )
        # Missing required profile columns on purpose -> infeasible
        conn.execute("CREATE TABLE player_profile (canonical_id TEXT)")
        conn.commit()

    fake_parquet = repo / "profiles.parquet"
    fake_parquet.write_text("placeholder", encoding="utf-8")

    cfg = {
        "model_dir": "trainer/models",
        "state_db_path": str(state_db),
        "prediction_log_db_path": str(repo / "pred.db"),
        "window": {
            "start_ts": "2026-01-01T00:00:00+08:00",
            "end_ts": "2026-01-08T00:00:00+08:00",
        },
        "thresholds": {},
        "backtest_metrics_path": "trainer/out_backtest/backtest_metrics.json",
        "slice_contract": {
            "T0": "2026-01-15T00:00:00+08:00",
            "recall_score_threshold": 0.5,
            "min_slice_n": 1,
            "auto_profiles_from_state_db": True,
            "profile_parquet_path": str(fake_parquet),
            "eval_rows": [
                {
                    "canonical_id": "c1",
                    "decision_ts": "2026-01-05T12:00:00+08:00",
                    "table_id": "T1",
                    "score": 0.9,
                    "label": 1,
                },
            ],
        },
    }
    with patch.object(
        collectors,
        "_collect_slice_profiles_from_parquet",
        return_value=(
            {
                "c1": {
                    "theo_win_sum_30d": 100.0,
                    "active_days_30d": 5,
                    "turnover_sum_30d": 200.0,
                    "days_since_first_session": 20,
                }
            },
            ["slice_profiles_parquet_fallback_used_for_test"],
        ),
    ) as p_fallback:
        bundle = collectors.collect_phase1_artifacts(
            run_id, cfg, repo_root=repo, orchestrator_dir=orch
        )
    assert p_fallback.called
    sc = bundle.get("slice_contract") or {}
    assert sc.get("slice_data_incomplete") is False
    assert len(sc.get("row_annotations") or []) == 1


def test_collect_phase1_profiles_fallback_to_clickhouse_when_state_db_and_parquet_fail(
    tmp_path: Path,
) -> None:
    """ClickHouse fallback should run when state_db infeasible and Parquet not available."""
    import json
    import sqlite3

    import collectors

    repo = tmp_path / "repo"
    repo.mkdir()
    orch = tmp_path / "orch"
    run_id = "run_slice_profiles_ch_fallback"
    logs = orch / "state" / run_id / "logs"
    logs.mkdir(parents=True)
    metrics_dir = repo / "trainer" / "out_backtest"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "backtest_metrics.json").write_text("{}", encoding="utf-8")
    (logs / "r1_r6.stdout.log").write_text(json.dumps({"ok": True}), encoding="utf-8")

    state_db = repo / "state.db"
    with sqlite3.connect(state_db) as conn:
        conn.execute(
            "CREATE TABLE validation_results (bet_id TEXT, alert_ts TEXT, validated_at TEXT, result INT)"
        )
        conn.execute("CREATE TABLE player_profile (canonical_id TEXT)")
        conn.commit()

    cfg = {
        "model_dir": "trainer/models",
        "state_db_path": str(state_db),
        "prediction_log_db_path": str(repo / "pred.db"),
        "window": {
            "start_ts": "2026-01-01T00:00:00+08:00",
            "end_ts": "2026-01-08T00:00:00+08:00",
        },
        "thresholds": {},
        "backtest_metrics_path": "trainer/out_backtest/backtest_metrics.json",
        "slice_contract": {
            "T0": "2026-01-15T00:00:00+08:00",
            "recall_score_threshold": 0.5,
            "min_slice_n": 1,
            "auto_profiles_from_state_db": True,
            "auto_profiles_from_clickhouse": True,
            "eval_rows": [
                {
                    "canonical_id": "c1",
                    "decision_ts": "2026-01-05T12:00:00+08:00",
                    "table_id": "T1",
                    "score": 0.9,
                    "label": 1,
                },
            ],
        },
    }
    with patch.object(
        collectors,
        "_collect_slice_profiles_from_clickhouse",
        return_value=(
            {
                "c1": {
                    "theo_win_sum_30d": 100.0,
                    "active_days_30d": 5,
                    "turnover_sum_30d": 200.0,
                    "days_since_first_session": 20,
                }
            },
            ["slice_profiles_clickhouse_fallback_used_for_test"],
        ),
    ) as p_ch:
        bundle = collectors.collect_phase1_artifacts(
            run_id, cfg, repo_root=repo, orchestrator_dir=orch
        )
    assert p_ch.called
    sc = bundle.get("slice_contract") or {}
    assert sc.get("slice_data_incomplete") is False
    assert len(sc.get("row_annotations") or []) == 1
