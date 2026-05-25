"""Offline serving backtest: mock ClickHouse + Feast, production feature path."""

from __future__ import annotations

import json
import shutil
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

from trainer_hightier.config import default_hightier_serving_config
from trainer_hightier.serving.feast_online_adapter import MockFeastOnlineAdapter
from trainer_hightier.serving.feast_readiness import FeastReadinessGateResult
from trainer_hightier.serving.offline_serving_backtest import (
    build_offline_scoring_batch,
    resolve_offline_context,
    run_offline_production_pipeline,
    run_offline_serving_backtest,
    summarize_offline_result,
)
from trainer_hightier.tests.test_scorer_v2_feast import _write_min_bundle


def _fake_bets() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "bet_id": [101.0, 102.0],
            "player_id": [1, 1],
            "gaming_day": pd.to_datetime(["2026-05-18", "2026-05-18"]),
            "payout_complete_dtm": pd.to_datetime(
                ["2026-05-18T10:00:00+00:00", "2026-05-18T11:00:00+00:00"],
                utc=True,
            ),
            "__etl_insert_Dtm": pd.to_datetime(
                ["2026-05-18T10:00:00+00:00", "2026-05-18T11:00:00+00:00"],
                utc=True,
            ),
            "wager": [100.0, 50.0],
            "casino_win": [0.0, 0.0],
            "payout_odds": [2.0, 1.5],
            "session_id": [1, 1],
            "table_id": [1, 1],
            "is_back_bet": [0, 0],
            "bet_type": ["", ""],
            "type_of_bet": ["", ""],
            "position_idx": [None, None],
            "casino_player_id": [None, None],
            "status": [None, None],
        }
    )


def _deploy_layout(tmp_path: Path) -> Path:
    bundle_dir = _write_min_bundle(tmp_path)
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    feast_repo = deploy / "feast_repo"
    feast_repo.mkdir(parents=True)
    (feast_repo / "data").mkdir(parents=True, exist_ok=True)
    (feast_repo / "feature_store.yaml").write_text(
        "project: test\nprovider: local\nregistry: data/registry.db\n"
        "online_store:\n  type: sqlite\n  path: data/online_store.db\n",
        encoding="utf-8",
    )
    (feast_repo / "data" / "online_store.db").write_bytes(b"")
    map_dest = deploy / "mapping" / "canonical_player_mapping.parquet"
    allow_dest = deploy / "mapping" / "adt_allowed.parquet"
    map_dest.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"player_id": [1], "canonical_id": ["c1"]}).to_parquet(map_dest, index=False)
    pd.DataFrame({"player_id": [1]}).to_parquet(allow_dest, index=False)
    rel = {
        "model_bundle_dir": bundle_dir.name,
        "canonical_mapping_parquet": "mapping/canonical_player_mapping.parquet",
        "adt_allowlist_parquet": "mapping/adt_allowed.parquet",
        "feast_repo_dir": "feast_repo",
        "local_state_dir": "local_state",
        "feast_artifacts_dir": "artifacts/feast",
        "snapshot_manifest_dir": "snapshots",
    }
    (deploy / "deploy_bundle_paths.json").write_text(json.dumps(rel), encoding="utf-8")
    shutil.copytree(bundle_dir, deploy / bundle_dir.name)
    return deploy


def test_offline_pipeline_mock_feast(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Replay PIT + mock Feast + predict without ClickHouse."""
    import trainer_hightier.serving.scorer as scorer_mod

    deploy = _deploy_layout(tmp_path)
    bets = _fake_bets()
    pool = bets.copy()
    monkeypatch.setattr(scorer_mod, "fetch_bet_pool_window", lambda *a, **k: pool.copy())
    monkeypatch.setattr(
        "trainer_hightier.serving.audit_supplier_root_cause.fetch_bet_pool_window",
        lambda *a, **k: pool.copy(),
    )
    monkeypatch.setattr(
        scorer_mod,
        "attach_trial_bet_behavior_1h",
        lambda staged, _p: staged.assign(
            bet__bets_cnt__w1h=1.0,
            bet__wager_sum__w1h=1.0,
            bet__back_bet_ratio__w1h=0.0,
            bet__payout_odds_avg__w1h=1.5,
        ),
    )
    monkeypatch.setattr(
        "trainer_hightier.serving.offline_serving_backtest.run_deploy_feast_readiness_check",
        lambda **k: FeastReadinessGateResult(
            ok=True,
            mid_fresh=None,
            slow_fresh=None,
            hard_failure_reason=None,
            readiness_path=tmp_path / "r.json",
            deploy_lookup_smoke={"ok": True},
        ),
    )

    ctx = resolve_offline_context(
        bundle_dir=deploy,
        model_dir=None,
        mapping_parquet=None,
        allowlist_parquet=None,
        feast_repo=deploy / "feast_repo",
        slow_patron_parquet=None,
        use_feast_online=True,
    )
    batch = build_offline_scoring_batch(bets, cfg=ctx.cfg)
    adapter = MockFeastOnlineAdapter(
        features_by_canonical={
            "c1": {"fe__bets_cnt__w1d": 3.0, "patron__adt__w180d_m1snap": 100.0},
        },
    )
    result = run_offline_production_pipeline(batch, ctx, adapter, strict_smoke=False)
    summary = summarize_offline_result(result, ctx)
    assert summary["n_bets"] == 2
    assert summary["n_scored"] == 2
    assert "fe__bets_cnt__w1d" in summary["feature_null_fraction"]


def test_run_offline_serving_backtest_monkeypatched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI helper loads bets via patched gaming-day fetch."""
    deploy = _deploy_layout(tmp_path)
    monkeypatch.setattr(
        "trainer_hightier.serving.offline_serving_backtest.fetch_bets_gaming_day_window",
        lambda **k: _fake_bets(),
    )
    monkeypatch.setattr(
        "trainer_hightier.serving.offline_serving_backtest._build_feast_online_adapter",
        lambda ctx: MockFeastOnlineAdapter(
            features_by_canonical={
                "c1": {"fe__bets_cnt__w1d": 1.0, "patron__adt__w180d_m1snap": 1.0},
            },
        ),
    )
    monkeypatch.setattr(
        "trainer_hightier.serving.offline_serving_backtest.run_deploy_feast_readiness_check",
        lambda **k: FeastReadinessGateResult(
            ok=True,
            mid_fresh=None,
            slow_fresh=None,
            hard_failure_reason=None,
            readiness_path=tmp_path / "r.json",
            deploy_lookup_smoke=None,
        ),
    )
    import trainer_hightier.serving.scorer as scorer_mod

    pool = _fake_bets().copy()
    monkeypatch.setattr(scorer_mod, "fetch_bet_pool_window", lambda *a, **k: pool.copy())
    monkeypatch.setattr(
        "trainer_hightier.serving.audit_supplier_root_cause.fetch_bet_pool_window",
        lambda *a, **k: pool.copy(),
    )
    monkeypatch.setattr(
        scorer_mod,
        "attach_trial_bet_behavior_1h",
        lambda staged, _p: staged.assign(
            bet__bets_cnt__w1h=1.0,
            bet__wager_sum__w1h=1.0,
            bet__back_bet_ratio__w1h=0.0,
            bet__payout_odds_avg__w1h=1.5,
        ),
    )

    report = run_offline_serving_backtest(
        bundle_dir=deploy,
        gaming_day_start=date(2026, 5, 18),
        gaming_day_end=date(2026, 5, 18),
        max_bets=10,
    )
    assert report["n_scored"] >= 1
    assert default_hightier_serving_config().adt_allowed_players_parquet is not None


def test_resolve_hot_pool_player_ids_expands_canonical_aliases(tmp_path: Path) -> None:
    """Batch player B must pull alias player A into the hot pool (training pid CTE)."""
    from trainer_hightier.serving.offline_serving_backtest import resolve_hot_pool_player_ids

    mapping = tmp_path / "canonical_player_mapping.parquet"
    pd.DataFrame(
        [
            {"player_id": 1, "canonical_id": "patron_x"},
            {"player_id": 2, "canonical_id": "patron_x"},
        ],
    ).to_parquet(mapping, index=False)
    bets = pd.DataFrame({"player_id": [2]})
    assert resolve_hot_pool_player_ids(bets, mapping) == [1, 2]
