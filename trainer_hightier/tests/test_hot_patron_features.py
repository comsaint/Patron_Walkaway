"""Tests for hot-patron peer lookup materialization."""

from __future__ import annotations

import numpy as np
import pandas as pd

from trainer_hightier.feature_experiment.hot_patron_features import (
    _peer_lookup_from_train,
    materialize_hot_patron_features,
)


def _mini_frame(*, gaming_day: str, n: int, wager_scale: float) -> pd.DataFrame:
    """Build a minimal enriched frame for hot-patron materialization tests."""

    rng = np.random.default_rng(42)
    adt = rng.uniform(10_000, 500_000, size=n)
    return pd.DataFrame(
        {
            "gaming_day_event": [gaming_day] * n,
            "player_id": np.arange(n, dtype=np.int64),
            "game_id": np.arange(n, dtype=np.int64),
            "patron__adt__w180d_m1snap": adt,
            "fe__canonical__wager_sum__today": rng.uniform(1_000, 10_000, size=n) * wager_scale,
            "fe__canonical__avg_wager__today": rng.uniform(100, 1_000, size=n),
            "fe__wager_sum__w15m": rng.uniform(100, 5_000, size=n),
            "fe__canonical__elapsed_sec_since_first_bet__today": rng.uniform(600, 7200, size=n),
            "mid_term_snapshot_missing_flag": np.zeros(n),
            "patron__gaming_days_cnt__w180d_m1snap": rng.integers(5, 40, size=n),
            "patron__theo_win_sum__w180d_m1snap": rng.uniform(10_000, 100_000, size=n),
        }
    )


def test_historical_adt_decile_peer_lookup_covers_future_days() -> None:
    """Train-only ADT-decile lookup should populate peer features on unseen val days."""

    train = _mini_frame(gaming_day="2026-01-01", n=200, wager_scale=1.0)
    val = _mini_frame(gaming_day="2026-05-01", n=80, wager_scale=1.2)
    lookup = _peer_lookup_from_train(train)
    out = materialize_hot_patron_features(val, peer_lookup=lookup)
    peer_null = float(out["fe__hot__peer_wager_z__adt_decile"].isna().mean())
    avg_null = float(out["fe__hot__avg_wager_today_over_peer_p95__adt_decile"].isna().mean())
    assert peer_null < 0.5, f"peer_wager_z null_rate={peer_null:.3f}"
    assert avg_null < 0.5, f"avg_wager peer null_rate={avg_null:.3f}"
