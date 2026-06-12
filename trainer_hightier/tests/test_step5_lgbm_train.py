"""Unit tests for Step 5 LightGBM training helpers."""

from __future__ import annotations

import importlib

import numpy as np
import pandas as pd
import pytest

_b5 = importlib.import_module("trainer_hightier.05_lgbm_train")
pick_threshold_precision_floor = _b5.pick_threshold_precision_floor
aggregate_bets_to_player_game = _b5.aggregate_bets_to_player_game


def test_pick_threshold_feasible_prefers_max_recall() -> None:
    """Among precision >= floor, choose operating point with highest recall."""

    y = np.array([1, 0, 1, 1, 0], dtype=np.int8)
    scores = np.array([0.9, 0.8, 0.7, 0.6, 0.5], dtype=np.float64)
    rep = pick_threshold_precision_floor(y, scores, min_precision=0.5)
    assert rep.feasible
    assert rep.recall == pytest.approx(1.0)
    assert rep.alert_count == 4


def test_pick_threshold_infeasible_best_precision() -> None:
    """When floor unreachable, maximize precision then recall."""

    y = np.array([0, 1, 0, 0], dtype=np.int8)
    scores = np.array([0.99, 0.5, 0.4, 0.3], dtype=np.float64)
    rep = pick_threshold_precision_floor(y, scores, min_precision=0.95)
    assert not rep.feasible
    assert rep.precision == pytest.approx(0.5)
    assert rep.recall == pytest.approx(1.0)
    assert rep.alert_count == 2


def test_split_metrics_block_true_labels_per_hour() -> None:
    """``true_labels_per_hour`` = positive count / window_hours (baseline label density)."""

    y = np.array([1, 0, 1, 0, 0], dtype=np.int8)
    scores = np.array([0.9, 0.1, 0.8, 0.2, 0.3], dtype=np.float64)
    block = _b5._split_metrics_block("val", y, scores, threshold=0.5, window_hours=10.0)
    assert block["val_positives"] == 2
    assert block["val_true_labels_per_hour"] == pytest.approx(0.2)
    assert block["val_alerts_per_hour"] == pytest.approx(0.2)


def test_split_metrics_block_omits_per_hour_when_window_invalid() -> None:
    """Invalid ``window_hours`` leaves density fields as ``None``."""

    y = np.array([1, 0], dtype=np.int8)
    scores = np.array([0.9, 0.1], dtype=np.float64)
    block = _b5._split_metrics_block("train", y, scores, threshold=0.5, window_hours=None)
    assert block["train_alerts_per_hour"] is None
    assert block["train_true_labels_per_hour"] is None


def test_pick_threshold_all_negative_early_exit() -> None:
    """No positives: degenerate result without scanning prefixes."""

    y = np.zeros(5, dtype=np.int8)
    scores = np.linspace(0.2, 1.0, 5).astype(np.float64)
    rep = pick_threshold_precision_floor(y, scores, min_precision=0.8)
    assert not rep.feasible
    assert rep.alert_count == 0


def test_aggregate_bets_to_player_game_max_score_and_label() -> None:
    """Same player-game aggregates with max score and any-positive label."""

    df = pd.DataFrame(
        {
            "player_id": [1, 1, 1, 2],
            "game_id": [100.0, 100.0, 200.0, 100.0],
            "walkaway_label": [0, 1, 0, 0],
            "payout_complete_dtm": pd.to_datetime(
                [
                    "2026-06-01 10:00:00",
                    "2026-06-01 10:01:00",
                    "2026-06-01 10:02:00",
                    "2026-06-01 11:00:00",
                ],
            ),
            "bet_id": [1.0, 2.0, 3.0, 4.0],
        }
    )
    scores = np.array([0.3, 0.9, 0.4, 0.2], dtype=np.float64)
    out = aggregate_bets_to_player_game(df, scores, split_name="val")
    assert out.player_game_count == 3
    assert out.bet_count == 4
    assert out.excluded_bets == 0
    assert set(out.y_true.tolist()) <= {0, 1}
    assert float(out.scores.max()) == pytest.approx(0.9)
    pos_games = int(np.sum(out.y_true == 1))
    assert pos_games == 1
    assert list(out.candidates.columns) == [
        "player_id",
        "game_id",
        "player_game_score",
        "player_game_label",
        "bet_count",
        "alert_ts",
        "bet_id",
    ]
    game100 = out.candidates.loc[out.candidates["game_id"] == 100.0].iloc[0]
    assert float(game100["player_game_score"]) == pytest.approx(0.9)
    assert int(game100["player_game_label"]) == 1
    assert float(game100["bet_id"]) == pytest.approx(2.0)


def test_aggregate_bets_to_player_game_preserves_large_integer_game_ids() -> None:
    """Adjacent game_ids beyond float53 precision must not collapse into one group."""

    gid_a = 9_007_199_254_740_992
    gid_b = 9_007_199_254_740_993
    assert float(gid_a) == float(gid_b)

    df = pd.DataFrame(
        {
            "player_id": [1, 1],
            "game_id": [gid_a, gid_b],
            "walkaway_label": [0, 1],
            "payout_complete_dtm": pd.to_datetime(
                ["2026-06-01 10:00:00", "2026-06-01 10:01:00"],
            ),
            "bet_id": [1, 2],
        },
    )
    scores = np.array([0.4, 0.9], dtype=np.float64)
    out = aggregate_bets_to_player_game(df, scores, split_name="val")
    assert out.player_game_count == 2
    assert set(out.candidates["game_id"].astype("int64").tolist()) == {gid_a, gid_b}


def test_aggregate_bets_to_player_game_excludes_invalid_keys() -> None:
    """Null player_id/game_id rows are excluded from player-game metrics."""

    df = pd.DataFrame(
        {
            "player_id": [1, None],
            "game_id": [100.0, 100.0],
            "walkaway_label": [0, 1],
            "payout_complete_dtm": pd.to_datetime(["2026-06-01 10:00:00", "2026-06-01 10:01:00"]),
            "bet_id": [1.0, 2.0],
        }
    )
    scores = np.array([0.5, 0.8], dtype=np.float64)
    out = aggregate_bets_to_player_game(df, scores, split_name="test")
    assert out.excluded_bets == 1
    assert out.player_game_count == 1
    assert len(out.scores) == 1


def test_player_game_metrics_at_threshold_counts_groups() -> None:
    """At a fixed threshold, alert_count equals player-game rows above threshold."""

    y = np.array([1, 0], dtype=np.int8)
    scores = np.array([0.95, 0.85], dtype=np.float64)
    prec, rec, _f1, alerts = _b5._metrics_at_threshold(y, scores, threshold=0.8)
    assert alerts == 2
    assert prec == pytest.approx(0.5)
    assert rec == pytest.approx(1.0)


def _synthetic_train_frame(*, n_neg: int, n_pos: int) -> pd.DataFrame:
    neg = pd.DataFrame({"walkaway_label": [0] * n_neg, "bet_id": np.arange(n_neg, dtype=float)})
    pos = pd.DataFrame(
        {
            "walkaway_label": [1] * n_pos,
            "bet_id": np.arange(n_neg, n_neg + n_pos, dtype=float),
        },
    )
    return pd.concat([neg, pos], ignore_index=True)


def test_downsample_train_negatives_preserves_all_positives() -> None:
    from trainer_hightier.utils.train_negative_sampling import downsample_train_negatives

    df = _synthetic_train_frame(n_neg=100, n_pos=5)
    out, counts = downsample_train_negatives(df, neg_sample_frac=0.2, neg_sample_seed=11)
    assert counts["train_positives_kept"] == 5
    assert int(out["walkaway_label"].sum()) == 5
    assert counts["train_negatives_after"] < counts["train_negatives_before"]


def test_downsample_train_negatives_reproducible_with_seed() -> None:
    from trainer_hightier.utils.train_negative_sampling import downsample_train_negatives

    df = _synthetic_train_frame(n_neg=200, n_pos=3)
    out_a, meta_a = downsample_train_negatives(df, neg_sample_frac=0.25, neg_sample_seed=99)
    out_b, meta_b = downsample_train_negatives(df, neg_sample_frac=0.25, neg_sample_seed=99)
    assert meta_a == meta_b
    assert set(out_a["bet_id"].tolist()) == set(out_b["bet_id"].tolist())


def test_materialize_sampled_train_disabled_uses_source_train(tmp_path: Path) -> None:
    from trainer_hightier.config import SamplePolicy
    from trainer_hightier.utils.train_negative_sampling import materialize_sampled_train_parquet

    splits = tmp_path / "splits"
    splits.mkdir()
    train_p = splits / "train.parquet"
    pd.DataFrame({"walkaway_label": [0, 1], "bet_id": [1.0, 2.0]}).to_parquet(train_p)
    out, meta = materialize_sampled_train_parquet(
        train_parquet=train_p,
        splits_dir=splits,
        policy=SamplePolicy(neg_sample_frac=1.0),
    )
    assert out.resolve() == train_p.resolve()
    assert meta["enabled"] is False


def test_downsample_train_negatives_rejects_missing_label_column() -> None:
    from trainer_hightier.utils.train_negative_sampling import downsample_train_negatives

    df = pd.DataFrame({"bet_id": [1.0, 2.0]})
    with pytest.raises(ValueError, match="missing label column"):
        downsample_train_negatives(df, neg_sample_frac=0.5, neg_sample_seed=1)


def test_downsample_train_negatives_rejects_null_labels() -> None:
    from trainer_hightier.utils.train_negative_sampling import downsample_train_negatives

    df = pd.DataFrame({"walkaway_label": [0.0, None], "bet_id": [1.0, 2.0]})
    with pytest.raises(ValueError, match="null_ratio"):
        downsample_train_negatives(df, neg_sample_frac=0.5, neg_sample_seed=1)


def test_downsample_train_negatives_rejects_invalid_frac() -> None:
    from trainer_hightier.utils.train_negative_sampling import downsample_train_negatives

    df = _synthetic_train_frame(n_neg=10, n_pos=2)
    with pytest.raises(ValueError, match="neg_sample_frac must be in"):
        downsample_train_negatives(df, neg_sample_frac=0.0, neg_sample_seed=1)


def test_materialize_sampled_train_cache_hit(tmp_path: Path) -> None:
    from trainer_hightier.config import SamplePolicy
    from trainer_hightier.utils.train_negative_sampling import materialize_sampled_train_parquet

    splits = tmp_path / "splits"
    splits.mkdir()
    train_p = splits / "train.parquet"
    pd.DataFrame(
        {
            "walkaway_label": [0] * 80 + [1] * 20,
            "bet_id": np.arange(100, dtype=float),
        },
    ).to_parquet(train_p)
    policy = SamplePolicy(neg_sample_frac=0.3, neg_sample_seed=7)
    _, meta1 = materialize_sampled_train_parquet(
        train_parquet=train_p,
        splits_dir=splits,
        policy=policy,
    )
    assert meta1["cache_hit"] is False
    assert (splits / "train_sampled.parquet").is_file()

    _, meta2 = materialize_sampled_train_parquet(
        train_parquet=train_p,
        splits_dir=splits,
        policy=policy,
    )
    assert meta2["cache_hit"] is True
    assert meta2["train_rows_after"] == meta1["train_rows_after"]
    assert meta2["val_test_evaluation_unsampled"] is True

