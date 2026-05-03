#!/usr/bin/env python3
"""Phase-1 LDA end-to-end per calendar day: raw L0 (optional) → preprocess → L1 run_* → Gate1.

**End-to-end** here means: optional **raw** Parquet ingested with ``l0_ingest.py`` (``t_bet``; optional
``t_session``), then ``preprocess_bet_v1``, then ``run_fact`` / ``run_bet_map`` / ``run_day_bridge``,
then **Gate1** on each of those three artifacts. In raw mode, this orchestrator can build the
BET-DQ-03 rated allowlist from ``t_session`` via
``trainer.identity.build_rated_eligible_player_ids_df`` and forward it to preprocess.

**Input modes** (pick one, or omit for **default** — see below):

* **Default (no flag)** — if ``<repo>/data/gmwds_t_bet.parquet`` exists: same as ``--bet-parquet`` on that
  file with ``--source-snapshot-id snap_gmwds_t_bet_local`` (skip L0; preprocess filters each day).
  Matches README local Parquet layout. If the file is missing, exit with a message to pass an explicit mode.
* ``--raw-t-bet-parquet`` — run L0 ingest per day (same file repeated is disk-heavy; see epilog).
* ``--bet-parquet`` — skip L0; requires ``--source-snapshot-id`` for L1 paths (override default snap id).
* ``--l0-existing`` — discover ``snap_*`` under ``l0_layered`` that already has ``t_bet`` parts for
  each ``gaming_day`` (deterministic: lexicographically first if several match).

**Calendar days**: pass ``--date-from`` / ``--date-to`` (inclusive) for an explicit range. If you **omit
both**, the plan is **every ``gaming_day`` that appears in the bet source** (sorted): for
``--bet-parquet`` / ``--raw-t-bet-parquet`` that is ``SELECT DISTINCT gaming_day`` from that Parquet;
for ``--l0-existing`` it is every ``l0_layered/*/t_bet/gaming_day=*`` partition that contains
``part-*.parquet``. You must pass **both** dates or **neither**.

Exits non-zero on first failing subprocess.

All L0/L1 paths use **``<repo>/data``** (same as other LDA CLIs); this orchestrator does **not**
accept ``--data-root`` so runs stay tied to the repo layout.

**Resumable state (LDA-E1-09)**: optional DuckDB **``materialization_state``** via ``--state-store``,
``--resume`` (skip succeeded steps when ``input_hash`` unchanged), ``--force`` (rerun all steps),
and ``--stop-after-date`` (exit after one successful day). Default DB path when ``--resume``/``--force``
omit ``--state-store``: ``data/l1_layered/materialization_state.duckdb``. See ``pipelines/layered_data_assets/docs/RUNBOOK.md`` §5.1.

**Logging**: by default stderr shows a short banner, tqdm postfix (current ``gaming_day`` + phase), and one ``[LDA]`` line per subprocess with timing and a brief result summary (Gate1 JSON is not printed). Use ``--echo-commands`` for the previous verbose argv / live worker streams.

**Ingestion / E1-11 fixes**: the orchestrator **always** passes an ingestion registry to ``preprocess_bet_v1``:
the canonical ``schema/preprocess_bet_ingestion_fix_registry.yaml`` unless you override with
``--ingestion-fix-registry-yaml``. If that file (or the override path) is missing, the program exits
immediately with an error. Optional ``--ingestion-fix-registry-version-expected`` fail-fast locks
``registry_version``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import textwrap
import time
import traceback
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .repo_root import discover_repo_root

_REPO_ROOT = discover_repo_root()
_SCRIPTS = _REPO_ROOT / "scripts"
LDA_FIXED_DATA_ROOT = (_REPO_ROOT / "data").resolve()


def _stderr_line(msg: str, *, emit: Callable[[str], None] | None = None) -> None:
    """Print one stderr line; use ``emit`` when provided (e.g. :meth:`_DayRangeProgressBar.write_stderr_line`)."""
    if emit is not None:
        emit(msg)
    else:
        print(msg, file=sys.stderr, flush=True)


from ..io.l0_paths import (
    discover_l0_snapshot_ids_for_partition,
    l0_partition_dir,
)
from ..io.l1_paths import (
    l1_bet_cleaned_parquet_path,
    l1_run_bet_map_partition_dir,
    l1_run_day_bridge_partition_dir,
    l1_run_fact_partition_dir,
)
from ..orchestration.lda_day_range_v1 import (
    distinct_gaming_days_from_l0_t_bet_layout,
    distinct_gaming_days_from_t_bet_parquet,
    inclusive_iso_date_strings,
)
from ..orchestration.oom_runner_v1 import (
    append_jsonl_record,
    apply_duckdb_resource_pragmas,
    write_failure_context,
)
from ..orchestration.materialization_state_store_v1 import (
    ARTIFACT_GATE1_RUN_BET_MAP,
    ARTIFACT_GATE1_RUN_DAY_BRIDGE,
    ARTIFACT_GATE1_RUN_FACT,
    ARTIFACT_PREPROCESS_BET,
    ARTIFACT_RUN_BET_MAP,
    ARTIFACT_RUN_DAY_BRIDGE,
    ARTIFACT_RUN_FACT,
    default_state_store_path,
    ensure_materialization_state_schema,
    fetch_state_row,
    hash_gate1_inputs,
    hash_preprocess_inputs,
    hash_run_materialize_inputs,
    mark_step_failed,
    mark_step_running,
    mark_step_succeeded,
    parquet_row_count,
    should_skip_step,
)

_GATE1_ARTIFACTS: tuple[str, ...] = ("run_fact", "run_bet_map", "run_day_bridge")
_GATE1_STATE_KIND: dict[str, str] = {
    "run_fact": ARTIFACT_GATE1_RUN_FACT,
    "run_bet_map": ARTIFACT_GATE1_RUN_BET_MAP,
    "run_day_bridge": ARTIFACT_GATE1_RUN_DAY_BRIDGE,
}

# Canonical local export (README / trainer --use-local-parquet); used when no source CLI flag is set.
_DEFAULT_LOCAL_T_BET_NAME = "gmwds_t_bet.parquet"
_DEFAULT_BET_PARQUET_SOURCE_SNAPSHOT_ID = "snap_gmwds_t_bet_local"


def apply_default_ingestion_registry_args(args: argparse.Namespace) -> None:
    """Resolve preprocess ingestion registry path (mandatory for this orchestrator).

    If ``args.ingestion_fix_registry_yaml`` is set, it must be an existing file (resolved path).
    Otherwise sets the canonical ``<repo>/schema/preprocess_bet_ingestion_fix_registry.yaml``,
    which **must** exist or :class:`ValueError` is raised.

    Sets ``args._lda_defaulted_ingestion_registry`` when the canonical default is used (banner only).

    Raises:
        FileNotFoundError: If an explicit ``--ingestion-fix-registry-yaml`` path is not a file.
        ValueError: If the canonical registry path is required but missing.
    """
    explicit = getattr(args, "ingestion_fix_registry_yaml", None)
    if explicit is not None:
        p = Path(explicit).resolve()
        if not p.is_file():
            raise FileNotFoundError(f"ingestion fix registry not found: {p}")
        args.ingestion_fix_registry_yaml = p
        return
    p = (_REPO_ROOT / "schema" / "preprocess_bet_ingestion_fix_registry.yaml").resolve()
    if not p.is_file():
        raise ValueError(
            f"Required ingestion fix registry is missing: {p}. "
            "Restore this file in the repository or pass --ingestion-fix-registry-yaml to an existing YAML."
        )
    args.ingestion_fix_registry_yaml = p
    args._lda_defaulted_ingestion_registry = True  # noqa: SLF001


def apply_default_lda_source_args(args: argparse.Namespace, *, data_root: Path) -> None:
    """When no source mode flag was given, use ``data/gmwds_t_bet.parquet`` if present (bet-parquet path).

    Sets ``args.bet_parquet`` and ``args.source_snapshot_id`` when appropriate; sets
    ``args._lda_defaulted_local_t_bet`` for banner messaging.

    Raises:
        ValueError: If no mode was given and the default Parquet file is absent.
    """
    if args.bet_parquet is not None or args.raw_t_bet_parquet is not None or args.l0_existing:
        return
    default_path = (data_root / _DEFAULT_LOCAL_T_BET_NAME).resolve()
    if not default_path.is_file():
        raise ValueError(
            "No source mode: pass one of --bet-parquet, --raw-t-bet-parquet, or --l0-existing; "
            f"or place the canonical local t_bet export at {default_path} "
            f"(same as README / trainer --use-local-parquet). "
            "Then you may run with only --date-from and --date-to."
        )
    args.bet_parquet = default_path
    if not args.source_snapshot_id or not str(args.source_snapshot_id).strip():
        args.source_snapshot_id = _DEFAULT_BET_PARQUET_SOURCE_SNAPSHOT_ID
    args._lda_defaulted_local_t_bet = True  # noqa: SLF001 — orchestrator-only marker


class _DayRangeProgressBar:
    """tqdm bar over calendar days, or no-op when disabled / tqdm missing."""

    def __init__(self, total: int, *, disable: bool) -> None:
        self._inner: object | None = None
        if disable or total < 1:
            return
        try:
            from tqdm import tqdm

            self._inner = tqdm(
                total=total,
                desc="LDA day-range",
                unit="day",
                leave=True,
                file=sys.stderr,
                dynamic_ncols=True,
                mininterval=0.25,
            )
        except ImportError:
            self._inner = None

    def write_stderr_line(self, msg: str) -> None:
        """Print one status line without breaking the in-place tqdm bar."""
        if self._inner is not None:
            from tqdm import tqdm

            tqdm.write(msg, file=sys.stderr)
            return
        print(msg, file=sys.stderr, flush=True)

    def set_postfix_str(self, text: str) -> None:
        if self._inner is not None and hasattr(self._inner, "set_postfix_str"):
            self._inner.set_postfix_str(text, refresh=True)

    def update(self, n: int = 1) -> None:
        if self._inner is not None and hasattr(self._inner, "update"):
            self._inner.update(n)

    def close(self) -> None:
        if self._inner is not None and hasattr(self._inner, "close"):
            self._inner.close()


def _print_run_banner(
    *,
    args: argparse.Namespace,
    days: list[str],
    data_root: Path,
    gate_parent: Path,
    eligible_player_ids_parquet: Path | None,
) -> None:
    """Print a short human-readable plan to stderr."""
    n = len(days)
    if args.bet_parquet is not None:
        tag = ""
        if getattr(args, "_lda_defaulted_local_t_bet", False):
            tag = " [default gmwds_t_bet + snap_gmwds_t_bet_local]"
        src = f"bet-parquet={args.bet_parquet} snap={args.source_snapshot_id}{tag}"
    elif args.raw_t_bet_parquet is not None:
        extra = f" session={args.raw_t_session_parquet}" if args.raw_t_session_parquet else ""
        src = f"raw-t-bet-parquet={args.raw_t_bet_parquet}{extra}"
    else:
        src = "l0-existing (snap_* per day)"
    reg = args.ingestion_fix_registry_yaml
    if getattr(args, "_lda_defaulted_ingestion_registry", False):
        reg_note = "preprocess registry ON (default schema/preprocess_bet_ingestion_fix_registry.yaml)"
    else:
        reg_note = f"preprocess registry ON ({Path(reg).name})"
    lines = [
        f"[LDA] plan {n} day(s) {days[0]} .. {days[-1]} | data={data_root} | {src}",
        f"[LDA] per day: L0? -> preprocess -> L1(run_fact, run_bet_map, run_day_bridge) -> Gate1(x3) | gate1_out={gate_parent.resolve()}",
        f"[LDA] {reg_note}",
    ]
    if eligible_player_ids_parquet is not None:
        lines.append(f"[LDA] BET-DQ-03 eligible ids: {eligible_player_ids_parquet}")
    if _state_tracking_enabled(args):
        sp = _resolve_state_db_path(args, data_root=data_root)
        sfx = f"resume={bool(args.resume)} force={bool(args.force)}"
        lines.append(f"[LDA] state DB: {sp} ({sfx})")
    if getattr(args, "stop_after_date", None):
        lines.append(f"[LDA] stop-after: {args.stop_after_date.strip()}")
    if bool(getattr(args, "echo_commands", False)):
        lines.append("[LDA] echo-commands=ON (full argv + worker stdout/stderr live)")
    for ln in lines:
        print(ln, file=sys.stderr, flush=True)


def _worker_ok_tail(stdout: str, stderr: str) -> str:
    """Last ``OK …`` line from merged worker output (preprocess / materialize / l0_ingest)."""
    blob = ((stdout or "") + "\n" + (stderr or "")).splitlines()
    for line in reversed(blob):
        s = line.strip()
        if s.startswith("OK "):
            return s
    return "OK (no summary line)"


def _gate1_stdout_summary(stdout: str) -> str:
    """One-line summary from Gate1 JSON on stdout."""
    raw = (stdout or "").strip()
    if not raw:
        return "no stdout"
    tail = raw.splitlines()[-1].strip()
    try:
        data = json.loads(tail)
    except json.JSONDecodeError:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return "gate1 JSON?"
    fp_ok = bool(data.get("all_row_fingerprints_match"))
    cnt_ok = bool(data.get("all_row_counts_match"))
    nprof = int(data.get("profiles_evaluated") or 0)
    return f"profiles={nprof} fp_ok={fp_ok} counts_ok={cnt_ok}"


def _l0_bet_parquet_paths_for_day(data_root: Path, l0_snapshot_id: str, gaming_day: str) -> list[Path]:
    """Return sorted ``part-*.parquet`` under the L0 ``t_bet`` partition for ``gaming_day``."""
    d = l0_partition_dir(data_root, l0_snapshot_id, "t_bet", "gaming_day", gaming_day)
    paths = sorted(d.glob("part-*.parquet"))
    return [p.resolve() for p in paths if p.is_file()]


def _state_tracking_enabled(args: argparse.Namespace) -> bool:
    """Return True if materialization state DB should be used."""
    return getattr(args, "state_store", None) is not None or bool(args.resume) or bool(args.force)


def _resolve_state_db_path(args: argparse.Namespace, *, data_root: Path) -> Path:
    """Return DuckDB path for ``materialization_state`` (default under ``l1_layered``)."""
    if getattr(args, "state_store", None) is not None:
        return Path(args.state_store).resolve()
    return default_state_store_path(data_root)


def _run_step(
    cmd: list[str],
    *,
    label: str,
    step: str,
    capture_output: bool = False,
    echo_commands: bool = False,
    emit_stderr: Callable[[str], None] | None = None,
) -> str | None:
    """Run one subprocess; raise :class:`RuntimeError` on failure. Optionally return merged stdout/stderr."""
    quiet = not echo_commands
    effective_capture = bool(capture_output) or quiet
    if echo_commands:
        _stderr_line(f"[orchestrator] >> {step} -- START {label}", emit=emit_stderr)
        _stderr_line(f"[orchestrator]   command: {' '.join(cmd)}", emit=emit_stderr)
    t0 = time.monotonic()
    proc = subprocess.run(
        cmd,
        cwd=str(_REPO_ROOT),
        capture_output=effective_capture,
        text=effective_capture,
    )
    dt = time.monotonic() - t0
    out_s = proc.stdout or "" if effective_capture else ""
    err_s = proc.stderr or "" if effective_capture else ""
    blob = (out_s + "\n" + err_s) if effective_capture else None
    if proc.returncode != 0:
        _stderr_line(
            f"[LDA] FAIL {step} ({label}) exit={proc.returncode} after {dt:.1f}s",
            emit=emit_stderr,
        )
        if effective_capture and blob:
            _stderr_line(blob.strip()[-8000:], emit=emit_stderr)
        raise RuntimeError(f"{label} failed with exit code {proc.returncode}")
    if echo_commands:
        _stderr_line(f"[orchestrator] OK {step} -- DONE {label} in {dt:.1f}s", emit=emit_stderr)
    else:
        is_gate1 = "gate1-" in step
        if is_gate1:
            summ = _gate1_stdout_summary(out_s)
        else:
            summ = _worker_ok_tail(out_s, err_s)
        if "L0" in step:
            phase = "L0"
        elif is_gate1:
            phase = "Gate1"
        elif "preprocess" in step:
            phase = "preprocess"
        else:
            phase = "L1"
        _stderr_line(f"[LDA] {phase} | {step} | {dt:.1f}s | {summ}", emit=emit_stderr)
    return blob


def _parse_l0_snapshot_id_from_ingest_output(blob: str) -> str:
    """Parse ``OK snapshot_id=...`` line from ``l0_ingest`` stdout/stderr."""
    for line in blob.splitlines():
        s = line.strip()
        if s.startswith("OK snapshot_id="):
            return s.split("=", 1)[1].strip()
    raise RuntimeError("l0_ingest did not print a line OK snapshot_id=...")


def _effective_fp_for_hash(fp_ingest: Path | None, user_fp: Path | None) -> Path | None:
    """Prefer ingest-side fingerprint file, else user override (same as materialize argv)."""
    if fp_ingest is not None and fp_ingest.is_file():
        return fp_ingest
    if user_fp is not None and user_fp.is_file():
        return user_fp
    return None


def _run_tracked_subprocess(
    *,
    state_con: object | None,
    artifact_kind: str,
    gaming_day: str,
    sid: str,
    input_hash: str,
    resume: bool,
    force: bool,
    label: str,
    step: str,
    cmd: list[str],
    output_uri: str | None,
    row_count_parquet: Path | None,
    echo_commands: bool,
    emit_stderr: Callable[[str], None] | None = None,
) -> None:
    """Optionally honor materialization state (skip / mark running / succeeded / failed)."""
    if state_con is None:
        _run_step(cmd, label=label, step=step, echo_commands=echo_commands, emit_stderr=emit_stderr)
        return
    prev = fetch_state_row(
        state_con,
        artifact_kind=artifact_kind,
        gaming_day=gaming_day,
        source_snapshot_id=sid,
    )
    do_skip = should_skip_step(resume=resume, force=force, row=prev, input_hash=input_hash)
    if do_skip and row_count_parquet is not None and not Path(row_count_parquet).is_file():
        _stderr_line(
            f"[LDA] WARN cannot skip {step}: expected output {row_count_parquet} missing",
            emit=emit_stderr,
        )
        do_skip = False
    if do_skip and artifact_kind.startswith("gate1_") and output_uri:
        out_p = Path(output_uri)
        if not out_p.is_dir() or not any(out_p.iterdir()):
            _stderr_line(
                f"[LDA] WARN cannot skip {step}: gate1 output dir missing or empty ({out_p})",
                emit=emit_stderr,
            )
            do_skip = False
    if do_skip:
        _stderr_line(f"[LDA] SKIP {step} (resume, unchanged input_hash)", emit=emit_stderr)
        return
    attempt = mark_step_running(
        state_con,
        artifact_kind=artifact_kind,
        gaming_day=gaming_day,
        source_snapshot_id=sid,
        input_hash=input_hash,
    )
    try:
        _run_step(cmd, label=label, step=step, echo_commands=echo_commands, emit_stderr=emit_stderr)
    except RuntimeError as exc:
        mark_step_failed(
            state_con,
            artifact_kind=artifact_kind,
            gaming_day=gaming_day,
            source_snapshot_id=sid,
            input_hash=input_hash,
            attempt=attempt,
            error_summary=str(exc),
        )
        raise
    rc: int | None = None
    if row_count_parquet is not None and Path(row_count_parquet).is_file():
        rc = parquet_row_count(state_con, Path(row_count_parquet))
    mark_step_succeeded(
        state_con,
        artifact_kind=artifact_kind,
        gaming_day=gaming_day,
        source_snapshot_id=sid,
        input_hash=input_hash,
        attempt=attempt,
        output_uri=output_uri,
        row_count=rc,
        row_hash=None,
    )


def _fp_args_for_materialize(
    *,
    fp_from_ingest: Path | None,
    user_fp: Path | None,
) -> list[str]:
    """Build ``--l0-fingerprint-json`` argv fragment; prefer fingerprint next to ingest output."""
    if fp_from_ingest is not None and fp_from_ingest.is_file():
        return ["--l0-fingerprint-json", str(fp_from_ingest.resolve())]
    if user_fp is not None:
        return ["--l0-fingerprint-json", str(user_fp.resolve())]
    return []


def _ingestion_registry_cli_args(args: argparse.Namespace) -> list[str]:
    """Build optional preprocess ``--ingestion-fix-registry-*`` argv fragment."""
    out: list[str] = []
    reg = getattr(args, "ingestion_fix_registry_yaml", None)
    if reg is not None:
        out.extend(["--ingestion-fix-registry-yaml", str(Path(reg).resolve())])
    ver = getattr(args, "ingestion_fix_registry_version_expected", None)
    if ver is not None and str(ver).strip():
        out.extend(["--ingestion-fix-registry-version-expected", str(ver).strip()])
    return out


def _assert_eligible_session_row_budget(count: int, *, max_rows: int) -> None:
    """Fail fast when cutoff-filtered ``t_session`` row count exceeds the operator budget."""
    if max_rows <= 0:
        return
    if count > max_rows:
        raise RuntimeError(
            f"cutoff-filtered t_session row count {count:,} exceeds "
            f"--eligible-build-max-session-rows={max_rows:,}; "
            "raise the limit, pre-slice the Parquet export, or run on a machine with enough RAM "
            "only after explicit planning."
        )


def _parse_cutoff_dtm(raw_value: str) -> datetime:
    """Parse ``--cutoff-dtm`` into a :class:`datetime` (ISO-8601; accepts trailing ``Z``)."""
    text = str(raw_value).strip()
    if not text:
        raise ValueError("--cutoff-dtm must be a non-empty ISO datetime string")
    norm = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(norm)
    except ValueError as exc:
        raise ValueError(
            f"Invalid --cutoff-dtm {raw_value!r}; expected ISO-8601 like 2026-01-31T23:59:59+08:00"
        ) from exc


def _build_rated_eligible_player_ids_parquet(
    *,
    raw_t_session_parquet: Path,
    cutoff_dtm: datetime,
    data_root: Path,
    emit_stderr: Callable[[str], None] | None = None,
    max_session_rows: int = 5_000_000,
    duckdb_memory_limit_mb: int | None = None,
    duckdb_threads: int = 1,
    failure_context_path: Path | None = None,
    run_log_path: Path | None = None,
) -> Path:
    """Build BET-DQ-03 rated eligible allowlist once per orchestrator run.

    Uses a deterministic cache path under ``data/tmp_lda_gate1_day_range/eligible`` keyed by
    ``raw_t_session_parquet`` file stats + ``cutoff_dtm``.

    Resource guards (E1-16): optional DuckDB ``memory_limit`` / ``threads``, a fail-fast row budget
    on cutoff-filtered ``t_session`` rows (``max_session_rows``; ``0`` disables), JSONL run log,
    and a JSON failure context on any exception.
    """
    src = raw_t_session_parquet.resolve()
    if not src.is_file():
        raise FileNotFoundError(f"raw_t_session parquet not found: {src}")
    st = src.stat()
    key_raw = f"{src.as_posix()}|{int(st.st_size)}|{int(getattr(st, 'st_mtime_ns', int(st.st_mtime * 1e9)))}|{cutoff_dtm.isoformat()}"
    key = hashlib.sha256(key_raw.encode("utf-8")).hexdigest()[:16]
    out_dir = (data_root / "tmp_lda_gate1_day_range" / "eligible").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"rated_eligible_{key}.parquet"
    if out_path.is_file():
        _stderr_line(f"[LDA] rated eligible cache hit: {out_path}", emit=emit_stderr)
        return out_path

    ctx_default = out_dir / "last_eligible_build_failure.json"
    diag_base: dict[str, object] = {
        "raw_t_session_parquet": str(src),
        "cutoff_dtm": cutoff_dtm.isoformat(),
        "max_session_rows": int(max_session_rows),
        "duckdb_memory_limit_mb": duckdb_memory_limit_mb,
        "duckdb_threads": int(duckdb_threads),
        "cache_key_prefix": key,
    }

    try:
        import duckdb
    except ImportError as exc:
        raise RuntimeError("duckdb is required to auto-build rated eligible ids from --raw-t-session-parquet.") from exc

    from trainer.identity import _REQUIRED_SESSION_COLS, build_rated_eligible_player_ids_df

    cols = sorted(_REQUIRED_SESSION_COLS)
    cols_sql = ", ".join(f'"{c}"' for c in cols)
    cutoff_sql = cutoff_dtm.isoformat(sep=" ")
    filter_tail = (
        "FROM read_parquet(?) "
        "WHERE COALESCE(TRY_CAST(session_end_dtm AS TIMESTAMP), TRY_CAST(lud_dtm AS TIMESTAMP)) "
        "<= TRY_CAST(? AS TIMESTAMP)"
    )
    count_sql = f"SELECT COUNT(*) AS n {filter_tail}"
    select_sql = f"SELECT {cols_sql} {filter_tail}"
    params = [str(src), cutoff_sql]

    _stderr_line(
        f"[LDA] build rated eligible from {src.name} (cutoff={cutoff_dtm.isoformat()}, "
        f"max_session_rows={max_session_rows}, duckdb_threads={duckdb_threads}"
        f"{f', memory_limit_mb={duckdb_memory_limit_mb}' if duckdb_memory_limit_mb is not None else ''})",
        emit=emit_stderr,
    )

    t0 = time.monotonic()
    try:
        con = duckdb.connect()
        try:
            apply_duckdb_resource_pragmas(
                con,
                memory_limit_mb=duckdb_memory_limit_mb,
                threads=int(duckdb_threads),
            )
            filtered_count: int | None = None
            if int(max_session_rows) > 0:
                row = con.execute(count_sql, params).fetchone()
                filtered_count = int(row[0]) if row is not None else 0
                _assert_eligible_session_row_budget(filtered_count, max_rows=int(max_session_rows))
                if run_log_path is not None:
                    append_jsonl_record(
                        Path(run_log_path),
                        {
                            "event": "eligible_build_count",
                            "filtered_session_rows": filtered_count,
                            "max_session_rows": int(max_session_rows),
                            "elapsed_sec": round(time.monotonic() - t0, 3),
                            "source": str(src),
                        },
                    )
            sessions_df = con.execute(select_sql, params).fetchdf()
        finally:
            con.close()
        if int(max_session_rows) > 0:
            _assert_eligible_session_row_budget(len(sessions_df), max_rows=int(max_session_rows))
        eligible_df = build_rated_eligible_player_ids_df(sessions_df, cutoff_dtm)
        eligible_df.to_parquet(out_path, index=False)
        dt = time.monotonic() - t0
        _stderr_line(
            f"[LDA] rated eligible wrote {len(eligible_df):,} player_id(s) -> {out_path} ({dt:.1f}s)",
            emit=emit_stderr,
        )
        if run_log_path is not None:
            append_jsonl_record(
                Path(run_log_path),
                {
                    "event": "eligible_build_done",
                    "distinct_rated_player_ids": int(len(eligible_df)),
                    "dataframe_rows_in": int(len(sessions_df)),
                    "filtered_session_rows": filtered_count,
                    "output_parquet": str(out_path),
                    "elapsed_sec": round(dt, 3),
                },
            )
        return out_path
    except Exception as exc:
        ctx = (failure_context_path or ctx_default).resolve()
        payload = {
            **diag_base,
            "error": str(exc),
            "error_type": type(exc).__name__,
            "traceback": traceback.format_exc(),
        }
        write_failure_context(ctx, payload)
        _stderr_line(f"[LDA] eligible build failure context -> {ctx}", emit=emit_stderr)
        raise


def _resolve_eligible_player_ids_parquet(
    *,
    args: argparse.Namespace,
    data_root: Path,
    dry_run: bool,
    emit_stderr: Callable[[str], None] | None = None,
) -> Path | None:
    """Resolve BET-DQ-03 allowlist path for preprocess.

    - If ``--eligible-player-ids-parquet`` is provided, requires that file to exist.
    - In raw mode, auto-build from ``--raw-t-session-parquet`` + ``--cutoff-dtm`` when no explicit file.
    """
    explicit = getattr(args, "eligible_player_ids_parquet", None)
    if explicit is not None:
        ep = Path(explicit).resolve()
        if not ep.is_file():
            raise FileNotFoundError(f"eligible-player-ids parquet not found: {ep}")
        return ep

    if getattr(args, "raw_t_bet_parquet", None) is None:
        return None
    if getattr(args, "raw_t_session_parquet", None) is None:
        return None
    cutoff_raw = getattr(args, "cutoff_dtm", None)
    if cutoff_raw is None or not str(cutoff_raw).strip():
        return None
    cutoff = _parse_cutoff_dtm(str(cutoff_raw))
    if dry_run:
        _stderr_line(
            "[LDA] dry-run: would build BET-DQ-03 eligible-player-ids from --raw-t-session-parquet",
            emit=emit_stderr,
        )
        return None
    return _build_rated_eligible_player_ids_parquet(
        raw_t_session_parquet=Path(args.raw_t_session_parquet),
        cutoff_dtm=cutoff,
        data_root=data_root,
        emit_stderr=emit_stderr,
        max_session_rows=int(getattr(args, "eligible_build_max_session_rows", 5_000_000)),
        duckdb_memory_limit_mb=getattr(args, "eligible_build_duckdb_memory_limit_mb", None),
        duckdb_threads=int(getattr(args, "eligible_build_duckdb_threads", 1)),
        failure_context_path=(
            Path(args.eligible_build_failure_context).resolve()
            if getattr(args, "eligible_build_failure_context", None) is not None
            else None
        ),
        run_log_path=(
            Path(args.eligible_build_run_log).resolve()
            if getattr(args, "eligible_build_run_log", None) is not None
            else None
        ),
    )


def _hash_preprocess_inputs_for_day(
    *,
    args: argparse.Namespace,
    sid: str,
    d: str,
    pre_paths: list[Path],
    fp_for_hash: Path | None,
    eligible_player_ids_parquet: Path | None,
) -> str:
    """``input_hash`` for preprocess; includes registry file stats when enabled."""
    reg = getattr(args, "ingestion_fix_registry_yaml", None)
    reg_p = reg.resolve() if reg is not None else None
    ver = getattr(args, "ingestion_fix_registry_version_expected", None)
    ver_s = str(ver).strip() if ver is not None and str(ver).strip() else None
    return hash_preprocess_inputs(
        source_snapshot_id=sid,
        gaming_day=d,
        preprocess_input_paths=pre_paths,
        fingerprint_path=fp_for_hash,
        eligible_player_ids_parquet=eligible_player_ids_parquet,
        ingestion_fix_registry_path=reg_p,
        ingestion_fix_registry_version_expected=ver_s,
    )


def _resolve_snapshot_id_for_day(
    d: str,
    *,
    args: argparse.Namespace,
    data_root: Path,
    py: str,
    dry_run: bool,
    echo_commands: bool,
    emit_stderr: Callable[[str], None] | None = None,
) -> tuple[str, Path | None, list[str], str]:
    """Return ``(snapshot_id, fingerprint_path_or_none, preprocess_input_paths, label)``."""
    if args.bet_parquet is not None:
        sid = args.source_snapshot_id.strip()
        paths = [str(args.bet_parquet.resolve())]
        return sid, None, paths, str(args.bet_parquet)

    if args.raw_t_bet_parquet is not None:
        bet_cmd = [
            py,
            str(_SCRIPTS / "l0_ingest.py"),
            "--data-root",
            str(data_root),
            "--anchor-path",
            str(_REPO_ROOT),
            "--table",
            "t_bet",
            "--partition-key",
            "gaming_day",
            "--partition-value",
            d,
            "--source",
            str(args.raw_t_bet_parquet.resolve()),
        ]
        if args.raw_t_session_parquet is not None:
            sess_cmd = [
                py,
                str(_SCRIPTS / "l0_ingest.py"),
                "--data-root",
                str(data_root),
                "--anchor-path",
                str(_REPO_ROOT),
                "--table",
                "t_session",
                "--partition-key",
                "gaming_day",
                "--partition-value",
                d,
                "--source",
                str(args.raw_t_session_parquet.resolve()),
            ]
        else:
            sess_cmd = None

        if dry_run:
            if echo_commands:
                _stderr_line(f"[orchestrator] dry-run would run: {' '.join(bet_cmd)}", emit=emit_stderr)
                if sess_cmd is not None:
                    _stderr_line(
                        f"[orchestrator] dry-run would run: {' '.join(sess_cmd)}",
                        emit=emit_stderr,
                    )
            return "dry_run_snap", None, [str(args.raw_t_bet_parquet.resolve())], "raw(dry-run)"

        blob = _run_step(
            bet_cmd,
            label=f"l0_ingest t_bet {d}",
            step=f"{d} L0-t_bet",
            capture_output=True,
            echo_commands=echo_commands,
            emit_stderr=emit_stderr,
        )
        assert blob is not None
        sid = _parse_l0_snapshot_id_from_ingest_output(blob)
        fp_path = data_root / "l0_layered" / sid / "snapshot_fingerprint.json"

        if sess_cmd is not None:
            _run_step(
                sess_cmd,
                label=f"l0_ingest t_session {d}",
                step=f"{d} L0-t_session",
                echo_commands=echo_commands,
                emit_stderr=emit_stderr,
            )

        parts = _l0_bet_parquet_paths_for_day(data_root, sid, d)
        if not parts:
            raise RuntimeError(f"L0 t_bet partition empty after ingest for {d} snapshot {sid}")
        return sid, fp_path, [str(p) for p in parts], f"L0 {len(parts)} part(s) snap={sid}"

    ids = discover_l0_snapshot_ids_for_partition(
        data_root,
        table="t_bet",
        partition_key="gaming_day",
        partition_value=d,
    )
    if not ids:
        raise RuntimeError(
            f"No existing L0 t_bet partition for gaming_day={d} under {data_root / 'l0_layered'}"
        )
    if len(ids) > 1:
        _stderr_line(
            f"[LDA] WARN multiple L0 snapshots for {d}: {ids}; using {ids[0]}",
            emit=emit_stderr,
        )
    sid = ids[0]
    fp_path = data_root / "l0_layered" / sid / "snapshot_fingerprint.json"
    parts = _l0_bet_parquet_paths_for_day(data_root, sid, d)
    return sid, fp_path if fp_path.is_file() else None, [str(p) for p in parts], f"L0 {len(parts)} part(s) snap={sid}"


def _run_lda_pipeline_for_day(
    d: str,
    *,
    args: argparse.Namespace,
    data_root: Path,
    py: str,
    user_fp: Path | None,
    gate_parent: Path,
    day_index: int,
    day_total: int,
    dry_run: bool,
    state_con: object | None,
    resume: bool,
    force: bool,
    echo_commands: bool,
    pbar: _DayRangeProgressBar,
    eligible_player_ids_parquet: Path | None,
) -> bool:
    """Run L0 (optional), L1 preprocess + three run_* jobs, and three Gate1 invocations.

    Returns:
        ``True`` if caller should stop iterating days (``--stop-after-date`` hit after success).
    """
    emit = pbar.write_stderr_line
    if echo_commands:
        emit(f"[orchestrator] === Day {day_index}/{day_total}: {d} ===")
    pbar.set_postfix_str(f"{d} discover")
    sid, fp_ingest, pre_inputs, label_in = _resolve_snapshot_id_for_day(
        d,
        args=args,
        data_root=data_root,
        py=py,
        dry_run=dry_run,
        echo_commands=echo_commands,
        emit_stderr=emit,
    )
    if echo_commands:
        emit(f"[orchestrator]   snapshot_id={sid}  preprocess inputs ({label_in})")
        emit(f"[orchestrator]   paths: {pre_inputs}")

    fp_args = _fp_args_for_materialize(fp_from_ingest=fp_ingest, user_fp=user_fp)
    if dry_run:
        return False

    fp_for_hash = _effective_fp_for_hash(fp_ingest, user_fp)
    pre_paths = [Path(p) for p in pre_inputs]
    h_pre = _hash_preprocess_inputs_for_day(
        args=args,
        sid=sid,
        d=d,
        pre_paths=pre_paths,
        fp_for_hash=fp_for_hash,
        eligible_player_ids_parquet=eligible_player_ids_parquet,
    )

    pre_cmd = [
        py,
        str(_SCRIPTS / "preprocess_bet_v1.py"),
        "--data-root",
        str(data_root),
        "--source-snapshot-id",
        sid,
        "--gaming-day",
        d,
        *fp_args,
        *_ingestion_registry_cli_args(args),
    ]
    for path in pre_inputs:
        pre_cmd.extend(["--input", path])
    if eligible_player_ids_parquet is not None:
        pre_cmd.extend(["--eligible-player-ids-parquet", str(eligible_player_ids_parquet)])
    cleaned = l1_bet_cleaned_parquet_path(data_root, sid, d)
    pbar.set_postfix_str(f"{d} preprocess")
    _run_tracked_subprocess(
        state_con=state_con,
        artifact_kind=ARTIFACT_PREPROCESS_BET,
        gaming_day=d,
        sid=sid,
        input_hash=h_pre,
        resume=resume,
        force=force,
        label=f"preprocess {d}",
        step=f"{d} preprocess",
        cmd=pre_cmd,
        output_uri=cleaned.resolve().as_posix(),
        row_count_parquet=cleaned,
        echo_commands=echo_commands,
        emit_stderr=emit,
    )

    common_mat = [
        "--data-root",
        str(data_root),
        "--source-snapshot-id",
        sid,
        "--input",
        str(cleaned),
        *fp_args,
    ]
    rf_cmd = [
        py,
        str(_SCRIPTS / "materialize_run_fact_v1.py"),
        *common_mat,
        "--run-end-gaming-day",
        d,
        "--l1-preprocess-gaming-day",
        d,
    ]
    rf_out = l1_run_fact_partition_dir(data_root, sid, d) / "run_fact.parquet"
    pbar.set_postfix_str(f"{d} L1 run_fact")
    h_rf = hash_run_materialize_inputs(
        artifact_kind=ARTIFACT_RUN_FACT,
        source_snapshot_id=sid,
        gaming_day=d,
        cleaned_parquet=cleaned,
        fingerprint_path=fp_for_hash,
    )
    _run_tracked_subprocess(
        state_con=state_con,
        artifact_kind=ARTIFACT_RUN_FACT,
        gaming_day=d,
        sid=sid,
        input_hash=h_rf,
        resume=resume,
        force=force,
        label=f"run_fact {d}",
        step=f"{d} run_fact",
        cmd=rf_cmd,
        output_uri=rf_out.resolve().as_posix(),
        row_count_parquet=rf_out,
        echo_commands=echo_commands,
        emit_stderr=emit,
    )

    bm_cmd = [
        py,
        str(_SCRIPTS / "materialize_run_bet_map_v1.py"),
        *common_mat,
        "--run-end-gaming-day",
        d,
        "--l1-preprocess-gaming-day",
        d,
    ]
    bm_out = l1_run_bet_map_partition_dir(data_root, sid, d) / "run_bet_map.parquet"
    pbar.set_postfix_str(f"{d} L1 run_bet_map")
    h_bm = hash_run_materialize_inputs(
        artifact_kind=ARTIFACT_RUN_BET_MAP,
        source_snapshot_id=sid,
        gaming_day=d,
        cleaned_parquet=cleaned,
        fingerprint_path=fp_for_hash,
    )
    _run_tracked_subprocess(
        state_con=state_con,
        artifact_kind=ARTIFACT_RUN_BET_MAP,
        gaming_day=d,
        sid=sid,
        input_hash=h_bm,
        resume=resume,
        force=force,
        label=f"run_bet_map {d}",
        step=f"{d} run_bet_map",
        cmd=bm_cmd,
        output_uri=bm_out.resolve().as_posix(),
        row_count_parquet=bm_out,
        echo_commands=echo_commands,
        emit_stderr=emit,
    )

    br_cmd = [
        py,
        str(_SCRIPTS / "materialize_run_day_bridge_v1.py"),
        *common_mat,
        "--bet-gaming-day",
        d,
        "--l1-preprocess-gaming-day",
        d,
    ]
    br_out = l1_run_day_bridge_partition_dir(data_root, sid, d) / "run_day_bridge.parquet"
    pbar.set_postfix_str(f"{d} L1 run_day_bridge")
    h_br = hash_run_materialize_inputs(
        artifact_kind=ARTIFACT_RUN_DAY_BRIDGE,
        source_snapshot_id=sid,
        gaming_day=d,
        cleaned_parquet=cleaned,
        fingerprint_path=fp_for_hash,
    )
    _run_tracked_subprocess(
        state_con=state_con,
        artifact_kind=ARTIFACT_RUN_DAY_BRIDGE,
        gaming_day=d,
        sid=sid,
        input_hash=h_br,
        resume=resume,
        force=force,
        label=f"run_day_bridge {d}",
        step=f"{d} run_day_bridge",
        cmd=br_cmd,
        output_uri=br_out.resolve().as_posix(),
        row_count_parquet=br_out,
        echo_commands=echo_commands,
        emit_stderr=emit,
    )

    for art in _GATE1_ARTIFACTS:
        out_dir = gate_parent.resolve() / f"gate1_{d}_{art}"
        g_cmd = [
            py,
            str(_SCRIPTS / "gate1_l1_determinism_v1.py"),
            "--artifact",
            art,
            "--data-root",
            str(data_root),
            "--l1-source-snapshot-id",
            sid,
            "--l1-preprocess-gaming-day",
            d,
            "--output-dir",
            str(out_dir),
        ]
        if art in ("run_fact", "run_bet_map"):
            g_cmd.extend(["--run-end-gaming-day", d])
        else:
            g_cmd.extend(["--bet-gaming-day", d])
        if args.verbose:
            g_cmd.append("--verbose")
        if args.profiles_json:
            g_cmd.extend(["--profiles-json", args.profiles_json])
        pbar.set_postfix_str(f"{d} Gate1 {art}")
        g_kind = _GATE1_STATE_KIND[art]
        h_g = hash_gate1_inputs(
            artifact_kind=g_kind,
            source_snapshot_id=sid,
            gaming_day=d,
            gate1_output_dir=out_dir,
            profiles_json=args.profiles_json,
        )
        _run_tracked_subprocess(
            state_con=state_con,
            artifact_kind=g_kind,
            gaming_day=d,
            sid=sid,
            input_hash=h_g,
            resume=resume,
            force=force,
            label=f"gate1 {art} {d}",
            step=f"{d} gate1-{art}",
            cmd=g_cmd,
            output_uri=out_dir.resolve().as_posix(),
            row_count_parquet=None,
            echo_commands=echo_commands,
            emit_stderr=emit,
        )

    stop_after = getattr(args, "stop_after_date", None)
    if stop_after and stop_after.strip() == d.strip():
        emit(f"[LDA] stop-after-date {stop_after}: stopping after {d}")
        return True
    return False


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """CLI for day-range orchestration."""
    p = argparse.ArgumentParser(
        description=__doc__.strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """
            Raw t_bet (runs l0_ingest per day; fingerprint includes partition_value so snapshot_id
            usually differs each day). WARNING: repeating the same huge file per day duplicates L0
            storage under separate snap_* roots — prefer per-day raw files or --bet-parquet when L0
            is already materialized. Source Parquet must be L0 t_bet-shaped (player_id, bet_id,
            gaming_day); not training/feature slices. BET-DQ-03 is fail-closed: raw mode must
            pass either --eligible-player-ids-parquet, or --raw-t-session-parquet with --cutoff-dtm.

              python scripts/lda_l1_gate1_day_range_v1.py \\
                --date-from 2026-01-01 --date-to 2026-01-01 \\
                --raw-t-bet-parquet data/gmwds_t_bet.parquet \\
                --raw-t-session-parquet data/gmwds_t_session.parquet \\
                --cutoff-dtm 2026-01-31T23:59:59+08:00

            Skip L0; fixed L1 snapshot (multi-day bet file filtered in preprocess):

              python scripts/lda_l1_gate1_day_range_v1.py --source-snapshot-id snap_abc \\
                --date-from 2026-01-01 --date-to 2026-01-03 --bet-parquet data/gmwds_t_bet.parquet --verbose

            Use existing L0 layout (discover snap_* per day):

              python scripts/lda_l1_gate1_day_range_v1.py --date-from 2026-01-01 \\
                --date-to 2026-01-01 --l0-existing --verbose

            Dates only (requires data/gmwds_t_bet.parquet; same as trainer local export):

              python scripts/lda_l1_gate1_day_range_v1.py --date-from 2026-01-01 --date-to 2026-01-02

            All gaming_day values found in the default bet Parquet (omit both date flags):

              python scripts/lda_l1_gate1_day_range_v1.py --dry-run --no-progress
            """
        ).strip(),
    )
    p.add_argument(
        "--source-snapshot-id",
        default=None,
        help="Required with --bet-parquet: L1 / lineage id. Must not be set with --raw-t-bet-parquet or --l0-existing.",
    )
    p.add_argument(
        "--date-from",
        default=None,
        metavar="YYYY-MM-DD",
        help="First calendar day (inclusive). Omit both --date-from and --date-to to run every gaming_day present in the bet source.",
    )
    p.add_argument(
        "--date-to",
        default=None,
        metavar="YYYY-MM-DD",
        help="Last calendar day (inclusive). Omit both date flags to run every gaming_day present in the bet source.",
    )
    src = p.add_mutually_exclusive_group(required=False)
    src.add_argument(
        "--bet-parquet",
        type=Path,
        help="Skip L0; one bet Parquet (multi-day ok); preprocess filters each --gaming-day",
    )
    src.add_argument(
        "--raw-t-bet-parquet",
        type=Path,
        help="Raw t_bet Parquet: run l0_ingest per calendar day before preprocess",
    )
    src.add_argument(
        "--l0-existing",
        action="store_true",
        help="Use existing L0 t_bet partitions; discover snap_* per day under l0_layered",
    )
    p.add_argument(
        "--raw-t-session-parquet",
        type=Path,
        default=None,
        help=(
            "Raw t_session Parquet for raw mode: l0_ingest per day after t_bet, and (with --cutoff-dtm) "
            "auto-build BET-DQ-03 eligible-player-ids via trainer.identity"
        ),
    )
    p.add_argument(
        "--eligible-player-ids-parquet",
        type=Path,
        default=None,
        help="Optional explicit BET-DQ-03 allowlist parquet (single player_id column) forwarded to preprocess",
    )
    p.add_argument(
        "--cutoff-dtm",
        type=str,
        default=None,
        metavar="ISO_DATETIME",
        help=(
            "Required for raw-mode auto-build from --raw-t-session-parquet. "
            "ISO-8601, e.g. 2026-01-31T23:59:59+08:00"
        ),
    )
    p.add_argument(
        "--eligible-build-max-session-rows",
        type=int,
        default=5_000_000,
        metavar="N",
        help=(
            "After cutoff filter on t_session, fail if row count exceeds N before loading into pandas "
            "(0 disables; default 5_000_000 to reduce laptop OOM risk)"
        ),
    )
    p.add_argument(
        "--eligible-build-duckdb-memory-limit-mb",
        type=int,
        default=None,
        metavar="MB",
        help="Optional DuckDB SET memory_limit for eligible build scan (>= 64 if set; see oom_runner_v1)",
    )
    p.add_argument(
        "--eligible-build-duckdb-threads",
        type=int,
        default=1,
        metavar="N",
        help="DuckDB threads for eligible build (default 1 to reduce peak RAM)",
    )
    p.add_argument(
        "--eligible-build-failure-context",
        type=Path,
        default=None,
        metavar="PATH",
        help="JSON diagnostics path on eligible auto-build failure (default: under data/tmp_lda_gate1_day_range/eligible/)",
    )
    p.add_argument(
        "--eligible-build-run-log",
        type=Path,
        default=None,
        metavar="PATH",
        help="Append JSONL for eligible auto-build phases (count / done)",
    )
    p.add_argument(
        "--l0-fingerprint-json",
        type=Path,
        default=None,
        help="Optional fingerprint override for preprocess/materialize when not using ingest output",
    )
    p.add_argument(
        "--ingestion-fix-registry-yaml",
        type=Path,
        default=None,
        help=(
            "Ingestion registry YAML for preprocess_bet_v1 (BET-INGEST-FIX-004 + synthetic observed-at). "
            "Default: repo schema/preprocess_bet_ingestion_fix_registry.yaml (required; must exist)."
        ),
    )
    p.add_argument(
        "--ingestion-fix-registry-version-expected",
        type=str,
        default=None,
        help="Optional fail-fast registry_version check (forwarded to preprocess_bet_v1)",
    )
    p.add_argument(
        "--profiles-json",
        default=None,
        help="Forwarded to gate1 (e.g. '[[null,2],[null,1]]'); omit for gate1 defaults",
    )
    p.add_argument("--verbose", action="store_true", help="Forward --verbose to each gate1")
    p.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable tqdm bar over calendar days",
    )
    p.add_argument(
        "--echo-commands",
        action="store_true",
        help="Verbose operator mode: print full subprocess argv and stream worker output (default: concise [LDA] lines + captured summaries)",
    )
    p.add_argument(
        "--gate1-output-parent",
        type=Path,
        default=None,
        help="Parent dir for gate1 outputs (default: <repo>/data/tmp_lda_gate1_day_range)",
    )
    p.add_argument("--dry-run", action="store_true", help="Print planned commands / discovery only")
    p.add_argument(
        "--state-store",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "DuckDB file for materialization_state (LDA-E1-09). When omitted but --resume or --force is set, "
            "defaults to <repo>/data/l1_layered/materialization_state.duckdb"
        ),
    )
    p.add_argument(
        "--resume",
        action="store_true",
        help="Skip steps that already succeeded with the same input_hash (requires state DB; see --state-store)",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Ignore succeeded rows and rerun all steps in the date range (updates state DB)",
    )
    p.add_argument(
        "--stop-after-date",
        default=None,
        metavar="YYYY-MM-DD",
        help="After successfully finishing this calendar day (including Gate1), exit without later days",
    )
    return p.parse_args(argv)


def _resolve_orchestrator_days(args: argparse.Namespace, *, data_root: Path) -> list[str]:
    """Return ordered ``YYYY-MM-DD`` day list: explicit inclusive range or distinct source days.

    Raises:
        ValueError: If only one of ``--date-from`` / ``--date-to`` is set, or resolution fails.
        FileNotFoundError: If a Parquet path for auto-discovery is missing.
    """
    raw_from = getattr(args, "date_from", None)
    raw_to = getattr(args, "date_to", None)
    d_from = raw_from.strip() if raw_from is not None and str(raw_from).strip() else ""
    d_to = raw_to.strip() if raw_to is not None and str(raw_to).strip() else ""
    if bool(d_from) ^ bool(d_to):
        raise ValueError(
            "Pass both --date-from and --date-to, or omit both to use all gaming_day values from the bet source."
        )
    if d_from and d_to:
        return inclusive_iso_date_strings(d_from, d_to)
    if args.bet_parquet is not None:
        return distinct_gaming_days_from_t_bet_parquet(Path(args.bet_parquet))
    if args.raw_t_bet_parquet is not None:
        return distinct_gaming_days_from_t_bet_parquet(Path(args.raw_t_bet_parquet))
    if args.l0_existing:
        return distinct_gaming_days_from_l0_t_bet_layout(data_root)
    raise RuntimeError("internal: no source mode after validate_mode")


def _validate_mode(args: argparse.Namespace) -> int | None:
    """Return exit code on validation error, or None if OK."""
    n_raw = int(args.raw_t_bet_parquet is not None)
    n_bet = int(args.bet_parquet is not None)
    n_l0 = int(args.l0_existing)
    if n_raw + n_bet + n_l0 != 1:
        print(
            "Choose exactly one of: --raw-t-bet-parquet, --bet-parquet, --l0-existing",
            file=sys.stderr,
        )
        return 2
    if args.bet_parquet is not None:
        if not args.source_snapshot_id or not str(args.source_snapshot_id).strip():
            print("--source-snapshot-id is required with --bet-parquet", file=sys.stderr)
            return 2
    elif args.source_snapshot_id:
        print(
            "Do not pass --source-snapshot-id with --raw-t-bet-parquet or --l0-existing "
            "(snapshot is derived per day).",
            file=sys.stderr,
        )
        return 2
    if args.raw_t_session_parquet is not None and args.raw_t_bet_parquet is None:
        print("--raw-t-session-parquet requires --raw-t-bet-parquet", file=sys.stderr)
        return 2
    if args.raw_t_bet_parquet is not None:
        has_explicit_eligible = args.eligible_player_ids_parquet is not None
        has_raw_session = args.raw_t_session_parquet is not None
        has_cutoff = bool(getattr(args, "cutoff_dtm", None) and str(args.cutoff_dtm).strip())
        if not has_explicit_eligible and not has_raw_session:
            print(
                "raw mode requires BET-DQ-03 allowlist: pass --eligible-player-ids-parquet "
                "or --raw-t-session-parquet with --cutoff-dtm",
                file=sys.stderr,
            )
            return 2
        if has_raw_session and not has_explicit_eligible and not has_cutoff:
            print("--cutoff-dtm is required with --raw-t-session-parquet in raw mode", file=sys.stderr)
            return 2
    if getattr(args, "cutoff_dtm", None) and str(args.cutoff_dtm).strip():
        try:
            _parse_cutoff_dtm(str(args.cutoff_dtm))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    em = int(getattr(args, "eligible_build_max_session_rows", 0))
    if em < 0:
        print("--eligible-build-max-session-rows must be >= 0", file=sys.stderr)
        return 2
    et = int(getattr(args, "eligible_build_duckdb_threads", 1))
    if et < 1:
        print("--eligible-build-duckdb-threads must be >= 1", file=sys.stderr)
        return 2
    emem = getattr(args, "eligible_build_duckdb_memory_limit_mb", None)
    if emem is not None and int(emem) < 64:
        print("--eligible-build-duckdb-memory-limit-mb must be >= 64 when set", file=sys.stderr)
        return 2
    if bool(args.resume) and bool(args.force):
        print("[LDA] NOTE: both --resume and --force; --force wins (no skips)", file=sys.stderr)
    return None


def main(argv: list[str] | None = None) -> int:
    """Run the end-to-end pipeline for each day in the inclusive range."""
    args = _parse_args(argv)
    try:
        apply_default_lda_source_args(args, data_root=LDA_FIXED_DATA_ROOT)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    err = _validate_mode(args)
    if err is not None:
        return err
    try:
        apply_default_ingestion_registry_args(args)
    except (ValueError, FileNotFoundError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    data_root = LDA_FIXED_DATA_ROOT
    try:
        days = _resolve_orchestrator_days(args, data_root=data_root)
    except (ValueError, FileNotFoundError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if getattr(args, "stop_after_date", None):
        stop_d = str(args.stop_after_date).strip()
        if stop_d not in days:
            print(
                f"--stop-after-date {stop_d!r} must be one of the planned gaming_day values (inclusive list)",
                file=sys.stderr,
            )
            return 2

    py = sys.executable
    gate_parent = args.gate1_output_parent or (data_root / "tmp_lda_gate1_day_range")
    user_fp = args.l0_fingerprint_json.resolve() if args.l0_fingerprint_json else None
    try:
        eligible_player_ids_parquet = _resolve_eligible_player_ids_parquet(
            args=args,
            data_root=data_root,
            dry_run=bool(args.dry_run),
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    _print_run_banner(
        args=args,
        days=days,
        data_root=data_root,
        gate_parent=gate_parent,
        eligible_player_ids_parquet=eligible_player_ids_parquet,
    )

    state_con: object | None = None
    if _state_tracking_enabled(args) and not args.dry_run:
        try:
            import duckdb
        except ImportError:
            print("duckdb is required for --state-store / --resume / --force (see requirements.txt).", file=sys.stderr)
            return 2
        state_path = _resolve_state_db_path(args, data_root=data_root)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_con = duckdb.connect(str(state_path))
        ensure_materialization_state_schema(state_con)

    resume = bool(args.resume) and not bool(args.force)
    force = bool(args.force)
    echo_commands = bool(getattr(args, "echo_commands", False))
    pbar = _DayRangeProgressBar(len(days), disable=args.no_progress)
    try:
        for i, d in enumerate(days, start=1):
            try:
                stop_early = _run_lda_pipeline_for_day(
                    d,
                    args=args,
                    data_root=data_root,
                    py=py,
                    user_fp=user_fp,
                    gate_parent=gate_parent,
                    day_index=i,
                    day_total=len(days),
                    dry_run=args.dry_run,
                    state_con=state_con,
                    resume=resume,
                    force=force,
                    echo_commands=echo_commands,
                    pbar=pbar,
                    eligible_player_ids_parquet=eligible_player_ids_parquet,
                )
            except RuntimeError as exc:
                pbar.write_stderr_line(f"[LDA] ABORT day {d}: {exc}")
                raise
            pbar.update(1)
            if stop_early:
                break
    finally:
        pbar.close()
        if state_con is not None:
            state_con.close()

    if args.dry_run:
        print(f"[LDA] dry-run done ({len(days)} day(s))", file=sys.stderr, flush=True)
        return 0

    print(f"[LDA] OK completed {len(days)} day(s)", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
