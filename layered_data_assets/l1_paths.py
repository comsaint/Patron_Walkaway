"""L1 directory layout under ``data/l1_layered/`` (aligned with L0 ``source_snapshot_id``)."""
from __future__ import annotations

from pathlib import Path

from layered_data_assets.l0_paths import validate_source_snapshot_id


def l1_layered_root(data_root: Path) -> Path:
    """Return ``<data_root>/l1_layered``."""
    if not isinstance(data_root, Path):
        raise TypeError(f"data_root must be pathlib.Path, got {type(data_root).__name__}")
    return data_root / "l1_layered"


def l1_snapshot_root(data_root: Path, snapshot_id: str) -> Path:
    """Return L1 root for one ``source_snapshot_id`` batch."""
    validate_source_snapshot_id(snapshot_id)
    return l1_layered_root(data_root) / snapshot_id.strip()


def l1_bet_partition_dir(data_root: Path, snapshot_id: str, gaming_day: str) -> Path:
    """Return directory for cleaned ``t_bet`` Hive partition (``gaming_day=...``)."""
    if not isinstance(gaming_day, str) or not gaming_day.strip():
        raise ValueError(f"gaming_day must be a non-empty string, got {gaming_day!r}")
    if ".." in gaming_day or "/" in gaming_day or "\\" in gaming_day or "=" in gaming_day:
        raise ValueError(f"invalid gaming_day for path segment: {gaming_day!r}")
    root = l1_snapshot_root(data_root, snapshot_id)
    return root / "t_bet" / f"gaming_day={gaming_day.strip()}"


def l1_run_bet_map_partition_dir(data_root: Path, snapshot_id: str, run_end_gaming_day: str) -> Path:
    """Return directory for ``run_bet_map`` Hive partition (``run_end_gaming_day=...``)."""
    if not isinstance(run_end_gaming_day, str) or not run_end_gaming_day.strip():
        raise ValueError(f"run_end_gaming_day must be a non-empty string, got {run_end_gaming_day!r}")
    if (
        ".." in run_end_gaming_day
        or "/" in run_end_gaming_day
        or "\\" in run_end_gaming_day
        or "=" in run_end_gaming_day
    ):
        raise ValueError(f"invalid run_end_gaming_day for path segment: {run_end_gaming_day!r}")
    root = l1_snapshot_root(data_root, snapshot_id)
    return root / "run_bet_map" / f"run_end_gaming_day={run_end_gaming_day.strip()}"


def l1_run_fact_partition_dir(data_root: Path, snapshot_id: str, run_end_gaming_day: str) -> Path:
    """Return directory for ``run_fact`` Hive partition (``run_end_gaming_day=...``)."""
    if not isinstance(run_end_gaming_day, str) or not run_end_gaming_day.strip():
        raise ValueError(f"run_end_gaming_day must be a non-empty string, got {run_end_gaming_day!r}")
    if (
        ".." in run_end_gaming_day
        or "/" in run_end_gaming_day
        or "\\" in run_end_gaming_day
        or "=" in run_end_gaming_day
    ):
        raise ValueError(f"invalid run_end_gaming_day for path segment: {run_end_gaming_day!r}")
    root = l1_snapshot_root(data_root, snapshot_id)
    return root / "run_fact" / f"run_end_gaming_day={run_end_gaming_day.strip()}"
