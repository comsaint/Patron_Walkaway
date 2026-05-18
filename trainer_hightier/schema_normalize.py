"""Post-load schema / dtype coercion for bets and sessions DataFrames."""

from __future__ import annotations

from typing import Tuple

import pandas as pd

BET_CATEGORICAL_COLUMNS = ("table_id", "position_idx", "is_back_bet")
SESSION_CATEGORICAL_COLUMNS = ("table_id",)
BET_KEY_NUMERIC_COLUMNS = ("bet_id", "session_id", "player_id")
SESSION_KEY_NUMERIC_COLUMNS = ("session_id", "player_id")


def normalize_bets_sessions(bets: pd.DataFrame, sessions: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Normalize schema dtypes; shallow-copy inputs; do not mutate arguments."""

    bets_out = bets.copy(deep=False)
    sessions_out = sessions.copy(deep=False)

    for col in BET_CATEGORICAL_COLUMNS:
        if col in bets_out.columns:
            bets_out[col] = bets_out[col].astype("category")

    for col in SESSION_CATEGORICAL_COLUMNS:
        if col in sessions_out.columns:
            sessions_out[col] = sessions_out[col].astype("category")

    for col in BET_KEY_NUMERIC_COLUMNS:
        if col in bets_out.columns:
            bets_out[col] = pd.to_numeric(bets_out[col], errors="coerce")

    for col in SESSION_KEY_NUMERIC_COLUMNS:
        if col in sessions_out.columns:
            sessions_out[col] = pd.to_numeric(sessions_out[col], errors="coerce")

    return bets_out, sessions_out
