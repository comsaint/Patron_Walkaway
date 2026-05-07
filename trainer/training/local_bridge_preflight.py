"""WS1/WS2/WS3: Auto-build preflight for local Parquet bridge ingress (Issue #14).

WS4 v2: ClickHouse vs local branching for trainer/backtester/scorer/validator is centralized
in ``trainer.training.cross_entry_preflight.run_cross_entry_data_preflight``; local mode
still calls ``ensure_local_bridge_ready_for_training`` from there.

WS1: When an MVP snapshot exists under ``data/parallel_lda_mvp/``, call
``emit_trainer_local_parquet`` and copy ``data/mvp_trainer_bridge/`` manifest to
``data/trainer_local_parquet_bridge.manifest.json`` (trainer ingress).

WS2: When **no** snapshot exists, optionally run a **subprocess**
``python -m parallel_lda_mvp.run_mvp --emit-trainer-local-parquet`` from repo root
(full MVP + bridge emit in one pass per ``run_mvp``). Then copy the bridge manifest
only (no second emit). Disabled when env ``TRAINER_AUTOBUILD_FULL_MVP`` is
``0``/``false``/``no``/``off``.

WS3 (log contract): Every user-facing line uses ``AutoBuild[<phase>]: <message>``.
Phases (stable ids for grep / dashboards):

- ``orchestrate`` — not-ready banner, final probe failure, and end **summary**.
- ``bridge_emit`` — in-process ``emit_trainer_local_parquet`` + ingress install (snap path).
- ``mvp_full`` — L0 guard, subprocess ``run_mvp``, copy from bridge dir (no-snap path).

Example::

    AutoBuild[orchestrate]: local bridge not ready (...). Materializing ...
    AutoBuild[bridge_emit]: emit_trainer_local_parquet snap_root=...
    AutoBuild[summary]: total_s=12.345 bridge_emit_s=12.300

Subprocess stdout/stderr are **not** captured (no ``capture_output``); timing uses
``time.perf_counter`` deltas only.

L0 contract (``run_mvp`` defaults): ``resolve_t_bet_paths`` / ``resolve_t_session`` —
see ``parallel_lda_mvp/run_mvp.py`` (``PARALLEL_LDA_MVP_T_BET``, ``gmwds_t_bet.parquet``, etc.).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from trainer.training import data_sources

# Read ``LOCAL_PARQUET_DIR`` via ``data_sources`` so tests can monkeypatch it.
PROJECT_ROOT = data_sources.PROJECT_ROOT

_TRAINER_AUTOBUILD_FULL_MVP_ENV = "TRAINER_AUTOBUILD_FULL_MVP"


def _autobuild_log(
    phase: str,
    message: str,
    logger: logging.Logger,
    *,
    level: int = logging.INFO,
    also_print: bool = True,
) -> str:
    """Emit one ``AutoBuild[<phase>]:`` line to logger and optionally stdout."""
    full = f"AutoBuild[{phase}]: {message}"
    logger.log(level, full)
    if also_print:
        print(full, flush=True)
    return full


def _format_timing_suffix(timings: dict[str, float]) -> str:
    """Human-readable timing fragment for errors (ASCII-safe keys)."""
    parts: list[str] = []
    for key in ("bridge_emit", "mvp_full", "manifest_copy", "total"):
        if key in timings:
            parts.append(f"{key}_s={timings[key]:.3f}")
    return ("; " + " ".join(parts)) if parts else ""


def _format_summary_line(timings: dict[str, float]) -> str:
    """Single-line summary for successful materialization."""
    parts: list[str] = []
    if "total" in timings:
        parts.append(f"total_s={timings['total']:.3f}")
    for key in ("bridge_emit", "mvp_full", "manifest_copy"):
        if key in timings:
            parts.append(f"{key}_s={timings[key]:.3f}")
    return " ".join(parts) if parts else "total_s=0.000"


def _autobuild_full_mvp_enabled() -> bool:
    """Return False if ``TRAINER_AUTOBUILD_FULL_MVP`` disables full MVP autobuild."""
    v = os.environ.get(_TRAINER_AUTOBUILD_FULL_MVP_ENV, "").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    return True


def _resolve_default_snap_root(data_dir: Path) -> Optional[Path]:
    """Return MVP snapshot root under *data_dir* or ``None`` if none exist."""
    mvp = data_dir / "parallel_lda_mvp"
    if not mvp.is_dir():
        return None
    env = os.environ.get("PARALLEL_LDA_MVP_SNAPSHOT_ID", "").strip()
    if env:
        cand = (mvp / env).resolve()
        if cand.is_dir():
            return cand
    snaps = [p for p in mvp.iterdir() if p.is_dir() and p.name.startswith("snap_")]
    if not snaps:
        return None
    return max(snaps, key=lambda p: p.stat().st_mtime).resolve()


def _install_manifest_for_trainer_ingress(src: Path, dst: Path) -> None:
    """Copy *src* manifest JSON to trainer ingress path *dst*."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _assert_l0_inputs_for_full_mvp(data_dir: Path) -> None:
    """Fail-fast if ``run_mvp`` would not resolve t_bet / t_session (WS2 contract)."""
    try:
        from parallel_lda_mvp.run_mvp import resolve_t_bet_paths, resolve_t_session
    except ImportError as exc:
        raise RuntimeError(
            f"AutoBuild[mvp_full]: cannot import parallel_lda_mvp.run_mvp ({exc!r})."
        ) from exc
    root = data_dir.resolve()
    try:
        resolve_t_bet_paths(root)
        resolve_t_session(root)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "AutoBuild[mvp_full]: missing L0 Parquet inputs required for "
            "`python -m parallel_lda_mvp.run_mvp --emit-trainer-local-parquet`. "
            f"Detail: {exc}. "
            "Export t_bet/t_session to data/ (e.g. gmwds_t_bet.parquet) or set "
            "PARALLEL_LDA_MVP_T_BET / PARALLEL_LDA_MVP_T_SESSION."
        ) from exc


def _bridge_manifest_path_under_mvp_trainer_bridge() -> Path:
    """Path to manifest written by ``emit_trainer_local_parquet`` / end of ``run_mvp --emit-…``."""
    from parallel_lda_mvp.trainer_bridge_mvp import trainer_bridge_output_dir

    return trainer_bridge_output_dir(data_sources.LOCAL_PARQUET_DIR) / "trainer_local_parquet_bridge.manifest.json"


def _run_full_mvp_with_bridge_emit_subprocess(
    *,
    repo_root: Path,
    logger: logging.Logger,
    timings: Optional[dict[str, float]] = None,
) -> None:
    """Run ``run_mvp`` with ``--emit-trainer-local-parquet`` in a subprocess (cwd=repo root)."""
    cmd = [sys.executable, "-m", "parallel_lda_mvp.run_mvp", "--emit-trainer-local-parquet"]
    cmd_s = " ".join(cmd)
    _autobuild_log(
        "mvp_full",
        f"starting subprocess (high RAM / long runtime possible): {cmd_s} cwd={repo_root}",
        logger,
        level=logging.WARNING,
    )
    t0 = time.perf_counter()
    proc = subprocess.run(
        cmd,
        cwd=str(repo_root.resolve()),
        env=os.environ.copy(),
    )
    elapsed = time.perf_counter() - t0
    if timings is not None:
        timings["mvp_full"] = elapsed
    if proc.returncode != 0:
        suf = _format_timing_suffix(timings) if timings else ""
        raise RuntimeError(
            f"AutoBuild[mvp_full]: subprocess exit code={proc.returncode} after {elapsed:.1f}s. "
            f"Command: {cmd_s}. Inspect [parallel_lda_mvp] lines above.{suf}"
        )
    _autobuild_log(
        "mvp_full",
        f"subprocess finished OK elapsed_s={elapsed:.1f}",
        logger,
        level=logging.INFO,
    )


def _run_bridge_emit_only(
    snap: Path,
    *,
    logger: logging.Logger,
    ingress: Path,
    timings: Optional[dict[str, float]] = None,
) -> None:
    """Call ``emit_trainer_local_parquet`` for *snap* and install manifest at *ingress*."""
    t0 = time.perf_counter()
    try:
        from parallel_lda_mvp.trainer_bridge_mvp import emit_trainer_local_parquet
    except ImportError as exc:
        raise RuntimeError(
            f"AutoBuild[bridge_emit]: cannot import parallel_lda_mvp.trainer_bridge_mvp ({exc!r}). "
            "Install repo dependencies or run from repo root."
        ) from exc

    _autobuild_log(
        "bridge_emit",
        f"emit_trainer_local_parquet snap_root={snap} data_dir={data_sources.LOCAL_PARQUET_DIR}",
        logger,
        level=logging.INFO,
    )
    try:
        written = emit_trainer_local_parquet(
            snap_root=snap,
            data_dir=data_sources.LOCAL_PARQUET_DIR.resolve(),
            phase_c=True,
        )
    except Exception as exc:
        raise RuntimeError(
            f"AutoBuild[bridge_emit]: emit_trainer_local_parquet failed: {exc!r}. "
            "See [trainer_bridge_mvp] logs above."
        ) from exc

    src_mf = Path(written).resolve()
    if not src_mf.is_file():
        raise RuntimeError(
            f"AutoBuild[bridge_emit]: expected manifest at {src_mf} after emit; got {written!r}"
        )
    _install_manifest_for_trainer_ingress(src_mf, ingress)
    elapsed = time.perf_counter() - t0
    if timings is not None:
        timings["bridge_emit"] = elapsed
    _autobuild_log(
        "bridge_emit",
        f"manifest installed at {ingress} elapsed_s={elapsed:.1f}",
        logger,
        level=logging.INFO,
    )


def ensure_local_bridge_ready_for_training(*, logger: logging.Logger) -> None:
    """If local bridge ingress is not ready, materialize then re-probe.

    Order: probe → (snapshot: bridge emit only | no snapshot: full ``run_mvp`` + copy manifest).

    Raises
    ------
    RuntimeError
        If materialization is disabled, inputs are missing, subprocess fails, or readiness
        never becomes true.
    """
    r0 = data_sources.probe_trainer_local_parquet_bridge_readiness()
    if r0.ready:
        return

    t_total0 = time.perf_counter()
    timings: dict[str, float] = {}

    reason_txt = "; ".join(r0.reasons) if r0.reasons else "unknown"
    _autobuild_log(
        "orchestrate",
        f"local bridge not ready ({reason_txt}). "
        "Materializing (may take several minutes on large data) …",
        logger,
        level=logging.WARNING,
    )

    ingress = data_sources.trainer_local_parquet_bridge_manifest_path()
    snap = _resolve_default_snap_root(data_sources.LOCAL_PARQUET_DIR)

    if snap is not None:
        _run_bridge_emit_only(snap, logger=logger, ingress=ingress, timings=timings)
    else:
        if not _autobuild_full_mvp_enabled():
            raise RuntimeError(
                "AutoBuild[mvp_full]: no snapshot under data/parallel_lda_mvp/ and "
                f"{_TRAINER_AUTOBUILD_FULL_MVP_ENV} disables full MVP autobuild. "
                "Run `python -m parallel_lda_mvp.run_mvp` (or with --emit-trainer-local-parquet), "
                f"or set {_TRAINER_AUTOBUILD_FULL_MVP_ENV} unset/true to allow autobuild, "
                "or set PARALLEL_LDA_MVP_SNAPSHOT_ID to an existing snap_* folder."
            )
        _assert_l0_inputs_for_full_mvp(data_sources.LOCAL_PARQUET_DIR)
        _run_full_mvp_with_bridge_emit_subprocess(
            repo_root=PROJECT_ROOT, logger=logger, timings=timings
        )
        src_mf = _bridge_manifest_path_under_mvp_trainer_bridge()
        if not src_mf.is_file():
            raise RuntimeError(
                f"AutoBuild[mvp_full]: run_mvp finished but bridge manifest missing at {src_mf}. "
                "Check MVP logs for failures before the bridge emit step."
            )
        t_copy0 = time.perf_counter()
        _install_manifest_for_trainer_ingress(src_mf, ingress)
        timings["manifest_copy"] = time.perf_counter() - t_copy0
        _autobuild_log(
            "mvp_full",
            f"copied bridge manifest to {ingress} manifest_copy_s={timings['manifest_copy']:.3f}",
            logger,
            level=logging.INFO,
        )

    r1 = data_sources.probe_trainer_local_parquet_bridge_readiness()
    timings["total"] = time.perf_counter() - t_total0

    if not r1.ready:
        r1_txt = "; ".join(r1.reasons) if r1.reasons else "unknown"
        raise RuntimeError(
            f"AutoBuild[orchestrate]: readiness still false after materialization: {r1_txt}"
            f"{_format_timing_suffix(timings)}"
        )

    _autobuild_log(
        "summary",
        _format_summary_line(timings),
        logger,
        level=logging.INFO,
    )
