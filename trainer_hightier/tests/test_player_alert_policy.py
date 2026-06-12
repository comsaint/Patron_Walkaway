"""Unit tests for offline player-level cooldown simulation."""

from __future__ import annotations

import logging
import sqlite3

import pandas as pd
import pytest

from trainer_hightier.config import (
    ALERT_HORIZON_MIN,
    HightierServingConfig,
    PLAYER_ALERT_COOLDOWN_MIN,
    PlayerAlertPolicyConfig,
    Step5TrainConfig,
)
from trainer_hightier.evaluation.player_alert_policy import (
    apply_serving_player_alert_suppression,
    build_player_alert_policy_metadata,
    compare_player_alert_policies,
    operational_simulated_metrics_block,
    simulate_player_cooldown_alerts,
    warn_player_alert_policy_mismatch,
)
from trainer_hightier.serving.state_db import init_state_db


def _candidates_frame(rows: list[dict[str, object]]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def test_cooldown_boundary_suppresses_under_15_min_allows_at_15() -> None:
    """10:08 is suppressed; 10:15 is allowed under a 15-minute cooldown."""

    base = pd.Timestamp("2026-06-01 10:00:00")
    candidates = _candidates_frame(
        [
            {
                "player_id": 1,
                "game_id": 100,
                "player_game_score": 0.9,
                "player_game_label": 1,
                "alert_ts": base,
                "bet_id": 1,
            },
            {
                "player_id": 1,
                "game_id": 101,
                "player_game_score": 0.85,
                "player_game_label": 1,
                "alert_ts": base + pd.Timedelta(minutes=8),
                "bet_id": 2,
            },
            {
                "player_id": 1,
                "game_id": 102,
                "player_game_score": 0.8,
                "player_game_label": 0,
                "alert_ts": base + pd.Timedelta(minutes=15),
                "bet_id": 3,
            },
        ],
    )
    out = simulate_player_cooldown_alerts(candidates, threshold=0.5, cooldown_min=15)
    by_game = out.set_index("game_id")
    assert bool(by_game.loc[100, "is_raised"])
    assert bool(by_game.loc[101, "is_suppressed"])
    assert bool(by_game.loc[102, "is_raised"])


def test_different_players_do_not_suppress_each_other() -> None:
    """Cooldown is scoped per player_id only."""

    ts = pd.Timestamp("2026-06-01 10:00:00")
    candidates = _candidates_frame(
        [
            {
                "player_id": 1,
                "game_id": 100,
                "player_game_score": 0.9,
                "player_game_label": 1,
                "alert_ts": ts,
                "bet_id": 1,
            },
            {
                "player_id": 2,
                "game_id": 100,
                "player_game_score": 0.9,
                "player_game_label": 1,
                "alert_ts": ts + pd.Timedelta(minutes=1),
                "bet_id": 2,
            },
        ],
    )
    out = simulate_player_cooldown_alerts(candidates, threshold=0.5, cooldown_min=15)
    assert int(out["is_raised"].sum()) == 2
    assert int(out["is_suppressed"].sum()) == 0


def test_same_timestamp_prefers_higher_score_then_lower_bet_id() -> None:
    """Tie-break keeps the highest-score candidate when alert_ts matches."""

    ts = pd.Timestamp("2026-06-01 10:00:00")
    candidates = _candidates_frame(
        [
            {
                "player_id": 1,
                "game_id": 100,
                "player_game_score": 0.7,
                "player_game_label": 0,
                "alert_ts": ts,
                "bet_id": 10,
            },
            {
                "player_id": 1,
                "game_id": 101,
                "player_game_score": 0.9,
                "player_game_label": 1,
                "alert_ts": ts,
                "bet_id": 20,
            },
        ],
    )
    out = simulate_player_cooldown_alerts(candidates, threshold=0.5, cooldown_min=15)
    by_game = out.set_index("game_id")
    assert bool(by_game.loc[101, "is_raised"])
    assert bool(by_game.loc[100, "is_suppressed"])


def test_no_candidate_above_threshold() -> None:
    """Below-threshold rows are not candidates and never raised."""

    candidates = _candidates_frame(
        [
            {
                "player_id": 1,
                "game_id": 100,
                "player_game_score": 0.2,
                "player_game_label": 1,
                "alert_ts": pd.Timestamp("2026-06-01 10:00:00"),
                "bet_id": 1,
            },
        ],
    )
    out = simulate_player_cooldown_alerts(candidates, threshold=0.5, cooldown_min=15)
    assert not bool(out.iloc[0]["is_candidate"])
    assert not bool(out.iloc[0]["is_raised"])
    assert not bool(out.iloc[0]["is_suppressed"])


def test_conservative_recall_counts_suppressed_positive_as_missed() -> None:
    """Suppressed positives remain in the recall denominator."""

    base = pd.Timestamp("2026-06-01 10:00:00")
    candidates = _candidates_frame(
        [
            {
                "player_id": 1,
                "game_id": 100,
                "player_game_score": 0.9,
                "player_game_label": 1,
                "alert_ts": base,
                "bet_id": 1,
            },
            {
                "player_id": 1,
                "game_id": 101,
                "player_game_score": 0.85,
                "player_game_label": 1,
                "alert_ts": base + pd.Timedelta(minutes=8),
                "bet_id": 2,
            },
            {
                "player_id": 1,
                "game_id": 102,
                "player_game_score": 0.8,
                "player_game_label": 0,
                "alert_ts": base + pd.Timedelta(minutes=20),
                "bet_id": 3,
            },
        ],
    )
    block = operational_simulated_metrics_block("val", candidates, threshold=0.5, cooldown_min=15)
    assert block["val_operational_simulated_true_positives"] == 1
    assert block["val_operational_simulated_positives"] == 2
    assert block["val_operational_simulated_recall"] == pytest.approx(0.5)
    assert block["val_operational_simulated_precision"] == pytest.approx(0.5)
    assert block["val_operational_simulated_candidate_alerts"] == 3
    assert block["val_operational_simulated_alerts"] == 2
    assert block["val_operational_simulated_suppressed_alerts"] == 1


def test_player_alert_policy_config_defaults_enable_suppression() -> None:
    """Shared train policy defaults to suppression on with 60-minute offline cooldown."""

    policy = PlayerAlertPolicyConfig()
    assert policy.suppression_enabled is True
    assert policy.cooldown_min == PLAYER_ALERT_COOLDOWN_MIN
    assert policy.threshold_selection_enabled is False
    assert policy.sample_weight_enabled is False
    assert Step5TrainConfig().player_alert_policy.cooldown_min == PLAYER_ALERT_COOLDOWN_MIN
    assert HightierServingConfig().player_alert_policy.cooldown_min == ALERT_HORIZON_MIN
    assert HightierServingConfig().player_alert_policy.suppression_enabled is True


def test_build_player_alert_policy_metadata_keys() -> None:
    """Training artifacts record the agreed policy metadata contract."""

    meta = build_player_alert_policy_metadata(PlayerAlertPolicyConfig())
    assert meta["player_alert_policy_suppression_enabled"] is True
    assert meta["player_alert_policy_cooldown_min"] == PLAYER_ALERT_COOLDOWN_MIN
    assert meta["player_alert_policy_train_alert_ts_source"] == "payout_complete_dtm"
    assert meta["player_alert_policy_operational_metrics_reported"] is True


def test_serving_suppression_disabled_writes_all_candidates() -> None:
    """Disabled serving policy raises every player-game candidate."""

    ts = "2026-06-01T10:00:00+08:00"
    alerts = pd.DataFrame(
        [
            {"bet_id": "1", "player_id": 10, "ts": ts, "score": 0.9},
            {"bet_id": "2", "player_id": 10, "ts": ts, "score": 0.8},
        ],
    )
    raised, suppressed, decisions = apply_serving_player_alert_suppression(
        alerts,
        conn=None,
        suppression_enabled=False,
        cooldown_min=15,
    )
    assert len(raised) == 2
    assert suppressed.empty
    assert decisions["1"].raised is True
    assert decisions["2"].raised is True


def test_serving_suppression_boundary_and_db_state(tmp_path) -> None:
    """Production suppression uses alerts table state and <15m / >=15m boundary."""

    db = tmp_path / "state.db"
    init_state_db(db)
    base_ts = "2026-06-01T10:00:00+08:00"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO alerts(bet_id, ts, player_id, score) VALUES (?, ?, ?, ?)",
            ("0", base_ts, 10, 0.99),
        )
        conn.commit()
        alerts = pd.DataFrame(
            [
                {
                    "bet_id": "1",
                    "player_id": 10,
                    "ts": "2026-06-01T10:08:00+08:00",
                    "score": 0.9,
                },
                {
                    "bet_id": "2",
                    "player_id": 10,
                    "ts": "2026-06-01T10:15:00+08:00",
                    "score": 0.85,
                },
            ],
        )
        raised, suppressed, decisions = apply_serving_player_alert_suppression(
            alerts,
            conn=conn,
            suppression_enabled=True,
            cooldown_min=15,
        )
    assert len(raised) == 1
    assert str(raised.iloc[0]["bet_id"]) == "2"
    assert len(suppressed) == 1
    assert str(suppressed.iloc[0]["bet_id"]) == "1"
    assert decisions["1"].suppressed is True
    assert decisions["1"].suppression_reason == "player_cooldown_15m"
    assert decisions["2"].raised is True


def test_compare_player_alert_policies_detects_mismatch() -> None:
    """Artifact vs serving mismatch lines are returned without hard failure."""

    artifact = build_player_alert_policy_metadata(PlayerAlertPolicyConfig())
    serving = PlayerAlertPolicyConfig(suppression_enabled=False)
    mismatches = compare_player_alert_policies(artifact, serving)
    assert any("suppression_enabled" in line for line in mismatches)


def test_warn_player_alert_policy_mismatch_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    """Train/serve mismatch emits structured warning but does not raise."""

    artifact = build_player_alert_policy_metadata(PlayerAlertPolicyConfig())
    serving = PlayerAlertPolicyConfig(suppression_enabled=False)
    caplog.set_level(logging.WARNING)
    warn_player_alert_policy_mismatch(
        logging.getLogger("test_player_alert_policy"),
        training_metrics=artifact,
        serving_policy=serving,
    )
    assert any("player_alert_policy_mismatch" in rec.message for rec in caplog.records)
