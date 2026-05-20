"""Scorer v2 Feast runtime: mock adapter, missing policy, cursor advance."""

from __future__ import annotations

import pickle
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest
from sklearn.dummy import DummyClassifier
from zoneinfo import ZoneInfo

from trainer_hightier.config import FEATURE_CANDIDATE_REGISTRY_SNAPSHOT_FILENAME
from trainer_hightier.feature_experiment.candidate_registry_loader import load_candidate_registry
from trainer_hightier.serving.feast_online_adapter import (
    FEAST_CANONICAL_ENTITY_NAME,
    FEAST_CANONICAL_JOIN_KEY,
    MockFeastOnlineAdapter,
    _extract_entity_join_keys,
    apply_entity_missing_policy,
    compute_row_missing_audits,
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
from trainer_hightier.serving.feature_supply import (
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
        "feature_columns": list(plan.baseline_cols + plan.feast_mid_cols + plan.feast_slow_cols),
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


def test_build_scorer_supplier_plan_routes_mid_and_slow(tmp_path: Path) -> None:
    reg = tmp_path / "registry.yaml"
    _write_min_registry(reg)
    snap = load_candidate_registry(reg)
    feats = ("wager", "fe__bets_cnt__w1d", "patron__adt__w180d_m1snap")
    plan = build_scorer_supplier_plan(snap, feats)
    assert plan.feast_mid_cols == ("fe__bets_cnt__w1d",)
    assert plan.feast_slow_cols == ("patron__adt__w180d_m1snap",)
    assert_scorer_supplier_plan_or_raise(plan)


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
    from trainer_hightier.serving.feature_state_store import ActiveSnapshotManifest

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
    pool = bets.copy()
    cmap = tmp_path / "map.parquet"
    pd.DataFrame({"player_id": [10, 11], "canonical_id": ["c10", "c11"]}).to_parquet(cmap, index=False)

    monkeypatch.setattr(scorer_mod, "fetch_bets_incremental_etl_probe", lambda *a, **k: bets.iloc[0:0])
    monkeypatch.setattr(scorer_mod, "fetch_bets_incremental", lambda *a, **k: bets.copy())
    monkeypatch.setattr(scorer_mod, "fetch_bet_pool_window", lambda *a, **k: pool.copy())
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
    monkeypatch.setattr(scorer_mod, "append_hightier_prediction_log", lambda *a, **k: None)

    bundle_dir = _write_min_bundle(tmp_path)
    from trainer_hightier.serving.model_bundle import load_hightier_model_bundle

    bundle = load_hightier_model_bundle(bundle_dir=bundle_dir)
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
            manifest=ActiveSnapshotManifest(
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
            ),
            high_adt_only=False,
            allowlist_ids=frozenset(),
        )
        after = get_last_processed_etl_insert(conn)
    finally:
        conn.close()
    assert n == 2
    assert after is not None
    assert pd.Timestamp(after).as_unit("ns") == etl_late.as_unit("ns")


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
