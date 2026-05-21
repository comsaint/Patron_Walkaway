"""Scorer v2 Feast runtime: mock adapter, missing policy, cursor advance."""

from __future__ import annotations

import pickle
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest
from sklearn.dummy import DummyClassifier
from zoneinfo import ZoneInfo

from trainer_hightier.config import FEATURE_CANDIDATE_REGISTRY_SNAPSHOT_FILENAME, default_hightier_serving_config
from trainer_hightier.feature_experiment.candidate_registry_loader import load_candidate_registry
from trainer_hightier.serving.feast_online_adapter import (
    FEAST_CANONICAL_ENTITY_NAME,
    FEAST_CANONICAL_JOIN_KEY,
    MockFeastOnlineAdapter,
    RowMissingAudit,
    _extract_entity_join_keys,
    apply_entity_missing_policy,
    compute_row_missing_audits,
    enrich_row_audits_composite_upstream,
    join_feast_lookup,
    resolve_online_feature_refs,
    run_feast_scorer_schema_smoke_check,
)
from trainer_hightier.feature_experiment.feast_mid_term_spike import (
    SPIKE_FEATURE_SERVICE_NAME as MID_SPIKE_FEATURE_SERVICE_NAME,
    SPIKE_FEATURE_VIEW_NAME as MID_SPIKE_FEATURE_VIEW_NAME,
)
from trainer_hightier.feature_experiment.feast_long_term_spike import (
    SPIKE_FEATURE_VIEW_NAME as LONG_SPIKE_FEATURE_VIEW_NAME,
)
from trainer_hightier.feature_experiment.feature_cadence import MID_TERM_COMPOSITE_FEATURE_COLUMNS
from trainer_hightier.serving.feature_builder import (
    attach_mid_term_composite_columns,
    attach_short_term_pit_features,
    assert_short_term_pit_columns_supported,
)
from trainer_hightier.serving.feature_supply import (
    ScorerSupplierPlan,
    assert_feast_plan_schema_support_or_raise,
    assert_scorer_supplier_plan_or_raise,
    build_scorer_supplier_plan,
    scorer_supplier_route_counts,
)
from trainer_hightier.serving.prediction_log import (
    append_hightier_prediction_log,
    append_skipped_entity_missing_log,
    init_prediction_log_db,
)
from trainer_hightier.serving.state_db import get_last_processed_etl_insert, init_state_db


def _write_min_registry(path: Path, *, include_mid: bool = True, include_slow: bool = True) -> None:
    feats = ["wager", "player_id"]
    if include_mid:
        feats.append("fe__bets_cnt__w1d")
    if include_slow:
        feats.append("patron__adt__w180d_m1snap")
    lines = ["registry_version: scorer-v2-test", "features:"]
    for fid in feats:
        if fid.startswith("fe__"):
            lines.extend(
                [
                    f"  - feature_id: {fid}",
                    "    group_id: g",
                    "    source: fe_derived",
                    "    status: active",
                    "    enabled_for: [baseline]",
                    "    time_horizon: mid_term",
                    "    max_lookback: P1D",
                ]
            )
        elif fid.startswith("patron__"):
            lines.extend(
                [
                    f"  - feature_id: {fid}",
                    "    group_id: g",
                    "    source: feast_slow_180d",
                    "    status: active",
                    "    enabled_for: [baseline]",
                    "    time_horizon: long_term",
                    "    max_lookback: P180D",
                ]
            )
        else:
            lines.extend(
                [
                    f"  - feature_id: {fid}",
                    "    group_id: g",
                    "    source: baseline_model",
                    "    status: active",
                    "    enabled_for: [baseline]",
                    "    time_horizon: none",
                ]
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_min_bundle(tmp_path: Path) -> Path:
    reg = tmp_path / "feature_candidate_registry.yaml"
    _write_min_registry(reg)
    snap = load_candidate_registry(reg)
    plan = build_scorer_supplier_plan(snap, ("wager", "player_id", "fe__bets_cnt__w1d", "patron__adt__w180d_m1snap"))
    assert_scorer_supplier_plan_or_raise(plan)
    model = DummyClassifier(strategy="constant", constant=1)
    model.fit([[0.0], [1.0]], [0, 1])
    payload = {
        "model": model,
        "feature_columns": list(
            plan.baseline_cols
            + plan.feast_mid_cols
            + plan.mid_composite_cols
            + plan.feast_slow_cols
        ),
        "threshold": 0.99,
        "categorical_columns": [],
        "category_categories": {},
    }
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    (bundle_dir / "model.pkl").write_bytes(pickle.dumps(payload))
    (bundle_dir / FEATURE_CANDIDATE_REGISTRY_SNAPSHOT_FILENAME).write_text(
        reg.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (bundle_dir / "model_version").write_text("test-v2", encoding="utf-8")
    return bundle_dir


def _manifest_v_test(tmp_path: Path) -> Any:
    from trainer_hightier.serving.feature_state_store import ActiveSnapshotManifest

    return ActiveSnapshotManifest(
        version="v-test",
        slow_patron_parquet=tmp_path / "unused.parquet",
        fe_derived_parquet=None,
        trial_bet_behavior_parquet=None,
        adt_allowlist_parquet=None,
        adt_allowlist_version=None,
        coverage_end_exclusive="2099-01-01T00:00:00+00:00",
        training_cutoff_iso=None,
        mid_term_snapshot_parquet=None,
        fe_short_term_parquet=None,
        raw={"mid_term_grain": "canonical_daily_asof"},
    )


def _patch_score_once_ch_io(
    monkeypatch: pytest.MonkeyPatch,
    scorer_mod: Any,
    *,
    bets: pd.DataFrame,
    pool: pd.DataFrame | None = None,
) -> None:
    """Stub ClickHouse fetches and snapshot gates for mock Feast score_once tests."""
    pool_df = bets.copy() if pool is None else pool
    monkeypatch.setattr(scorer_mod, "fetch_bets_incremental_etl_probe", lambda *a, **k: bets.iloc[0:0])
    monkeypatch.setattr(scorer_mod, "fetch_bets_incremental", lambda *a, **k: bets.copy())
    monkeypatch.setattr(scorer_mod, "fetch_bet_pool_window", lambda *a, **k: pool_df.copy())
    monkeypatch.setattr(
        scorer_mod,
        "attach_trial_bet_behavior_1h",
        lambda staged, _pool: staged.assign(
            bet__bets_cnt__w1h=1.0,
            bet__wager_sum__w1h=1.0,
            bet__back_bet_ratio__w1h=1.0,
            bet__payout_odds_avg__w1h=1.0,
        ),
    )
    monkeypatch.setattr(scorer_mod, "post_join_feature_smoke", lambda *a, **k: [])
    monkeypatch.setattr(
        scorer_mod,
        "build_scoring_snapshot_gate",
        lambda **k: MagicMock(allow_scoring=True, degraded=False, hard_failure_reason=None),
    )
    monkeypatch.setattr(scorer_mod, "validate_mid_term_artifact", lambda *a, **k: None)
    monkeypatch.setattr(scorer_mod, "read_mid_term_anchor_max", lambda *a, **k: None)
    monkeypatch.setattr(
        scorer_mod,
        "evaluate_mid_term_freshness",
        lambda **k: MagicMock(status="fresh", staleness_days=0),
    )


def _patch_serving_prediction_log_path(
    monkeypatch: pytest.MonkeyPatch,
    scorer_mod: Any,
    tmp_path: Path,
) -> Path:
    """Route prediction_log writes to a temp DB (P2 mock end-to-end)."""
    pred_db = tmp_path / "prediction_log.db"
    base = default_hightier_serving_config()
    monkeypatch.setattr(
        scorer_mod,
        "default_hightier_serving_config",
        lambda: replace(base, prediction_log_db_path=pred_db),
    )
    return pred_db


def _sample_bets_two_rows() -> tuple[pd.DataFrame, pd.Timestamp]:
    hk = ZoneInfo("Asia/Hong_Kong")
    etl_early = pd.Timestamp("2025-06-01 10:00:00", tz=hk)
    etl_late = pd.Timestamp("2025-06-01 10:05:00", tz=hk)
    bets = pd.DataFrame(
        {
            "bet_id": [1.0, 2.0],
            "is_back_bet": [1, 1],
            "bet_type": ["x", "x"],
            "type_of_bet": ["y", "y"],
            "__etl_insert_Dtm": [etl_early, etl_late],
            "payout_complete_dtm": [etl_early, etl_late],
            "gaming_day": pd.to_datetime(["2025-06-01", "2025-06-01"]),
            "session_id": ["s1", "s2"],
            "player_id": [10, 11],
            "table_id": [1, 1],
            "position_idx": [1, 2],
            "wager": [100.0, 200.0],
            "casino_win": [0.0, 0.0],
            "payout_odds": [1.0, 1.0],
            "status": [1, 1],
        }
    )
    return bets, etl_late


def test_build_scorer_supplier_plan_routes_mid_and_slow(tmp_path: Path) -> None:
    reg = tmp_path / "registry.yaml"
    _write_min_registry(reg)
    snap = load_candidate_registry(reg)
    feats = ("wager", "fe__bets_cnt__w1d", "patron__adt__w180d_m1snap")
    plan = build_scorer_supplier_plan(snap, feats)
    assert plan.feast_mid_cols == ("fe__bets_cnt__w1d",)
    assert plan.mid_composite_cols == ()
    assert plan.feast_slow_cols == ("patron__adt__w180d_m1snap",)
    assert_scorer_supplier_plan_or_raise(plan)


def test_build_scorer_supplier_plan_splits_composite_mid_term(tmp_path: Path) -> None:
    reg = tmp_path / "registry.yaml"
    lines = ["registry_version: scorer-v2-test", "features:"]
    for fid, horizon, lookback in (
        ("fe__bets_cnt__w1d", "mid_term", "P1D"),
        ("fe__wager_cv_w7d", "mid_term", "P7D"),
        ("fe__wager_sum__w15m", "short_term", "PT15M"),
    ):
        lines.extend(
            [
                f"  - feature_id: {fid}",
                "    group_id: g",
                "    source: fe_derived",
                "    status: active",
                "    enabled_for: [baseline]",
                f"    time_horizon: {horizon}",
                f"    max_lookback: {lookback}",
            ]
        )
    reg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    snap = load_candidate_registry(reg)
    plan = build_scorer_supplier_plan(
        snap,
        ("fe__bets_cnt__w1d", "fe__wager_cv_w7d", "fe__wager_sum__w15m"),
    )
    assert plan.mid_composite_cols == ("fe__wager_cv_w7d",)
    assert plan.short_term_cols == ("fe__wager_sum__w15m",)
    assert "fe__wager_cv_w7d" in MID_TERM_COMPOSITE_FEATURE_COLUMNS
    assert plan.feast_mid_cols == (
        "fe__bets_cnt__w1d",
        "fe__std_wager_w7d",
        "fe__avg_abs_wager_w7d",
    )
    counts = scorer_supplier_route_counts(plan)
    assert counts["mid_term_composite"] == 1
    assert counts["feast_online_mid"] == 3
    assert counts["short_term_pit_builder"] == 1


def test_build_scorer_supplier_plan_includes_feast_deps_for_all_composites(tmp_path: Path) -> None:
    """Production composites must pull Feast mid inputs, not only direct model mid cols."""
    reg = tmp_path / "registry.yaml"
    lines = ["registry_version: scorer-v2-test", "features:"]
    rows = [
        ("fe__bets_cnt__w1d", "mid_term", "P1D"),
        ("fe__wager_sum__w15m_over_w1d", "mid_term", "P1D"),
        ("fe__wager_cv_w7d", "mid_term", "P7D"),
        ("fe__payout_odds_z_prior_w30d", "mid_term", "P30D"),
        ("fe__interarrival__last_gap_z__w7d", "mid_term", "P7D"),
    ]
    for fid, horizon, lookback in rows:
        lines.extend(
            [
                f"  - feature_id: {fid}",
                "    group_id: g",
                "    source: fe_derived",
                "    status: active",
                "    enabled_for: [baseline]",
                f"    time_horizon: {horizon}",
                f"    max_lookback: {lookback}",
            ]
        )
    reg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    snap = load_candidate_registry(reg)
    plan = build_scorer_supplier_plan(snap, tuple(fid for fid, _, _ in rows))
    assert plan.mid_composite_cols == (
        "fe__wager_sum__w15m_over_w1d",
        "fe__wager_cv_w7d",
        "fe__payout_odds_z_prior_w30d",
        "fe__interarrival__last_gap_z__w7d",
    )
    assert set(plan.feast_mid_cols) == {
        "fe__bets_cnt__w1d",
        "fe__wager_sum__w1d",
        "fe__std_wager_w7d",
        "fe__avg_abs_wager_w7d",
        "fe__prior_odds_mean_w30d",
        "fe__prior_odds_std_w30d",
        "fe__interarrival_avg_w7d",
        "fe__interarrival_std_w7d",
    }
    assert_scorer_supplier_plan_or_raise(plan)


def test_registry_runtime_inputs_drive_feast_closure(tmp_path: Path) -> None:
    """Composite deps come from registry ``runtime_inputs``, not only legacy maps."""
    reg = tmp_path / "registry.yaml"
    reg.write_text(
        """
registry_version: t
features:
  - feature_id: fe__wager_cv_w7d
    group_id: g
    source: fe_derived
    status: active
    enabled_for: [baseline]
    time_horizon: mid_term
    max_lookback: P7D
    allowed_training_supplier: mid_term_daily_snapshot
    runtime_supplier: composite
    runtime_inputs:
      feast_online_mid:
        - fe__std_wager_w7d
        - fe__avg_abs_wager_w7d
""".strip()
        + "\n",
        encoding="utf-8",
    )
    snap = load_candidate_registry(reg)
    plan = build_scorer_supplier_plan(snap, ("fe__wager_cv_w7d",))
    assert plan.mid_composite_cols == ("fe__wager_cv_w7d",)
    assert set(plan.feast_mid_cols) == {"fe__std_wager_w7d", "fe__avg_abs_wager_w7d"}
    assert_scorer_supplier_plan_or_raise(plan)


def test_assert_feast_plan_schema_rejects_unknown_mid_column(tmp_path: Path) -> None:
    plan = ScorerSupplierPlan(
        baseline_cols=(),
        feast_trial_cols=(),
        feast_mid_cols=("fe__not_in_production_schema__w1d",),
        mid_composite_cols=(),
        feast_slow_cols=(),
        short_term_cols=(),
        unknown_cols=(),
    )
    with pytest.raises(ValueError, match=r"PRODUCTION_MID_TERM_FEATURE_COLUMNS"):
        assert_feast_plan_schema_support_or_raise(plan)


def test_enrich_row_audits_composite_upstream_nulls() -> None:
    staged = pd.DataFrame(
        {
            "fe__wager_cv_w7d": [None],
            "fe__std_wager_w7d": [None],
            "fe__avg_abs_wager_w7d": [10.0],
        }
    )
    audits = [
        RowMissingAudit(
            model_features_missing=1,
            fe_features_missing=1,
            feast_mid_missing=0,
            feast_slow_missing=0,
            short_term_missing=0,
        )
    ]
    enriched = enrich_row_audits_composite_upstream(
        staged,
        audits,
        composite_cols=("fe__wager_cv_w7d",),
        runtime_inputs_by_feature={
            "fe__wager_cv_w7d": (("feast_online_mid", ("fe__std_wager_w7d", "fe__avg_abs_wager_w7d")),),
        },
    )
    summary = enriched[0].family_summary()
    assert summary["composite_upstream_null"] == 1
    assert summary["upstream_null__fe__std_wager_w7d"] == 1


def test_assert_scorer_supplier_plan_rejects_unknown_column(tmp_path: Path) -> None:
    reg = tmp_path / "registry.yaml"
    _write_min_registry(reg)
    snap = load_candidate_registry(reg)
    plan = build_scorer_supplier_plan(snap, ("wager", "fe__not_in_registry__w1d"))
    assert plan.unknown_cols == ("fe__not_in_registry__w1d",)
    with pytest.raises(ValueError, match=r"unknown columns"):
        assert_scorer_supplier_plan_or_raise(plan)


def test_assert_scorer_supplier_plan_rejects_unmapped_mid_fe(tmp_path: Path) -> None:
    reg = tmp_path / "registry.yaml"
    reg.write_text(
        """
registry_version: t
features:
  - feature_id: fe__custom_mid_only__w1d
    group_id: g
    source: fe_derived
    status: active
    enabled_for: [baseline]
    time_horizon: mid_term
    max_lookback: P1D
""".strip()
        + "\n",
        encoding="utf-8",
    )
    snap = load_candidate_registry(reg)
    plan = build_scorer_supplier_plan(snap, ("fe__custom_mid_only__w1d",))
    assert plan.unknown_cols == ("fe__custom_mid_only__w1d",)
    with pytest.raises(ValueError, match=r"unknown columns"):
        assert_scorer_supplier_plan_or_raise(plan)


def test_attach_mid_term_composite_columns_from_feast_inputs() -> None:
    staged = pd.DataFrame(
        {
            "fe__wager_sum__w15m": [30.0],
            "fe__wager_sum__w1d": [100.0],
            "fe__avg_abs_wager_w7d": [10.0],
            "fe__std_wager_w7d": [4.0],
            "fe__prior_odds_mean_w30d": [2.0],
            "fe__prior_odds_std_w30d": [0.5],
            "payout_odds": [2.5],
            "fe__time_since_last_bet_sec": [20.0],
            "fe__interarrival_avg_w7d": [10.0],
            "fe__interarrival_std_w7d": [5.0],
        }
    )
    got = attach_mid_term_composite_columns(
        staged,
        (
            "fe__wager_sum__w15m_over_w1d",
            "fe__wager_cv_w7d",
            "fe__payout_odds_z_prior_w30d",
            "fe__interarrival__last_gap_z__w7d",
        ),
    )
    assert float(got.iloc[0]["fe__wager_sum__w15m_over_w1d"]) == pytest.approx(0.3)
    assert float(got.iloc[0]["fe__wager_cv_w7d"]) == pytest.approx(0.4)
    assert float(got.iloc[0]["fe__payout_odds_z_prior_w30d"]) == pytest.approx(1.0)
    assert float(got.iloc[0]["fe__interarrival__last_gap_z__w7d"]) == pytest.approx(2.0)


def test_entity_missing_policy_hard_fail_above_threshold() -> None:
    staged = pd.DataFrame({"canonical_id": ["a", "b", "c"], "x": [1, 2, 3]})
    lookup_df = pd.DataFrame({"canonical_id": ["a"], "fe__bets_cnt__w1d": [1.0]})
    lookup = join_feast_lookup(
        staged,
        lookup_df,
        feature_columns=("fe__bets_cnt__w1d",),
        mid_columns=("fe__bets_cnt__w1d",),
        slow_columns=(),
    )
    with pytest.raises(RuntimeError, match=r"entity row missing rate"):
        apply_entity_missing_policy(staged, lookup, fail_fraction=0.10)


def test_entity_missing_policy_skips_rows_below_threshold() -> None:
    staged = pd.DataFrame({"canonical_id": [f"c{i}" for i in range(10)]})
    lookup_df = pd.DataFrame(
        {
            "canonical_id": [f"c{i}" for i in range(9)],
            "fe__bets_cnt__w1d": [float(i) for i in range(9)],
        }
    )
    lookup = join_feast_lookup(
        staged,
        lookup_df,
        feature_columns=("fe__bets_cnt__w1d",),
        mid_columns=("fe__bets_cnt__w1d",),
        slow_columns=(),
    )
    scorable, skipped, diag = apply_entity_missing_policy(staged, lookup, fail_fraction=0.10)
    assert len(scorable) == 9
    assert len(skipped) == 1
    assert diag.n_entity_missing == 1


def test_score_once_cursor_advances_all_scored_rows_not_only_alerts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trainer_hightier.serving import scorer as scorer_mod
    from trainer_hightier.serving.model_bundle import load_hightier_model_bundle

    bets, etl_late = _sample_bets_two_rows()
    cmap = tmp_path / "map.parquet"
    pd.DataFrame({"player_id": [10, 11], "canonical_id": ["c10", "c11"]}).to_parquet(cmap, index=False)
    _patch_score_once_ch_io(monkeypatch, scorer_mod, bets=bets)
    _patch_serving_prediction_log_path(monkeypatch, scorer_mod, tmp_path)
    init_prediction_log_db(tmp_path / "prediction_log.db")

    bundle = load_hightier_model_bundle(bundle_dir=_write_min_bundle(tmp_path))
    adapter = MockFeastOnlineAdapter(
        features_by_canonical={
            "c10": {"fe__bets_cnt__w1d": 1.0, "patron__adt__w180d_m1snap": 5.0},
            "c11": {"fe__bets_cnt__w1d": 2.0, "patron__adt__w180d_m1snap": 6.0},
        }
    )
    db = tmp_path / "state.db"
    init_state_db(db)
    conn = sqlite3.connect(db)
    try:
        n = scorer_mod.score_once(
            conn,
            bundle,
            feast_adapter=adapter,
            mapping_parquet=cmap,
            manifest=_manifest_v_test(tmp_path),
            high_adt_only=False,
            allowlist_ids=frozenset(),
        )
        after = get_last_processed_etl_insert(conn)
        pred_rows = sqlite3.connect(tmp_path / "prediction_log.db").execute(
            "SELECT COUNT(*) FROM prediction_log WHERE scoring_status = 'scored'"
        ).fetchone()[0]
        alert_rows = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
    finally:
        conn.close()
    assert n == 2
    assert after is not None
    assert pd.Timestamp(after).as_unit("ns") == etl_late.as_unit("ns")
    assert pred_rows == 2
    assert alert_rows == 2


def test_score_once_mock_e2e_entity_missing_writes_skipped_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2-4: one entity-missing row below batch threshold still advances scorable cursor."""
    from trainer_hightier.serving import scorer as scorer_mod
    from trainer_hightier.serving.model_bundle import load_hightier_model_bundle

    hk = ZoneInfo("Asia/Hong_Kong")
    etl = pd.Timestamp("2025-06-01 10:00:00", tz=hk)
    n_rows = 10
    bets = pd.DataFrame(
        {
            "bet_id": [float(i + 1) for i in range(n_rows)],
            "is_back_bet": [1] * n_rows,
            "bet_type": ["x"] * n_rows,
            "type_of_bet": ["y"] * n_rows,
            "__etl_insert_Dtm": [etl] * n_rows,
            "payout_complete_dtm": [etl] * n_rows,
            "gaming_day": pd.to_datetime(["2025-06-01"] * n_rows),
            "session_id": [f"s{i}" for i in range(n_rows)],
            "player_id": list(range(10, 10 + n_rows)),
            "table_id": [1] * n_rows,
            "position_idx": list(range(1, n_rows + 1)),
            "wager": [100.0] * n_rows,
            "casino_win": [0.0] * n_rows,
            "payout_odds": [1.0] * n_rows,
            "status": [1] * n_rows,
        }
    )
    cmap = tmp_path / "map.parquet"
    pd.DataFrame(
        {
            "player_id": list(range(10, 10 + n_rows)),
            "canonical_id": [f"c{i}" for i in range(n_rows)],
        }
    ).to_parquet(cmap, index=False)
    _patch_score_once_ch_io(monkeypatch, scorer_mod, bets=bets)
    pred_db = _patch_serving_prediction_log_path(monkeypatch, scorer_mod, tmp_path)
    init_prediction_log_db(pred_db)

    bundle = load_hightier_model_bundle(bundle_dir=_write_min_bundle(tmp_path))
    adapter = MockFeastOnlineAdapter(
        features_by_canonical={f"c{i}": {"fe__bets_cnt__w1d": float(i), "patron__adt__w180d_m1snap": 1.0} for i in range(9)},
        absent_canonical=frozenset({"c9"}),
    )
    state_db = tmp_path / "state.db"
    init_state_db(state_db)
    conn = sqlite3.connect(state_db)
    try:
        n_alerts = scorer_mod.score_once(
            conn,
            bundle,
            feast_adapter=adapter,
            mapping_parquet=cmap,
            manifest=_manifest_v_test(tmp_path),
            high_adt_only=False,
            allowlist_ids=frozenset(),
        )
        pred_conn = sqlite3.connect(pred_db)
        try:
            scored_n, skipped_n = pred_conn.execute(
                "SELECT "
                "SUM(CASE WHEN scoring_status = 'scored' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN scoring_status = 'skipped_entity_missing' THEN 1 ELSE 0 END) "
                "FROM prediction_log"
            ).fetchone()
        finally:
            pred_conn.close()
    finally:
        conn.close()
    assert n_alerts == 9
    assert scored_n == 9
    assert skipped_n == 1


def test_attach_short_term_pit_features_from_bounded_pool() -> None:
    """P2-5: short-term fe__* from in-memory pool without Parquet supplier."""
    hk = ZoneInfo("Asia/Hong_Kong")
    t0 = pd.Timestamp("2025-06-01 10:00:00", tz=hk)
    pool = pd.DataFrame(
        {
            "bet_id": [1.0, 2.0],
            "player_id": [10, 10],
            "canonical_id": ["c10", "c10"],
            "session_id": [1, 1],
            "table_id": [1, 1],
            "gaming_day": pd.to_datetime(["2025-06-01", "2025-06-01"]),
            "payout_complete_dtm": [t0, t0 + pd.Timedelta(minutes=5)],
            "wager": [50.0, 100.0],
            "payout_odds": [2.0, 2.0],
            "casino_win": [0.0, 0.0],
        }
    )
    staged = pool.loc[[1]].copy()
    cols = ("fe__wager_sum__w15m", "fe__time_since_last_bet_sec")
    assert_short_term_pit_columns_supported(cols)
    got = attach_short_term_pit_features(staged, pool, columns=cols)
    assert "fe__wager_sum__w15m" in got.columns
    assert pd.notna(got.iloc[0]["fe__wager_sum__w15m"])
    assert float(got.iloc[0]["fe__time_since_last_bet_sec"]) == pytest.approx(300.0)


def test_attach_short_term_pit_features_accepts_decimal_odds() -> None:
    """Production ClickHouse decimals should not make DuckDB infer a narrow DECIMAL."""
    from decimal import Decimal

    hk = ZoneInfo("Asia/Hong_Kong")
    t0 = pd.Timestamp("2025-06-01 10:00:00", tz=hk)
    pool = pd.DataFrame(
        {
            "bet_id": [1.0, 2.0],
            "player_id": [10, 10],
            "canonical_id": ["c10", "c10"],
            "session_id": [1, 1],
            "table_id": [1, 1],
            "gaming_day": pd.to_datetime(["2025-06-01", "2025-06-01"]),
            "payout_complete_dtm": [t0, t0 + pd.Timedelta(minutes=5)],
            "wager": [Decimal("50.0000"), Decimal("100.0000")],
            "payout_odds": [Decimal("2.0000"), Decimal("100.0000")],
            "casino_win": [Decimal("0.0000"), Decimal("0.0000")],
        }
    )
    staged = pool.loc[[1]].copy()

    got = attach_short_term_pit_features(
        staged,
        pool,
        columns=("fe__odds__payout_odds_to_recent_max_ratio__w1h",),
    )

    assert float(got.iloc[0]["fe__odds__payout_odds_to_recent_max_ratio__w1h"]) == pytest.approx(50.0)


def test_assert_short_term_pit_unsupported_column_raises() -> None:
    with pytest.raises(ValueError, match=r"bounded PIT does not support"):
        assert_short_term_pit_columns_supported(("fe__totally_new_feature__w1h",))


def test_mock_feast_adapter_cell_null_allowed() -> None:
    adapter = MockFeastOnlineAdapter(
        features_by_canonical={"c1": {"fe__bets_cnt__w1d": pd.NA, "patron__adt__w180d_m1snap": 1.0}}
    )
    out = adapter.lookup_mid_slow(
        ["c1"],
        mid_columns=("fe__bets_cnt__w1d",),
        slow_columns=("patron__adt__w180d_m1snap",),
    )
    staged = pd.DataFrame({"canonical_id": ["c1"]})
    lookup = join_feast_lookup(
        staged,
        out,
        feature_columns=("fe__bets_cnt__w1d", "patron__adt__w180d_m1snap"),
        mid_columns=("fe__bets_cnt__w1d",),
        slow_columns=("patron__adt__w180d_m1snap",),
    )
    scorable, skipped, _ = apply_entity_missing_policy(staged, lookup, fail_fraction=0.10)
    assert len(scorable) == 1
    assert len(skipped) == 0


def test_join_feast_lookup_dedupes_duplicate_canonical_rows() -> None:
    """Feast SDK may return multiple rows per canonical_id; join must not explode bets."""
    n_bets = 5
    staged = pd.DataFrame(
        {
            "canonical_id": ["c1"] * n_bets,
            "bet_id": [float(i) for i in range(n_bets)],
        }
    )
    lookup_df = pd.DataFrame(
        {
            "canonical_id": ["c1", "c1", "c1"],
            "fe__bets_cnt__w1d": [1.0, 2.0, 3.0],
        }
    )
    lookup = join_feast_lookup(
        staged,
        lookup_df,
        feature_columns=("fe__bets_cnt__w1d",),
        mid_columns=("fe__bets_cnt__w1d",),
        slow_columns=(),
    )
    assert len(lookup.values) == n_bets
    assert lookup.values.index.tolist() == staged.index.tolist()
    assert float(lookup.values.iloc[-1]["fe__bets_cnt__w1d"]) == pytest.approx(3.0)


def test_entity_missing_rate_zero_passes() -> None:
    staged = pd.DataFrame({"canonical_id": ["a", "b"]})
    lookup_df = pd.DataFrame({"canonical_id": ["a", "b"], "fe__bets_cnt__w1d": [1.0, 2.0]})
    lookup = join_feast_lookup(
        staged,
        lookup_df,
        feature_columns=("fe__bets_cnt__w1d",),
        mid_columns=("fe__bets_cnt__w1d",),
        slow_columns=(),
    )
    scorable, skipped, diag = apply_entity_missing_policy(
        staged, lookup, fail_fraction=0.10, mid_columns=("fe__bets_cnt__w1d",)
    )
    assert len(scorable) == 2
    assert len(skipped) == 0
    assert diag.n_entity_missing == 0


def test_entity_missing_failure_message_names_supplier() -> None:
    staged = pd.DataFrame({"canonical_id": ["a", "b", "c"]})
    lookup_df = pd.DataFrame({"canonical_id": ["a"], "fe__bets_cnt__w1d": [1.0]})
    lookup = join_feast_lookup(
        staged,
        lookup_df,
        feature_columns=("fe__bets_cnt__w1d", "patron__adt__w180d_m1snap"),
        mid_columns=("fe__bets_cnt__w1d",),
        slow_columns=("patron__adt__w180d_m1snap",),
    )
    with pytest.raises(RuntimeError, match=r"supplier=feast_online"):
        apply_entity_missing_policy(
            staged,
            lookup,
            fail_fraction=0.10,
            mid_columns=("fe__bets_cnt__w1d",),
            slow_columns=("patron__adt__w180d_m1snap",),
        )


def test_compute_row_missing_audits_allows_structural_nulls() -> None:
    features = pd.DataFrame(
        {
            "wager": [100.0],
            "fe__prior_wager_mean_w30d": [pd.NA],
            "patron__adt__w180d_m1snap": [5.0],
        }
    )
    audits = compute_row_missing_audits(
        features,
        ("wager", "fe__prior_wager_mean_w30d", "patron__adt__w180d_m1snap"),
        feast_mid_cols=("fe__prior_wager_mean_w30d",),
        feast_slow_cols=("patron__adt__w180d_m1snap",),
        short_term_cols=(),
    )
    assert audits[0].model_features_missing == 1
    assert audits[0].feast_mid_missing == 1
    assert audits[0].feast_slow_missing == 0


def test_prediction_log_writes_missing_family_json(tmp_path: Path) -> None:
    db = tmp_path / "pred.db"
    init_prediction_log_db(db)
    staged = pd.DataFrame({"bet_id": [1.0], "player_id": [10], "canonical_id": ["c1"]})
    features = pd.DataFrame({"wager": [1.0], "fe__bets_cnt__w1d": [pd.NA]})
    audits = compute_row_missing_audits(
        features,
        ("wager", "fe__bets_cnt__w1d"),
        feast_mid_cols=("fe__bets_cnt__w1d",),
        feast_slow_cols=(),
        short_term_cols=(),
    )
    append_hightier_prediction_log(
        db,
        scored_at="2025-01-01T00:00:00+08:00",
        model_version="mv",
        staged=staged,
        prob=np.array([0.5]),
        threshold=0.9,
        features=features,
        feature_columns=("wager", "fe__bets_cnt__w1d"),
        row_audits=audits,
    )
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT scoring_status, model_features_missing, missing_family_json FROM prediction_log"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == "scored"
    assert row[1] == 1
    assert row[2] is not None and "feast_mid_missing" in row[2]


def test_skipped_entity_missing_log(tmp_path: Path) -> None:
    db = tmp_path / "pred.db"
    init_prediction_log_db(db)
    skipped = pd.DataFrame({"bet_id": [2.0], "canonical_id": ["c_missing"]})
    append_skipped_entity_missing_log(
        db,
        scored_at="2025-01-01T00:00:00+08:00",
        model_version="mv",
        skipped=skipped,
        feature_columns=("wager", "fe__bets_cnt__w1d"),
        threshold=0.5,
        feast_mid_cols=("fe__bets_cnt__w1d",),
    )
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT scoring_status, score, model_features_missing FROM prediction_log").fetchone()
    finally:
        conn.close()
    assert row == ("skipped_entity_missing", 0.0, 2)


def test_scorer_supplier_route_counts(tmp_path: Path) -> None:
    reg = tmp_path / "registry.yaml"
    reg.write_text(
        """
registry_version: t
features:
  - feature_id: wager
    group_id: g
    source: baseline_model
    status: active
    enabled_for: [baseline]
    time_horizon: none
  - feature_id: fe__bets_cnt__w1d
    group_id: g
    source: fe_derived
    status: active
    enabled_for: [baseline]
    time_horizon: mid_term
    max_lookback: P1D
""".strip()
        + "\n",
        encoding="utf-8",
    )
    snap = load_candidate_registry(reg)
    plan = build_scorer_supplier_plan(snap, ("wager", "fe__bets_cnt__w1d"))
    counts = scorer_supplier_route_counts(plan)
    assert counts["baseline_model"] == 1
    assert counts["feast_online_mid"] == 1


def test_resolve_online_feature_refs_maps_mid_and_slow() -> None:
    refs = resolve_online_feature_refs(
        ("fe__bets_cnt__w1d",),
        ("patron__adt__w180d_m1snap",),
    )
    assert "mid_term_daily_spike_features:fe__bets_cnt__w1d" in refs
    assert "long_term_slow_spike_features:patron__adt__w180d_m1snap" in refs


def test_resolve_online_feature_refs_unknown_column_raises() -> None:
    with pytest.raises(ValueError, match=r"no spike online feature ref"):
        resolve_online_feature_refs(("fe__does_not_exist__w1d",), ())


def test_feast_schema_smoke_requires_registry(tmp_path: Path) -> None:
    repo = tmp_path / "feast_repo"
    repo.mkdir()
    with pytest.raises(FileNotFoundError, match=r"registry missing"):
        run_feast_scorer_schema_smoke_check(
            repo,
            mid_columns=("fe__bets_cnt__w1d",),
            slow_columns=(),
            run_online_probe=False,
        )


def test_extract_entity_join_keys_feast_063_singular_join_key() -> None:
    from unittest.mock import MagicMock

    entity = MagicMock(spec=[])
    entity.join_key = FEAST_CANONICAL_JOIN_KEY
    assert _extract_entity_join_keys(entity) == [FEAST_CANONICAL_JOIN_KEY]


def test_extract_entity_join_keys_plural_list() -> None:
    from unittest.mock import MagicMock

    entity = MagicMock(join_keys=[FEAST_CANONICAL_JOIN_KEY])
    assert _extract_entity_join_keys(entity) == [FEAST_CANONICAL_JOIN_KEY]


def test_feast_schema_smoke_accepts_singular_join_key(tmp_path: Path) -> None:
    from feast.value_type import ValueType
    from unittest.mock import MagicMock, patch

    repo = tmp_path / "feast_repo"
    (repo / "data").mkdir(parents=True)
    (repo / "data" / "registry.db").write_bytes(b"x")
    entity = MagicMock(spec=[])
    entity.join_key = FEAST_CANONICAL_JOIN_KEY
    entity.value_type = ValueType.STRING
    mid_view = MagicMock(schema=[type("Field", (), {"name": "fe__bets_cnt__w1d"})()])

    class _FakeStore:
        def __init__(self, *, repo_path: str) -> None:
            pass

        def get_entity(self, name: str):
            if name != FEAST_CANONICAL_ENTITY_NAME:
                raise KeyError(name)
            return entity

        def get_feature_view(self, name: str):
            if name == MID_SPIKE_FEATURE_VIEW_NAME:
                return mid_view
            raise KeyError(name)

        def get_feature_service(self, name: str):
            if name == MID_SPIKE_FEATURE_SERVICE_NAME:
                return MagicMock()
            raise KeyError(name)

        def get_online_features(self, *, features, entity_rows):
            return MagicMock(to_df=lambda: pd.DataFrame({FEAST_CANONICAL_JOIN_KEY: ["probe-c1"]}))

    with patch("feast.FeatureStore", _FakeStore):
        result = run_feast_scorer_schema_smoke_check(
            repo,
            mid_columns=("fe__bets_cnt__w1d",),
            slow_columns=(),
            probe_canonical_id="probe-c1",
        )
    assert result.online_probe_ok is True


def test_feast_schema_smoke_validates_entity_and_views(tmp_path: Path) -> None:
    from feast.value_type import ValueType
    from unittest.mock import MagicMock, patch

    repo = tmp_path / "feast_repo"
    (repo / "data").mkdir(parents=True)
    (repo / "data" / "registry.db").write_bytes(b"x")

    entity = MagicMock(join_keys=[FEAST_CANONICAL_JOIN_KEY], value_type=ValueType.STRING)
    mid_view = MagicMock(schema=[type("Field", (), {"name": "fe__bets_cnt__w1d"})()])

    class _FakeStore:
        def __init__(self, *, repo_path: str) -> None:
            self.repo_path = repo_path

        def get_entity(self, name: str):
            if name != FEAST_CANONICAL_ENTITY_NAME:
                raise KeyError(name)
            return entity

        def get_feature_view(self, name: str):
            if name == MID_SPIKE_FEATURE_VIEW_NAME:
                return mid_view
            raise KeyError(name)

        def get_feature_service(self, name: str):
            if name == MID_SPIKE_FEATURE_SERVICE_NAME:
                return MagicMock()
            raise KeyError(name)

        def get_online_features(self, *, features, entity_rows):
            assert entity_rows == {FEAST_CANONICAL_JOIN_KEY: ["probe-c1"]}
            return MagicMock(to_df=lambda: pd.DataFrame({FEAST_CANONICAL_JOIN_KEY: ["probe-c1"]}))

    with patch("feast.FeatureStore", _FakeStore):
        result = run_feast_scorer_schema_smoke_check(
            repo,
            mid_columns=("fe__bets_cnt__w1d",),
            slow_columns=(),
            probe_canonical_id="probe-c1",
        )
    assert result.online_probe_ok is True
    assert result.mid_feature_service == MID_SPIKE_FEATURE_SERVICE_NAME


def test_feast_schema_smoke_entity_key_mismatch(tmp_path: Path) -> None:
    from feast.value_type import ValueType
    from unittest.mock import MagicMock, patch

    repo = tmp_path / "feast_repo"
    (repo / "data").mkdir(parents=True)
    (repo / "data" / "registry.db").write_bytes(b"x")
    bad_entity = MagicMock(spec=[])
    bad_entity.join_key = "player_id"
    bad_entity.value_type = ValueType.INT64

    class _FakeStore:
        def __init__(self, *, repo_path: str) -> None:
            pass

        def get_entity(self, _name: str):
            return bad_entity

    with patch("feast.FeatureStore", _FakeStore):
        with pytest.raises(RuntimeError, match=r"entity key mismatch"):
            run_feast_scorer_schema_smoke_check(
                repo,
                mid_columns=("fe__bets_cnt__w1d",),
                slow_columns=(),
                run_online_probe=False,
            )


def test_feast_schema_smoke_feature_name_mismatch(tmp_path: Path) -> None:
    from feast.value_type import ValueType
    from unittest.mock import MagicMock, patch

    repo = tmp_path / "feast_repo"
    (repo / "data").mkdir(parents=True)
    (repo / "data" / "registry.db").write_bytes(b"x")
    entity = MagicMock(join_keys=[FEAST_CANONICAL_JOIN_KEY], value_type=ValueType.STRING)
    mid_view = MagicMock(schema=[type("Field", (), {"name": "fe__other_col"})()])

    class _FakeStore:
        def __init__(self, *, repo_path: str) -> None:
            pass

        def get_entity(self, _name: str):
            return entity

        def get_feature_view(self, name: str):
            if name == MID_SPIKE_FEATURE_VIEW_NAME:
                return mid_view
            raise KeyError(name)

        def get_feature_service(self, name: str):
            return MagicMock()

    with patch("feast.FeatureStore", _FakeStore):
        with pytest.raises(RuntimeError, match=r"feature name mismatch"):
            run_feast_scorer_schema_smoke_check(
                repo,
                mid_columns=("fe__bets_cnt__w1d",),
                slow_columns=(),
                run_online_probe=False,
            )


def test_feast_schema_smoke_missing_feature_service(tmp_path: Path) -> None:
    from feast.value_type import ValueType
    from unittest.mock import MagicMock, patch

    repo = tmp_path / "feast_repo"
    (repo / "data").mkdir(parents=True)
    (repo / "data" / "registry.db").write_bytes(b"x")
    entity = MagicMock(join_keys=[FEAST_CANONICAL_JOIN_KEY], value_type=ValueType.STRING)
    mid_view = MagicMock(schema=[type("Field", (), {"name": "fe__bets_cnt__w1d"})()])

    class _FakeStore:
        def __init__(self, *, repo_path: str) -> None:
            pass

        def get_entity(self, _name: str):
            return entity

        def get_feature_view(self, _name: str):
            return mid_view

        def get_feature_service(self, _name: str):
            raise KeyError("missing service")

    with patch("feast.FeatureStore", _FakeStore):
        with pytest.raises(RuntimeError, match=r"feature service"):
            run_feast_scorer_schema_smoke_check(
                repo,
                mid_columns=("fe__bets_cnt__w1d",),
                slow_columns=(),
                run_online_probe=False,
            )


def test_feast_readiness_gate_fails_when_document_missing(tmp_path: Path) -> None:
    from trainer_hightier.serving.feast_readiness import evaluate_feast_readiness_gate

    path = tmp_path / "feast_online_readiness.json"
    gate = evaluate_feast_readiness_gate(
        None,
        require_mid=True,
        require_slow=True,
        readiness_path=path,
        close_hour=3,
        mid_hard_cap_days=3,
        slow_hard_cap_days=3,
        slow_grace_days=1,
    )
    assert gate.ok is False
    assert gate.hard_failure_reason is not None
    assert "feast_online_readiness.json missing" in gate.hard_failure_reason


def test_feast_readiness_roundtrip_and_fresh_gate(tmp_path: Path) -> None:
    from datetime import date

    from trainer_hightier.serving.feast_readiness import (
        FeastLayerReadiness,
        FeastOnlineReadiness,
        evaluate_feast_readiness_gate,
        layer_readiness_from_mid_spike_report,
        load_feast_online_readiness,
        write_feast_online_readiness,
    )
    from trainer_hightier.serving.snapshot_freshness import expected_mid_term_anchor, serving_gaming_day

    hk = ZoneInfo("Asia/Hong_Kong")
    anchor = expected_mid_term_anchor(serving_gaming_day(close_hour=3))
    report = {
        "snapshot_scope": "production",
        "mid_term_anchor_gaming_day_max": anchor.isoformat(),
        "feast_spike_rows": 100,
        "lookup_batch_size": 10,
        "lookup_ok_rows": 10,
        "lookup_missing_by_feature": {},
        "feature_columns": ["fe__bets_cnt__w1d"],
    }
    mid_layer = layer_readiness_from_mid_spike_report(report)
    slow_layer = FeastLayerReadiness(
        layer="slow_patron",
        source_scope="adt_allowlist",
        anchor_gaming_day_max=date(anchor.year, anchor.month, 1),
        generated_at=mid_layer.generated_at,
        row_count=50,
        distinct_canonical_count=None,
        cell_null_counts={},
        lookup_sample_size=10,
        lookup_entity_present_rate=1.0,
        feature_columns=("patron__adt__w180d_m1snap",),
        feast_feature_view="long_term_slow_spike_features",
        materialize_source="test",
    )
    doc = FeastOnlineReadiness(
        schema_version=1,
        generated_at=mid_layer.generated_at,
        feast_repo=str(tmp_path / "feast_repo"),
        mid_term=mid_layer,
        slow_patron=slow_layer,
    )
    out = tmp_path / "feast_online_readiness.json"
    write_feast_online_readiness(doc, out)
    loaded = load_feast_online_readiness(out)
    assert loaded is not None
    assert loaded.mid_term is not None
    assert loaded.mid_term.anchor_gaming_day_max == anchor
    gate = evaluate_feast_readiness_gate(
        loaded,
        require_mid=True,
        require_slow=True,
        readiness_path=out,
        close_hour=3,
        mid_hard_cap_days=3,
        slow_hard_cap_days=3,
        slow_grace_days=1,
    )
    assert gate.ok is True
    assert gate.mid_fresh is not None
    assert gate.mid_fresh.status == "fresh"


def test_feast_readiness_rejects_training_scope_mid() -> None:
    from trainer_hightier.serving.feast_readiness import (
        FeastLayerReadiness,
        FeastOnlineReadiness,
        evaluate_feast_readiness_gate,
    )

    hk = ZoneInfo("Asia/Hong_Kong")
    now = pd.Timestamp.now(tz=hk).to_pydatetime()
    mid = FeastLayerReadiness(
        layer="mid_term",
        source_scope="training_step4_only",
        anchor_gaming_day_max=pd.Timestamp("2026-05-19").date(),
        generated_at=now,
        row_count=1,
        distinct_canonical_count=1,
        cell_null_counts={},
        lookup_sample_size=None,
        lookup_entity_present_rate=None,
        feature_columns=("fe__bets_cnt__w1d",),
        feast_feature_view=None,
        materialize_source="test",
    )
    doc = FeastOnlineReadiness(
        schema_version=1,
        generated_at=now,
        feast_repo="/tmp/feast_repo",
        mid_term=mid,
        slow_patron=None,
    )
    gate = evaluate_feast_readiness_gate(
        doc,
        require_mid=True,
        require_slow=False,
        readiness_path=Path("/tmp/feast_online_readiness.json"),
        close_hour=3,
        mid_hard_cap_days=3,
        slow_hard_cap_days=3,
        slow_grace_days=1,
    )
    assert gate.ok is False
    assert "training-scoped" in (gate.hard_failure_reason or "")


def test_scorer_v2_alerts_validator_and_api_compatible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P6-4: scorer v2 alert rows satisfy validator columns and ML API protocol."""
    from trainer_hightier.serving.api_server import _alerts_to_protocol_records
    from trainer_hightier.serving.contracts import assert_alerts_dataframe_validator_ready
    from trainer_hightier.serving import scorer as scorer_mod
    from trainer_hightier.serving.model_bundle import load_hightier_model_bundle

    bets, _ = _sample_bets_two_rows()
    bets["payout_odds"] = [0.5, 0.5]
    cmap = tmp_path / "map.parquet"
    pd.DataFrame({"player_id": [10, 11], "canonical_id": ["c10", "c11"]}).to_parquet(cmap, index=False)
    _patch_score_once_ch_io(monkeypatch, scorer_mod, bets=bets)
    _patch_serving_prediction_log_path(monkeypatch, scorer_mod, tmp_path)
    init_prediction_log_db(tmp_path / "prediction_log.db")

    bundle = load_hightier_model_bundle(bundle_dir=_write_min_bundle(tmp_path))
    adapter = MockFeastOnlineAdapter(
        features_by_canonical={
            "c10": {"fe__bets_cnt__w1d": 1.0, "patron__adt__w180d_m1snap": 5.0},
            "c11": {"fe__bets_cnt__w1d": 2.0, "patron__adt__w180d_m1snap": 6.0},
        }
    )
    state_db = tmp_path / "state.db"
    init_state_db(state_db)
    conn = sqlite3.connect(state_db)
    try:
        n_alerts = scorer_mod.score_once(
            conn,
            bundle,
            feast_adapter=adapter,
            mapping_parquet=cmap,
            manifest=_manifest_v_test(tmp_path),
            high_adt_only=False,
            allowlist_ids=frozenset(),
        )
        alerts = pd.read_sql_query("SELECT * FROM alerts", conn)
    finally:
        conn.close()
    assert n_alerts == 2
    assert_alerts_dataframe_validator_ready(alerts)
    alerts["ts_dt"] = pd.to_datetime(alerts["ts"], errors="coerce")
    records = _alerts_to_protocol_records(alerts)
    assert len(records) == 2
    for rec in records:
        assert rec["bet_id"] is not None
        assert rec["player_id"] is not None
        assert "canonical_id" not in rec


def test_scorer_v2_integration_records_cycle_metrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P6-2: mock CH + mock Feast + bundle reaches predict and records cycle metrics."""
    from trainer_hightier.serving import scorer as scorer_mod
    from trainer_hightier.serving.model_bundle import load_hightier_model_bundle

    bets, _ = _sample_bets_two_rows()
    cmap = tmp_path / "map.parquet"
    pd.DataFrame({"player_id": [10, 11], "canonical_id": ["c10", "c11"]}).to_parquet(cmap, index=False)
    _patch_score_once_ch_io(monkeypatch, scorer_mod, bets=bets)
    _patch_serving_prediction_log_path(monkeypatch, scorer_mod, tmp_path)
    init_prediction_log_db(tmp_path / "prediction_log.db")
    bundle = load_hightier_model_bundle(bundle_dir=_write_min_bundle(tmp_path))
    adapter = MockFeastOnlineAdapter(
        features_by_canonical={
            "c10": {"fe__bets_cnt__w1d": 1.0, "patron__adt__w180d_m1snap": 1.0},
            "c11": {"fe__bets_cnt__w1d": 2.0, "patron__adt__w180d_m1snap": 2.0},
        }
    )
    state_db = tmp_path / "state.db"
    init_state_db(state_db)
    conn = sqlite3.connect(state_db)
    try:
        scorer_mod.score_once(
            conn,
            bundle,
            feast_adapter=adapter,
            mapping_parquet=cmap,
            manifest=_manifest_v_test(tmp_path),
            high_adt_only=False,
            allowlist_ids=frozenset(),
        )
    finally:
        conn.close()
    metrics = scorer_mod.get_last_scorer_cycle_metrics()
    assert metrics is not None
    assert metrics["n_alerts"] == 2
    cr = metrics["cycle_readiness"]
    assert cr["n_requested"] == cr["n_scored"] == 2


def test_scorer_dry_run_report_row_alignment_verdict() -> None:
    from trainer_hightier.serving.scorer_dry_run import build_dry_run_report_from_cycle

    report = build_dry_run_report_from_cycle(
        model_version="test-v2",
        cycle_readiness={
            "n_requested": 2000,
            "n_scored": 2000,
            "n_skipped_entity_missing": 0,
            "entity_missing_rate": 0.0,
            "entity_missing_fail_fraction": 0.1,
            "lookup_latency_ms": 48.0,
        },
        n_alerts=1,
    )
    acc = report.acceptance_summary()
    assert acc["verdict"] == "pass"
    assert acc["row_count_aligned"] is True

    bad = build_dry_run_report_from_cycle(
        model_version="test-v2",
        cycle_readiness={
            "n_requested": 2000,
            "n_scored": 177830,
            "entity_missing_rate": 0.0,
            "entity_missing_fail_fraction": 0.1,
            "lookup_latency_ms": 48.0,
        },
        n_alerts=1,
    )
    assert bad.acceptance_summary()["verdict"] == "fail"
