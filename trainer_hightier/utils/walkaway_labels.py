"""Materialize walkaway labels for cleaned bets (parity with ``trainer.labels``).

Only bets present in the cleaned ``t_bet`` Parquet are considered. The successor
of a bet is the **next cleaned bet** with the same ``canonical_id`` (after G3 sort);
this matches passing a ``bets_df`` that already excludes non–high-tier rows.

**Observation boundary (H1):** By default ``window_end`` and ``extended_end`` are
both set to ``MAX(payout_complete_dtm)`` over the joined frame. Terminal bets whose
``payout_complete_dtm + WALKAWAY_GAP_MIN`` exceeds that boundary are ``censored``.
Override ``extended_end`` if your ingest truly allows observing silence beyond the
last bet timestamp.

**RAM:** This loads the joined ``(bet_id, canonical_id, payout_complete_dtm)``
frame into pandas. ~30M rows may need tens of GB; use a workstation profile or
chunk the pipeline if you hit OOM.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow.parquet as pq
from zoneinfo import ZoneInfo

from trainer_hightier.config import (
    DEFAULT_WALKAWAY_LABEL_CONTRACT,
    DuckDbRuntimeConfig,
    HK_TZ as HK_TZ_STR,
    WALKAWAY_GAP_MIN,
    WalkawayLabelContract,
)
from trainer_hightier.utils.canonical_mapping import default_canonical_mapping_parquet_path
from trainer_hightier.walkaway_compute_labels import compute_labels
from trainer_hightier.utils.bet_l0_preprocess import (
    cleaned_bet_dataset_has_any_parquet,
    first_parquet_under_for_schema,
    resolved_cleaned_bet_read_parquet_sql,
)
from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas

logger = logging.getLogger(__name__)

HK_TZ = ZoneInfo(HK_TZ_STR)


def _path_posix(path: Path) -> str:
    return str(Path(path).resolve()).replace("\\", "/")


def default_cleaned_bet_parquet_path(*, repo_root: Path | None = None) -> Path:
    """Default cleaned bet input path (same default as Feast)."""
    base = Path(__file__).resolve().parents[2] if repo_root is None else repo_root
    return (base / "trainer_hightier" / "artifacts" / "cleaned" / "cleaned__gmwds_t_bet").resolve()


def default_walkaway_labels_parquet_path(
    *,
    repo_root: Path | None = None,
    label_contract: WalkawayLabelContract | None = None,
) -> Path:
    """Default output under ``artifacts/labels/``; non-default gaps use ``walkaway_labels_gap{N}.parquet``."""
    base = Path(__file__).resolve().parents[2] if repo_root is None else repo_root
    contract = label_contract or DEFAULT_WALKAWAY_LABEL_CONTRACT
    out_dir = base / "trainer_hightier" / "artifacts" / "labels"
    if int(contract.walkaway_gap_min) == int(WALKAWAY_GAP_MIN):
        basename = "walkaway_labels.parquet"
    else:
        basename = f"walkaway_labels_gap{int(contract.walkaway_gap_min)}.parquet"
    return (out_dir / basename).resolve()


def build_joined_bets_sql(
    cleaned_bet_parquet: Path,
    canonical_mapping_parquet: Path,
) -> str:
    """Return DuckDB SQL for cleaned bets inner-joined to canonical mapping."""
    src_bet = Path(cleaned_bet_parquet).resolve()
    src_map = Path(canonical_mapping_parquet).resolve()
    bet_from = resolved_cleaned_bet_read_parquet_sql(src_bet)
    map_esc = _path_posix(src_map).replace("'", "''")
    return f"""
WITH cleaned AS (
  SELECT
    TRY_CAST(bet_id AS DOUBLE) AS bet_id,
    TRY_CAST(player_id AS BIGINT) AS player_id,
    CAST(payout_complete_dtm AS TIMESTAMPTZ) AS payout_complete_dtm
  FROM {bet_from} AS _cbet
),
map_dedup AS (
  SELECT
    player_id,
    ANY_VALUE(TRIM(CAST(canonical_id AS VARCHAR))) AS canonical_id
  FROM read_parquet('{map_esc}')
  WHERE TRY_CAST(player_id AS BIGINT) IS NOT NULL
  GROUP BY player_id
),
joined AS (
  SELECT
    c.bet_id,
    c.payout_complete_dtm,
    m.canonical_id
  FROM cleaned c
  INNER JOIN map_dedup m ON c.player_id = m.player_id
  WHERE c.bet_id IS NOT NULL
    AND c.payout_complete_dtm IS NOT NULL
    AND m.canonical_id IS NOT NULL
    AND TRIM(m.canonical_id) <> ''
)
SELECT bet_id, canonical_id, payout_complete_dtm FROM joined
""".strip()


def load_joined_bets_dataframe(
    cleaned_bet_parquet: Path,
    canonical_mapping_parquet: Path,
    *,
    duckdb_runtime: DuckDbRuntimeConfig | None = None,
    payout_yyyymm: str | None = None,
    canonical_shard: int | None = None,
    canonical_shard_count: int | None = None,
) -> pd.DataFrame:
    """Load joined bet rows; optional payout month and canonical shard filters."""
    src_bet = Path(cleaned_bet_parquet).resolve()
    src_map = Path(canonical_mapping_parquet).resolve()
    if not (src_bet.is_file() or cleaned_bet_dataset_has_any_parquet(src_bet)):
        raise FileNotFoundError(f"cleaned bet parquet not found: {src_bet}")
    if not src_map.is_file():
        raise FileNotFoundError(f"canonical mapping parquet not found: {src_map}")
    sql = build_joined_bets_sql(src_bet, src_map)
    filters: list[str] = []
    if payout_yyyymm is not None:
        ym = str(payout_yyyymm).strip()
        if len(ym) != 6 or not ym.isdigit():
            raise ValueError(f"payout_yyyymm must be six digits, got {payout_yyyymm!r}")
        filters.append(
            f"strftime(CAST(payout_complete_dtm AS TIMESTAMPTZ), '%Y%m') = '{ym}'",
        )
    if canonical_shard is not None:
        if canonical_shard_count is None or int(canonical_shard_count) < 1:
            raise ValueError("canonical_shard_count must be >= 1 when canonical_shard is set")
        shard = int(canonical_shard)
        count = int(canonical_shard_count)
        if shard < 0 or shard >= count:
            raise ValueError(f"canonical_shard must be in [0, {count}), got {shard!r}")
        filters.append(
            f"(abs(hash(CAST(canonical_id AS VARCHAR))) % {count}) = {shard}",
        )
    if filters:
        sql = f"SELECT * FROM ({sql}) AS _j WHERE {' AND '.join(filters)}"
    con = duckdb.connect(database=":memory:")
    try:
        if duckdb_runtime is not None:
            apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        df = con.execute(sql).df()
    finally:
        con.close()
    if df.empty:
        return df
    pcd_series = df["payout_complete_dtm"]
    if pcd_series.dt.tz is not None:
        df = df.copy()
        df["payout_complete_dtm"] = pcd_series.dt.tz_convert(HK_TZ).dt.tz_localize(None)
    return df


def list_joined_bet_payout_months(
    cleaned_bet_parquet: Path,
    canonical_mapping_parquet: Path,
    *,
    duckdb_runtime: DuckDbRuntimeConfig | None = None,
) -> tuple[str, ...]:
    """List distinct payout ``YYYYMM`` months on joined cleaned bets."""
    base = build_joined_bets_sql(cleaned_bet_parquet, canonical_mapping_parquet)
    con = duckdb.connect(database=":memory:")
    try:
        if duckdb_runtime is not None:
            apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        rows = con.execute(
            f"""
            SELECT DISTINCT strftime(CAST(payout_complete_dtm AS TIMESTAMPTZ), '%Y%m') AS yyyymm
            FROM ({base}) AS _j
            WHERE payout_complete_dtm IS NOT NULL
            ORDER BY 1
            """,
        ).fetchall()
    finally:
        con.close()
    return tuple(str(r[0]) for r in rows if r and r[0] is not None)


def write_walkaway_labels_from_joined_dataframe(
    joined: pd.DataFrame,
    out_parquet: Path,
    *,
    window_end: datetime | pd.Timestamp | None = None,
    extended_end: datetime | pd.Timestamp | None = None,
    label_contract: WalkawayLabelContract | None = None,
) -> Path:
    """Run :func:`~trainer_hightier.walkaway_compute_labels.compute_labels` and write Parquet."""
    dst = Path(out_parquet).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    df = joined
    if df.empty:
        empty = pd.DataFrame(
            {
                "bet_id": pd.Series(dtype="float64"),
                "canonical_id": pd.Series(dtype="string"),
                "payout_complete_dtm": pd.Series(dtype="datetime64[ns]"),
                "label": pd.Series(dtype="int8"),
                "censored": pd.Series(dtype=bool),
            }
        )
        empty.to_parquet(dst, index=False)
        return dst
    max_pcd = pd.Timestamp(df["payout_complete_dtm"].max())
    we = max_pcd if window_end is None else pd.Timestamp(window_end)
    ee = we if extended_end is None else pd.Timestamp(extended_end)
    labeled = compute_labels(df, window_end=we, extended_end=ee, label_contract=label_contract)
    out = labeled[["bet_id", "canonical_id", "payout_complete_dtm", "label", "censored"]]
    out.to_parquet(dst, index=False)
    return dst


def materialize_walkaway_labels_from_cleaned_bet(
    cleaned_bet_parquet: Path | None = None,
    canonical_mapping_parquet: Path | None = None,
    out_parquet: Path | None = None,
    window_end: datetime | pd.Timestamp | None = None,
    extended_end: datetime | pd.Timestamp | None = None,
    duckdb_runtime: DuckDbRuntimeConfig | None = None,
    label_contract: WalkawayLabelContract | None = None,
) -> Path:
    """Join cleaned bets to ``canonical_id``, then run :func:`~trainer_hightier.walkaway_compute_labels.compute_labels`.

    Args:
        cleaned_bet_parquet: Cleaned bet Parquet (must include ``bet_id``, ``player_id``,
            ``payout_complete_dtm``).
        canonical_mapping_parquet: ``player_id`` / ``canonical_id`` Parquet.
        out_parquet: Output path; default under ``artifacts/labels/``.
        window_end: Training window end for label semantics; default ``MAX(payout)``.
        extended_end: C1 extended end for H1; default same as ``window_end``.
        duckdb_runtime: Optional DuckDB PRAGMAs for the join step.

    Returns:
        Resolved path to the written Parquet.

    Raises:
        FileNotFoundError: If inputs are missing.
        ValueError: If required columns are absent from the cleaned bet schema.
    """
    src_bet = Path(cleaned_bet_parquet or default_cleaned_bet_parquet_path()).resolve()
    if not (src_bet.is_file() or cleaned_bet_dataset_has_any_parquet(src_bet)):
        raise FileNotFoundError(f"cleaned bet parquet not found: {src_bet}")
    src_map = Path(canonical_mapping_parquet or default_canonical_mapping_parquet_path()).resolve()
    if not src_map.is_file():
        raise FileNotFoundError(f"canonical mapping parquet not found: {src_map}")
    dst = Path(out_parquet or default_walkaway_labels_parquet_path()).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)

    need_bet = ("bet_id", "player_id", "payout_complete_dtm")
    cols = set(pq.read_schema(first_parquet_under_for_schema(src_bet)).names)
    missing = tuple(c for c in need_bet if c not in cols)
    if missing:
        raise ValueError(f"cleaned bet missing columns {list(missing)}; got {sorted(cols)}")

    sql = build_joined_bets_sql(src_bet, src_map)
    bet_from = resolved_cleaned_bet_read_parquet_sql(src_bet)
    con = duckdb.connect(database=":memory:")
    try:
        if duckdb_runtime is not None:
            apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        n_matched = con.execute(f"SELECT COUNT(*) FROM ({sql}) AS _j").fetchone()[0]
        n_clean = con.execute(
            f"""
            SELECT COUNT(*) FROM {bet_from} AS _q
            WHERE TRY_CAST(bet_id AS DOUBLE) IS NOT NULL
              AND TRY_CAST(player_id AS BIGINT) IS NOT NULL
              AND payout_complete_dtm IS NOT NULL
            """,
        ).fetchone()[0]
    finally:
        con.close()

    if n_matched < n_clean:
        logger.warning(
            "walkaway labels: %d cleaned bets with non-null bet_id join to mapping; %d total cleaned rows — "
            "%d rows dropped (no mapping / null pcd / null bet_id)",
            int(n_matched),
            int(n_clean),
            int(n_clean - n_matched),
        )

    df = load_joined_bets_dataframe(
        src_bet,
        src_map,
        duckdb_runtime=duckdb_runtime,
    )
    if df.empty:
        write_walkaway_labels_from_joined_dataframe(df, dst)
        logger.warning("walkaway labels: no rows after join; wrote empty parquet to %s", dst)
        return dst

    write_walkaway_labels_from_joined_dataframe(
        df,
        dst,
        window_end=window_end,
        extended_end=extended_end,
        label_contract=label_contract,
    )
    logger.info("walkaway labels: rows=%d written %s", int(len(df)), dst)
    return dst
