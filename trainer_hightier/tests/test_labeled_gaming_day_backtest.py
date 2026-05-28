"""Labeled gaming-day backtest defaults (CH + labels + refresh@eval_end + test_ap)."""

from __future__ import annotations

import json
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from trainer_hightier.serving.feast_online_adapter import MockFeastOnlineAdapter
from trainer_hightier.serving.feast_online_refresh import _mid_export_bounds, _slow_export_bounds
from trainer_hightier.serving.offline_serving_backtest import (
    _label_payout_bounds,
    fetch_bets_gaming_day_window,
    run_labeled_gaming_day_backtest,
    run_offline_serving_backtest,
)
from trainer_hightier.serving.snapshot_freshness import serving_day_for_eval_gaming_day_end
from trainer_hightier.tests.test_offline_serving_backtest import _deploy_layout, _fake_bets


def test_serving_day_for_eval_end_matches_mid_anchor_semantics() -> None:
    """Anchor for bets on gaming day E is E-1 when serving_day=E."""
    eval_end = date(2026, 5, 18)
    serving = serving_day_for_eval_gaming_day_end(eval_end)
    anchor_start, anchor_end, _, _ = _mid_export_bounds(
        close_hour=3,
        serving_day=serving,
    )
    assert anchor_end == eval_end - timedelta(days=1)
    assert anchor_start == anchor_end


def test_mid_slow_bounds_use_explicit_serving_day() -> None:
    """Refresh@eval_end injects serving_day into export bounds."""
    serving = date(2026, 5, 20)
    _, anchor_end, _, bets_end = _mid_export_bounds(close_hour=3, serving_day=serving)
    g_start, g_end = _slow_export_bounds(close_hour=3, lookback_days=180, serving_day=serving)
    assert anchor_end == date(2026, 5, 19)
    assert bets_end == anchor_end
    assert g_end == date(2026, 5, 19)


def test_label_payout_bounds_extends_lookahead() -> None:
    """extended_end includes LABEL_LOOKAHEAD + WALKAWAY_GAP beyond window_end."""
    bets = _fake_bets()
    window_end, extended_end = _label_payout_bounds(bets)
    assert extended_end > window_end


def test_payout_bound_hk_naive_mixed_tz_compare() -> None:
    """Aware start and naive end must compare without TypeError."""
    from zoneinfo import ZoneInfo

    from trainer_hightier.serving.offline_serving_backtest import _payout_bound_hk_naive

    start = datetime(2026, 5, 1, 10, 0, tzinfo=ZoneInfo("Asia/Hong_Kong"))
    end = datetime(2026, 5, 12, 5, 0)
    assert _payout_bound_hk_naive(end) >= _payout_bound_hk_naive(start)


def test_normalize_payout_hk_naive_matches_compute_labels() -> None:
    """Tz-aware CH payouts must not crash ``compute_labels`` boundary compare."""
    from trainer_hightier.serving.offline_serving_backtest import _normalize_payout_hk_naive
    from trainer_hightier.walkaway_compute_labels import compute_labels

    bets = _fake_bets()
    window_end, extended_end = _label_payout_bounds(bets)
    corpus = bets.assign(canonical_id="c1")
    labeled = compute_labels(
        _normalize_payout_hk_naive(corpus),
        window_end=window_end,
        extended_end=extended_end,
    )
    assert "label" in labeled.columns


def test_fetch_bets_gaming_day_no_max_bets_omits_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When max_bets is None, CH query must not include LIMIT."""
    captured: list[str] = []

    class _Client:
        def query_df(self, q: str, parameters: dict | None = None) -> pd.DataFrame:
            captured.append(q)
            return pd.DataFrame()

    monkeypatch.setattr(
        "trainer_hightier.serving.offline_serving_backtest.get_clickhouse_client",
        lambda: _Client(),
    )
    fetch_bets_gaming_day_window(
        cfg=MagicMock(
            source_db="db",
            tbet="t_bet",
            placeholder_player_id=0,
            hightier_scorer_player_id_chunk_size=1000,
        ),
        allowlist_ids=frozenset({1}),
        gaming_day_start=date(2026, 5, 1),
        gaming_day_end=date(2026, 5, 7),
        max_bets=None,
    )
    assert captured
    assert "LIMIT" not in captured[0].upper()


def test_run_labeled_gaming_day_backtest_monkeypatched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default labeled path produces test_ap with mocked CH/Feast/refresh."""
    deploy = _deploy_layout(tmp_path)
    bets = _fake_bets()
    bets["walkaway_label"] = np.int8(0)

    monkeypatch.setattr(
        "trainer_hightier.serving.offline_serving_backtest.run_feast_refresh_at_eval_end",
        lambda ctx, eval_gaming_day_end: {
            "skipped": False,
            "serving_day": eval_gaming_day_end.isoformat(),
            "feast_refresh_anchor": (eval_gaming_day_end - timedelta(days=1)).isoformat(),
        },
    )
    monkeypatch.setattr(
        "trainer_hightier.serving.offline_serving_backtest.fetch_bets_gaming_day_window",
        lambda **k: bets.drop(columns=["walkaway_label"], errors="ignore"),
    )
    monkeypatch.setattr(
        "trainer_hightier.serving.offline_serving_backtest._attach_walkaway_labels_to_eval_bets",
        lambda eval_bets, **k: eval_bets.assign(walkaway_label=np.int8([0, 1])),
    )
    monkeypatch.setattr(
        "trainer_hightier.serving.offline_serving_backtest._build_feast_online_adapter",
        lambda ctx: MockFeastOnlineAdapter(
            features_by_canonical={
                "c1": {"fe__bets_cnt__w1d": 1.0, "patron__adt__w180d_m1snap": 1.0},
            },
        ),
    )
    import trainer_hightier.serving.scorer as scorer_mod

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
        lambda **k: __import__(
            "trainer_hightier.serving.feast_readiness",
            fromlist=["FeastReadinessGateResult"],
        ).FeastReadinessGateResult(
            ok=True,
            mid_fresh=None,
            slow_fresh=None,
            hard_failure_reason=None,
            readiness_path=tmp_path / "r.json",
            deploy_lookup_smoke={"ok": True},
        ),
    )

    report = run_labeled_gaming_day_backtest(
        bundle_dir=deploy,
        gaming_day_start=date(2026, 5, 18),
        gaming_day_end=date(2026, 5, 18),
        skip_refresh=False,
    )
    assert report["mode"] == "labeled_gaming_day_backtest"
    assert report["feature_supplier"] == "feast_online"
    assert "test_ap" in report["metrics"]
    assert report["metrics"]["test_samples"] == 2


def test_quick_replay_still_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy run_offline_serving_backtest remains for --quick-replay."""
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
        lambda **k: __import__(
            "trainer_hightier.serving.feast_readiness",
            fromlist=["FeastReadinessGateResult"],
        ).FeastReadinessGateResult(
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
    assert "test_ap" not in report
    assert report["n_scored"] >= 1
