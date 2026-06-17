"""Player-game serving ready queue (Wave 4 dry-run).

Pending / completed state is separate from the incremental ETL cursor. Dry-run
records metrics only; it does not write production alerts or score models.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Final
from zoneinfo import ZoneInfo

import pandas as pd

from trainer_hightier.config import SCORER_POLL_INTERVAL_SECONDS, default_hightier_serving_config
from trainer_hightier.player_game_grain import (
    BET_ID_COLUMN,
    GAME_ID_COLUMN,
    PLAYER_ID_COLUMN,
    PV_COLUMN,
    compute_serving_due_ts,
)
from trainer_hightier.serving.feature_builder import attach_synthetic_etl_and_prediction_visible

logger = logging.getLogger(__name__)

PlayerGameRefetchFn = Callable[[int, int], pd.DataFrame]

_PENDING_TABLE: Final[str] = "pg_pending_player_games"
_COMPLETED_TABLE: Final[str] = "pg_completed_player_games"
_CYCLE_TABLE: Final[str] = "pg_ready_queue_dry_run_cycles"


@dataclass(frozen=True)
class PlayerGameReadyDryRunSummary:
    """Counts from one dry-run queue cycle."""

    n_enqueued: int
    n_due_processed: int
    n_deferred: int
    n_completed: int
    n_skipped_completed: int


@dataclass(frozen=True)
class PlayerGameReadyProcessResult:
    """Outcome of processing one pending player-game."""

    action: str
    player_id: int
    game_id: int
    attempt_count: int = 0
    player_game_ready_ts: pd.Timestamp | None = None


def init_player_game_ready_queue_tables(conn: sqlite3.Connection) -> None:
    """Create pending / completed / dry-run cycle tables (idempotent)."""

    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_PENDING_TABLE} (
            player_id INTEGER NOT NULL,
            game_id INTEGER NOT NULL,
            first_seen_prediction_visible_ts TEXT NOT NULL,
            due_ts TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            initial_bet_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (player_id, game_id)
        )
        """,
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_COMPLETED_TABLE} (
            player_id INTEGER NOT NULL,
            game_id INTEGER NOT NULL,
            player_game_ready_ts TEXT NOT NULL,
            dry_run_completed_at TEXT NOT NULL,
            representative_bet_id INTEGER,
            bet_count INTEGER,
            pending_age_sec REAL,
            ready_lag_sec REAL,
            late_after_score_hypothetical INTEGER NOT NULL DEFAULT 0,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (player_id, game_id)
        )
        """,
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_CYCLE_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_ts TEXT NOT NULL,
            n_enqueued INTEGER NOT NULL,
            n_due_processed INTEGER NOT NULL,
            n_deferred INTEGER NOT NULL,
            n_completed INTEGER NOT NULL,
            n_skipped_completed INTEGER NOT NULL
        )
        """,
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_pg_pending_due ON {_PENDING_TABLE}(due_ts)",
    )


def _ts_iso(ts: pd.Timestamp | datetime) -> str:
    """Serialize one timestamp for SQLite storage."""

    parsed = pd.Timestamp(ts)
    if parsed.tzinfo is None:
        parsed = parsed.tz_localize("UTC")
    return parsed.isoformat()


def _parse_ts(raw: str | None) -> pd.Timestamp:
    """Parse one stored ISO timestamp."""

    if not raw:
        raise ValueError("expected non-empty timestamp string")
    out = pd.to_datetime(raw, errors="coerce", utc=True)
    if pd.isna(out):
        raise ValueError(f"invalid timestamp string: {raw!r}")
    return pd.Timestamp(out)


def _completed_keys(conn: sqlite3.Connection) -> frozenset[tuple[int, int]]:
    """Return all completed player-game keys."""

    rows = conn.execute(
        f"SELECT player_id, game_id FROM {_COMPLETED_TABLE}",
    ).fetchall()
    return frozenset((int(r[0]), int(r[1])) for r in rows)


def _prepare_bets_with_pv(bets: pd.DataFrame) -> pd.DataFrame:
    """Ensure player-game keys and prediction visibility timestamps exist."""

    if bets.empty:
        return bets.copy()
    work = bets.copy()
    if PV_COLUMN not in work.columns:
        work = attach_synthetic_etl_and_prediction_visible(work)
    work[PLAYER_ID_COLUMN] = pd.to_numeric(work[PLAYER_ID_COLUMN], errors="coerce").astype("Int64")
    work[GAME_ID_COLUMN] = pd.to_numeric(work[GAME_ID_COLUMN], errors="coerce").astype("Int64")
    return work


def enqueue_player_games_from_bets(
    conn: sqlite3.Connection,
    bets: pd.DataFrame,
    *,
    now_ts: datetime,
    holdback_seconds: int = SCORER_POLL_INTERVAL_SECONDS,
) -> int:
    """Upsert pending rows from incremental bets; return number of keys touched."""

    work = _prepare_bets_with_pv(bets)
    valid = work[PLAYER_ID_COLUMN].notna() & work[GAME_ID_COLUMN].notna() & work[PV_COLUMN].notna()
    work = work.loc[valid]
    if work.empty:
        return 0
    completed = _completed_keys(conn)
    now_iso = _ts_iso(now_ts)
    n_touch = 0
    grouped = work.groupby([PLAYER_ID_COLUMN, GAME_ID_COLUMN], dropna=True, sort=False)
    for (player_id, game_id), grp in grouped:
        pid = int(player_id)
        gid = int(game_id)
        if (pid, gid) in completed:
            continue
        first_seen = pd.to_datetime(grp[PV_COLUMN], errors="coerce", utc=True).min()
        if pd.isna(first_seen):
            continue
        due = compute_serving_due_ts(first_seen, holdback_seconds=holdback_seconds)
        bet_count = int(len(grp))
        row = conn.execute(
            f"""
            SELECT first_seen_prediction_visible_ts, initial_bet_count
            FROM {_PENDING_TABLE}
            WHERE player_id = ? AND game_id = ?
            """,
            (pid, gid),
        ).fetchone()
        if row is None:
            conn.execute(
                f"""
                INSERT INTO {_PENDING_TABLE} (
                    player_id, game_id,
                    first_seen_prediction_visible_ts, due_ts,
                    attempt_count, initial_bet_count,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (pid, gid, _ts_iso(first_seen), _ts_iso(due), bet_count, now_iso, now_iso),
            )
            n_touch += 1
            continue
        existing_first = _parse_ts(str(row[0]))
        merged_first = min(existing_first, pd.Timestamp(first_seen))
        merged_due = compute_serving_due_ts(merged_first, holdback_seconds=holdback_seconds)
        initial_bets = min(int(row[1]), bet_count)
        conn.execute(
            f"""
            UPDATE {_PENDING_TABLE}
            SET first_seen_prediction_visible_ts = ?,
                due_ts = ?,
                initial_bet_count = ?,
                updated_at = ?
            WHERE player_id = ? AND game_id = ?
            """,
            (_ts_iso(merged_first), _ts_iso(merged_due), initial_bets, now_iso, pid, gid),
        )
        n_touch += 1
    return n_touch


def refetch_player_game_from_frame(
    all_bets: pd.DataFrame,
    player_id: int,
    game_id: int,
) -> pd.DataFrame:
    """Filter a bet frame to one player-game (test / offline dry-run helper)."""

    work = _prepare_bets_with_pv(all_bets)
    mask = (
        pd.to_numeric(work[PLAYER_ID_COLUMN], errors="coerce") == int(player_id)
    ) & (pd.to_numeric(work[GAME_ID_COLUMN], errors="coerce") == int(game_id))
    return work.loc[mask].copy()


def fetch_player_game_bets_clickhouse(player_id: int, game_id: int) -> pd.DataFrame:
    """Re-fetch all currently eligible bets for one player-game from ClickHouse."""

    from trainer_hightier.serving.scorer import fetch_bets_incremental

    cfg = default_hightier_serving_config()
    lookback = float(cfg.player_game_ready_queue_refetch_lookback_hours)
    lim = int(cfg.hightier_scorer_max_bets_per_cycle)
    bets = fetch_bets_incremental(
        None,
        lookback_hours=lookback,
        limit_rows=lim,
        allowlist_player_ids=frozenset({int(player_id)}),
    )
    if bets.empty:
        return bets
    out = refetch_player_game_from_frame(bets, player_id, game_id)
    return out


def _representative_bet_id(bets: pd.DataFrame) -> int | None:
    """Pick audit representative bet (last by visibility, pcd, bet_id)."""

    if bets.empty or BET_ID_COLUMN not in bets.columns:
        return None
    work = bets.copy()
    work["_pv"] = pd.to_datetime(work[PV_COLUMN], errors="coerce", utc=True)
    work["_pcd"] = pd.to_datetime(work["payout_complete_dtm"], errors="coerce", utc=True)
    work["_bid"] = pd.to_numeric(work[BET_ID_COLUMN], errors="coerce").fillna(-1)
    work = work.sort_values(by=["_pv", "_pcd", "_bid"], ascending=[True, True, True])
    last = work.iloc[-1]
    bid = pd.to_numeric(last[BET_ID_COLUMN], errors="coerce")
    return int(bid) if pd.notna(bid) else None


def _list_due_pending(conn: sqlite3.Connection, now_ts: datetime) -> list[tuple[int, int]]:
    """Return pending keys whose due time has passed."""

    now_iso = _ts_iso(now_ts)
    rows = conn.execute(
        f"""
        SELECT p.player_id, p.game_id
        FROM {_PENDING_TABLE} AS p
        LEFT JOIN {_COMPLETED_TABLE} AS c
          ON p.player_id = c.player_id AND p.game_id = c.game_id
        WHERE p.due_ts <= ?
          AND c.player_id IS NULL
        ORDER BY p.due_ts ASC, p.player_id ASC, p.game_id ASC
        """,
        (now_iso,),
    ).fetchall()
    return [(int(r[0]), int(r[1])) for r in rows]


def process_one_pending_player_game(
    conn: sqlite3.Connection,
    player_id: int,
    game_id: int,
    *,
    now_ts: datetime,
    fetch_fn: PlayerGameRefetchFn,
    holdback_seconds: int = SCORER_POLL_INTERVAL_SECONDS,
) -> PlayerGameReadyProcessResult:
    """Re-fetch one pending player-game and defer or complete dry-run."""

    row = conn.execute(
        f"""
        SELECT first_seen_prediction_visible_ts, attempt_count, initial_bet_count
        FROM {_PENDING_TABLE}
        WHERE player_id = ? AND game_id = ?
        """,
        (int(player_id), int(game_id)),
    ).fetchone()
    if row is None:
        return PlayerGameReadyProcessResult("missing", int(player_id), int(game_id))
    first_seen = _parse_ts(str(row[0]))
    attempt_count = int(row[1])
    initial_bet_count = int(row[2])
    now_pd = pd.Timestamp(now_ts).tz_convert("UTC") if pd.Timestamp(now_ts).tzinfo else pd.Timestamp(now_ts, tz="UTC")

    bets = fetch_fn(int(player_id), int(game_id))
    bets = _prepare_bets_with_pv(bets)
    if bets.empty:
        return PlayerGameReadyProcessResult("empty_refetch", int(player_id), int(game_id), attempt_count)

    max_pv = pd.to_datetime(bets[PV_COLUMN], errors="coerce", utc=True).max()
    if pd.isna(max_pv):
        return PlayerGameReadyProcessResult("no_pv", int(player_id), int(game_id), attempt_count)
    if max_pv > now_pd:
        now_iso = _ts_iso(now_ts)
        conn.execute(
            f"""
            UPDATE {_PENDING_TABLE}
            SET attempt_count = attempt_count + 1,
                due_ts = ?,
                updated_at = ?
            WHERE player_id = ? AND game_id = ?
            """,
            (
                _ts_iso(compute_serving_due_ts(max_pv, holdback_seconds=holdback_seconds)),
                now_iso,
                int(player_id),
                int(game_id),
            ),
        )
        return PlayerGameReadyProcessResult(
            "deferred",
            int(player_id),
            int(game_id),
            attempt_count + 1,
            pd.Timestamp(max_pv),
        )

    rep_bet = _representative_bet_id(bets)
    bet_count = int(len(bets))
    pending_age = float((now_pd - first_seen).total_seconds())
    ready_lag = float((now_pd - pd.Timestamp(max_pv)).total_seconds())
    late_flag = int(attempt_count > 0 or bet_count > initial_bet_count)
    now_iso = _ts_iso(now_ts)
    conn.execute(
        f"""
        INSERT INTO {_COMPLETED_TABLE} (
            player_id, game_id, player_game_ready_ts, dry_run_completed_at,
            representative_bet_id, bet_count,
            pending_age_sec, ready_lag_sec,
            late_after_score_hypothetical, attempt_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(player_id, game_id) DO NOTHING
        """,
        (
            int(player_id),
            int(game_id),
            _ts_iso(max_pv),
            now_iso,
            rep_bet,
            bet_count,
            pending_age,
            ready_lag,
            late_flag,
            attempt_count,
        ),
    )
    conn.execute(
        f"DELETE FROM {_PENDING_TABLE} WHERE player_id = ? AND game_id = ?",
        (int(player_id), int(game_id)),
    )
    return PlayerGameReadyProcessResult(
        "completed",
        int(player_id),
        int(game_id),
        attempt_count,
        pd.Timestamp(max_pv),
    )


def process_due_player_games(
    conn: sqlite3.Connection,
    *,
    now_ts: datetime,
    fetch_fn: PlayerGameRefetchFn,
    holdback_seconds: int = SCORER_POLL_INTERVAL_SECONDS,
) -> PlayerGameReadyDryRunSummary:
    """Process all due pending player-games without scoring."""
    due_keys = _list_due_pending(conn, now_ts)
    n_deferred = 0
    n_completed = 0
    n_skipped = 0

    cfg = default_hightier_serving_config()
    # If using the default ClickHouse refetch function, prefer chunked multi-player refetches
    # to reduce per-player HTTP query fanout. Fall back to per-player refetch on errors.
    try:
        chunk_sz = int(cfg.hightier_scorer_player_id_chunk_size)
    except Exception:
        chunk_sz = 500

    # Helper to update counters from a result
    def _count_result(res: PlayerGameReadyProcessResult) -> None:
        nonlocal n_deferred, n_completed, n_skipped
        if res.action == "deferred":
            n_deferred += 1
        elif res.action == "completed":
            n_completed += 1
        else:
            n_skipped += 1

    if fetch_fn is None:
        # Default to the ClickHouse refetch path
        from trainer_hightier.serving.scorer import fetch_bets_incremental

        for i in range(0, len(due_keys), chunk_sz):
            chunk = due_keys[i : i + chunk_sz]
            player_ids = frozenset(int(pid) for pid, _ in chunk)
            try:
                bets_chunk = fetch_bets_incremental(
                    None,
                    lookback_hours=float(cfg.player_game_ready_queue_refetch_lookback_hours),
                    limit_rows=int(cfg.hightier_scorer_max_bets_per_cycle),
                    allowlist_player_ids=player_ids,
                )
            except Exception as exc:
                logger.warning(
                    "[pg_ready_queue] chunked refetch failed (%s); falling back to per-player refetch",
                    exc,
                )
                # fallback: process per-player in this chunk
                for pid, gid in chunk:
                    res = process_one_pending_player_game(
                        conn,
                        pid,
                        gid,
                        now_ts=now_ts,
                        fetch_fn=fetch_fn,
                        holdback_seconds=holdback_seconds,
                    )
                    _count_result(res)
                continue

            # For each player-game in the chunk, provide a fetch_fn that extracts the player's
            # subset from the pre-fetched DataFrame to avoid additional ClickHouse round-trips.
            for pid, gid in chunk:
                def _make_fetch(frame: pd.DataFrame):
                    return lambda p, g: refetch_player_game_from_frame(frame, p, g)

                res = process_one_pending_player_game(
                    conn,
                    pid,
                    gid,
                    now_ts=now_ts,
                    fetch_fn=_make_fetch(bets_chunk),
                    holdback_seconds=holdback_seconds,
                )
                _count_result(res)
    else:
        # Non-default fetch_fn (test hooks, injected frames): preserve per-player processing.
        for pid, gid in due_keys:
            res = process_one_pending_player_game(
                conn,
                pid,
                gid,
                now_ts=now_ts,
                fetch_fn=fetch_fn,
                holdback_seconds=holdback_seconds,
            )
            _count_result(res)

    return PlayerGameReadyDryRunSummary(
        n_enqueued=0,
        n_due_processed=len(due_keys),
        n_deferred=n_deferred,
        n_completed=n_completed,
        n_skipped_completed=n_skipped,
    )


def run_player_game_ready_queue_dry_run_cycle(
    conn: sqlite3.Connection,
    *,
    incremental_bets: pd.DataFrame,
    now_ts: datetime | None = None,
    fetch_fn: PlayerGameRefetchFn | None = None,
    holdback_seconds: int = SCORER_POLL_INTERVAL_SECONDS,
) -> PlayerGameReadyDryRunSummary:
    """Enqueue from incremental bets and process due pending (dry-run only)."""

    cfg = default_hightier_serving_config()
    hk = ZoneInfo(cfg.hk_tz)
    now = now_ts or datetime.now(hk)
    refetch = fetch_fn or fetch_player_game_bets_clickhouse
    n_enqueued = enqueue_player_games_from_bets(
        conn,
        incremental_bets,
        now_ts=now,
        holdback_seconds=holdback_seconds,
    )
    proc = process_due_player_games(
        conn,
        now_ts=now,
        fetch_fn=refetch,
        holdback_seconds=holdback_seconds,
    )
    summary = PlayerGameReadyDryRunSummary(
        n_enqueued=n_enqueued,
        n_due_processed=proc.n_due_processed,
        n_deferred=proc.n_deferred,
        n_completed=proc.n_completed,
        n_skipped_completed=proc.n_skipped_completed,
    )
    conn.execute(
        f"""
        INSERT INTO {_CYCLE_TABLE} (
            cycle_ts, n_enqueued, n_due_processed,
            n_deferred, n_completed, n_skipped_completed
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            _ts_iso(now),
            summary.n_enqueued,
            summary.n_due_processed,
            summary.n_deferred,
            summary.n_completed,
            summary.n_skipped_completed,
        ),
    )
    logger.info(
        "[pg_ready_queue] dry_run enqueued=%d due=%d deferred=%d completed=%d skipped=%d",
        summary.n_enqueued,
        summary.n_due_processed,
        summary.n_deferred,
        summary.n_completed,
        summary.n_skipped_completed,
    )
    return summary


def summarize_dry_run_metrics(conn: sqlite3.Connection) -> dict[str, float | int]:
    """Aggregate dry-run latency / completeness metrics from completed rows."""

    row = conn.execute(
        f"""
        SELECT
            COUNT(*) AS n_completed,
            AVG(pending_age_sec) AS avg_pending_age_sec,
            AVG(ready_lag_sec) AS avg_ready_lag_sec,
            SUM(late_after_score_hypothetical) AS n_late_hypothetical
        FROM {_COMPLETED_TABLE}
        """,
    ).fetchone()
    if row is None:
        return {
            "n_completed": 0,
            "avg_pending_age_sec": 0.0,
            "avg_ready_lag_sec": 0.0,
            "n_late_hypothetical": 0,
        }
    return {
        "n_completed": int(row[0] or 0),
        "avg_pending_age_sec": float(row[1] or 0.0),
        "avg_ready_lag_sec": float(row[2] or 0.0),
        "n_late_hypothetical": int(row[3] or 0),
    }
