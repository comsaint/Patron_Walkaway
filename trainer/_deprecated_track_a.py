"""Deprecated Track A — Featuretools DFS (DEC-022). Kept for backward compatibility.

Trainer/scorer may still call these; new code should use Track Profile / LLM / Human.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional, Tuple

import pandas as pd

try:
    from config import HIST_AVG_BET_CAP  # type: ignore[import]
except ModuleNotFoundError:
    from trainer.config import HIST_AVG_BET_CAP  # type: ignore[import]

logger = logging.getLogger(__name__)

_NUMERIC_BET_COLS = ["wager", "payout", "player_win", "num_games_with_wager"]
_NUMERIC_SESSION_COLS = ["turnover", "player_win", "num_games_with_wager"]


def _ft():
    import featuretools as ft_mod  # noqa: F401
    return ft_mod


def build_entity_set(
    bets_df: pd.DataFrame,
    sessions_df: pd.DataFrame,
    canonical_map: pd.DataFrame,
    session_time_col: str = "session_avail_dtm",
):
    ft_mod = _ft()
    bets = bets_df.copy()
    sessions = sessions_df.copy()
    for col in _NUMERIC_BET_COLS:
        if col in bets.columns:
            bets[col] = bets[col].fillna(0).clip(upper=HIST_AVG_BET_CAP)
    for col in _NUMERIC_SESSION_COLS:
        if col in sessions.columns:
            sessions[col] = sessions[col].fillna(0).clip(upper=HIST_AVG_BET_CAP)
    players = (
        canonical_map[["canonical_id"]]
        .drop_duplicates(subset=["canonical_id"])
        .copy()
    )
    es = ft_mod.EntitySet(id="walkaway")
    es = es.add_dataframe(
        dataframe_name="t_bet",
        dataframe=bets,
        index="bet_id",
        time_index="payout_complete_dtm",
    )
    es = es.add_dataframe(
        dataframe_name="t_session",
        dataframe=sessions,
        index="session_id",
        time_index=session_time_col,
    )
    es = es.add_dataframe(
        dataframe_name="player",
        dataframe=players,
        index="canonical_id",
    )
    es = es.add_relationship("t_session", "session_id", "t_bet", "session_id")
    es = es.add_relationship("player", "canonical_id", "t_session", "canonical_id")
    return es


def run_dfs_exploration(
    es,
    cutoff_df: pd.DataFrame,
    max_depth: int = 2,
    agg_primitives: Optional[List[str]] = None,
    trans_primitives: Optional[List[str]] = None,
) -> Tuple[pd.DataFrame, list]:
    ft_mod = _ft()
    _agg = agg_primitives or [
        "count", "sum", "mean", "max", "min", "trend",
        "num_unique", "time_since_last",
    ]
    _trans = trans_primitives or ["time_since_previous", "cum_sum", "cum_mean"]
    feature_matrix, feature_defs = ft_mod.dfs(
        entityset=es,
        target_dataframe_name="t_bet",
        cutoff_time=cutoff_df,
        agg_primitives=_agg,
        trans_primitives=_trans,
        max_depth=max_depth,
        verbose=False,
    )
    return feature_matrix, feature_defs


def save_feature_defs(feature_defs: list, path: Path) -> None:
    ft_mod = _ft()
    ft_mod.save_features(feature_defs, str(path))
    logger.info("Saved %d feature definitions to %s", len(feature_defs), path)


def load_feature_defs(path: Path) -> list:
    ft_mod = _ft()
    feature_defs = ft_mod.load_features(str(path))
    logger.info("Loaded %d feature definitions from %s", len(feature_defs), path)
    return feature_defs


def compute_feature_matrix(es, saved_feature_defs: list, cutoff_df: pd.DataFrame) -> pd.DataFrame:
    ft_mod = _ft()
    return ft_mod.calculate_feature_matrix(
        features=saved_feature_defs,
        entityset=es,
        cutoff_time=cutoff_df,
        verbose=False,
    )
