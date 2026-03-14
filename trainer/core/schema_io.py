# ---------------------------------------------------------------------------
# Post-Load Normalizer — schema/dtype 正規化（與資料來源無關）
# ---------------------------------------------------------------------------
#
# 原則（PLAN.md § Post-Load Normalizer）：
#   Always preprocess data input the same way regardless of source.
# 不論資料來自 Parquet、ClickHouse、API 或 ETL，只要進入 pipeline
# （trainer / scorer / backtester / ETL），都必須先經同一個 normalizer，
# 再進行後續 DQ、特徵或寫出。型別契約一致，避免來源不同導致靜默行為差異。
#
# 職責：僅對「剛載入的 raw bets / sessions DataFrame」做 schema/dtype 正規化
# （含標註 categorical）。不負責：過濾、時區、業務 DQ、identity、特徵計算、cache key。
#
# Categorical：保留 NaN 在 category 中，不對 categorical 做 fillna 再 astype。
# Key numeric：to_numeric(..., errors="coerce")，不在此處 fillna，由 apply_dq 或下游負責。
# ---------------------------------------------------------------------------

from __future__ import annotations

from typing import Tuple

import pandas as pd

# Single source of truth for normalizer column sets (PLAN.md Post-Load Normalizer).

BET_CATEGORICAL_COLUMNS = ("table_id", "position_idx", "is_back_bet")
SESSION_CATEGORICAL_COLUMNS = ("table_id",)
BET_KEY_NUMERIC_COLUMNS = ("bet_id", "session_id", "player_id")
SESSION_KEY_NUMERIC_COLUMNS = ("session_id", "player_id")


def normalize_bets_sessions(
    bets: pd.DataFrame,
    sessions: pd.DataFrame,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Normalise schema/dtype of raw bets and sessions; return copies, do not mutate inputs.

    - Categorical columns (if present): astype("category") — NaN is preserved in category.
    - Key numeric columns (if present): pd.to_numeric(..., errors="coerce") — no fillna here.

    Callers (trainer, scorer, backtester, ETL) must run this on loaded data before
    apply_dq or any business logic. See PLAN.md § Post-Load Normalizer.
    """
    bets_out = bets.copy()
    sessions_out = sessions.copy()

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
