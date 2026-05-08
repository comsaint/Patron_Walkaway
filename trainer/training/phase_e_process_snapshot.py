"""Phase E diagnostics: process RSS and system available RAM (psutil).

Used only when ``config.A3_PHASE_E_DIAG_MEMORY_SNAPSHOT`` is true.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional


def phase_e_diag_memory_enabled(cfg: Any) -> bool:
    """Return True when Phase E should emit memory snapshot logs."""
    return bool(getattr(cfg, "A3_PHASE_E_DIAG_MEMORY_SNAPSHOT", False))


def log_phase_e_memory(
    logger: logging.Logger,
    tag: str,
    *,
    cfg: Any,
    extra: Optional[Mapping[str, Any]] = None,
) -> None:
    """Log one line with RSS, VMS, and available RAM if diagnostics enabled."""
    if not phase_e_diag_memory_enabled(cfg):
        return
    try:
        import psutil
    except ImportError:
        logger.warning("A3 PhaseE_diag tag=%s psutil_unavailable", tag)
        return
    proc = psutil.Process()
    mi = proc.memory_info()
    rss_mb = float(mi.rss) / (1024.0**2)
    vms_mb = float(getattr(mi, "vms", 0)) / (1024.0**2)
    avail_gb: Optional[float] = None
    try:
        avail_gb = float(psutil.virtual_memory().available) / (1024.0**3)
    except Exception:
        avail_gb = None
    parts = [
        f"A3 PhaseE_diag tag={tag} rss_mb={rss_mb:.1f}",
        f"vms_mb={vms_mb:.1f}",
    ]
    if avail_gb is not None:
        parts.append(f"avail_ram_gb={avail_gb:.2f}")
    if extra:
        parts.extend(f"{k}={v}" for k, v in extra.items())
    logger.info(" ".join(parts))
