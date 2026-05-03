"""trainer/identity.py
=====================
Player identity resolution — D2 Canonical ID strategy.

Provides two public interfaces that share the same core M:N resolution logic:

Training path (offline / Parquet)
----------------------------------
``build_canonical_mapping_from_df(sessions_df, cutoff_dtm)``
    Pure-pandas path.  Works with local Parquet and is used by tests.

``build_canonical_mapping(client, cutoff_dtm)``
    ClickHouse path.  Runs the FND-01 CTE queries, then calls the shared
    M:N resolution helper.

Online scoring path
--------------------
``resolve_canonical_id(player_id, session_id, mapping_df, session_lookup, obs_time)``
    Three-step D2 resolution (SSOT §6.4).  Returns a canonical_id string.

Design notes
------------
* Both offline and online paths use ``CASINO_PLAYER_ID_CLEAN_SQL`` /
  ``_clean_casino_player_id()`` to strip whitespace and string-"null" values
  (FND-03).
* ``cutoff_dtm`` must be the training window end; only sessions whose
  ``COALESCE(session_end_dtm, lud_dtm) <= cutoff_dtm`` are used, preventing
  future identity links from leaking into training (B1).
* FND-12 fake-account exclusion: player_ids with exactly 1 session and
  ≤1 game with wager are dropped from the mapping entirely.
* D3: ``cutoff_window`` mapping is built on the entire training window (legacy).
  ``pit_asof`` uses :func:`merge_pit_canonical_to_bets` with per-bet
  ``merge_asof`` on session link ``link_usable_time`` (Phase 2 / B3).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable, Optional, Set
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

_HK_TZ = ZoneInfo("Asia/Hong_Kong")

try:
    from config import (  # type: ignore[import]
        CASINO_PLAYER_ID_CLEAN_SQL,
        PLACEHOLDER_PLAYER_ID,
        SESSION_AVAIL_DELAY_MIN,
        SOURCE_DB,
        TSESSION,
    )
except ModuleNotFoundError:
    from trainer.config import (  # type: ignore[import]
        CASINO_PLAYER_ID_CLEAN_SQL,
        PLACEHOLDER_PLAYER_ID,
        SESSION_AVAIL_DELAY_MIN,
        SOURCE_DB,
        TSESSION,
    )

logger = logging.getLogger(__name__)

# Columns that must be present in the sessions_df passed to
# build_canonical_mapping_from_df (R11: early validation).
# __etl_insert_Dtm is optional (used only as tiebreaker in FND-01 dedup).
_REQUIRED_SESSION_COLS: frozenset[str] = frozenset({
    "session_id",
    "lud_dtm",
    "player_id",
    "casino_player_id",
    "session_end_dtm",
    "is_manual",
    "is_deleted",
    "is_canceled",
    "num_games_with_wager",
    "turnover",  # FND-04: required for ghost-session filter
})

# ---------------------------------------------------------------------------
# ClickHouse SQL templates
# (Note: no FINAL on t_session — G1: ReplicatedReplacingMergeTree without
#  version column makes FINAL non-deterministic.  FND-01 CTE handles dedup.)
# ---------------------------------------------------------------------------

_FND01_CTE_TMPL = """\
WITH deduped AS (
    SELECT *,
        ROW_NUMBER() OVER (
            PARTITION BY session_id
            ORDER BY lud_dtm DESC NULLS LAST, __etl_insert_Dtm DESC
        ) AS rn
    FROM {db}.{table}
)"""

# Step 1 — extract player_id ↔ casino_player_id edges for rated players.
# casino_player_id is cleaned inline (FND-03).  Cutoff applied via
# COALESCE(session_end_dtm, lud_dtm) to use the business event time (FND-13).
# FND-04: exclude ghost sessions with no real wager activity (SSOT §5).
_LINKS_SQL_TMPL = """\
{cte}
SELECT player_id,
       ({clean_sql}) AS casino_player_id,
       lud_dtm
FROM deduped
WHERE rn = 1
  AND is_manual = 0
  AND is_deleted = 0 AND is_canceled = 0
  AND player_id IS NOT NULL AND player_id != {placeholder}
  AND COALESCE(session_end_dtm, lud_dtm) <= '{cutoff_dtm}'
  AND (COALESCE(turnover, 0) > 0 OR COALESCE(num_games_with_wager, 0) > 0)
  AND ({clean_sql}) IS NOT NULL"""

# Step 2 — identify dummy / fake-account player_ids (FND-12 / E6 / I1 / I2).
# Groups by player_id after FND-01 dedup; COALESCE on Nullable num_games_with_wager.
# cutoff_dtm applied here too (R8 fix) so pandas and SQL paths agree on which
# sessions count toward the dummy criterion.
# FND-04: exclude ghost sessions (SSOT §5).
_DUMMY_SQL_TMPL = """\
{cte}
SELECT player_id
FROM deduped
WHERE rn = 1
  AND is_manual = 0
  AND is_deleted = 0 AND is_canceled = 0
  AND player_id IS NOT NULL AND player_id != {placeholder}
  AND COALESCE(session_end_dtm, lud_dtm) <= '{cutoff_dtm}'
  AND (COALESCE(turnover, 0) > 0 OR COALESCE(num_games_with_wager, 0) > 0)
GROUP BY player_id
HAVING COUNT(session_id) = 1
   AND SUM(COALESCE(num_games_with_wager, 0)) <= 1"""


def _build_links_sql(cutoff_dtm: datetime) -> str:
    cte = _FND01_CTE_TMPL.format(db=SOURCE_DB, table=TSESSION)
    return _LINKS_SQL_TMPL.format(
        cte=cte,
        clean_sql=CASINO_PLAYER_ID_CLEAN_SQL,
        placeholder=PLACEHOLDER_PLAYER_ID,
        cutoff_dtm=cutoff_dtm.strftime("%Y-%m-%d %H:%M:%S"),
    )


def _build_dummy_sql(cutoff_dtm: datetime) -> str:
    """Build the FND-12 dummy-detection SQL.

    Parameters
    ----------
    cutoff_dtm : datetime
        Same training-window end used in the links query (B1 / R8 fix):
        only sessions before this point count toward the dummy criterion,
        matching the pandas-path behaviour of ``_identify_dummy_player_ids``.
    """
    cte = _FND01_CTE_TMPL.format(db=SOURCE_DB, table=TSESSION)
    return _DUMMY_SQL_TMPL.format(
        cte=cte,
        placeholder=PLACEHOLDER_PLAYER_ID,
        cutoff_dtm=cutoff_dtm.strftime("%Y-%m-%d %H:%M:%S"),
    )


# ---------------------------------------------------------------------------
# Pure-pandas helpers (shared by offline path and tests)
# ---------------------------------------------------------------------------

def _clean_casino_player_id(series: pd.Series) -> pd.Series:
    """Apply FND-03: strip whitespace and convert 'null'/'NULL'/empty strings
    to NaN; return the *trimmed* value for valid entries.

    Parity note (R7): SQL path uses ``trim(casino_player_id)``, so we must
    return the stripped string — not the original — to keep train-serve
    canonical_ids identical for IDs that contain surrounding whitespace.
    """
    stripped = series.astype(str).str.strip()
    # Parity with CASINO_PLAYER_ID_CLEAN_SQL: only '' and 'null' are invalid (R1701).
    mask_invalid = stripped.str.lower().isin(["", "null"])
    valid_mask = series.notna() & ~mask_invalid
    # Return the trimmed value for valid entries (parity with SQL trim()).
    # stripped for null/invalid rows contains artefacts like "nan"/"<NA>";
    # valid_mask=False for those, so they are replaced by pd.NA.
    return stripped.where(valid_mask, other=pd.NA)


def _to_hk_naive_datetime64_ns(series: pd.Series) -> pd.Series:
    """Normalize datetime-like values to HK-local tz-naive ``datetime64[ns]``.

    Local Parquet and ClickHouse/DuckDB paths can disagree on both timezone
    awareness and timestamp unit (``us`` vs ``ns``). ``merge_asof`` requires an
    exact dtype match, while the pipeline interior expects HK tz-naive times.
    """
    ts = pd.to_datetime(series, errors="coerce")
    if isinstance(ts.dtype, pd.DatetimeTZDtype):
        ts = ts.dt.tz_convert(_HK_TZ).dt.tz_localize(None)
    return ts.astype("datetime64[ns]")


def _fnd01_dedup_pandas(sessions_df: pd.DataFrame) -> pd.DataFrame:
    """Replicate FND-01 ROW_NUMBER dedup in pandas.

    Keeps the row with the latest ``lud_dtm`` (then latest
    ``__etl_insert_Dtm`` as tiebreaker) for each ``session_id``.
    NaT sorts as NULLS LAST (i.e. after non-null values in DESC order).
    """
    sort_helper = pd.DataFrame(
        {
            "session_id": sessions_df["session_id"],
            "_lud_sort": pd.to_datetime(sessions_df["lud_dtm"], errors="coerce"),
            "_etl_sort": pd.to_datetime(
                sessions_df.get("__etl_insert_Dtm", pd.NaT), errors="coerce"
            ),
            "_row_pos": range(len(sessions_df)),
        },
        index=sessions_df.index,
    )
    keep_row_pos = (
        sort_helper.sort_values(
            ["session_id", "_lud_sort", "_etl_sort"],
            ascending=[True, False, False],
            na_position="last",
        )
        .drop_duplicates(subset=["session_id"], keep="first")["_row_pos"]
        .to_numpy()
    )
    return sessions_df.iloc[keep_row_pos].copy()


def _identify_dummy_player_ids(deduped_df: pd.DataFrame) -> Set:
    """Return the set of player_ids that match FND-12 fake-account criteria.

    A player_id is considered a dummy if it has exactly 1 session and
    ≤1 game with a wager (COALESCE-safe for Nullable column).
    """
    games_num = pd.to_numeric(
        deduped_df.get("num_games_with_wager", pd.Series(0, index=deduped_df.index)),
        errors="coerce",
    ).fillna(0)
    turnover_num = pd.to_numeric(
        deduped_df.get("turnover", pd.Series(0, index=deduped_df.index)),
        errors="coerce",
    ).fillna(0)
    keep_mask = (
        (deduped_df["is_manual"] == 0)
        & (deduped_df["is_deleted"] == 0)
        & (deduped_df["is_canceled"] == 0)
        & deduped_df["player_id"].notna()
        & (deduped_df["player_id"] != PLACEHOLDER_PLAYER_ID)
        & ((turnover_num > 0) | (games_num > 0))
    )
    valid = deduped_df.loc[keep_mask, ["player_id", "session_id"]].copy()
    valid["_games"] = games_num.loc[keep_mask].to_numpy()
    agg = valid.groupby("player_id").agg(
        session_cnt=("session_id", "count"),
        total_games=("_games", "sum"),
    )
    dummy_ids = agg.loc[
        (agg["session_cnt"] == 1) & (agg["total_games"] <= 1)
    ].index
    return set(dummy_ids)


def _apply_mn_resolution(
    links_df: pd.DataFrame,
    dummy_player_ids: Set,
) -> pd.DataFrame:
    """Core M:N conflict resolution — returns DataFrame[player_id, canonical_id].

    Parameters
    ----------
    links_df : DataFrame with columns [player_id, casino_player_id, lud_dtm]
        Contains only rated sessions (casino_player_id already cleaned and
        guaranteed non-null).
    dummy_player_ids : set
        player_ids identified as FND-12 fake accounts; excluded from result.

    M:N conflict rules (SSOT §6.3)
    --------------------------------
    Case 1 — same casino_player_id ↔ multiple player_ids (断链重发):
        All those player_ids map to canonical_id = casino_player_id.
        Handled naturally: each player_id picks its casino_player_id as
        canonical, and they all happen to converge on the same string.

    Case 2 — same player_id ↔ multiple casino_player_ids (换卡):
        Keep the casino_player_id with the most recent lud_dtm.
        The conflict list is logged at WARNING level for auditing.
    """
    if links_df.empty:
        return pd.DataFrame(columns=["player_id", "canonical_id"])

    df = links_df.copy()
    df["lud_dtm"] = pd.to_datetime(df["lud_dtm"], errors="coerce")

    # Case 2 audit: find player_ids with >1 distinct casino_player_id
    card_counts = df.groupby("player_id")["casino_player_id"].nunique()
    swapped = card_counts[card_counts > 1]
    if not swapped.empty:
        logger.warning(
            "D2 Case 2 (card swap): %d player_id(s) mapped to multiple "
            "casino_player_ids — keeping most recent: %s",
            len(swapped),
            swapped.index.tolist()[:20],  # cap log size
        )

    # Resolve Case 2: per player_id, keep row with max lud_dtm
    resolved = (
        df.sort_values("lud_dtm", ascending=False, na_position="last")
        .drop_duplicates(subset=["player_id"], keep="first")
        [["player_id", "casino_player_id"]]
        .rename(columns={"casino_player_id": "canonical_id"})
    )
    # Ensure every value is a plain Python str (not pd.NA / int).
    # Under pandas 3.x infer_string=True the column dtype will be StringDtype,
    # which is expected and equally correct for downstream str comparisons.
    resolved["canonical_id"] = resolved["canonical_id"].astype(str)

    # Exclude FND-12 dummy player_ids
    resolved = resolved[~resolved["player_id"].isin(dummy_player_ids)]
    return resolved.reset_index(drop=True)


def build_pit_session_links_dataframe(
    sessions_df: pd.DataFrame,
    cutoff_dtm: datetime,
    session_avail_delay_min: int,
    placeholder_player_id: int,
) -> pd.DataFrame:
    """Build rated session edges with ``link_usable_time`` for PIT merge_asof (B3 / D3).

    A session link becomes usable at
    ``COALESCE(session_end_dtm, lud_dtm) + session_avail_delay_min``,
    mirroring ``resolve_canonical_id`` H2 (session must have ended before the
    observation minus ingest delay).

    Parameters
    ----------
    sessions_df:
        Raw ``t_session`` rows with columns required by ``build_canonical_mapping_from_df``.
    cutoff_dtm:
        Training window end; only sessions with business time ``<= cutoff_dtm`` are kept.
    session_avail_delay_min:
        Minutes after session business time before the link is observable.
    placeholder_player_id:
        Sentinel ``player_id`` excluded from links.

    Returns
    -------
    DataFrame sorted by ``player_id``, ``link_usable_time``, ``lud_dtm`` with columns
    ``player_id``, ``casino_player_id`` (cleaned str), ``lud_dtm``, ``link_usable_time``.
    """
    missing = _REQUIRED_SESSION_COLS - set(sessions_df.columns)
    if missing:
        raise ValueError(f"sessions_df is missing required columns: {sorted(missing)}")
    deduped = _fnd01_dedup_pandas(sessions_df)
    session_time = deduped["session_end_dtm"].fillna(deduped["lud_dtm"])
    session_time = pd.to_datetime(session_time, errors="coerce")
    cutoff_ts = pd.Timestamp(cutoff_dtm)
    if hasattr(session_time, "dt"):
        col_tz = session_time.dt.tz
        if col_tz is not None and cutoff_ts.tz is None:
            cutoff_ts = cutoff_ts.tz_localize(col_tz)
        elif col_tz is None and cutoff_ts.tz is not None:
            cutoff_ts = cutoff_ts.replace(tzinfo=None)
    _turnover = pd.to_numeric(
        deduped.get("turnover", pd.Series(0.0, index=deduped.index)),
        errors="coerce",
    ).fillna(0)
    _games = deduped["num_games_with_wager"].fillna(0)
    mask = (
        (deduped["is_manual"] == 0)
        & (deduped["is_deleted"] == 0)
        & (deduped["is_canceled"] == 0)
        & deduped["player_id"].notna()
        & (deduped["player_id"] != placeholder_player_id)
        & (session_time <= cutoff_ts)
        & ((_turnover > 0) | (_games > 0))
    )
    filtered = deduped.loc[mask].copy()
    cleaned_casino_player_id = _clean_casino_player_id(filtered["casino_player_id"])
    rated_mask = cleaned_casino_player_id.notna()
    out = filtered.loc[rated_mask, ["player_id", "lud_dtm"]].copy()
    out["casino_player_id"] = cleaned_casino_player_id.loc[rated_mask].astype(str)
    st = session_time.loc[rated_mask]
    out["link_usable_time"] = st + pd.Timedelta(minutes=int(session_avail_delay_min))
    out["lud_dtm"] = pd.to_datetime(out["lud_dtm"], errors="coerce")
    out["link_usable_time"] = pd.to_datetime(out["link_usable_time"], errors="coerce")
    return out.sort_values(
        ["player_id", "link_usable_time", "lud_dtm"],
        ascending=True,
        kind="stable",
    ).reset_index(drop=True)


def merge_pit_canonical_to_bets(
    bets_df: pd.DataFrame,
    links_df: pd.DataFrame,
) -> pd.DataFrame:
    """Attach ``canonical_id`` and ``_pit_rated`` via point-in-time ``merge_asof``.

    Parameters
    ----------
    bets_df:
        Must include ``player_id``, ``payout_complete_dtm``, ``bet_id``.
    links_df:
        Output of :func:`build_pit_session_links_dataframe` (non-empty).

    Returns
    -------
    Copy of ``bets_df`` with ``canonical_id`` (str), ``_pit_rated`` (bool).
    """
    out = bets_df.copy()
    if links_df is None or links_df.empty:
        out["canonical_id"] = out["player_id"].astype(str)
        out["_pit_rated"] = False
        return out
    req = {"player_id", "payout_complete_dtm", "bet_id"}
    miss = req - set(out.columns)
    if miss:
        raise ValueError(f"bets_df missing columns for PIT merge: {sorted(miss)}")
    # pandas merge_asof requires exact dtype match on time keys.
    # DuckDB-derived links may be tz-aware and/or datetime64[us] while bets are ns.
    out["payout_complete_dtm"] = _to_hk_naive_datetime64_ns(
        out["payout_complete_dtm"]
    )
    lk = links_df[
        ["player_id", "casino_player_id", "link_usable_time", "lud_dtm"]
    ].rename(columns={"lud_dtm": "link_row_lud_dtm"})
    lk["link_usable_time"] = _to_hk_naive_datetime64_ns(lk["link_usable_time"])
    lk["link_row_lud_dtm"] = _to_hk_naive_datetime64_ns(lk["link_row_lud_dtm"])
    work = out.assign(_pit_row=np.arange(len(out), dtype=np.int64))
    work = work.sort_values(
        ["payout_complete_dtm", "player_id", "bet_id"],
        ascending=True,
        kind="stable",
    )
    lk2 = lk.sort_values(
        ["link_usable_time", "player_id", "link_row_lud_dtm"],
        ascending=True,
        kind="stable",
    )
    merged = pd.merge_asof(
        work,
        lk2,
        left_on="payout_complete_dtm",
        right_on="link_usable_time",
        by="player_id",
        direction="backward",
        allow_exact_matches=True,
    )
    merged = merged.sort_values("_pit_row", kind="stable")
    cpid = merged["casino_player_id"]
    canonical = cpid.where(cpid.notna(), merged["player_id"].astype(str)).astype(str)
    out["canonical_id"] = canonical.to_numpy()
    out["_pit_rated"] = cpid.notna().to_numpy()
    return out


# ---------------------------------------------------------------------------
# Public API — links + dummy (DuckDB path; PLAN Canonical mapping Step 3)
# ---------------------------------------------------------------------------

def build_canonical_mapping_from_links(
    links_df: pd.DataFrame,
    dummy_pids: Set,
) -> pd.DataFrame:
    """Build player_id → canonical_id mapping from pre-computed links and FND-12 dummies.

    Used by the DuckDB canonical-mapping path (PLAN Step 3): DuckDB produces
    links (player_id, casino_player_id, lud_dtm) and dummy_pids; this function
    applies FND-03 cleaning and M:N resolution.

    Parameters
    ----------
    links_df : DataFrame
        Must have columns [player_id, casino_player_id, lud_dtm]. May contain
        rows with null/invalid casino_player_id; those are dropped before M:N.
    dummy_pids : set
        FND-12 dummy/fake-account player_ids to exclude from the result.

    Returns
    -------
    DataFrame with columns [player_id, canonical_id] (str).
    """
    required = {"player_id", "casino_player_id", "lud_dtm"}
    missing = required - set(links_df.columns)
    if missing:
        raise ValueError(f"links_df is missing required columns: {sorted(missing)}")
    if links_df.empty:
        return pd.DataFrame(columns=["player_id", "canonical_id"])
    # A04: single copy of rated rows/cols only (avoid full links_df.copy()).
    rated = links_df.loc[
        links_df["casino_player_id"].notna(), ["player_id", "casino_player_id", "lud_dtm"]
    ].copy()
    rated["casino_player_id"] = _clean_casino_player_id(rated["casino_player_id"])
    rated = rated[rated["casino_player_id"].notna()]
    return _apply_mn_resolution(rated, dummy_pids)


# ---------------------------------------------------------------------------
# Public API — offline / Parquet path
# ---------------------------------------------------------------------------

def build_canonical_mapping_from_df(
    sessions_df: pd.DataFrame,
    cutoff_dtm: datetime,
) -> pd.DataFrame:
    """Build player_id → canonical_id mapping from a sessions DataFrame.

    This is the pure-pandas path for offline training and tests.
    Applies FND-01 dedup, all DQ filters, FND-12 exclusion, and D2 M:N
    resolution entirely in-process without a ClickHouse connection.

    Parameters
    ----------
    sessions_df : DataFrame
        Raw (or pre-fetched) t_session rows.  Must include columns:
        session_id, lud_dtm, __etl_insert_Dtm (optional), player_id,
        casino_player_id, session_end_dtm, is_manual, is_deleted,
        is_canceled, num_games_with_wager, turnover.
    cutoff_dtm : datetime
        Training window end.  Only sessions with
        COALESCE(session_end_dtm, lud_dtm) <= cutoff_dtm are used (B1).

    Returns
    -------
    DataFrame with columns [player_id, canonical_id] (str).
    """
    # R11: validate required columns upfront for a clear error message
    missing = _REQUIRED_SESSION_COLS - set(sessions_df.columns)
    if missing:
        raise ValueError(
            f"sessions_df is missing required columns: {sorted(missing)}"
        )

    # Step 1 — FND-01 dedup
    deduped = _fnd01_dedup_pandas(sessions_df)

    # Step 2 — DQ filters (mirrors WHERE clause in SQL template)
    session_time = deduped["session_end_dtm"].fillna(deduped["lud_dtm"])
    session_time = pd.to_datetime(session_time, errors="coerce")
    cutoff_ts = pd.Timestamp(cutoff_dtm)
    # Bidirectional tz alignment (R1203): align cutoff_ts to match session_time tz.
    if hasattr(session_time, "dt"):
        col_tz = session_time.dt.tz
        if col_tz is not None and cutoff_ts.tz is None:
            # tz-aware column, tz-naive cutoff → localize cutoff to column tz
            cutoff_ts = cutoff_ts.tz_localize(col_tz)
        elif col_tz is None and cutoff_ts.tz is not None:
            # tz-naive column, tz-aware cutoff → strip cutoff tz
            cutoff_ts = cutoff_ts.replace(tzinfo=None)

    # FND-04: exclude ghost sessions with no real wager activity (SSOT §5)
    # R1301: coerce turnover to numeric to avoid TypeError on object dtype (e.g. string)
    _turnover = pd.to_numeric(
        deduped.get("turnover", pd.Series(0.0, index=deduped.index)),
        errors="coerce",
    ).fillna(0)
    _games = deduped["num_games_with_wager"].fillna(0)
    mask = (
        (deduped["is_manual"] == 0)
        & (deduped["is_deleted"] == 0)
        & (deduped["is_canceled"] == 0)
        & deduped["player_id"].notna()
        & (deduped["player_id"] != PLACEHOLDER_PLAYER_ID)
        & (session_time <= cutoff_ts)
        & ((_turnover > 0) | (_games > 0))
    )
    filtered = deduped.loc[mask]

    # Step 3 — FND-12 fake-account exclusion
    dummy_pids = _identify_dummy_player_ids(filtered)
    logger.info("FND-12: identified %d dummy player_id(s)", len(dummy_pids))

    # Step 4 — FND-03 casino_player_id cleaning
    cleaned_casino_player_id = _clean_casino_player_id(filtered["casino_player_id"])

    # Step 5 — extract links (rated players only: casino_player_id not null).
    # Copy only the surviving rated rows instead of the full DQ-filtered session set.
    rated_mask = cleaned_casino_player_id.notna()
    links_df = filtered.loc[
        rated_mask,
        ["player_id", "casino_player_id", "lud_dtm"],
    ].copy()
    links_df["casino_player_id"] = cleaned_casino_player_id.loc[rated_mask].to_numpy()

    # Step 6 — M:N resolution
    return _apply_mn_resolution(links_df, dummy_pids)


def build_rated_eligible_player_ids_df(
    sessions_df: pd.DataFrame,
    cutoff_dtm: datetime,
) -> pd.DataFrame:
    """Return distinct rated ``player_id`` values for layered LDA preprocess (BET-DQ-03).

    Single shared entry point for **rated-only** ``player_id`` allow-lists: delegates
    to :func:`build_canonical_mapping_from_df` (same FND-01 / FND-03 / FND-04 /
    FND-12 / D2 semantics as training). Use this when wiring
    ``pipelines.layered_data_assets`` into ``trainer``; for standalone LDA runs,
    write the returned frame to Parquet and pass
    ``preprocess_bet_v1 --eligible-player-ids-parquet``.

    Parameters
    ----------
    sessions_df
        Same schema as required by :func:`build_canonical_mapping_from_df`.
    cutoff_dtm
        Mapping cutoff (B1); passed through unchanged.

    Returns
    -------
    pd.DataFrame
        One column ``player_id`` (``int64``), one row per distinct rated player.
    """
    mapping = build_canonical_mapping_from_df(sessions_df, cutoff_dtm)
    if mapping.empty:
        return pd.DataFrame({"player_id": pd.Series([], dtype="int64")})
    out = mapping[["player_id"]].drop_duplicates().reset_index(drop=True)
    out["player_id"] = pd.to_numeric(out["player_id"], errors="coerce").dropna().astype("int64")
    return out


# ---------------------------------------------------------------------------
# Public API — ClickHouse path
# ---------------------------------------------------------------------------

def get_dummy_player_ids(client, cutoff_dtm: datetime) -> Set:
    """Return the set of player_ids that are FND-12 dummy/fake accounts (ClickHouse).

    Use this in the trainer to drop dummy rows from training data (TRN-04).
    """
    dummy_sql = _build_dummy_sql(cutoff_dtm)
    dummy_df = client.query_df(dummy_sql)
    out: Set = set()
    for x in dummy_df["player_id"].dropna():
        try:
            out.add(int(x))
        except (ValueError, TypeError):
            continue
    return out


def get_dummy_player_ids_from_df(sessions_df: pd.DataFrame, cutoff_dtm: datetime) -> Set:
    """Return the set of player_ids that are FND-12 dummy/fake accounts (pandas).

    Use this in the trainer when using --use-local-parquet to drop dummy rows (TRN-04).
    """
    deduped = _fnd01_dedup_pandas(sessions_df)
    session_time = deduped["session_end_dtm"].fillna(deduped["lud_dtm"])
    session_time = pd.to_datetime(session_time, errors="coerce")
    cutoff_ts = pd.Timestamp(cutoff_dtm)
    # R1302: bidirectional tz alignment (parity with build_canonical_mapping_from_df)
    if hasattr(session_time, "dt"):
        col_tz = session_time.dt.tz
        if col_tz is not None and cutoff_ts.tz is None:
            cutoff_ts = cutoff_ts.tz_localize(col_tz)
        elif col_tz is None and cutoff_ts.tz is not None:
            cutoff_ts = cutoff_ts.replace(tzinfo=None)
    # FND-04: exclude ghost sessions (SSOT §5)
    # R1301: coerce turnover to numeric to avoid TypeError on object dtype
    _turnover = pd.to_numeric(
        deduped.get("turnover", pd.Series(0.0, index=deduped.index)),
        errors="coerce",
    ).fillna(0)
    _games = deduped["num_games_with_wager"].fillna(0)
    mask = (
        (deduped["is_manual"] == 0)
        & (deduped["is_deleted"] == 0)
        & (deduped["is_canceled"] == 0)
        & deduped["player_id"].notna()
        & (deduped["player_id"] != PLACEHOLDER_PLAYER_ID)
        & (session_time <= cutoff_ts)
        & ((_turnover > 0) | (_games > 0))
    )
    filtered = deduped.loc[mask]
    return _identify_dummy_player_ids(filtered)


def build_canonical_mapping(client, cutoff_dtm: datetime) -> pd.DataFrame:
    """Build player_id → canonical_id mapping using ClickHouse.

    Runs the two FND-01 CTE queries (links + dummy detection), then calls
    the same ``_apply_mn_resolution`` as the offline path.

    Parameters
    ----------
    client : clickhouse_connect client (from db_conn.get_clickhouse_client())
    cutoff_dtm : datetime — training window end (B1 leakage prevention)

    Returns
    -------
    DataFrame with columns [player_id, canonical_id] (str).
    """
    links_sql = _build_links_sql(cutoff_dtm)
    dummy_sql = _build_dummy_sql(cutoff_dtm)

    logger.info("identity: fetching rated links (cutoff=%s)", cutoff_dtm)
    links_df = client.query_df(links_sql)

    logger.info("identity: fetching dummy player_ids")
    dummy_df = client.query_df(dummy_sql)
    dummy_pids = set(dummy_df["player_id"].tolist())
    logger.info("FND-12: identified %d dummy player_id(s) via SQL", len(dummy_pids))

    return _apply_mn_resolution(links_df, dummy_pids)


# ---------------------------------------------------------------------------
# Public API — online scoring path
# ---------------------------------------------------------------------------

def resolve_canonical_id(
    player_id,
    session_id: Optional[str],
    mapping_df: pd.DataFrame,
    session_lookup: Optional[Callable[[str], Optional[dict]]],
    obs_time: Optional[datetime] = None,
) -> Optional[str]:
    """Three-step D2 identity resolution for online scoring (SSOT §6.4).

    Parameters
    ----------
    player_id : int or None
        The player_id from t_bet.  May be None / PLACEHOLDER_PLAYER_ID.
    session_id : str or None
        The session_id from the current bet.
    mapping_df : DataFrame[player_id, canonical_id]
        Pre-built mapping from ``build_canonical_mapping*``.
    session_lookup : callable or None
        ``session_id → dict | None`` where dict has keys
        ``casino_player_id`` and ``session_avail_dtm`` (datetime).
        If None, step 1 is skipped.
    obs_time : datetime or None
        Current observation time (used for available-time gate — H2).
        Defaults to ``datetime.utcnow()`` if not provided.

    Returns
    -------
    canonical_id as a string.

    Resolution order
    ----------------
    1. Current session: if ``session_lookup`` resolves ``session_id`` to a
       record that has a valid ``casino_player_id`` AND
       ``session_avail_dtm <= obs_time - SESSION_AVAIL_DELAY_MIN``,
       return that casino_player_id (H2 available-time gate).
       The subtraction (R9 fix) ensures the session ended *at least*
       SESSION_AVAIL_DELAY_MIN minutes ago, i.e. ClickHouse has had time
       to ingest and replicate the data.
    2. Mapping cache: look up ``player_id`` in ``mapping_df``.
       Supports both unindexed DataFrames (column-scan) and DataFrames
       indexed by ``player_id`` (O(1) ``.at[]`` lookup — R10 fix).
    3. Fallback: ``str(player_id)`` (non-rated).
    """
    now = obs_time or datetime.now(_HK_TZ).replace(tzinfo=None)

    # Step 1 — current session card (with available-time gate, H2)
    if session_id and session_lookup is not None:
        session_rec = session_lookup(session_id)
        if session_rec:
            avail_dtm = session_rec.get("session_avail_dtm")
            cpid = session_rec.get("casino_player_id")
            # R9 fix: use MINUS so that only sessions that ended at least
            # SESSION_AVAIL_DELAY_MIN minutes ago are considered available.
            if (
                cpid
                and avail_dtm is not None
                and avail_dtm <= now - timedelta(minutes=SESSION_AVAIL_DELAY_MIN)
            ):
                return str(cpid)

    # Step 2 — mapping cache (R10 fix: support indexed mapping_df)
    if player_id is not None and player_id != PLACEHOLDER_PLAYER_ID:
        if mapping_df.index.name == "player_id":
            # O(1) lookup when mapping_df is pre-indexed for batch scoring
            try:
                return str(mapping_df.at[player_id, "canonical_id"])
            except KeyError:
                pass
        else:
            rows = mapping_df.loc[
                mapping_df["player_id"] == player_id, "canonical_id"
            ]
            if not rows.empty:
                return str(rows.iloc[0])

    # Step 3 — fallback: treat as non-rated using raw player_id
    if player_id is not None and player_id != PLACEHOLDER_PLAYER_ID:
        return str(player_id)
    return None  # no usable identity (player_id is null or placeholder)
