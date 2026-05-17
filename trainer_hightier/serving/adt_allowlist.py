"""High ADT allowlist helpers (training-parity ``adt_allowed_players`` Parquet)."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Optional

import pandas as pd

from trainer_hightier.config import HightierServingConfig
from trainer_hightier.serving.feature_state_store import ActiveSnapshotManifest
from trainer_hightier.utils.patron_session_metrics import default_adt_allowed_players_parquet_path

logger = logging.getLogger(__name__)


def parquet_player_id_row_count(path: Path) -> int:
    """Return row count from Parquet metadata when possible, else from pandas."""
    p = Path(path).resolve()
    if not p.is_file():
        raise FileNotFoundError(p)
    try:
        import pyarrow.parquet as pq

        meta = pq.ParquetFile(p).metadata
        if meta is not None:
            return int(meta.num_rows)
    except Exception:
        pass
    return int(len(pd.read_parquet(p, columns=["player_id"])))


def sha256_file(path: Path) -> str:
    """SHA-256 hex digest of file bytes."""
    p = Path(path).resolve()
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def filter_bets_by_adt_allowlist(bets: pd.DataFrame, allowlist: frozenset[int], *, player_col: str = "player_id") -> pd.DataFrame:
    """Keep rows whose ``player_id`` is in ``allowlist`` (numeric coercion)."""
    if bets.empty:
        return bets
    if player_col not in bets.columns:
        raise ValueError(f"bets missing column {player_col!r}; columns={list(bets.columns)[:40]}")
    pid = pd.to_numeric(bets[player_col], errors="coerce")
    mask = pid.notna() & pid.astype("int64", copy=False).isin(allowlist)
    return bets.loc[mask].copy()


def load_adt_allowlist_ids(path: Path) -> frozenset[int]:
    """Distinct ``player_id`` values from allowlist Parquet."""
    p = Path(path).resolve()
    if not p.is_file():
        raise FileNotFoundError(p)
    df = pd.read_parquet(p, columns=["player_id"])
    s = pd.to_numeric(df["player_id"], errors="coerce").dropna()
    return frozenset(int(x) for x in s.astype("int64", copy=False).tolist())


def resolve_adt_allowlist_path(
    cfg: HightierServingConfig,
    *,
    manifest: ActiveSnapshotManifest | None,
    cli_path: Optional[Path] = None,
) -> Path:
    """Resolve allowlist Parquet: CLI > manifest > config > default by quantile."""
    if cli_path is not None:
        return Path(cli_path).resolve()
    if manifest is not None:
        ap = getattr(manifest, "adt_allowlist_parquet", None)
        if ap is not None and ap.is_file():
            return ap.resolve()
    if cfg.adt_allowed_players_parquet is not None:
        cp = Path(cfg.adt_allowed_players_parquet).resolve()
        return cp
    return default_adt_allowed_players_parquet_path(float(cfg.adt_allowlist_quantile)).resolve()


def check_training_allowlist_sha256(
    training_metrics: dict,
    actual_sha256: str,
    *,
    fail_fast: bool,
) -> bool:
    """Compare allowlist file hash vs optional ``training_metrics['adt_allowlist_sha256']``.

    Returns
    -------
    bool
        ``True`` if OK or training metrics omit the expected hash; ``False`` if mismatch and
        ``fail_fast`` is ``False`` (caller should mark degraded / alert).
    """
    expected = training_metrics.get("adt_allowlist_sha256")
    if not expected or not str(expected).strip():
        logger.warning(
            "[adt_allowlist] training_metrics has no adt_allowlist_sha256; skip strict hash parity"
        )
        return True
    exp = str(expected).strip().lower()
    act = actual_sha256.strip().lower()
    if exp == act:
        return True
    msg = (
        f"adt_allowlist SHA256 mismatch: training_metrics expects {exp[:16]}… "
        f"but serving file has {act[:16]}…"
    )
    if fail_fast:
        raise ValueError(msg)
    logger.error("[adt_allowlist] %s (continuing because fail_fast=False)", msg)
    return False
