"""Full-run MVP snapshot closeout: Phase A--E contract checks.

Runs against a real ``parallel_lda_mvp`` output tree (default: the user's
``snap_mvp_3517270f93ca8239``). Skips when the snapshot is absent so CI stays green.

Environment:
- ``MVP_CLOSEOUT_SNAPSHOT_ID`` — folder name under ``data/parallel_lda_mvp/``
  (default: ``snap_mvp_3517270f93ca8239``).
- ``MVP_CLOSEOUT_REPO_ROOT`` — optional override for repo root (default: parent
  of ``parallel_lda_mvp`` package).

Phase mapping (this test file):
- **A** — Ingestion / mapping posture: registry path + ingest hash + late-arrival
  tuning keys present in ``mvp_summary.json``.
- **B** — L1 preprocess: ``t_bet/cleaned__<day>.parquet`` exists for every
  ``days`` entry in each month summary.
- **C** — L1 span outputs: ``run_fact``, ``trip_run_map``, ``trip_fact`` Parquet
  exist per day (Phase C trip materialization complete).
- **D** — Phase D observability keys aligned with
  ``materialization_state_store_v1`` (``recompute_rounds``, etc.).
- **E** — Trainer Phase E compat: canonical empty ``reason_codes`` constant
  exists; MVP cleaned bet Parquet must **not** carry a ``reason_codes`` column
  (sample one file per month).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from parallel_lda_mvp.run_mvp import repo_root
from pipelines.layered_data_assets.orchestration.materialization_state_store_v1 import (
    METRIC_KEY_RECOMPUTE_ROUNDS,
    METRIC_KEY_RECOMPUTE_STOP_REASON,
    METRIC_KEY_ROW_FINGERPRINT_CHANGED,
    RECOMPUTE_STOP_SINGLE_PASS,
)


def _closeout_snapshot_id() -> str:
    raw = os.environ.get("MVP_CLOSEOUT_SNAPSHOT_ID", "").strip()
    return raw or "snap_mvp_3517270f93ca8239"


def _repo_root() -> Path:
    env = os.environ.get("MVP_CLOSEOUT_REPO_ROOT", "").strip()
    return Path(env).resolve() if env else repo_root()


def _snap_root() -> Path:
    rid = _closeout_snapshot_id()
    return (_repo_root() / "data" / "parallel_lda_mvp" / rid).resolve()


def _load_summary(path: Path) -> dict[str, Any]:
    return dict(json.loads(path.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def mvp_snap() -> Path:
    """Return snap root or skip entire module when snapshot missing."""
    root = _snap_root()
    if not root.is_dir():
        pytest.skip(f"MVP closeout snapshot not found: {root}")
    summaries = sorted(root.glob("gaming_ym=*/mvp_summary.json"))
    if not summaries:
        pytest.skip(f"No mvp_summary.json under {root}")
    return root


def test_phase_a_ingestion_and_mapping_keys(mvp_snap: Path) -> None:
    """Phase A: each month summary carries ingestion + mapping contract fields."""
    reg_suffix = "schema/preprocess_l0_data_contract_registry.yaml"
    canonical_reg = (_repo_root() / reg_suffix).resolve()
    assert canonical_reg.is_file(), f"repo missing ingestion registry: {canonical_reg}"
    for sum_path in sorted(mvp_snap.glob("gaming_ym=*/mvp_summary.json")):
        s = _load_summary(sum_path)
        assert s.get("ingest_yaml_content_sha256"), sum_path
        assert "late_arrival_recompute_hours" in s and int(s["late_arrival_recompute_hours"]) >= 0
        assert "late_arrival_window_days" in s and int(s["late_arrival_window_days"]) >= 0
        assert isinstance(s.get("mapping_identity_health"), dict), sum_path
        reg = s.get("ingestion_fix_registry_yaml")
        assert reg, sum_path
        norm = str(reg).replace("\\", "/")
        assert norm.endswith(reg_suffix), (sum_path, reg)


def test_phase_b_preprocess_cleaned_per_day(mvp_snap: Path) -> None:
    """Phase B: cleaned Parquet exists for every planned gaming_day in the month."""
    for sum_path in sorted(mvp_snap.glob("gaming_ym=*/mvp_summary.json")):
        month_dir = sum_path.parent
        t_bet = month_dir / "t_bet"
        s = _load_summary(sum_path)
        days = s.get("days")
        assert isinstance(days, list) and days, sum_path
        for d in days:
            cleaned = t_bet / f"cleaned__{d}.parquet"
            assert cleaned.is_file(), f"missing {cleaned}"


def test_phase_c_run_trip_artifacts_per_day(mvp_snap: Path) -> None:
    """Phase C: run_fact, trip_run_map, trip_fact present for each day."""
    for sum_path in sorted(mvp_snap.glob("gaming_ym=*/mvp_summary.json")):
        month_dir = sum_path.parent
        rf = month_dir / "run_fact"
        trm = month_dir / "trip_run_map"
        tf = month_dir / "trip_fact"
        s = _load_summary(sum_path)
        for d in s["days"]:
            assert (rf / f"run_fact__{d}.parquet").is_file(), d
            assert (trm / f"trip_run_map__{d}.parquet").is_file(), d
            assert (tf / f"trip_fact__{d}.parquet").is_file(), d


def test_phase_d_recompute_metrics_in_summary(mvp_snap: Path) -> None:
    """Phase D: mvp_summary carries the same metric keys as gate1 / state store."""
    for sum_path in sorted(mvp_snap.glob("gaming_ym=*/mvp_summary.json")):
        s = _load_summary(sum_path)
        assert int(s[METRIC_KEY_RECOMPUTE_ROUNDS]) == 1, sum_path
        assert str(s[METRIC_KEY_RECOMPUTE_STOP_REASON]) == RECOMPUTE_STOP_SINGLE_PASS, sum_path
        v = s.get(METRIC_KEY_ROW_FINGERPRINT_CHANGED)
        assert v is None or isinstance(v, bool), sum_path


def test_phase_e_no_reason_codes_column_on_cleaned_sample(mvp_snap: Path) -> None:
    """Phase E: trainer SHAP ``reason_codes`` must not leak into MVP cleaned bets."""
    from trainer.core import config

    assert getattr(config, "SCORER_REASON_CODES_DEFAULT_EMPTY", None) == "[]"

    for sum_path in sorted(mvp_snap.glob("gaming_ym=*/mvp_summary.json")):
        month_dir = sum_path.parent
        s = _load_summary(sum_path)
        days = s.get("days")
        assert isinstance(days, list) and days, sum_path
        first_day = str(days[0])
        p = month_dir / "t_bet" / f"cleaned__{first_day}.parquet"
        df = pd.read_parquet(p, columns=None)
        assert "reason_codes" not in df.columns, p


def test_snapshot_id_consistent_across_months(mvp_snap: Path) -> None:
    """Sanity: all summaries reference the same snapshot id as the folder name."""
    expected = _closeout_snapshot_id()
    for sum_path in sorted(mvp_snap.glob("gaming_ym=*/mvp_summary.json")):
        s = _load_summary(sum_path)
        assert s.get("source_snapshot_id") == expected, sum_path
