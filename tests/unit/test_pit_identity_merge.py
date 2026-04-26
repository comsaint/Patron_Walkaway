"""Unit tests for PIT identity merge (B3)."""
from __future__ import annotations

import pandas as pd

from trainer.identity import merge_pit_canonical_to_bets


def test_merge_pit_canonical_to_bets_backward_only() -> None:
    """Later session link must not be visible before link_usable_time."""
    bets = pd.DataFrame(
        {
            "bet_id": [1, 2],
            "player_id": [10, 10],
            "payout_complete_dtm": pd.to_datetime(
                ["2024-01-01 10:00:00", "2024-01-01 12:00:00"]
            ),
        }
    )
    links = pd.DataFrame(
        {
            "player_id": [10, 10],
            "casino_player_id": ["cp_early", "cp_late"],
            "lud_dtm": pd.to_datetime(["2024-01-01 09:00:00", "2024-01-01 11:00:00"]),
            "link_usable_time": pd.to_datetime(
                ["2024-01-01 09:07:00", "2024-01-01 11:07:00"]
            ),
        }
    )
    out = merge_pit_canonical_to_bets(bets, links)
    assert out.loc[0, "canonical_id"] == "cp_early"
    assert out.loc[1, "canonical_id"] == "cp_late"
    assert bool(out.loc[0, "_pit_rated"]) is True
    assert bool(out.loc[1, "_pit_rated"]) is True


def test_merge_pit_canonical_unrated_before_first_link() -> None:
    """Bet before any link_usable_time is unrated for PIT prune."""
    bets = pd.DataFrame(
        {
            "bet_id": [1],
            "player_id": [99],
            "payout_complete_dtm": pd.to_datetime(["2024-01-01 08:00:00"]),
        }
    )
    links = pd.DataFrame(
        {
            "player_id": [99],
            "casino_player_id": ["cp1"],
            "lud_dtm": pd.to_datetime(["2024-01-01 09:00:00"]),
            "link_usable_time": pd.to_datetime(["2024-01-01 09:07:00"]),
        }
    )
    out = merge_pit_canonical_to_bets(bets, links)
    assert out.loc[0, "canonical_id"] == "99"
    assert bool(out.loc[0, "_pit_rated"]) is False


def test_merge_pit_canonical_handles_us_ns_datetime_mismatch() -> None:
    """merge_asof should succeed when left/right time units differ (ns vs us)."""
    bets = pd.DataFrame(
        {
            "bet_id": [1],
            "player_id": [7],
            "payout_complete_dtm": pd.to_datetime(
                ["2024-01-01 10:00:00"]
            ).astype("datetime64[ns]"),
        }
    )
    links = pd.DataFrame(
        {
            "player_id": [7],
            "casino_player_id": ["cp7"],
            "lud_dtm": pd.to_datetime(["2024-01-01 09:00:00"]).astype("datetime64[us]"),
            "link_usable_time": pd.to_datetime(["2024-01-01 09:07:00"]).astype(
                "datetime64[us]"
            ),
        }
    )
    out = merge_pit_canonical_to_bets(bets, links)
    assert out.loc[0, "canonical_id"] == "cp7"
    assert bool(out.loc[0, "_pit_rated"]) is True


def test_merge_pit_canonical_handles_timezone_aware_bet_time() -> None:
    """PIT merge normalizes tz-aware bet times to HK tz-naive before merge_asof."""
    bets = pd.DataFrame(
        {
            "bet_id": [1],
            "player_id": [8],
            "payout_complete_dtm": pd.Series(
                pd.to_datetime(["2024-01-01 02:00:00"], utc=True)
            ),
        }
    )
    links = pd.DataFrame(
        {
            "player_id": [8],
            "casino_player_id": ["cp8"],
            "lud_dtm": pd.to_datetime(["2024-01-01 09:00:00"]).astype("datetime64[us]"),
            "link_usable_time": pd.to_datetime(["2024-01-01 09:07:00"]).astype(
                "datetime64[us]"
            ),
        }
    )
    out = merge_pit_canonical_to_bets(bets, links)
    assert out.loc[0, "canonical_id"] == "cp8"
    assert bool(out.loc[0, "_pit_rated"]) is True


def test_merge_pit_canonical_sorts_time_key_globally_with_by_key() -> None:
    """merge_asof requires global sort by time even when matching by player_id."""
    bets = pd.DataFrame(
        {
            "bet_id": [1, 2],
            "player_id": [2, 1],
            "payout_complete_dtm": pd.to_datetime(
                ["2024-01-01 12:00:00", "2024-01-01 10:00:00"]
            ),
        }
    )
    links = pd.DataFrame(
        {
            "player_id": [2, 1],
            "casino_player_id": ["cp2", "cp1"],
            "lud_dtm": pd.to_datetime(
                ["2024-01-01 11:00:00", "2024-01-01 09:00:00"]
            ),
            "link_usable_time": pd.to_datetime(
                ["2024-01-01 11:07:00", "2024-01-01 09:07:00"]
            ),
        }
    )
    out = merge_pit_canonical_to_bets(bets, links)
    assert out["canonical_id"].tolist() == ["cp2", "cp1"]
    assert out["_pit_rated"].tolist() == [True, True]
