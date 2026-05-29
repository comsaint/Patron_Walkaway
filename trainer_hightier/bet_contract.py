"""Bet L0 ingest column contract + lightweight dataframe checks.

Aligned with GDP include list / GDP_GMWDS_Raw_Schema_Dictionary §4 remarks in
legacy ``trainer.training.data_sources``.
"""

from __future__ import annotations

from typing import Final

import pandas as pd

#: Stable read order for L0 ``gmwds_t_bet`` → preprocess (must match parquet schema probes).
BET_INGEST_READ_COLS_ORDERED: Final[tuple[str, ...]] = (
    "bet_id",
    "session_id",
    "player_id",
    "game_id",
    "table_id",
    "payout_complete_dtm",
    "__etl_insert_Dtm",
    "wager",
    "wager_nn",
    "status",
    "casino_win",
    "payout_odds",
    "payout_ha",
    "base_ha",
    "is_back_bet",
    "position_idx",
    "position_code",
    "position_label",
    "bet_type",
    "type_of_bet",
    "commission",
    "max_wager",
    "std_dev",
    "theo_win",
    "theo_win_cash",
    "true_odds",
    "adjusted_theo_win",
    "is_settled",
    "bet_payout_type",
    "mixed_stack",
    "auto_resolve_stack",
    "__ts_ms",
    "__op",
    "__deleted",
)


def bet_ingest_read_cols_ordered() -> tuple[str, ...]:
    """Return the ordered ingest column projection for bets L0 parquet."""
    return BET_INGEST_READ_COLS_ORDERED


def assert_bets_gaming_day_event_contract(bets: pd.DataFrame, context: str) -> None:
    """Fail fast when ``gaming_day_event`` is missing or null (cleaned t_bet contract)."""

    if "gaming_day_event" not in bets.columns:
        raise ValueError(f"{context}: missing required column 'gaming_day_event' (no fallback)")
    if bets.empty:
        return
    if bets["gaming_day_event"].isna().any():
        n = int(bets["gaming_day_event"].isna().sum())
        raise ValueError(
            f"{context}: gaming_day_event must be non-null on all bet rows (found {n} nulls)"
        )
