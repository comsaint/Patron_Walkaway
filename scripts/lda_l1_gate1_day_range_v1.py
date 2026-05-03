#!/usr/bin/env python3
"""Phase-1 LDA end-to-end per calendar day: raw L0 (optional) → preprocess → L1 run_* → Gate1.

**End-to-end** here means: optional **raw** Parquet ingested with ``l0_ingest.py`` (``t_bet``; optional
``t_session``), then ``preprocess_bet_v1``, then ``run_fact`` / ``run_bet_map`` / ``run_day_bridge``,
then **Gate1** on each of those three artifacts. ``t_session`` is only landed to L0 today (no L1
consumer in this script).

**Input modes** (pick one, or omit for **default** — see below):

* **Default (no flag)** — if ``<repo>/data/gmwds_t_bet.parquet`` exists: same as ``--bet-parquet`` on that
  file with ``--source-snapshot-id snap_gmwds_t_bet_local`` (skip L0; preprocess filters each day).
  Matches README local Parquet layout. If the file is missing, exit with a message to pass an explicit mode.
* ``--raw-t-bet-parquet`` — run L0 ingest per day (same file repeated is disk-heavy; see epilog).
* ``--bet-parquet`` — skip L0; requires ``--source-snapshot-id`` for L1 paths (override default snap id).
* ``--l0-existing`` — discover ``snap_*`` under ``l0_layered`` that already has ``t_bet`` parts for
  each ``gaming_day`` (deterministic: lexicographically first if several match).

Exits non-zero on first failing subprocess.

All L0/L1 paths use **``<repo>/data``** (same as other LDA CLIs); this orchestrator does **not**
accept ``--data-root`` so runs stay tied to the repo layout.

**Resumable state (LDA-E1-09)**: optional DuckDB **``materialization_state``** via ``--state-store``,
``--resume`` (skip succeeded steps when ``input_hash`` unchanged), ``--force`` (rerun all steps),
and ``--stop-after-date`` (exit after one successful day). Default DB path when ``--resume``/``--force``
omit ``--state-store``: ``data/l1_layered/materialization_state.duckdb``. See ``layered_data_assets/RUNBOOK.md`` §5.1.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import textwrap
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO_ROOT / "scripts"
LDA_FIXED_DATA_ROOT = (_REPO_ROOT / "data").resolve()
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from layered_data_assets.l0_paths import (  # noqa: E402
    discover_l0_snapshot_ids_for_partition,
    l0_partition_dir,
)
from layered_data_assets.l1_paths import (  # noqa: E402
    l1_bet_cleaned_parquet_path,
    l1_run_bet_map_partition_dir,
    l1_run_day_bridge_partition_dir,
    l1_run_fact_partition_dir,
)
from layered_data_assets.lda_day_range_v1 import inclusive_iso_date_strings  # noqa: E402
from layered_data_assets.materialization_state_store_v1 import (  # noqa: E402
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
            )
        except ImportError:
            self._inner = None

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
) -> None:
    """Print a short human-readable plan to stderr."""
    n = len(days)
    if args.bet_parquet is not None:
        tag = ""
        if getattr(args, "_lda_defaulted_local_t_bet", False):
            tag = "  [default: repo data/gmwds_t_bet.parquet + local snap id]"
        src = f"--bet-parquet {args.bet_parquet}  (L1 snap={args.source_snapshot_id}){tag}"
    elif args.raw_t_bet_parquet is not None:
        extra = f"  optional session={args.raw_t_session_parquet}" if args.raw_t_session_parquet else ""
        src = f"--raw-t-bet-parquet {args.raw_t_bet_parquet}{extra}"
    else:
        src = "--l0-existing (discover snap_* per day from l0_layered)"
    lines = [
        "[orchestrator] -----------------------------------------------------------",
        f"[orchestrator] Plan: {n} calendar day(s)  {days[0]} .. {days[-1]}",
        f"[orchestrator] data-root (fixed): {data_root}",
        f"[orchestrator] Input mode: {src}",
        f"[orchestrator] Gate1 outputs under: {gate_parent.resolve()}",
        "[orchestrator] Per day: L0(raw)? -> preprocess -> run_fact -> run_bet_map -> run_day_bridge",
        "[orchestrator]           -> gate1(run_fact) -> gate1(run_bet_map) -> gate1(run_day_bridge)",
        "[orchestrator] (DuckDB steps may take a long time on large files.)",
    ]
    if _state_tracking_enabled(args):
        sp = _resolve_state_db_path(args, data_root=data_root)
        sfx = f"resume={bool(args.resume)} force={bool(args.force)}"
        lines.append(f"[orchestrator] Materialization state (LDA-E1-09): {sp}  ({sfx})")
    if getattr(args, "stop_after_date", None):
        lines.append(f"[orchestrator] Stop after day: {args.stop_after_date.strip()}")
    lines.extend(
        [
            "[orchestrator] -----------------------------------------------------------",
        ]
    )
    for ln in lines:
        print(ln, file=sys.stderr, flush=True)


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
) -> str | None:
    """Run one subprocess; raise :class:`RuntimeError` on failure. Optionally return merged stdout/stderr."""
    print(f"[orchestrator] >> {step} -- START {label}", file=sys.stderr, flush=True)
    print(f"[orchestrator]   command: {' '.join(cmd)}", file=sys.stderr, flush=True)
    t0 = time.monotonic()
    proc = subprocess.run(
        cmd,
        cwd=str(_REPO_ROOT),
        capture_output=capture_output,
        text=capture_output,
    )
    dt = time.monotonic() - t0
    blob = None
    if capture_output:
        blob = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if proc.returncode != 0:
        print(
            f"[orchestrator] !! {step} -- FAIL {label} after {dt:.1f}s (exit {proc.returncode})",
            file=sys.stderr,
            flush=True,
        )
        if capture_output and blob:
            print(blob[-4000:], file=sys.stderr, flush=True)
        raise RuntimeError(f"{label} failed with exit code {proc.returncode}")
    print(f"[orchestrator] OK {step} -- DONE {label} in {dt:.1f}s", file=sys.stderr, flush=True)
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
) -> None:
    """Optionally honor materialization state (skip / mark running / succeeded / failed)."""
    if state_con is None:
        _run_step(cmd, label=label, step=step)
        return
    prev = fetch_state_row(
        state_con,
        artifact_kind=artifact_kind,
        gaming_day=gaming_day,
        source_snapshot_id=sid,
    )
    do_skip = should_skip_step(resume=resume, force=force, row=prev, input_hash=input_hash)
    if do_skip and row_count_parquet is not None and not Path(row_count_parquet).is_file():
        print(
            f"[orchestrator] WARN cannot skip {step}: expected output {row_count_parquet} missing",
            file=sys.stderr,
            flush=True,
        )
        do_skip = False
    if do_skip and artifact_kind.startswith("gate1_") and output_uri:
        out_p = Path(output_uri)
        if not out_p.is_dir() or not any(out_p.iterdir()):
            print(
                f"[orchestrator] WARN cannot skip {step}: gate1 output dir missing or empty ({out_p})",
                file=sys.stderr,
                flush=True,
            )
            do_skip = False
    if do_skip:
        print(
            f"[orchestrator] SKIP {step} (resume: succeeded, input_hash unchanged)",
            file=sys.stderr,
            flush=True,
        )
        return
    attempt = mark_step_running(
        state_con,
        artifact_kind=artifact_kind,
        gaming_day=gaming_day,
        source_snapshot_id=sid,
        input_hash=input_hash,
    )
    try:
        _run_step(cmd, label=label, step=step)
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


def _resolve_snapshot_id_for_day(
    d: str,
    *,
    args: argparse.Namespace,
    data_root: Path,
    py: str,
    dry_run: bool,
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
            print(f"[orchestrator] dry-run would run: {' '.join(bet_cmd)}", file=sys.stderr, flush=True)
            if sess_cmd is not None:
                print(f"[orchestrator] dry-run would run: {' '.join(sess_cmd)}", file=sys.stderr, flush=True)
            return "dry_run_snap", None, [str(args.raw_t_bet_parquet.resolve())], "raw(dry-run)"

        blob = _run_step(bet_cmd, label=f"l0_ingest t_bet {d}", step=f"{d} L0-t_bet", capture_output=True)
        assert blob is not None
        sid = _parse_l0_snapshot_id_from_ingest_output(blob)
        fp_path = data_root / "l0_layered" / sid / "snapshot_fingerprint.json"

        if sess_cmd is not None:
            _run_step(sess_cmd, label=f"l0_ingest t_session {d}", step=f"{d} L0-t_session")

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
        print(
            f"[orchestrator] WARN multiple L0 snapshots for {d}: {ids}; using {ids[0]}",
            file=sys.stderr,
            flush=True,
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
) -> bool:
    """Run L0 (optional), L1 preprocess + three run_* jobs, and three Gate1 invocations.

    Returns:
        ``True`` if caller should stop iterating days (``--stop-after-date`` hit after success).
    """
    print(
        f"[orchestrator] === Day {day_index}/{day_total}: {d} ===",
        file=sys.stderr,
        flush=True,
    )
    sid, fp_ingest, pre_inputs, label_in = _resolve_snapshot_id_for_day(
        d, args=args, data_root=data_root, py=py, dry_run=dry_run
    )
    print(f"[orchestrator]   snapshot_id={sid}  preprocess inputs ({label_in})", file=sys.stderr, flush=True)
    print(f"[orchestrator]   paths: {pre_inputs}", file=sys.stderr, flush=True)

    fp_args = _fp_args_for_materialize(fp_from_ingest=fp_ingest, user_fp=user_fp)
    if dry_run:
        return False

    fp_for_hash = _effective_fp_for_hash(fp_ingest, user_fp)
    pre_paths = [Path(p) for p in pre_inputs]
    h_pre = hash_preprocess_inputs(
        source_snapshot_id=sid,
        gaming_day=d,
        preprocess_input_paths=pre_paths,
        fingerprint_path=fp_for_hash,
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
    ]
    for path in pre_inputs:
        pre_cmd.extend(["--input", path])
    cleaned = l1_bet_cleaned_parquet_path(data_root, sid, d)
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
        )

    stop_after = getattr(args, "stop_after_date", None)
    if stop_after and stop_after.strip() == d.strip():
        print(
            f"[orchestrator] --stop-after-date {stop_after}: stopping day-range loop after {d}",
            file=sys.stderr,
            flush=True,
        )
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
            gaming_day); not training/feature slices.

              python scripts/lda_l1_gate1_day_range_v1.py \\
                --date-from 2026-01-01 --date-to 2026-01-01 --raw-t-bet-parquet data/gmwds_t_bet.parquet

            Skip L0; fixed L1 snapshot (multi-day bet file filtered in preprocess):

              python scripts/lda_l1_gate1_day_range_v1.py --source-snapshot-id snap_abc \\
                --date-from 2026-01-01 --date-to 2026-01-03 --bet-parquet data/gmwds_t_bet.parquet --verbose

            Use existing L0 layout (discover snap_* per day):

              python scripts/lda_l1_gate1_day_range_v1.py --date-from 2026-01-01 \\
                --date-to 2026-01-01 --l0-existing --verbose

            Dates only (requires data/gmwds_t_bet.parquet; same as trainer local export):

              python scripts/lda_l1_gate1_day_range_v1.py --date-from 2026-01-01 --date-to 2026-01-02
            """
        ).strip(),
    )
    p.add_argument(
        "--source-snapshot-id",
        default=None,
        help="Required with --bet-parquet: L1 / lineage id. Must not be set with --raw-t-bet-parquet or --l0-existing.",
    )
    p.add_argument("--date-from", required=True, metavar="YYYY-MM-DD", help="First calendar day (inclusive)")
    p.add_argument("--date-to", required=True, metavar="YYYY-MM-DD", help="Last calendar day (inclusive)")
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
        help="Optional raw t_session Parquet: l0_ingest per day after t_bet (same gaming_day partition)",
    )
    p.add_argument(
        "--l0-fingerprint-json",
        type=Path,
        default=None,
        help="Optional fingerprint override for preprocess/materialize when not using ingest output",
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
    if bool(args.resume) and bool(args.force):
        print("[orchestrator] NOTE: both --resume and --force set; --force wins (no skips)", file=sys.stderr)
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

    data_root = LDA_FIXED_DATA_ROOT
    try:
        days = inclusive_iso_date_strings(args.date_from, args.date_to)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if getattr(args, "stop_after_date", None):
        stop_d = str(args.stop_after_date).strip()
        if stop_d not in days:
            print(
                f"--stop-after-date {stop_d!r} must fall within --date-from .. --date-to (inclusive)",
                file=sys.stderr,
            )
            return 2

    py = sys.executable
    gate_parent = args.gate1_output_parent or (data_root / "tmp_lda_gate1_day_range")
    user_fp = args.l0_fingerprint_json.resolve() if args.l0_fingerprint_json else None

    _print_run_banner(args=args, days=days, data_root=data_root, gate_parent=gate_parent)

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
    pbar = _DayRangeProgressBar(len(days), disable=args.no_progress)
    try:
        for i, d in enumerate(days, start=1):
            pbar.set_postfix_str(d)
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
                )
            except RuntimeError as exc:
                print(f"[orchestrator] ABORT day {d}: {exc}", file=sys.stderr, flush=True)
                raise
            pbar.update(1)
            if stop_early:
                break
    finally:
        pbar.close()
        if state_con is not None:
            state_con.close()

    if args.dry_run:
        print(f"[orchestrator] dry-run finished ({len(days)} day slot(s))", file=sys.stderr, flush=True)
        return 0

    print(f"[orchestrator] OK completed {len(days)} day(s)", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
