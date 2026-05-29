"""Regression tests for production snapshot freshness and manifest compatibility."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from trainer_hightier.config import (
    HK_TZ,
    HightierServingConfig,
    MID_TERM_GRAIN_CANONICAL_DAILY_ASOF,
    SLOW_PATRON_GRAIN_CANONICAL_ASOF,
    set_hightier_serving_deploy_override,
)
from trainer_hightier.feature_experiment.materialize_mid_term_daily_snapshot import (
    MID_TERM_SNAPSHOT_OUTPUT_COLUMNS,
)
from trainer_hightier.serving.contracts import META_KEY_SLOW_REFRESH_LAST_CHECK_DAY
from trainer_hightier.serving.feature_state_store import (
    ActiveSnapshotManifest,
    feature_state_meta_set,
    init_feature_state_db,
)
from trainer_hightier.serving.production_source_mirror import (
    resolve_production_bet_mirror_dir,
    resolve_production_session_mirror_path,
    validate_production_bet_mirror,
    validate_production_session_mirror,
)
from trainer_hightier.serving.snapshot_freshness import (
    build_deploy_startup_snapshot_plan,
    build_scoring_snapshot_gate,
    evaluate_mid_term_freshness,
    evaluate_slow_freshness,
    expected_mid_term_anchor,
    expected_slow_month_end_anchor,
    validate_mid_term_artifact,
    validate_slow_artifact,
)


def _write_mid_term_snapshot(path: Path, *, anchor: date) -> None:
    row: dict[str, list] = {
        "canonical_id": ["c1"],
        "anchor_gaming_day_event": [anchor.isoformat()],
    }
    for col in MID_TERM_SNAPSHOT_OUTPUT_COLUMNS:
        if col.startswith("fe__"):
            row[col] = [1.0]
    pq.write_table(pa.Table.from_pydict(row), path)


def _write_slow_snapshot(path: Path, *, anchor: date) -> None:
    pq.write_table(
        pa.Table.from_pydict(
            {
                "canonical_id": ["c1"],
                "anchor_gaming_day_event": [anchor.isoformat()],
                "patron__adt__w180d_m1snap": [100.0],
                "patron__theo_win_sum__w180d_m1snap": [10.0],
                "patron__gaming_days_cnt__w180d_m1snap": [5.0],
            }
        ),
        path,
    )


def test_legacy_manifest_parses_without_new_keys(tmp_path: Path) -> None:
    man = {
        "version": "v1",
        "slow_patron_parquet": "slow.parquet",
        "fe_derived_parquet": "fe.parquet",
    }
    parsed = ActiveSnapshotManifest.from_dict(man, manifest_dir=tmp_path)
    assert parsed.version == "v1"
    assert parsed.fe_short_term_parquet == parsed.fe_derived_parquet
    assert parsed.mid_term_snapshot_parquet is None


def test_new_manifest_parses_per_layer_keys(tmp_path: Path) -> None:
    man = {
        "version": "v2",
        "slow_patron_parquet": "slow.parquet",
        "fe_derived_parquet": "fe.parquet",
        "fe_short_term_parquet": "fe_short.parquet",
        "mid_term_snapshot_parquet": "mid.parquet",
        "mid_term_grain": MID_TERM_GRAIN_CANONICAL_DAILY_ASOF,
    }
    parsed = ActiveSnapshotManifest.from_dict(man, manifest_dir=tmp_path)
    assert parsed.fe_short_term_parquet.name == "fe_short.parquet"
    assert parsed.mid_term_snapshot_parquet.name == "mid.parquet"


def test_mid_term_freshness_stale_allowed_vs_hard_cap() -> None:
    serving = date(2026, 5, 19)
    expected = expected_mid_term_anchor(serving)
    fresh = evaluate_mid_term_freshness(anchor_max=expected, serving_day=serving, hard_cap_days=3)
    assert fresh.status == "fresh"
    stale = evaluate_mid_term_freshness(
        anchor_max=expected - timedelta(days=2),
        serving_day=serving,
        hard_cap_days=3,
    )
    assert stale.status == "stale_allowed"
    breached = evaluate_mid_term_freshness(
        anchor_max=expected - timedelta(days=4),
        serving_day=serving,
        hard_cap_days=3,
    )
    assert breached.status == "hard_cap_breached"


def test_validate_mid_term_rejects_all_null(tmp_path: Path) -> None:
    path = tmp_path / "mid.parquet"
    row: dict[str, list] = {
        "canonical_id": ["c1"],
        "anchor_gaming_day_event": ["2026-05-18"],
    }
    for col in MID_TERM_SNAPSHOT_OUTPUT_COLUMNS:
        if col.startswith("fe__"):
            row[col] = [None]
    pq.write_table(pa.Table.from_pydict(row), path)
    result = validate_mid_term_artifact(path)
    assert result.hard_failure
    assert "all-null" in result.message


def test_scoring_gate_allows_stale_but_blocks_hard_cap(tmp_path: Path) -> None:
    mid_path = tmp_path / "mid.parquet"
    slow_path = tmp_path / "slow.parquet"
    anchor = date(2026, 5, 15)
    _write_mid_term_snapshot(mid_path, anchor=anchor)
    _write_slow_snapshot(slow_path, anchor=date(2026, 4, 30))
    mid_val = validate_mid_term_artifact(mid_path)
    slow_val = validate_slow_artifact(slow_path)
    serving = date(2026, 5, 19)
    mid_fresh = evaluate_mid_term_freshness(anchor_max=anchor, serving_day=serving, hard_cap_days=3)
    slow_fresh = evaluate_slow_freshness(anchor_max=date(2026, 4, 30), serving_day=serving)
    gate = build_scoring_snapshot_gate(
        mid_term=mid_fresh,
        slow=slow_fresh,
        mid_validation=mid_val,
        slow_validation=slow_val,
    )
    assert gate.allow_scoring
    assert gate.degraded


def test_asof_boundary_anchor_less_than_gaming_day(tmp_path: Path) -> None:
    from trainer_hightier.serving.feature_builder import join_production_fe_suppliers

    mid_path = tmp_path / "mid.parquet"
    fe_path = tmp_path / "fe.parquet"
    d1 = date(2026, 5, 17)
    d2 = date(2026, 5, 18)
    _write_mid_term_snapshot(mid_path, anchor=d1)
    pq.write_table(
        pa.Table.from_pydict({"bet_id": [1.0], "fe__wager_sum__w15m": [5.0]}),
        fe_path,
    )
    rows = pd.DataFrame(
        {
            "bet_id": [1],
            "canonical_id": ["c1"],
            "gaming_day_event": [d2.isoformat()],
            "payout_odds": [2.0],
        }
    )
    out = join_production_fe_suppliers(
        rows,
        fe_short_term_parquet=fe_path,
        mid_term_snapshot_parquet=mid_path,
        short_term_columns=("fe__wager_sum__w15m",),
        mid_term_columns=("fe__bets_cnt__w1d", "fe__wager_sum__w1d"),
    )
    assert out.loc[0, "fe__bets_cnt__w1d"] == 1.0


def _serving_cfg(tmp_path: Path, snap_dir: Path) -> HightierServingConfig:
    cfg = replace(
        HightierServingConfig(),
        snapshot_manifest_dir=snap_dir,
        feature_state_db_path=tmp_path / "feature_state.db",
        production_cleaned_bet_mirror_dir=tmp_path / "source_mirror" / "cleaned_bet",
        production_cleaned_session_mirror_parquet=tmp_path / "source_mirror" / "cleaned_session.parquet",
    )
    set_hightier_serving_deploy_override(cfg)
    init_feature_state_db(cfg.feature_state_db_path)
    return cfg


def _publish_test_manifest(
    snap_dir: Path,
    *,
    mid_path: Path | None,
    slow_path: Path,
    mid_anchor: date | None = None,
    slow_anchor: date | None = None,
) -> None:
    payload: dict[str, object] = {
        "version": "test-v1",
        "slow_patron_parquet": slow_path.name,
        "slow_patron_grain": SLOW_PATRON_GRAIN_CANONICAL_ASOF,
        "adt_allowlist_parquet": slow_path.name,
    }
    if mid_path is not None:
        payload["mid_term_snapshot_parquet"] = mid_path.name
        payload["mid_term_grain"] = MID_TERM_GRAIN_CANONICAL_DAILY_ASOF
        if mid_anchor is not None:
            payload["mid_term_anchor_gaming_day_event_max"] = mid_anchor.isoformat()
    if slow_anchor is not None:
        payload["slow_anchor_gaming_day_event_max"] = slow_anchor.isoformat()
    (snap_dir / "active_manifest.json").write_text(json.dumps(payload), encoding="utf-8")


def test_default_production_mirror_paths_do_not_recurse(tmp_path: Path) -> None:
    """Default mirror resolution should derive sibling source_mirror paths."""

    snap_dir = tmp_path / "snapshots"
    cfg = replace(
        HightierServingConfig(),
        snapshot_manifest_dir=snap_dir,
        production_cleaned_bet_mirror_dir=None,
        production_cleaned_session_mirror_parquet=None,
    )
    set_hightier_serving_deploy_override(cfg)
    try:
        assert resolve_production_bet_mirror_dir() == tmp_path / "source_mirror" / "cleaned_bet"
        assert resolve_production_session_mirror_path() == tmp_path / "source_mirror" / "cleaned_session.parquet"
    finally:
        set_hightier_serving_deploy_override(None)


def test_expected_slow_month_end_anchor() -> None:
    assert expected_slow_month_end_anchor(date(2026, 5, 1)) == date(2026, 4, 30)
    assert expected_slow_month_end_anchor(date(2026, 1, 15)) == date(2025, 12, 31)


def test_startup_plan_hard_failure_vs_stale_allowed(tmp_path: Path) -> None:
    snap_dir = tmp_path / "snapshots"
    snap_dir.mkdir()
    serving = date(2026, 5, 19)
    expected_mid = expected_mid_term_anchor(serving)
    mid_path = snap_dir / "mid.parquet"
    slow_path = snap_dir / "slow.parquet"
    _write_mid_term_snapshot(mid_path, anchor=expected_mid - timedelta(days=2))
    _write_slow_snapshot(slow_path, anchor=date(2026, 4, 30))
    _publish_test_manifest(
        snap_dir,
        mid_path=mid_path,
        slow_path=slow_path,
        mid_anchor=expected_mid - timedelta(days=2),
        slow_anchor=date(2026, 4, 30),
    )
    cfg = _serving_cfg(tmp_path, snap_dir)
    after_04 = datetime(2026, 5, 19, 5, 0, tzinfo=ZoneInfo(HK_TZ))
    with patch("trainer_hightier.serving.snapshot_freshness.datetime") as mock_dt:
        mock_dt.now.return_value = after_04.astimezone(timezone.utc)
        mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
        plan = build_deploy_startup_snapshot_plan(cfg)
    assert not plan.mid_hard_failure
    assert not plan.slow_hard_failure
    assert not plan.mid_startup_refresh
    assert not plan.slow_startup_refresh
    assert plan.mid_background_refresh

    missing_mid_dir = tmp_path / "snapshots2"
    missing_mid_dir.mkdir()
    slow_only = missing_mid_dir / "slow.parquet"
    _write_slow_snapshot(slow_only, anchor=date(2026, 4, 30))
    _publish_test_manifest(
        missing_mid_dir,
        mid_path=None,
        slow_path=slow_only,
        slow_anchor=date(2026, 4, 30),
    )
    cfg2 = replace(cfg, snapshot_manifest_dir=missing_mid_dir)
    set_hightier_serving_deploy_override(cfg2)
    hard_plan = build_deploy_startup_snapshot_plan(cfg2)
    assert hard_plan.mid_hard_failure
    assert hard_plan.mid_startup_refresh
    assert not hard_plan.mid_background_refresh


def test_mid_term_refresh_needed_respects_04_00(tmp_path: Path) -> None:
    from trainer_hightier.deploy.main import _mid_term_refresh_needed

    snap_dir = tmp_path / "snapshots"
    snap_dir.mkdir()
    serving = date(2026, 5, 19)
    expected_mid = expected_mid_term_anchor(serving)
    mid_path = snap_dir / "mid.parquet"
    slow_path = snap_dir / "slow.parquet"
    _write_mid_term_snapshot(mid_path, anchor=expected_mid - timedelta(days=1))
    _write_slow_snapshot(slow_path, anchor=date(2026, 4, 30))
    _publish_test_manifest(
        snap_dir,
        mid_path=mid_path,
        slow_path=slow_path,
        mid_anchor=expected_mid - timedelta(days=1),
        slow_anchor=date(2026, 4, 30),
    )
    cfg = _serving_cfg(tmp_path, snap_dir)

    before_04 = datetime(2026, 5, 19, 2, 0, tzinfo=ZoneInfo(HK_TZ))
    after_04 = datetime(2026, 5, 19, 5, 0, tzinfo=ZoneInfo(HK_TZ))
    with patch("trainer_hightier.deploy.main.datetime") as mock_dt:
        mock_dt.now.return_value = before_04
        mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
        needed, _ = _mid_term_refresh_needed(cfg)
        assert not needed
    with patch("trainer_hightier.deploy.main.datetime") as mock_dt:
        mock_dt.now.return_value = after_04
        mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
        needed, reason = _mid_term_refresh_needed(cfg)
        assert needed
        assert "stale" in reason.lower()


def test_slow_refresh_daily_gate(tmp_path: Path) -> None:
    from trainer_hightier.deploy.main import _slow_refresh_needed

    snap_dir = tmp_path / "snapshots"
    snap_dir.mkdir()
    slow_path = snap_dir / "slow.parquet"
    _write_slow_snapshot(slow_path, anchor=date(2026, 3, 31))
    _publish_test_manifest(
        snap_dir,
        mid_path=None,
        slow_path=slow_path,
        slow_anchor=date(2026, 3, 31),
    )
    cfg = _serving_cfg(tmp_path, snap_dir)
    today = datetime.now(ZoneInfo(HK_TZ)).date().isoformat()
    feature_state_meta_set(META_KEY_SLOW_REFRESH_LAST_CHECK_DAY, today, path=cfg.feature_state_db_path)

    needed, reason = _slow_refresh_needed(cfg)
    assert not needed
    assert "already checked today" in reason


def test_bet_mirror_rejects_missing_columns(tmp_path: Path) -> None:
    bet_dir = tmp_path / "cleaned_bet"
    bet_dir.mkdir()
    pq.write_table(
        pa.Table.from_pydict({"gaming_day_event": ["2026-05-18"], "player_id": ["p1"]}),
        bet_dir / "part.parquet",
    )
    result = validate_production_bet_mirror(mirror_dir=bet_dir, required_lookback_days=3)
    assert not result.ok
    assert "missing columns" in result.message


def test_session_mirror_rejects_insufficient_coverage(tmp_path: Path) -> None:
    path = tmp_path / "cleaned_session.parquet"
    pq.write_table(
        pa.Table.from_pydict(
            {
                "player_id": ["p1"],
                "gaming_day_event": ["2026-05-18"],
                "theo_win": [1.0],
            }
        ),
        path,
    )
    result = validate_production_session_mirror(mirror_path=path, required_lookback_days=180)
    assert not result.ok
    assert "coverage" in result.message.lower()
