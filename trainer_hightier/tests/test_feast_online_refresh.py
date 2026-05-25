"""Tests for Feast online refresh orchestration."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
import pandas as pd
import pytest

from trainer_hightier.config import default_hightier_serving_config
from trainer_hightier.serving import feast_online_refresh as refresh_mod
from trainer_hightier.serving.feast_production_constants import PRODUCTION_MID_TERM_FEATURE_COLUMNS
from trainer_hightier.serving.feast_readiness import evaluate_feast_lookup_smoke_gate
from trainer_hightier.serving.feature_state_store import (
    feature_state_meta_get,
    init_feature_state_db,
)


def _mid_feature_row(canonical_id: str, anchor_day: str, *, wager: float) -> dict[str, object]:
    row: dict[str, object] = {
        "canonical_id": canonical_id,
        "anchor_gaming_day": pd.Timestamp(anchor_day),
    }
    for col in PRODUCTION_MID_TERM_FEATURE_COLUMNS:
        row[col] = wager if col == "fe__wager_sum__w1d" else 1.0
    return row


def _mid_feast_row(canonical_id: str, anchor_day: str, *, wager: float) -> dict[str, object]:
    ts = pd.Timestamp(anchor_day) + pd.Timedelta(hours=23, minutes=59, seconds=59)
    row: dict[str, object] = {
        "canonical_id": canonical_id,
        "event_timestamp": ts,
    }
    for col in PRODUCTION_MID_TERM_FEATURE_COLUMNS:
        row[col] = wager if col == "fe__wager_sum__w1d" else 1.0
    return row


def test_mid_export_bounds_daily_uses_single_anchor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(refresh_mod, "serving_gaming_day", lambda **_k: date(2025, 6, 2))
    monkeypatch.setattr(refresh_mod, "expected_mid_term_anchor", lambda _day: date(2025, 6, 1))
    anchor_start, anchor_end, bets_start, bets_end = refresh_mod._mid_export_bounds(
        close_hour=6,
        bootstrap_mid=False,
    )
    assert anchor_start == anchor_end == date(2025, 6, 1)
    assert bets_end == date(2025, 6, 1)
    assert bets_start == date(2025, 6, 1) - timedelta(days=int(refresh_mod.MID_TERM_SNAPSHOT_MAX_LOOKBACK_DAYS) - 1)


def test_mid_export_bounds_bootstrap_spans_anchor_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(refresh_mod, "serving_gaming_day", lambda **_k: date(2025, 6, 2))
    monkeypatch.setattr(refresh_mod, "expected_mid_term_anchor", lambda _day: date(2025, 6, 1))
    anchor_start, anchor_end, bets_start, _bets_end = refresh_mod._mid_export_bounds(
        close_hour=6,
        bootstrap_mid=True,
        bootstrap_anchor_days=30,
    )
    assert anchor_end == date(2025, 6, 1)
    assert anchor_start == date(2025, 5, 3)
    assert bets_start == anchor_start - timedelta(days=int(refresh_mod.MID_TERM_SNAPSHOT_MAX_LOOKBACK_DAYS) - 1)


def test_merge_mid_feast_carry_forward_keeps_latest_per_canonical(tmp_path: Path) -> None:
    prev = tmp_path / "prev_feast.parquet"
    daily = tmp_path / "daily_prod.parquet"
    out = tmp_path / "merged_feast.parquet"
    pd.DataFrame(
        [
            _mid_feast_row("c1", "2025-05-01", wager=10.0),
            _mid_feast_row("c2", "2025-05-01", wager=20.0),
        ]
    ).to_parquet(prev, index=False)
    pd.DataFrame(
        [
            _mid_feature_row("c1", "2025-05-02", wager=99.0),
            _mid_feature_row("c3", "2025-05-02", wager=30.0),
        ]
    ).to_parquet(daily, index=False)
    rows = refresh_mod.merge_mid_feast_carry_forward(
        previous_feast_parquet=prev,
        daily_snapshot_parquet=daily,
        feast_out=out,
    )
    assert rows == 3
    merged = pd.read_parquet(out)
    by_cid = merged.set_index("canonical_id")
    assert float(by_cid.loc["c1", "fe__wager_sum__w1d"]) == 99.0
    assert float(by_cid.loc["c2", "fe__wager_sum__w1d"]) == 20.0
    assert float(by_cid.loc["c3", "fe__wager_sum__w1d"]) == 30.0


def test_materialize_training_mid_feast_seed_filters_allowlist(tmp_path: Path) -> None:
    train = tmp_path / "train_mid.parquet"
    allow = tmp_path / "allow.parquet"
    mapping = tmp_path / "map.parquet"
    out = tmp_path / "seed.parquet"
    pd.DataFrame(
        [
            _mid_feature_row("c1", "2025-05-01", wager=10.0),
            _mid_feature_row("c2", "2025-05-01", wager=20.0),
            _mid_feature_row("c1", "2025-06-01", wager=11.0),
        ]
    ).to_parquet(train, index=False)
    pd.DataFrame({"player_id": [1, 2]}).to_parquet(allow, index=False)
    pd.DataFrame({"player_id": [1, 2], "canonical_id": ["c1", "c2"]}).to_parquet(mapping, index=False)
    meta = refresh_mod.materialize_training_mid_feast_seed(
        training_mid_snapshot=train,
        allowlist_parquet=allow,
        canonical_mapping_parquet=mapping,
        anchor_end=date(2025, 6, 15),
        out_parquet=out,
    )
    assert meta["distinct_canonical"] == 2
    seeded = pd.read_parquet(out)
    assert set(seeded["canonical_id"].astype(str)) == {"c1", "c2"}
    assert len(seeded) == 3


def test_evaluate_feast_lookup_smoke_gate_skips_mid_when_plan_empty() -> None:
    """Slow-only deploy must not fail mid smoke when mid_columns=()."""
    cfg = default_hightier_serving_config()
    smoke = {
        "entity_missing_rate": 0.0,
        "mid_cell_null_rate": 1.0,
    }
    ok, reason = evaluate_feast_lookup_smoke_gate(
        smoke,
        mid_columns=(),
        entity_missing_fail_fraction=float(cfg.scorer_feast_entity_missing_fail_fraction),
        mid_cell_null_fail_fraction=float(cfg.scorer_feast_mid_cell_null_fail_fraction),
        feast_spike_rows=None,
        allowlist_canonical_count=None,
        min_canonical_coverage_fraction=float(cfg.scorer_feast_mid_min_canonical_coverage_fraction),
        mid_smoke_columns=cfg.scorer_feast_mid_smoke_columns,
    )
    assert ok
    assert reason is None


def test_evaluate_feast_lookup_smoke_gate_fails_on_mid_cell_null_only() -> None:
    cfg = default_hightier_serving_config()
    smoke = {
        "entity_missing_rate": 0.0,
        "mid_cell_null_rate": 1.0,
    }
    ok, reason = evaluate_feast_lookup_smoke_gate(
        smoke,
        mid_columns=("fe__wager_sum__w1d",),
        entity_missing_fail_fraction=float(cfg.scorer_feast_entity_missing_fail_fraction),
        mid_cell_null_fail_fraction=float(cfg.scorer_feast_mid_cell_null_fail_fraction),
        feast_spike_rows=None,
        allowlist_canonical_count=None,
        min_canonical_coverage_fraction=float(cfg.scorer_feast_mid_min_canonical_coverage_fraction),
    )
    assert not ok
    assert reason is not None
    assert "mid cell null rate" in reason


def test_parse_refresh_layers_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="unsupported layers"):
        refresh_mod.parse_refresh_layers("mid,short")


def test_resolve_refresh_options_rejects_local_override_with_clickhouse() -> None:
    with pytest.raises(ValueError, match="local cleaned overrides"):
        refresh_mod._resolve_refresh_options(
            layers="mid",
            source="clickhouse",
            skip_apply=False,
            skip_materialize=False,
            smoke_only=False,
            dry_run=False,
            feast_repo=None,
            readiness_path=None,
            canonical_mapping=Path("/tmp/map.parquet"),
            adt_allowlist=Path("/tmp/allow.parquet"),
            local_cleaned_bet=Path("/tmp/bet.parquet"),
            local_cleaned_session=None,
            max_smoke_entities=10,
            summary_path=None,
        )


def _patch_feature_state_db(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    import trainer_hightier.serving.feature_state_store as fss

    cfg = replace(default_hightier_serving_config(), feature_state_db_path=str(db_path))
    monkeypatch.setattr(refresh_mod, "default_hightier_serving_config", lambda: cfg)
    monkeypatch.setattr(fss, "default_hightier_serving_config", lambda: cfg)


def test_dry_run_writes_run_row_without_readiness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    allow = tmp_path / "allow.parquet"
    cmap = tmp_path / "map.parquet"
    pd.DataFrame({"player_id": [1]}).to_parquet(allow, index=False)
    pd.DataFrame({"player_id": [1], "canonical_id": ["c1"]}).to_parquet(cmap, index=False)
    db_path = tmp_path / "feature_state.db"
    readiness = tmp_path / "feast_online_readiness.json"
    summary = tmp_path / "report.json"
    monkeypatch.setattr(
        refresh_mod,
        "resolve_adt_allowlist_path",
        lambda *_a, **_k: allow,
    )
    _patch_feature_state_db(monkeypatch, db_path)
    init_feature_state_db(db_path)
    opts = refresh_mod._resolve_refresh_options(
        layers="mid,slow",
        source="clickhouse",
        skip_apply=False,
        skip_materialize=False,
        smoke_only=False,
        dry_run=True,
        feast_repo=tmp_path / "feast_repo",
        readiness_path=readiness,
        canonical_mapping=cmap,
        adt_allowlist=allow,
        local_cleaned_bet=None,
        local_cleaned_session=None,
        max_smoke_entities=5,
        summary_path=summary,
    )
    report = refresh_mod.run_feast_online_refresh(opts)
    assert report["verdict"] == "dry_run"
    assert not readiness.exists()
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT status, source FROM feast_refresh_run ORDER BY id DESC LIMIT 1",
        ).fetchone()
    finally:
        conn.close()
    assert row == ("ok", "clickhouse")


def test_mocked_refresh_publishes_readiness_after_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    allow = tmp_path / "allow.parquet"
    cmap = tmp_path / "map.parquet"
    pd.DataFrame({"player_id": [1, 2]}).to_parquet(allow, index=False)
    pd.DataFrame({"player_id": [1, 2], "canonical_id": ["c1", "c2"]}).to_parquet(cmap, index=False)
    db_path = tmp_path / "feature_state.db"
    readiness = tmp_path / "feast_online_readiness.json"
    summary = tmp_path / "report.json"
    staging = tmp_path / "staging"
    staging.mkdir()
    bet_export = staging / "bet.parquet"
    sess_export = staging / "sess.parquet"
    pd.DataFrame(
        {
            "player_id": [1],
            "gaming_day": pd.Timestamp("2025-06-01"),
            "payout_complete_dtm": pd.Timestamp("2025-06-01 10:00:00"),
            "wager": [10.0],
            "payout_odds": [1.0],
        }
    ).to_parquet(bet_export, index=False)
    pd.DataFrame({"player_id": [1], "gaming_day": pd.Timestamp("2025-06-01"), "theo_win": [1.0]}).to_parquet(
        sess_export, index=False
    )

    mid_art = staging / "mid.parquet"
    slow_art = staging / "slow.parquet"
    mid_feast = tmp_path / "mid_feast.parquet"
    slow_feast = tmp_path / "slow_feast.parquet"

    def _fake_mid(*, opts, staging_dir, player_ids):  # noqa: ANN001
        return refresh_mod.LayerRefreshOutcome(
            layer="mid",
            status="ok",
            meta={
                "row_count": 2,
                "feast_spike_rows": 2,
                "snapshot_scope": "production",
                "mid_term_anchor_gaming_day_max": "2025-05-31",
            },
            export_meta={"rows_exported": 1, "export_seconds": 0.1},
            artifact_path=mid_art,
            feast_parquet_path=mid_feast,
            compute_seconds=0.2,
            detail={},
        )

    def _fake_slow(*, opts, staging_dir, player_ids):  # noqa: ANN001
        return refresh_mod.LayerRefreshOutcome(
            layer="slow",
            status="ok",
            meta={
                "row_count": 2,
                "snapshot_scope": "production",
                "slow_anchor_gaming_day_max": "2025-05-31",
            },
            export_meta={"rows_exported": 1, "export_seconds": 0.1},
            artifact_path=slow_art,
            feast_parquet_path=slow_feast,
            compute_seconds=0.2,
            detail={},
        )

    monkeypatch.setattr(refresh_mod, "load_adt_allowlist_ids", lambda _p: frozenset({1, 2}))
    monkeypatch.setattr(refresh_mod, "_refresh_mid_layer", _fake_mid)
    monkeypatch.setattr(refresh_mod, "_refresh_slow_layer", _fake_slow)
    monkeypatch.setattr(refresh_mod, "run_feast_apply", lambda _repo, **kwargs: 0.5)
    monkeypatch.setattr(refresh_mod, "run_feast_materialize_views", lambda *_a, **_k: 1.0)
    monkeypatch.setattr(
        "trainer_hightier.serving.feast_online_refresh.run_allowlist_feast_lookup_smoke",
        lambda **_k: {
            "ok": True,
            "sample_size": 2,
            "entity_missing_rate": 0.0,
            "mid_cell_null_rate": 0.0,
            "cell_null_counts": {},
        },
    )
    _patch_feature_state_db(monkeypatch, db_path)
    init_feature_state_db(db_path)
    opts = refresh_mod.RefreshOptions(
        layers=frozenset({"mid", "slow"}),
        source="clickhouse",
        skip_apply=False,
        skip_materialize=False,
        smoke_only=False,
        dry_run=False,
        feast_repo=tmp_path / "feast_repo",
        readiness_path=readiness,
        canonical_mapping=cmap,
        adt_allowlist=allow,
        local_cleaned_bet=None,
        local_cleaned_session=None,
        max_smoke_entities=2,
        summary_path=summary,
        bootstrap_mid=False,
        apply_schema=False,
    )
    report = refresh_mod.run_feast_online_refresh(opts)
    assert report["verdict"] == "ok"
    assert readiness.is_file()
    doc = json.loads(readiness.read_text(encoding="utf-8"))
    assert doc["mid_term"]["materialize_source"] == "feast_online_refresh"
    assert doc["slow_patron"]["materialize_source"] == "feast_online_refresh"
    conn = sqlite3.connect(db_path)
    try:
        layers = conn.execute(
            "SELECT layer, status FROM feast_refresh_layer WHERE run_id = ? ORDER BY layer",
            (report["run_id"],),
        ).fetchall()
    finally:
        conn.close()
    assert layers == [("mid", "ok"), ("slow", "ok")]
    assert feature_state_meta_get("feast_online_readiness_latest_run_id", path=db_path) == report["run_id"]
    stored = feature_state_meta_get("feast_online_readiness_latest_json", path=db_path)
    assert stored is not None
    assert json.loads(stored)["mid_term"]["materialize_source"] == "feast_online_refresh"


def test_smoke_failure_does_not_publish_readiness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    allow = tmp_path / "allow.parquet"
    cmap = tmp_path / "map.parquet"
    pd.DataFrame({"player_id": [1]}).to_parquet(allow, index=False)
    pd.DataFrame({"player_id": [1], "canonical_id": ["c1"]}).to_parquet(cmap, index=False)
    db_path = tmp_path / "feature_state.db"
    readiness = tmp_path / "feast_online_readiness.json"
    _patch_feature_state_db(monkeypatch, db_path)
    init_feature_state_db(db_path)
    monkeypatch.setattr(refresh_mod, "load_adt_allowlist_ids", lambda _p: frozenset({1}))
    monkeypatch.setattr(
        refresh_mod,
        "_refresh_mid_layer",
        lambda **_k: refresh_mod.LayerRefreshOutcome(
            layer="mid",
            status="ok",
            meta={"row_count": 1, "feast_spike_rows": 1, "mid_term_anchor_gaming_day_max": "2025-05-31"},
            export_meta={"rows_exported": 1},
            artifact_path=tmp_path / "mid.parquet",
            feast_parquet_path=tmp_path / "mid_feast.parquet",
            compute_seconds=0.1,
            detail={},
        ),
    )
    monkeypatch.setattr(refresh_mod, "run_feast_apply", lambda _repo, **kwargs: 0.1)
    monkeypatch.setattr(refresh_mod, "run_feast_materialize_views", lambda *_a, **_k: 0.1)
    monkeypatch.setattr(
        "trainer_hightier.serving.feast_online_refresh.run_allowlist_feast_lookup_smoke",
        lambda **_k: {
            "ok": False,
            "entity_missing_rate": 0.5,
            "mid_cell_null_rate": 0.0,
            "sample_size": 2,
        },
    )
    opts = refresh_mod.RefreshOptions(
        layers=frozenset({"mid"}),
        source="clickhouse",
        skip_apply=False,
        skip_materialize=False,
        smoke_only=False,
        dry_run=False,
        feast_repo=tmp_path / "feast_repo",
        readiness_path=readiness,
        canonical_mapping=cmap,
        adt_allowlist=allow,
        local_cleaned_bet=None,
        local_cleaned_session=None,
        max_smoke_entities=2,
        summary_path=tmp_path / "report.json",
        bootstrap_mid=False,
        apply_schema=False,
    )
    with pytest.raises(RuntimeError, match="entity missing rate"):
        refresh_mod.run_feast_online_refresh(opts)
    assert not readiness.exists()
    conn = sqlite3.connect(db_path)
    try:
        status = conn.execute(
            "SELECT status FROM feast_refresh_run ORDER BY id DESC LIMIT 1",
        ).fetchone()[0]
    finally:
        conn.close()
    assert status == "error"


def test_slow_only_refresh_runs_feast_apply_when_registry_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deploy slow-only first boot must apply schema before materialize."""
    allow = tmp_path / "allow.parquet"
    cmap = tmp_path / "map.parquet"
    pd.DataFrame({"player_id": [1]}).to_parquet(allow, index=False)
    pd.DataFrame({"player_id": [1], "canonical_id": ["c1"]}).to_parquet(cmap, index=False)
    feast_repo = tmp_path / "feast_repo"
    (feast_repo / "data").mkdir(parents=True)
    readiness = tmp_path / "feast_online_readiness.json"
    db_path = tmp_path / "feature_state.db"
    apply_calls: list[bool] = []

    def _capture_apply(_repo: Path, **kwargs: object) -> float:
        apply_calls.append(bool(kwargs.get("reset_runtime")))
        return 0.1

    monkeypatch.setattr(refresh_mod, "load_adt_allowlist_ids", lambda _p: frozenset({1}))
    monkeypatch.setattr(
        refresh_mod,
        "_refresh_slow_layer",
        lambda **_k: refresh_mod.LayerRefreshOutcome(
            layer="slow",
            status="ok",
            meta={"row_count": 1, "feast_spike_rows": 1, "slow_anchor_gaming_day_max": "2025-05-31"},
            export_meta={"rows_exported": 1},
            artifact_path=tmp_path / "slow.parquet",
            feast_parquet_path=tmp_path / "slow_feast.parquet",
            compute_seconds=0.1,
            detail={},
        ),
    )
    monkeypatch.setattr(refresh_mod, "run_feast_apply", _capture_apply)
    monkeypatch.setattr(refresh_mod, "run_feast_materialize_views", lambda *_a, **_k: 0.1)
    monkeypatch.setattr(
        "trainer_hightier.serving.feast_online_refresh.run_allowlist_feast_lookup_smoke",
        lambda **_k: {
            "ok": True,
            "sample_size": 1,
            "entity_missing_rate": 0.0,
            "mid_cell_null_rate": 0.0,
            "cell_null_counts": {},
        },
    )
    _patch_feature_state_db(monkeypatch, db_path)
    init_feature_state_db(db_path)
    opts = refresh_mod.RefreshOptions(
        layers=frozenset({"slow"}),
        source="clickhouse",
        skip_apply=True,
        skip_materialize=False,
        smoke_only=False,
        dry_run=False,
        feast_repo=feast_repo,
        readiness_path=readiness,
        canonical_mapping=cmap,
        adt_allowlist=allow,
        local_cleaned_bet=None,
        local_cleaned_session=None,
        max_smoke_entities=1,
        summary_path=tmp_path / "report.json",
        bootstrap_mid=False,
        apply_schema=False,
    )
    report = refresh_mod.run_feast_online_refresh(opts)
    assert report["verdict"] == "ok"
    assert apply_calls == [False]


def test_init_feature_state_db_creates_feast_refresh_tables(tmp_path: Path) -> None:
    db_path = init_feature_state_db(tmp_path / "fs.db")
    conn = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'",
            ).fetchall()
        }
    finally:
        conn.close()
    assert "feast_refresh_run" in tables
    assert "feast_refresh_layer" in tables
