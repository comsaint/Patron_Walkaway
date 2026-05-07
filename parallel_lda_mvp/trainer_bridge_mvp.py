"""Bridge ``parallel_lda_mvp`` snapshot outputs to trainer-shaped Parquet files.

Writes under ``<data_dir>/mvp_trainer_bridge/`` (never overwrites L0
``data/gmwds_t_bet.parquet`` / ``data/gmwds_t_session.parquet``). Optional run/trip LDA:
left-join L1 ``run_fact`` + ``trip_run_map`` + ``trip_fact`` onto each bet
(``player_id`` + ``payout_complete_dtm`` in ``[run_start_ts, run_end_ts]``),
emitting fixed ``lda_*`` DOUBLE columns for Track LLM passthrough features.

See ``parallel_lda_mvp/run_mvp.py`` CLI flags ``--emit-trainer-local-parquet``
and ``--trainer-bridge-emit-only``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
import tempfile
from datetime import datetime, timezone
import threading
from pathlib import Path
from typing import Any, Sequence

# Bet-level run/trip LDA pass-through columns (must match trainer ``_REQUIRED_BET_PARQUET_COLS`` suffix
# and ``features_candidates.yaml`` passthrough ``feature_id`` values).
LDA_RUN_TRIP_BET_COLUMNS: tuple[str, ...] = (
    "lda_l1_run_bet_count",
    "lda_trip_run_count",
    "lda_run_ord_in_trip",
    "lda_trip_is_closed",
    "lda_l1_run_duration_min",
)

_ENV_BRIDGE_SKIP = "PARALLEL_LDA_BRIDGE_SKIP_IF_UNCHANGED"
_ENV_DUCKDB_MEM = "PARALLEL_LDA_BRIDGE_DUCKDB_MEMORY_LIMIT"

# Subdir under ``data/`` for bridge outputs only (L0 stays at repo ``data/gmwds_t_*.parquet``).
MVP_TRAINER_BRIDGE_SUBDIR = "mvp_trainer_bridge"


def trainer_bridge_output_dir(data_dir: Path) -> Path:
    """Return ``<data_dir>/mvp_trainer_bridge`` (resolved)."""
    return (Path(data_dir).resolve() / MVP_TRAINER_BRIDGE_SUBDIR)


def _utc_now_iso() -> str:
    """Return current UTC time as ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _escape_sql_path(p: Path) -> str:
    """Escape a filesystem path for DuckDB single-quoted string literal."""
    return str(p.resolve().as_posix()).replace("'", "''")


def _parquet_glob_under(snap_root: Path, sub: str, prefix: str) -> list[Path]:
    """Return sorted Parquet paths under ``snap_root/gaming_ym=*/sub/prefix*.parquet``."""
    out: list[Path] = []
    for d in sorted(snap_root.glob("gaming_ym=*")):
        gdir = d / sub
        if not gdir.is_dir():
            continue
        for f in sorted(gdir.glob(f"{prefix}*.parquet")):
            if f.suffix == ".parquet" and f.is_file():
                out.append(f.resolve())
    return out


def _first_mvp_summary(snap_root: Path) -> tuple[dict[str, Any], Path]:
    """Load the first ``mvp_summary.json`` found under ``snap_root`` (sorted path)."""
    paths = sorted(snap_root.glob("gaming_ym=*/mvp_summary.json"))
    if not paths:
        raise FileNotFoundError(f"No mvp_summary.json under {snap_root}")
    p = paths[0]
    return dict(json.loads(p.read_text(encoding="utf-8"))), p


def _fingerprint_inputs(
    bet_paths: Sequence[Path],
    run_paths: Sequence[Path],
    trip_paths: Sequence[Path],
    map_paths: Sequence[Path],
    session_path: Path,
    *,
    join_run_trip_lda_to_bet: bool,
) -> str:
    """Return sha256 hex of sorted input paths + sizes (cheap reproducibility token)."""
    h = hashlib.sha256()
    h.update(f"join_run_trip_lda_to_bet={int(join_run_trip_lda_to_bet)}".encode("ascii"))
    for label, seq in (
        ("bet", bet_paths),
        ("run", run_paths),
        ("trip", trip_paths),
        ("map", map_paths),
    ):
        h.update(f"|{label}|".encode("ascii"))
        for p in sorted(Path(x).resolve() for x in seq):
            h.update(str(p).encode("utf-8"))
            try:
                h.update(str(int(p.stat().st_size)).encode("ascii"))
            except OSError:
                h.update(b"0")
    sp = session_path.resolve()
    h.update(b"|session|")
    h.update(str(sp).encode("utf-8"))
    try:
        h.update(str(int(sp.stat().st_size)).encode("ascii"))
    except OSError:
        h.update(b"0")
    return h.hexdigest()


def _read_manifest(path: Path) -> dict[str, Any] | None:
    """Return parsed manifest JSON or None if missing."""
    if not path.is_file():
        return None
    try:
        return dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return None


def _atomic_replace(src: Path, dst: Path) -> None:
    """Rename ``src`` over ``dst`` (same-volume replace)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(str(src), str(dst))


def _bridge_echo(msg: str) -> None:
    """User-facing line for bridge materialization (stdout)."""
    print(f"[Trainer bridge] {msg}", flush=True)


def _progress_bar_disabled() -> bool:
    """Match trainer: env or ``trainer.config.DISABLE_PROGRESS_BAR``."""
    v = os.environ.get("DISABLE_PROGRESS_BAR", "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    try:
        import trainer.config as _tc  # type: ignore[import-not-found]

        return bool(getattr(_tc, "DISABLE_PROGRESS_BAR", False))
    except Exception:
        return False


def _safe_unlink_tmp(path: Path) -> None:
    """Best-effort delete of temp Parquet (Windows may keep a lock briefly after DuckDB closes)."""
    for attempt in range(5):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            if attempt < 4:
                time.sleep(0.4)
            else:
                _bridge_echo(f"Warning: could not delete temp file (close other tools using it): {path}")


def _layman_join_spinner_caption(
    *,
    join_run_trip_lda_to_bet: bool,
    run_paths: list[Path],
    trip_paths: list[Path],
    map_paths: list[Path],
) -> str:
    """Short single-line caption for the live status line (fits narrow terminals)."""
    if not (join_run_trip_lda_to_bet and run_paths):
        return "DuckDB: reshaping bet columns -> temp Parquet"
    if trip_paths and map_paths:
        return "DuckDB: join bets to runs+trips, write temp Parquet (long step)"
    return "DuckDB: join bets to runs, write temp Parquet (long step)"


def _duckdb_run_with_activity(con: Any, sql: str, long_explanation: str, spinner_caption: str) -> None:
    """Run DuckDB on the **main thread** (required); show a live spinner + elapsed seconds.

    DuckDB connections are not safe to share across threads; a background thread
    must never call ``con.execute``.
    """
    _bridge_echo(long_explanation)
    if _progress_bar_disabled():
        t0 = time.perf_counter()
        con.execute(sql)
        _bridge_echo(f"DuckDB finished in {time.perf_counter() - t0:.1f}s.")
        return

    stop = threading.Event()
    t0 = time.perf_counter()

    def _spin() -> None:
        chars = "|/-\\"
        n = 0
        cap = spinner_caption[:52] + ("..." if len(spinner_caption) > 52 else "")
        width = 20
        while not stop.wait(0.2):
            elapsed = time.perf_counter() - t0
            c = chars[n % len(chars)]
            n += 1
            # Indeterminate bar (elapsed only, not % done - DuckDB has no progress API).
            phase = int(elapsed * 3) % (width + 4)
            bar = "".join("=" if abs(i - phase) <= 2 else "." for i in range(width))
            sys.stdout.write(f"\r[Trainer bridge] {c} [{bar}] {cap}  {elapsed:5.0f}s ")
            sys.stdout.flush()

    th = threading.Thread(target=_spin, name="trainer-bridge-heartbeat", daemon=True)
    th.start()
    try:
        con.execute(sql)
    finally:
        stop.set()
        th.join(timeout=3.0)
    sys.stdout.write("\n")
    sys.stdout.flush()
    _bridge_echo(f"DuckDB finished in {time.perf_counter() - t0:.1f}s.")


def _layman_join_headline(
    *,
    join_run_trip_lda_to_bet: bool,
    run_paths: list[Path],
    trip_paths: list[Path],
    map_paths: list[Path],
) -> str:
    """One-line human description of the heavy bet-table build (not internal mode names)."""
    if not (join_run_trip_lda_to_bet and run_paths):
        return "Building trainer bet file (copying required columns only; no run/trip add-ons)"
    if trip_paths and map_paths:
        return (
            "Building trainer bet file: matching each bet to its betting run and trip, "
            "then adding five small numeric summary columns (for model features)"
        )
    return (
        "Building trainer bet file: matching each bet to its betting run only "
        "(trip summaries set to zero - trip tables missing in this snapshot)"
    )


def emit_trainer_local_parquet(
    *,
    snap_root: Path,
    data_dir: Path,
    enrich_bet_with_run_trip_lda: bool = True,
    skip_if_unchanged: bool | None = None,
    duckdb_memory_limit: str | None = None,
) -> Path:
    """Materialize trainer-shaped bet/session Parquet under ``mvp_trainer_bridge/``.

    Never writes to L0 ``data/gmwds_t_bet.parquet`` or ``data/gmwds_t_session.parquet``.

    Parameters
    ----------
    snap_root : Path
        Snapshot root, e.g. ``data/parallel_lda_mvp/snap_mvp_…``.
    data_dir : Path
        Project ``data/`` directory (parent of ``parallel_lda_mvp/``).
    enrich_bet_with_run_trip_lda : bool
        When True and L1 ``run_fact`` parquet exist, append ``LDA_RUN_TRIP_BET_COLUMNS`` via joins.
    skip_if_unchanged : bool | None
        If None, read env ``PARALLEL_LDA_BRIDGE_SKIP_IF_UNCHANGED`` (``1`` = true).
    duckdb_memory_limit : str | None
        DuckDB ``SET memory_limit`` value, e.g. ``'4GB'``. Env
        ``PARALLEL_LDA_BRIDGE_DUCKDB_MEMORY_LIMIT`` when unset here.

    Returns
    -------
    Path
        Path to ``trainer_local_parquet_bridge.manifest.json`` under
        ``trainer_bridge_output_dir(data_dir)``.

    Raises
    ------
    FileNotFoundError
        If snapshot metadata or session source is missing.
    ValueError
        If required bet columns are absent from resolved inputs.
    """
    import duckdb

    from trainer.training.trainer import _REQUIRED_BET_PARQUET_COLS

    snap_root = snap_root.resolve()
    data_dir = data_dir.resolve()
    bridge_dir = trainer_bridge_output_dir(data_dir)
    if skip_if_unchanged is None:
        skip_if_unchanged = os.environ.get(_ENV_BRIDGE_SKIP, "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
    if duckdb_memory_limit is None:
        duckdb_memory_limit = os.environ.get(_ENV_DUCKDB_MEM, "").strip() or "4GB"

    try:
        _bridge_rel = str(bridge_dir.relative_to(data_dir))
    except ValueError:
        _bridge_rel = str(bridge_dir)
    _bridge_echo(
        f"Preparing trainer-ready copies under {_bridge_rel} "
        f"(reads snapshot at {snap_root.name}; does NOT modify your original L0 "
        f"{data_dir.name}/gmwds_t_bet.parquet)."
    )
    if os.environ.get("TRAINER_BRIDGE_VERBOSE", "").strip().lower() in ("1", "true", "yes"):
        _bridge_echo(
            f"(verbose) DuckDB memory_limit={duckdb_memory_limit!r} skip_if_unchanged={skip_if_unchanged} "
            f"enrich_bet_with_run_trip_lda_requested={enrich_bet_with_run_trip_lda}"
        )

    summary, summary_path = _first_mvp_summary(snap_root)
    raw_bet_paths = [Path(x) for x in (summary.get("t_bet_paths") or [])]
    if not raw_bet_paths:
        raise FileNotFoundError("mvp_summary missing non-empty t_bet_paths")
    for bp in raw_bet_paths:
        if not bp.is_file():
            raise FileNotFoundError(f"t_bet path not found: {bp}")

    session_src = Path(str(summary.get("t_session") or "")).expanduser()
    if not session_src.is_file():
        raise FileNotFoundError(f"t_session from mvp_summary not a file: {session_src!r}")

    sid = str(summary.get("source_snapshot_id") or "")
    fb = raw_bet_paths[0].name if raw_bet_paths else ""
    _bridge_echo(
        f"Using snapshot summary {summary_path.parent.name}/{summary_path.name} "
        f"(id={sid or 'unknown'}, month={summary.get('gaming_ym')!r})."
    )
    _bridge_echo(
        f"Reading {len(raw_bet_paths)} bet Parquet part(s), first file {fb!r}; "
        f"session file {session_src.name!r}."
    )

    run_paths = _parquet_glob_under(snap_root, "run_fact", "run_fact__")
    trip_paths = _parquet_glob_under(snap_root, "trip_fact", "trip_fact__")
    map_paths = _parquet_glob_under(snap_root, "trip_run_map", "trip_run_map__")
    _bridge_echo(
        f"Found helper tables in snapshot: {len(run_paths)} run chunks, "
        f"{len(trip_paths)} trip chunks, {len(map_paths)} run<->trip link chunks."
    )

    join_lda_columns = enrich_bet_with_run_trip_lda and bool(run_paths)
    fp = _fingerprint_inputs(
        raw_bet_paths,
        run_paths,
        trip_paths,
        map_paths,
        session_src,
        join_run_trip_lda_to_bet=join_lda_columns,
    )

    manifest_path = bridge_dir / "trainer_local_parquet_bridge.manifest.json"
    if skip_if_unchanged:
        old = _read_manifest(manifest_path)
        if (
            old
            and old.get("input_fingerprint") == fp
            and (bridge_dir / "gmwds_t_bet.parquet").is_file()
            and (bridge_dir / "gmwds_t_session.parquet").is_file()
        ):
            _bridge_echo(
                "Output already matches current inputs (fingerprint unchanged) - skipping rebuild. "
                f"Unset {_ENV_BRIDGE_SKIP} or set it to 0/false to force a full rebuild."
            )
            return manifest_path

    _bridge_echo("Inputs changed or first build - will rebuild bridge Parquet files.")

    required = list(_REQUIRED_BET_PARQUET_COLS)

    # Schema probe (cheap metadata read; avoids DuckDB DESCRIBE quirks across versions)
    import pyarrow.parquet as pq

    have = set(pq.read_schema(str(raw_bet_paths[0])).names)
    missing = [c for c in required if c not in have]
    if missing:
        raise ValueError(
            f"t_bet missing required columns for trainer load_local_parquet: missing={missing!r} "
            f"have_sample={sorted(have)[:40]!r} … (n={len(have)})"
        )
    n_lda_in_src = sum(1 for c in LDA_RUN_TRIP_BET_COLUMNS if c in have)
    n_req_hit = sum(1 for c in required if c in have)
    _bridge_echo(
        f"Source bet file looks valid: {n_req_hit}/{len(required)} trainer-required columns present, "
        f"{len(have)} columns total; {n_lda_in_src}/5 optional run/trip summary columns already in file."
    )

    bet_list_sql = ", ".join(f"'{_escape_sql_path(p)}'" for p in raw_bet_paths)
    bridge_dir.mkdir(parents=True, exist_ok=True)
    out_bet = bridge_dir / "gmwds_t_bet.parquet"
    out_sess = bridge_dir / "gmwds_t_session.parquet"
    tmp_dir = bridge_dir / ".trainer_bridge_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    bet_tmp = tmp_dir / f"gmwds_t_bet.parquet.tmp.{os.getpid()}"
    sess_tmp = tmp_dir / f"gmwds_t_session.parquet.tmp.{os.getpid()}"
    _bridge_echo(
        f"Writing to a temp file, then installing as {out_bet} and {out_sess} "
        f"(your original bet Parquet paths are never overwritten)."
    )

    try:
        con = duckdb.connect(database=":memory:")
        try:
            if duckdb_memory_limit:
                # memory_limit is a pragma value, not a path — use as literal with simple guard
                lim = str(duckdb_memory_limit).replace("'", "")
                con.execute(f"PRAGMA memory_limit='{lim}'")
                _bridge_echo(
                    f"DuckDB engine memory cap set to {lim!r} (raise via env {_ENV_DUCKDB_MEM} if you have RAM)."
                )

            cols_sql = ", ".join(f'"{c}"' for c in required)
            if not join_lda_columns:
                inner = f"SELECT {cols_sql} FROM read_parquet([{bet_list_sql}], union_by_name=true)"
                sql_mode = "bet_columns_only_no_run_join"
            else:
                run_sql = ", ".join(f"'{_escape_sql_path(p)}'" for p in run_paths)
                trip_sql_list = ", ".join(f"'{_escape_sql_path(p)}'" for p in trip_paths)
                map_sql_list = ", ".join(f"'{_escape_sql_path(p)}'" for p in map_paths)
                has_trip_layer = bool(trip_paths and map_paths)
                sql_mode = "join_run_and_trip" if has_trip_layer else "join_run_trip_columns_zeroed"
                if has_trip_layer:
                    inner = f"""
                    WITH b AS (
                      SELECT * FROM read_parquet([{bet_list_sql}], union_by_name=true)
                    ),
                    r AS (
                      SELECT * FROM read_parquet([{run_sql}], union_by_name=true)
                    ),
                    trm AS (
                      SELECT * FROM read_parquet([{map_sql_list}], union_by_name=true)
                    ),
                    t AS (
                      SELECT * FROM read_parquet([{trip_sql_list}], union_by_name=true)
                    ),
                    br AS (
                      SELECT
                        b.*,
                        TRY_CAST(r.bet_count AS DOUBLE) AS _lda_run_bet_count,
                        r.run_start_ts AS _lda_run_start_ts,
                        r.run_end_ts AS _lda_run_end_ts,
                        r.run_id AS _lda_run_id
                      FROM b
                      LEFT JOIN r
                        ON CAST(b.player_id AS BIGINT) = CAST(r.player_id AS BIGINT)
                       AND b.payout_complete_dtm >= r.run_start_ts
                       AND b.payout_complete_dtm <= r.run_end_ts
                      QUALIFY ROW_NUMBER() OVER (
                        PARTITION BY b.bet_id
                        ORDER BY r.run_start_ts NULLS LAST, r.run_end_ts NULLS LAST
                      ) = 1
                    ),
                    j AS (
                      SELECT
                        br.*,
                        trm.run_ord_in_trip,
                        trm.trip_id,
                        t.run_count AS trip_run_count,
                        t.is_trip_closed
                      FROM br
                      LEFT JOIN trm ON br._lda_run_id = trm.run_id
                      LEFT JOIN t ON trm.trip_id = t.trip_id
                    )
                    SELECT
                      {cols_sql},
                      COALESCE(j._lda_run_bet_count, 0.0) AS lda_l1_run_bet_count,
                      COALESCE(TRY_CAST(j.trip_run_count AS DOUBLE), 0.0) AS lda_trip_run_count,
                      COALESCE(TRY_CAST(j.run_ord_in_trip AS DOUBLE), 0.0) AS lda_run_ord_in_trip,
                      COALESCE(
                        CASE
                          WHEN j.is_trip_closed IS NULL THEN 0.0
                          WHEN CAST(j.is_trip_closed AS BOOLEAN) THEN 1.0
                          ELSE 0.0
                        END,
                        0.0
                      ) AS lda_trip_is_closed,
                      COALESCE(
                        CASE
                          WHEN j._lda_run_start_ts IS NULL OR j._lda_run_end_ts IS NULL THEN 0.0
                          ELSE date_diff('minute', j._lda_run_start_ts, j._lda_run_end_ts)::DOUBLE
                        END,
                        0.0
                      ) AS lda_l1_run_duration_min
                    FROM j
                    """.strip()
                else:
                    inner = f"""
                    WITH b AS (
                      SELECT * FROM read_parquet([{bet_list_sql}], union_by_name=true)
                    ),
                    r AS (
                      SELECT * FROM read_parquet([{run_sql}], union_by_name=true)
                    ),
                    br AS (
                      SELECT
                        b.*,
                        TRY_CAST(r.bet_count AS DOUBLE) AS _lda_run_bet_count,
                        r.run_start_ts AS _lda_run_start_ts,
                        r.run_end_ts AS _lda_run_end_ts
                      FROM b
                      LEFT JOIN r
                        ON CAST(b.player_id AS BIGINT) = CAST(r.player_id AS BIGINT)
                       AND b.payout_complete_dtm >= r.run_start_ts
                       AND b.payout_complete_dtm <= r.run_end_ts
                      QUALIFY ROW_NUMBER() OVER (
                        PARTITION BY b.bet_id
                        ORDER BY r.run_start_ts NULLS LAST, r.run_end_ts NULLS LAST
                      ) = 1
                    )
                    SELECT
                      {cols_sql},
                      COALESCE(br._lda_run_bet_count, 0.0) AS lda_l1_run_bet_count,
                      0.0 AS lda_trip_run_count,
                      0.0 AS lda_run_ord_in_trip,
                      0.0 AS lda_trip_is_closed,
                      COALESCE(
                        CASE
                          WHEN br._lda_run_start_ts IS NULL OR br._lda_run_end_ts IS NULL THEN 0.0
                          ELSE date_diff('minute', br._lda_run_start_ts, br._lda_run_end_ts)::DOUBLE
                        END,
                        0.0
                      ) AS lda_l1_run_duration_min
                    FROM br
                    """.strip()

            out_esc = _escape_sql_path(bet_tmp)
            _join_long = _layman_join_headline(
                join_run_trip_lda_to_bet=enrich_bet_with_run_trip_lda,
                run_paths=run_paths,
                trip_paths=trip_paths,
                map_paths=map_paths,
            )
            _join_spin = _layman_join_spinner_caption(
                join_run_trip_lda_to_bet=enrich_bet_with_run_trip_lda,
                run_paths=run_paths,
                trip_paths=trip_paths,
                map_paths=map_paths,
            )
            t_copy = time.perf_counter()
            _duckdb_run_with_activity(
                con,
                f"COPY ({inner}) TO '{out_esc}' (FORMAT PARQUET)",
                long_explanation=_join_long,
                spinner_caption=_join_spin,
            )
            _copy_elapsed = time.perf_counter() - t_copy
            if os.environ.get("TRAINER_BRIDGE_VERBOSE", "").strip().lower() in ("1", "true", "yes"):
                _bridge_echo(f"(verbose) internal build mode: {sql_mode}")
            row_count = int(con.execute(f"SELECT COUNT(*) FROM read_parquet('{out_esc}')").fetchone()[0])
            _bridge_echo(
                f"Main bet export finished: {row_count:,} rows in {_copy_elapsed:.1f}s "
                "(row count is read from the new file)."
            )
        finally:
            con.close()

        _bridge_echo(f"Copying session table to bridge folder (same bytes as {session_src.name}) ...")
        shutil.copy2(session_src, sess_tmp)
        sess_rows = None
        try:
            con2 = duckdb.connect(database=":memory:")
            sess_rows = int(
                con2.execute(f"SELECT COUNT(*) FROM read_parquet('{_escape_sql_path(sess_tmp)}')").fetchone()[0]
            )
            con2.close()
        except Exception:
            sess_rows = None
        if sess_rows is not None:
            _bridge_echo(f"Session copy OK: {sess_rows:,} rows.")

        _bridge_echo("Installing bet + session files into the bridge folder (atomic replace) ...")
        _atomic_replace(bet_tmp, out_bet)
        _atomic_replace(sess_tmp, out_sess)

        manifest: dict[str, Any] = {
            "artifact_kind": "trainer_local_parquet_bridge_v1",
            "bridge_output_dir": str(bridge_dir.as_posix()),
            "built_at": _utc_now_iso(),
            "input_fingerprint": fp,
            "source_snapshot_id": summary.get("source_snapshot_id"),
            "snap_root": str(snap_root.as_posix()),
            "bet_includes_run_trip_lda_columns": join_lda_columns,
            "t_bet_paths": [str(p.as_posix()) for p in raw_bet_paths],
            "t_session_source": str(session_src.as_posix()),
            "gmwds_t_bet": str(out_bet.as_posix()),
            "gmwds_t_session": str(out_sess.as_posix()),
            "bet_row_count": row_count,
            "session_row_count": sess_rows,
            "lda_run_trip_bet_column_names": list(LDA_RUN_TRIP_BET_COLUMNS)
            if join_lda_columns
            else [],
            "run_fact_parts": len(run_paths),
            "trip_fact_parts": len(trip_paths),
            "trip_run_map_parts": len(map_paths),
        }
        mf_tmp = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            delete=False,
            dir=str(tmp_dir),
            suffix=".manifest.json.tmp",
        )
        try:
            mf_tmp.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            mf_tmp.close()
            _bridge_echo(f"Writing bridge manifest {manifest_path.name} (trainer reads this next) ...")
            _atomic_replace(Path(mf_tmp.name), manifest_path)
        except Exception:
            Path(mf_tmp.name).unlink(missing_ok=True)
            raise

        _bridge_echo(
            "All set: bridge bet/session ready; run/trip add-on columns active="
            f"{bool(manifest['bet_includes_run_trip_lda_columns'])}. "
            f"Manifest: {manifest_path.name}"
        )
        _bridge_echo(
            "Tip: if an old training run still ignores new columns, use --force-recompute "
            "or delete trainer/.data/chunks cache."
        )
        return manifest_path
    finally:
        time.sleep(0.1)
        _safe_unlink_tmp(bet_tmp)
        _safe_unlink_tmp(sess_tmp)


def validate_feature_spec_cli() -> int:
    """Load trainer feature spec; return 0 on success."""
    try:
        from trainer.features.features import load_feature_spec
        from trainer.training.trainer import FEATURE_SPEC_PATH
    except ImportError as e:
        print(f"[Trainer bridge] import error: {e}", file=sys.stderr)
        return 1
    _bridge_echo(f"Validating feature spec YAML: {FEATURE_SPEC_PATH}")
    load_feature_spec(FEATURE_SPEC_PATH)
    _bridge_echo(f"Feature spec OK: {FEATURE_SPEC_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(validate_feature_spec_cli())
