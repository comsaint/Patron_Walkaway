"""Bridge ``parallel_lda_mvp`` snapshot outputs to trainer local Parquet contract.

Writes ``data/gmwds_t_bet.parquet`` + ``data/gmwds_t_session.parquet`` with
atomic replace. Optional Phase C: left-join L1 ``run_fact`` + ``trip_run_map`` +
``trip_fact`` onto each bet (``player_id`` + ``payout_complete_dtm`` in
``[run_start_ts, run_end_ts]``), emitting fixed ``lda_*`` DOUBLE columns for
Track LLM passthrough features.

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
from pathlib import Path
from typing import Any, Sequence

# Bet-level Phase C source columns (must match trainer ``_REQUIRED_BET_PARQUET_COLS`` suffix
# and ``features_candidates.yaml`` passthrough ``feature_id`` values).
LDA_PHASE_C_BET_COLUMNS: tuple[str, ...] = (
    "lda_l1_run_bet_count",
    "lda_trip_run_count",
    "lda_run_ord_in_trip",
    "lda_trip_is_closed",
    "lda_l1_run_duration_min",
)

_ENV_BRIDGE_SKIP = "PARALLEL_LDA_BRIDGE_SKIP_IF_UNCHANGED"
_ENV_DUCKDB_MEM = "PARALLEL_LDA_BRIDGE_DUCKDB_MEMORY_LIMIT"


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
    phase_c: bool,
) -> str:
    """Return sha256 hex of sorted input paths + sizes (cheap reproducibility token)."""
    h = hashlib.sha256()
    h.update(f"phase_c={int(phase_c)}".encode("ascii"))
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


def emit_trainer_local_parquet(
    *,
    snap_root: Path,
    data_dir: Path,
    phase_c: bool = True,
    skip_if_unchanged: bool | None = None,
    duckdb_memory_limit: str | None = None,
) -> Path:
    """Materialize ``gmwds_t_bet.parquet`` / ``gmwds_t_session`` under ``data_dir``.

    Parameters
    ----------
    snap_root : Path
        Snapshot root, e.g. ``data/parallel_lda_mvp/snap_mvp_…``.
    data_dir : Path
        Project ``data/`` directory (parent of ``parallel_lda_mvp/``).
    phase_c : bool
        When True and L1 parquet exist, append ``LDA_PHASE_C_BET_COLUMNS`` via joins.
    skip_if_unchanged : bool | None
        If None, read env ``PARALLEL_LDA_BRIDGE_SKIP_IF_UNCHANGED`` (``1`` = true).
    duckdb_memory_limit : str | None
        DuckDB ``SET memory_limit`` value, e.g. ``'4GB'``. Env
        ``PARALLEL_LDA_BRIDGE_DUCKDB_MEMORY_LIMIT`` when unset here.

    Returns
    -------
    Path
        Path to the bridge manifest JSON written beside outputs.

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
    if skip_if_unchanged is None:
        skip_if_unchanged = os.environ.get(_ENV_BRIDGE_SKIP, "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
    if duckdb_memory_limit is None:
        duckdb_memory_limit = os.environ.get(_ENV_DUCKDB_MEM, "").strip() or "4GB"

    print(
        f"[trainer_bridge_mvp] start snap_root={snap_root} "
        f"phase_c={phase_c} duckdb_memory_limit={duckdb_memory_limit!r} "
        f"skip_if_unchanged={skip_if_unchanged}",
        flush=True,
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
    print(
        f"[trainer_bridge_mvp] mvp_summary={summary_path.name} "
        f"source_snapshot_id={sid!r} gaming_ym={summary.get('gaming_ym')!r}",
        flush=True,
    )
    fb = raw_bet_paths[0].name if raw_bet_paths else ""
    print(
        f"[trainer_bridge_mvp] t_bet inputs n={len(raw_bet_paths)} first={fb!r} "
        f"session={session_src.name}",
        flush=True,
    )

    run_paths = _parquet_glob_under(snap_root, "run_fact", "run_fact__")
    trip_paths = _parquet_glob_under(snap_root, "trip_fact", "trip_fact__")
    map_paths = _parquet_glob_under(snap_root, "trip_run_map", "trip_run_map__")
    print(
        f"[trainer_bridge_mvp] L1 parquet parts run_fact={len(run_paths)} "
        f"trip_fact={len(trip_paths)} trip_run_map={len(map_paths)}",
        flush=True,
    )

    fp = _fingerprint_inputs(
        raw_bet_paths,
        run_paths,
        trip_paths,
        map_paths,
        session_src,
        phase_c=phase_c and bool(run_paths),
    )

    manifest_path = data_dir / "trainer_local_parquet_bridge.manifest.json"
    if skip_if_unchanged:
        old = _read_manifest(manifest_path)
        if (
            old
            and old.get("input_fingerprint") == fp
            and (data_dir / "gmwds_t_bet.parquet").is_file()
            and (data_dir / "gmwds_t_session.parquet").is_file()
        ):
            print(
                f"[trainer_bridge_mvp] skip unchanged fingerprint={fp[:16]}… "
                f"(set {_ENV_BRIDGE_SKIP}=0 to force)",
                flush=True,
            )
            return manifest_path

    print(f"[trainer_bridge_mvp] input_fingerprint prefix={fp[:16]}…", flush=True)

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
    n_lda_in_src = sum(1 for c in LDA_PHASE_C_BET_COLUMNS if c in have)
    n_req_hit = sum(1 for c in required if c in have)
    print(
        f"[trainer_bridge_mvp] schema OK first_bet_file columns={len(have)} "
        f"trainer_required_hit={n_req_hit}/{len(required)} "
        f"lda_cols_already_in_source={n_lda_in_src}",
        flush=True,
    )

    bet_list_sql = ", ".join(f"'{_escape_sql_path(p)}'" for p in raw_bet_paths)
    out_bet = data_dir / "gmwds_t_bet.parquet"
    out_sess = data_dir / "gmwds_t_session.parquet"
    tmp_dir = data_dir / ".trainer_bridge_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    bet_tmp = tmp_dir / f"gmwds_t_bet.parquet.tmp.{os.getpid()}"
    sess_tmp = tmp_dir / f"gmwds_t_session.parquet.tmp.{os.getpid()}"
    print(
        f"[trainer_bridge_mvp] tmp bet={bet_tmp.name} session={sess_tmp.name} "
        f"-> {out_bet.name} + {out_sess.name}",
        flush=True,
    )

    try:
        con = duckdb.connect(database=":memory:")
        try:
            if duckdb_memory_limit:
                # memory_limit is a pragma value, not a path — use as literal with simple guard
                lim = str(duckdb_memory_limit).replace("'", "")
                con.execute(f"PRAGMA memory_limit='{lim}'")

            cols_sql = ", ".join(f'"{c}"' for c in required)
            if not (phase_c and run_paths):
                inner = f"SELECT {cols_sql} FROM read_parquet([{bet_list_sql}], union_by_name=true)"
                sql_mode = "bet_columns_only_no_L1_join"
            else:
                run_sql = ", ".join(f"'{_escape_sql_path(p)}'" for p in run_paths)
                trip_sql_list = ", ".join(f"'{_escape_sql_path(p)}'" for p in trip_paths)
                map_sql_list = ", ".join(f"'{_escape_sql_path(p)}'" for p in map_paths)
                has_trip_layer = bool(trip_paths and map_paths)
                sql_mode = "phase_c_run_plus_trip" if has_trip_layer else "phase_c_run_only_trip_zeroed"
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
            print(f"[trainer_bridge_mvp] DuckDB COPY bet parquet mode={sql_mode} …", flush=True)
            t_copy = time.perf_counter()
            con.execute(f"COPY ({inner}) TO '{out_esc}' (FORMAT PARQUET)")
            row_count = int(con.execute(f"SELECT COUNT(*) FROM read_parquet('{out_esc}')").fetchone()[0])
            print(
                f"[trainer_bridge_mvp] DuckDB COPY done rows={row_count} "
                f"elapsed_s={time.perf_counter() - t_copy:.1f}",
                flush=True,
            )
        finally:
            con.close()

        print(f"[trainer_bridge_mvp] copying session {session_src.name} -> {sess_tmp.name} …", flush=True)
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
            print(f"[trainer_bridge_mvp] session row_count={sess_rows}", flush=True)

        print(f"[trainer_bridge_mvp] atomic install -> {out_bet} , {out_sess}", flush=True)
        _atomic_replace(bet_tmp, out_bet)
        _atomic_replace(sess_tmp, out_sess)

        manifest: dict[str, Any] = {
            "artifact_kind": "trainer_local_parquet_bridge_v1",
            "built_at": _utc_now_iso(),
            "input_fingerprint": fp,
            "source_snapshot_id": summary.get("source_snapshot_id"),
            "snap_root": str(snap_root.as_posix()),
            "phase_c": bool(phase_c and bool(run_paths)),
            "t_bet_paths": [str(p.as_posix()) for p in raw_bet_paths],
            "t_session_source": str(session_src.as_posix()),
            "gmwds_t_bet": str(out_bet.as_posix()),
            "gmwds_t_session": str(out_sess.as_posix()),
            "bet_row_count": row_count,
            "session_row_count": sess_rows,
            "lda_phase_c_columns": list(LDA_PHASE_C_BET_COLUMNS)
            if (phase_c and run_paths)
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
            print(f"[trainer_bridge_mvp] writing manifest {manifest_path.name}", flush=True)
            _atomic_replace(Path(mf_tmp.name), manifest_path)
        except Exception:
            Path(mf_tmp.name).unlink(missing_ok=True)
            raise

        print(
            f"[trainer_bridge_mvp] wrote {out_bet.name} rows={row_count} "
            f"phase_c={manifest['phase_c']} manifest={manifest_path.name}",
            flush=True,
        )
        print(
            "[trainer_bridge_mvp] hint: if trainer chunk cache masks new bet columns, "
            "re-run with --force-recompute (or clear trainer/.data/chunks).",
            flush=True,
        )
        return manifest_path
    finally:
        bet_tmp.unlink(missing_ok=True)
        sess_tmp.unlink(missing_ok=True)


def validate_feature_spec_cli() -> int:
    """Load trainer feature spec; return 0 on success."""
    try:
        from trainer.features.features import load_feature_spec
        from trainer.training.trainer import FEATURE_SPEC_PATH
    except ImportError as e:
        print(f"[trainer_bridge_mvp] import error: {e}", file=sys.stderr)
        return 1
    print(f"[trainer_bridge_mvp] validating feature spec: {FEATURE_SPEC_PATH}", flush=True)
    load_feature_spec(FEATURE_SPEC_PATH)
    print(f"[trainer_bridge_mvp] feature spec OK: {FEATURE_SPEC_PATH}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(validate_feature_spec_cli())
