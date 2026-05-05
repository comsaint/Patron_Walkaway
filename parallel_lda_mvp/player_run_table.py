"""Lite CLI: print run-level stats for one player from ``data/parallel_lda_mvp/<snap>/`` outputs.

Joins ``run_fact``, ``trip_run_map``, ``trip_fact``, and optional daily ``t_bet/cleaned__*.parquet``.
Trip bounds use ``trip_fact`` **gaming_day** dates (``trip_start_date`` / ``trip_end_date``); timestamp
columns are not populated in MVP ``trip_fact`` for trip end.
Main vs side bet counts use ``bet_type`` only: ``BANKER`` / ``PLAYER`` / ``TIE`` (case-insensitive
after trim) are main bets; every other value (including NULL / empty) counts as a side bet.

Bets are attributed to a run by ``payout_complete_dtm`` in ``[run_start_ts, run_end_ts]`` (same
semantics as gap-based run windows for typical sequences).

Usage (repo root)::

    python -m parallel_lda_mvp.player_run_table 141682932
    python -m parallel_lda_mvp.player_run_table 141682932 --snap-root data/parallel_lda_mvp/snap_mvp_xxx
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

import duckdb
import pandas as pd


def _repo_root() -> Path:
    """Return repository root (parent of ``parallel_lda_mvp``)."""
    return Path(__file__).resolve().parent.parent


def _sql_parquet_list(paths: Sequence[Path]) -> str:
    """Build ``read_parquet`` file list literal with SQL-escaped paths."""
    parts: list[str] = []
    for p in paths:
        s = p.resolve().as_posix().replace("'", "''")
        parts.append(f"'{s}'")
    return ", ".join(parts)


def _read_parquet_union(paths: Sequence[Path]) -> str:
    """``read_parquet([...], union_by_name=true)`` for mixed schemas across MVP shards."""
    lst = _sql_parquet_list(paths)
    return f"read_parquet([{lst}], union_by_name=true)"


def _default_parallel_lda_root() -> Path:
    """Return ``<repo>/data/parallel_lda_mvp``."""
    return _repo_root() / "data" / "parallel_lda_mvp"


def resolve_snap_root(*, parallel_lda_root: Path, snap_root: Path | None) -> Path:
    """Resolve MVP snapshot directory under ``parallel_lda_mvp``.

    Args:
        parallel_lda_root: Directory that contains ``snap_*`` month trees.
        snap_root: Explicit ``.../snap_mvp_<id>`` or ``parallel_lda_mvp``; if ``None``, infer.

    Returns:
        Absolute path to ``snap_mvp_*`` (child of ``parallel_lda_root`` when inferred).

    Raises:
        FileNotFoundError: If no usable snapshot directory exists.
        ValueError: If multiple snapshots exist and ``snap_root`` was not given.
    """
    if snap_root is not None:
        p = snap_root.expanduser().resolve()
        if not p.is_dir():
            raise FileNotFoundError(f"snap-root is not a directory: {p}")
        if p.name.startswith("snap_"):
            return p
        # Treat as ``parallel_lda_mvp`` parent: require a single ``snap_*`` child.
        snaps = sorted(x for x in p.iterdir() if x.is_dir() and x.name.startswith("snap_"))
        if len(snaps) == 1:
            return snaps[0]
        if not snaps:
            raise FileNotFoundError(f"No snap_* under {p}")
        raise ValueError(
            f"Multiple snapshots under {p}: {', '.join(s.name for s in snaps[:5])}"
            f"{'…' if len(snaps) > 5 else ''}; pass --snap-root explicitly."
        )

    if not parallel_lda_root.is_dir():
        raise FileNotFoundError(f"parallel_lda_mvp output root missing: {parallel_lda_root}")
    snaps = sorted(x for x in parallel_lda_root.iterdir() if x.is_dir() and x.name.startswith("snap_"))
    if not snaps:
        raise FileNotFoundError(f"No snap_* directories under {parallel_lda_root}")
    if len(snaps) > 1:
        raise ValueError(
            f"Multiple snapshots under {parallel_lda_root}; pass --snap-root. Found: "
            + ", ".join(s.name for s in snaps[:8])
            + ("…" if len(snaps) > 8 else "")
        )
    return snaps[0]


def collect_mvp_parquet_paths(snap_root: Path) -> tuple[list[Path], list[Path], list[Path], list[Path]]:
    """Collect run_fact, trip_run_map, trip_fact, and daily cleaned bet paths under ``snap_root``."""
    run_paths = sorted(snap_root.glob("gaming_ym=*/run_fact/run_fact__*.parquet"))
    map_paths = sorted(snap_root.glob("gaming_ym=*/trip_run_map/trip_run_map__*.parquet"))
    trip_paths = sorted(snap_root.glob("gaming_ym=*/trip_fact/trip_fact__*.parquet"))
    bet_paths = sorted(snap_root.glob("gaming_ym=*/t_bet/cleaned__*.parquet"))
    return run_paths, map_paths, trip_paths, bet_paths


def _build_query(
    *,
    run_paths: Sequence[Path],
    map_paths: Sequence[Path],
    trip_paths: Sequence[Path],
    bet_paths: Sequence[Path],
) -> str:
    """Return DuckDB SQL text; caller binds ``player_id`` as single positional param."""
    if not run_paths:
        raise ValueError("No run_fact parquet files found under snap-root.")
    if not map_paths or not trip_paths:
        raise ValueError("trip_run_map and/or trip_fact parquet files missing under snap-root.")

    rp = _read_parquet_union(run_paths)
    mp = _read_parquet_union(map_paths)
    tp = _read_parquet_union(trip_paths)
    has_bets = bool(bet_paths)
    bp = _read_parquet_union(bet_paths) if has_bets else ""

    main_side_sql = ""
    if has_bets:
        main_side_sql = f"""
, b AS (
  SELECT
    bet_id,
    player_id,
    payout_complete_dtm,
    wager,
    theo_win,
    bet_type
  FROM {bp}
  WHERE player_id = ?
),
b_agg AS (
  SELECT
    r.run_id,
    SUM(COALESCE(CAST(b.wager AS DOUBLE), 0.0)) AS total_wager,
    SUM(COALESCE(CAST(b.theo_win AS DOUBLE), 0.0)) AS total_theo,
    SUM(
      CASE
        WHEN UPPER(TRIM(COALESCE(CAST(b.bet_type AS VARCHAR), ''))) IN ('BANKER', 'PLAYER', 'TIE') THEN 1
        ELSE 0
      END
    )::BIGINT AS total_main_bets,
    SUM(
      CASE
        WHEN UPPER(TRIM(COALESCE(CAST(b.bet_type AS VARCHAR), ''))) IN ('BANKER', 'PLAYER', 'TIE') THEN 0
        ELSE 1
      END
    )::BIGINT AS total_side_bets
  FROM b
  INNER JOIN rf r
    ON b.player_id = r.player_id
   AND b.payout_complete_dtm >= r.run_start_ts
   AND b.payout_complete_dtm <= r.run_end_ts
  GROUP BY r.run_id
)
"""

    join_bet = "LEFT JOIN b_agg ba ON rf.run_id = ba.run_id" if has_bets else ""
    select_main_side = (
        "COALESCE(ba.total_main_bets, 0::BIGINT) AS total_main_bets,\n"
        "    COALESCE(ba.total_side_bets, 0::BIGINT) AS total_side_bets,"
        if has_bets
        else "NULL::BIGINT AS total_main_bets,\n    NULL::BIGINT AS total_side_bets,"
    )
    select_wager_theo = (
        "COALESCE(ba.total_wager, 0.0) AS total_wager,\n"
        "    COALESCE(ba.total_theo, 0.0) AS total_theo,"
        if has_bets
        else "NULL::DOUBLE AS total_wager,\n    NULL::DOUBLE AS total_theo,"
    )

    return f"""
WITH rf AS (
  SELECT
    CAST(run_id AS VARCHAR) AS run_id,
    player_id,
    run_start_ts,
    run_end_ts,
    bet_count
  FROM {rp}
  WHERE player_id = ?
),
trm AS (
  SELECT
    CAST(run_id AS VARCHAR) AS run_id,
    player_id,
    ANY_VALUE(CAST(trip_id AS VARCHAR)) AS trip_id
  FROM {mp}
  WHERE player_id = ?
  GROUP BY 1, 2
),
tf AS (
  SELECT
    CAST(trip_id AS VARCHAR) AS trip_id,
    player_id,
    MIN(trip_start_gaming_day) AS trip_start_gaming_day,
    MAX(trip_end_gaming_day) AS trip_end_gaming_day
  FROM {tp}
  WHERE player_id = ?
  GROUP BY 1, 2
){main_side_sql}
SELECT
    rf.run_id,
    trm.trip_id,
    TRY_CAST(tf.trip_start_gaming_day AS DATE) AS trip_start_date,
    TRY_CAST(tf.trip_end_gaming_day AS DATE) AS trip_end_date,
    rf.run_start_ts AS run_start_dtm,
    rf.run_end_ts AS run_end_dtm,
    rf.bet_count::BIGINT AS total_bets,
    {select_main_side}
    epoch(rf.run_end_ts) - epoch(rf.run_start_ts) AS total_duration_sec,
    {select_wager_theo}
FROM rf
LEFT JOIN trm
  ON rf.run_id = trm.run_id AND rf.player_id = trm.player_id
LEFT JOIN tf
  ON trm.trip_id = tf.trip_id AND trm.player_id = tf.player_id
{join_bet}
ORDER BY rf.run_start_ts ASC, rf.run_id ASC;
"""


def load_player_run_table(
    con: duckdb.DuckDBPyConnection,
    *,
    player_id: int,
    run_paths: Sequence[Path],
    map_paths: Sequence[Path],
    trip_paths: Sequence[Path],
    bet_paths: Sequence[Path],
) -> pd.DataFrame:
    """Load run-level table for ``player_id`` from MVP Parquet paths."""
    sql = _build_query(
        run_paths=run_paths,
        map_paths=map_paths,
        trip_paths=trip_paths,
        bet_paths=bet_paths,
    )
    has_bets = bool(bet_paths)
    params: list[int] = [player_id, player_id, player_id]
    if has_bets:
        params.append(player_id)
    return con.execute(sql, params).df()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Print run-level stats for one player (parallel_lda_mvp outputs).")
    p.add_argument("player_id", type=int, help="Numeric player_id (same as run_fact.player_id).")
    p.add_argument(
        "--snap-root",
        type=Path,
        default=None,
        help="Path to snap_mvp_* output root (defaults to sole snap under data/parallel_lda_mvp).",
    )
    p.add_argument(
        "--parallel-lda-root",
        type=Path,
        default=None,
        help="Directory containing snap_* (default: <repo>/data/parallel_lda_mvp).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry: resolve paths, query DuckDB, print table."""
    args = _parse_args(argv)
    parallel_root = (
        args.parallel_lda_root.expanduser().resolve()
        if args.parallel_lda_root is not None
        else _default_parallel_lda_root()
    )
    snap = resolve_snap_root(parallel_lda_root=parallel_root, snap_root=args.snap_root)
    run_p, map_p, trip_p, bet_p = collect_mvp_parquet_paths(snap)
    print(f"snap_root={snap}", file=sys.stderr)
    print(
        f"inputs run_fact={len(run_p)} trip_run_map={len(map_p)} trip_fact={len(trip_p)} "
        f"cleaned_day={len(bet_p)}",
        file=sys.stderr,
    )
    if len(run_p) > 400:
        print(
            "note: many shard files; DuckDB will scan all run/trip/cleaned Parquets "
            "(filter pushes to player_id but I/O can be heavy). Prefer a single snap-root.",
            file=sys.stderr,
        )
    con = duckdb.connect(database=":memory:")
    try:
        df = load_player_run_table(
            con,
            player_id=int(args.player_id),
            run_paths=run_p,
            map_paths=map_p,
            trip_paths=trip_p,
            bet_paths=bet_p,
        )
    finally:
        con.close()
    if df.empty:
        print(f"No runs for player_id={args.player_id}", file=sys.stderr)
        return 1
    with pd.option_context("display.max_columns", None, "display.width", 240, "display.max_colwidth", 36):
        print(df.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
