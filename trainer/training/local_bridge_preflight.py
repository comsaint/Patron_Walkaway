"""WS1: Auto-build preflight for local Parquet bridge ingress (Issue #14).

Ensures ``data/trainer_local_parquet_bridge.manifest.json`` exists and passes
:func:`trainer.training.data_sources.probe_trainer_local_parquet_bridge_readiness`
before training/backtest load paths call ``load_local_parquet``.

Materialization uses ``parallel_lda_mvp.trainer_bridge_mvp.emit_trainer_local_parquet``
against an MVP snapshot under ``data/parallel_lda_mvp/``, then copies the
written manifest from ``data/mvp_trainer_bridge/`` to the trainer ingress path.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path
from typing import Optional

from trainer.training.data_sources import (
    LOCAL_PARQUET_DIR,
    probe_trainer_local_parquet_bridge_readiness,
    trainer_local_parquet_bridge_manifest_path,
)


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


def ensure_local_bridge_ready_for_training(*, logger: logging.Logger) -> None:
    """If local bridge ingress is not ready, run bridge emit then re-probe.

    Raises
    ------
    RuntimeError
        If no snapshot exists, emit fails, or readiness never becomes true.
    """
    r0 = probe_trainer_local_parquet_bridge_readiness()
    if r0.ready:
        return

    reason_txt = "; ".join(r0.reasons) if r0.reasons else "unknown"
    msg_start = (
        f"AutoBuild: local bridge not ready ({reason_txt}). "
        "Materializing via trainer_bridge_mvp (may take several minutes on large data) …"
    )
    logger.warning(msg_start)
    print(msg_start, flush=True)

    snap = _resolve_default_snap_root(LOCAL_PARQUET_DIR)
    if snap is None:
        raise RuntimeError(
            "AutoBuild: no MVP snapshot found under "
            f"{LOCAL_PARQUET_DIR / 'parallel_lda_mvp'!s}. "
            "Run `python -m parallel_lda_mvp.run_mvp` once to materialize a snapshot, "
            "or set env PARALLEL_LDA_MVP_SNAPSHOT_ID to an existing snap_* folder name."
        )

    t0 = time.perf_counter()
    try:
        from parallel_lda_mvp.trainer_bridge_mvp import emit_trainer_local_parquet
    except ImportError as exc:
        raise RuntimeError(
            "AutoBuild: cannot import parallel_lda_mvp.trainer_bridge_mvp "
            f"({exc}). Install repo dependencies or run from repo root."
        ) from exc

    print(
        f"AutoBuild: emit_trainer_local_parquet snap_root={snap} data_dir={LOCAL_PARQUET_DIR}",
        flush=True,
    )
    try:
        written = emit_trainer_local_parquet(
            snap_root=snap,
            data_dir=LOCAL_PARQUET_DIR.resolve(),
            phase_c=True,
        )
    except Exception as exc:
        raise RuntimeError(
            f"AutoBuild: emit_trainer_local_parquet failed: {exc!r}. "
            "See logs above from [trainer_bridge_mvp] for stage details."
        ) from exc

    ingress = trainer_local_parquet_bridge_manifest_path()
    src_mf = Path(written).resolve()
    if not src_mf.is_file():
        raise RuntimeError(
            f"AutoBuild: expected manifest at {src_mf} after emit; got {written!r}"
        )
    _install_manifest_for_trainer_ingress(src_mf, ingress)
    elapsed = time.perf_counter() - t0
    done_msg = (
        f"AutoBuild: bridge manifest installed at {ingress} (elapsed_s={elapsed:.1f})."
    )
    logger.info(done_msg)
    print(done_msg, flush=True)

    r1 = probe_trainer_local_parquet_bridge_readiness()
    if not r1.ready:
        r1_txt = "; ".join(r1.reasons) if r1.reasons else "unknown"
        raise RuntimeError(
            f"AutoBuild: readiness still false after emit: {r1_txt}"
        )
