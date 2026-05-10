"""Label disk cache for training windows (L2-aligned semantics, legacy chunk path).

Caches the **full** output of :func:`trainer.labels.compute_labels` (before the
training-window + censored filter) under ``CHUNK_DIR`` so reruns with unchanged
source data and label semantics can skip ``compute_labels`` CPU.

Disable with environment ``DISABLE_LABEL_ASSET_CACHE=1`` (or ``true`` / ``yes``).

``LABEL_DEFINITION_VERSION`` (optional) bumps the cache when label *definition*
changes without code deploy.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

_LABEL_CACHE_SIDECAR_VERSION = 1


def _truthy_env(name: str) -> bool:
    v = (os.environ.get(name) or "").strip().lower()
    return v in ("1", "true", "yes", "y", "on")


def label_asset_cache_disabled() -> bool:
    """Return True when label disk cache reads/writes are disabled."""
    return _truthy_env("DISABLE_LABEL_ASSET_CACHE")


def label_definition_version() -> str:
    """Semantic version string for label rules (bump when definition changes)."""
    return (os.environ.get("LABEL_DEFINITION_VERSION") or "walkaway-label-v1").strip() or "walkaway-label-v1"


def label_intermediate_parquet_path(chunk: dict, chunk_dir: Path) -> Path:
    """Parquet path for cached ``compute_labels`` output (pre window/censor filter)."""
    ws = chunk["window_start"].strftime("%Y%m%d")
    we = chunk["window_end"].strftime("%Y%m%d")
    return chunk_dir / f"chunk_{ws}_{we}.label_intermediate.parquet"


def label_intermediate_sidecar_path(chunk: dict, chunk_dir: Path) -> Path:
    """JSON sidecar for :func:`label_intermediate_parquet_path`."""
    ws = chunk["window_start"].strftime("%Y%m%d")
    we = chunk["window_end"].strftime("%Y%m%d")
    return chunk_dir / f"chunk_{ws}_{we}.label_intermediate.cache_key"


def build_label_disk_cache_components(
    *,
    window_start_iso: str,
    window_end_iso: str,
    extended_end_iso: str,
    data_hash: str,
    walkaway_gap_min: int,
    alert_horizon_min: int,
    label_lookahead_min: int,
    identity_mapping_mode: str,
    pit_identity_engine: str,
    source_snapshot_id: str,
) -> Dict[str, Any]:
    """Return deterministic components for label intermediate disk cache."""
    sem = json.dumps(
        {
            "WALKAWAY_GAP_MIN": int(walkaway_gap_min),
            "ALERT_HORIZON_MIN": int(alert_horizon_min),
            "LABEL_LOOKAHEAD_MIN": int(label_lookahead_min),
        },
        sort_keys=True,
    )
    censoring_policy_id = hashlib.sha256(sem.encode()).hexdigest()[:16]
    identity_mapping_revision = f"{identity_mapping_mode}:{pit_identity_engine}"
    return {
        "kind": "trainer_label_intermediate_v1",
        "window_start": window_start_iso,
        "window_end": window_end_iso,
        "extended_end": extended_end_iso,
        "data_hash": str(data_hash),
        "label_definition_version": label_definition_version(),
        "censoring_policy_id": censoring_policy_id,
        "identity_mapping_revision": identity_mapping_revision,
        "source_snapshot_id": str(source_snapshot_id or "unknown"),
    }


def label_disk_cache_fingerprint(components: Dict[str, Any]) -> str:
    """Stable short fingerprint for sidecar JSON."""
    blob = json.dumps(components, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:24]


def _write_label_sidecar(fingerprint: str, components: Dict[str, Any], *, n_rows: int) -> str:
    payload = {
        "v": _LABEL_CACHE_SIDECAR_VERSION,
        "fingerprint": fingerprint,
        "pipeline": components,
        "n_rows": int(n_rows),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _read_label_sidecar(raw: str) -> Tuple[str, Optional[Dict[str, Any]], Optional[int]]:
    text = raw.strip()
    if not text:
        return "", None, None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return text, None, None
    if not isinstance(obj, dict):
        return text, None, None
    fp = obj.get("fingerprint")
    pipe = obj.get("pipeline")
    nrows = obj.get("n_rows")
    if not isinstance(fp, str) or not fp:
        return text, None, None
    pdict = pipe if isinstance(pipe, dict) else None
    n_int: Optional[int] = None
    if isinstance(nrows, int):
        n_int = nrows
    elif isinstance(nrows, str) and nrows.isdigit():
        n_int = int(nrows)
    elif nrows is not None:
        try:
            n_int = int(nrows)
        except (TypeError, ValueError):
            n_int = None
    return fp, pdict, n_int


def try_load_label_intermediate_cache(
    *,
    parquet_path: Path,
    sidecar_path: Path,
    expected_fingerprint: str,
    expected_components: Dict[str, Any],
    expected_n_rows: int,
) -> Optional[pd.DataFrame]:
    """Return cached label frame if sidecar matches; otherwise None."""
    if not parquet_path.is_file() or not sidecar_path.is_file():
        return None
    try:
        raw = sidecar_path.read_text(encoding="utf-8")
    except OSError:
        return None
    fp, prev, n_stored = _read_label_sidecar(raw)
    if fp != expected_fingerprint:
        return None
    if prev != expected_components:
        return None
    if n_stored is not None and n_stored != int(expected_n_rows):
        return None
    try:
        return pd.read_parquet(parquet_path)
    except Exception as exc:  # noqa: BLE001 — cache read defensive
        logger.warning("label_intermediate cache read failed (%s); recomputing labels", exc)
        return None


def write_label_intermediate_cache(
    *,
    labeled: pd.DataFrame,
    parquet_path: Path,
    sidecar_path: Path,
    components: Dict[str, Any],
) -> None:
    """Persist ``compute_labels`` output and JSON sidecar."""
    fp = label_disk_cache_fingerprint(components)
    labeled.to_parquet(parquet_path, index=False)
    sidecar_path.write_text(
        _write_label_sidecar(fp, components, n_rows=len(labeled)),
        encoding="utf-8",
    )
    logger.info(
        "Label intermediate cache written (%s rows, fp=%s)",
        len(labeled),
        fp,
    )


def contract_row_meta(*, coverage_end: datetime, source_snapshot_id: str) -> Dict[str, Any]:
    """Minimal metadata fields aligned with L2 label_asset contracts."""
    return {
        "source_snapshot_id": str(source_snapshot_id or "unknown"),
        "coverage_end": pd.Timestamp(coverage_end).isoformat(),
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "label_definition_version": label_definition_version(),
    }


def build_label_asset_contract_dataframe(
    labeled_rows: pd.DataFrame,
    *,
    source_snapshot_id: str,
    coverage_end: datetime,
) -> pd.DataFrame:
    """Build a narrow frame matching :data:`LABEL_ASSET_REQUIRED_COLUMNS`.

    Args:
        labeled_rows: Rows already filtered to the training/evaluation window;
            must include ``bet_id``, ``canonical_id``, ``label``, and either
            ``censored`` or ``is_censored``.
        source_snapshot_id: Upstream snapshot id for lineage.
        coverage_end: Label horizon upper bound (typically chunk ``extended_end``).

    Returns:
        DataFrame with exactly the L2 contract columns.

    Raises:
        ValueError: If required input columns are missing.
    """
    from trainer.training.l2_trainer_contracts import (
        LABEL_ASSET_REQUIRED_COLUMNS,
        assert_label_asset_columns_present,
    )

    need = {"bet_id", "canonical_id", "label"}
    missing_in = need - set(labeled_rows.columns)
    if missing_in:
        raise ValueError(f"build_label_asset_contract_dataframe: labeled_rows missing {sorted(missing_in)}")
    if "censored" in labeled_rows.columns:
        is_censored = labeled_rows["censored"].astype(bool)
    elif "is_censored" in labeled_rows.columns:
        is_censored = labeled_rows["is_censored"].astype(bool)
    else:
        raise ValueError("build_label_asset_contract_dataframe: need censored or is_censored column")
    meta = contract_row_meta(
        coverage_end=coverage_end,
        source_snapshot_id=source_snapshot_id,
    )
    out = pd.DataFrame(
        {
            "bet_id": labeled_rows["bet_id"],
            "canonical_id": labeled_rows["canonical_id"].astype(str),
            "label": labeled_rows["label"].astype("int8"),
            "is_censored": is_censored.astype(bool),
            "label_definition_version": meta["label_definition_version"],
            "source_snapshot_id": meta["source_snapshot_id"],
            "computed_at": meta["computed_at"],
            "coverage_end": meta["coverage_end"],
        }
    )
    assert_label_asset_columns_present(frozenset(out.columns))
    return out[list(LABEL_ASSET_REQUIRED_COLUMNS)]  # stable column order


def build_label_asset_store_lookup_components(
    *,
    identity_mapping_mode: str,
    pit_identity_engine: str,
    source_snapshot_id: str,
) -> Dict[str, Any]:
    """Snapshot-keyed components (no training window) for cross-window label reuse."""
    from trainer.core._config_training_domain import ALERT_HORIZON_MIN, LABEL_LOOKAHEAD_MIN, WALKAWAY_GAP_MIN
    from trainer.training.l2_reuse_keys import censoring_policy_id_from_semantics, identity_mapping_revision

    return {
        "kind": "trainer_label_asset_store_v1",
        "label_definition_version": label_definition_version(),
        "censoring_policy_id": censoring_policy_id_from_semantics(
            walkaway_gap_min=int(WALKAWAY_GAP_MIN),
            alert_horizon_min=int(ALERT_HORIZON_MIN),
            label_lookahead_min=int(LABEL_LOOKAHEAD_MIN),
        ),
        "identity_mapping_revision": identity_mapping_revision(
            identity_mapping_mode=str(identity_mapping_mode),
            pit_identity_engine=str(pit_identity_engine),
        ),
        "source_snapshot_id": str(source_snapshot_id or "unknown").strip() or "unknown",
    }


def label_asset_store_parquet_path(components: Dict[str, Any]) -> Path:
    """Parquet path for reuse store rows (full ``compute_labels`` schema)."""
    from trainer.training import data_sources

    blob = json.dumps(components, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    short = hashlib.sha256(blob).hexdigest()[:24]
    return data_sources.LOCAL_PARQUET_DIR / "label_asset_store" / short / "labels.parquet"


def try_load_label_rows_from_asset_store(*, bets: pd.DataFrame, components: Dict[str, Any]) -> Optional[pd.DataFrame]:
    """Return label frame when store covers all ``bets.bet_id`` (aligned to ``bets`` order)."""
    if "bet_id" not in bets.columns:
        return None
    path = label_asset_store_parquet_path(components)
    if not path.is_file():
        return None
    bids = bets["bet_id"]
    if bids.duplicated().any():
        return None
    try:
        store = pd.read_parquet(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("label asset store read failed (%s); recomputing labels", exc)
        return None
    if "bet_id" not in store.columns:
        return None
    need = int(len(bids))
    sub = store.loc[store["bet_id"].isin(bids)].copy()
    if len(sub) != need or int(sub["bet_id"].nunique()) != need:
        return None
    sub = sub.drop_duplicates(subset=["bet_id"], keep="last").set_index("bet_id").reindex(bids).reset_index(drop=True)
    if len(sub) != need or sub["bet_id"].isna().any():
        return None
    if not sub["bet_id"].tolist() == bids.tolist():
        return None
    return sub


def upsert_label_asset_store(labeled: pd.DataFrame, components: Dict[str, Any]) -> None:
    """Read-modify-write store with row-cap guard (may be expensive for huge stores)."""
    from trainer.core import _config_training_domain as _tdom

    if "bet_id" not in labeled.columns:
        return
    path = label_asset_store_parquet_path(components)
    path.parent.mkdir(parents=True, exist_ok=True)
    new_df = labeled.reset_index(drop=True)
    cap = int(getattr(_tdom, "L2_LABEL_ASSET_STORE_MAX_UPSERT_BYTES", 0))
    if path.is_file():
        try:
            sz = path.stat().st_size
        except OSError:
            sz = 0
        if sz > cap:
            logger.warning(
                "label asset store upsert skipped (file %d bytes > cap %d)",
                sz,
                cap,
            )
            return
        try:
            old = pd.read_parquet(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("label asset store upsert read failed (%s)", exc)
            old = None
        if old is not None:
            merged = pd.concat([old, new_df], ignore_index=True)
            merged = merged.drop_duplicates(subset=["bet_id"], keep="last")
            merged.to_parquet(path, index=False)
            logger.info("Label asset store upsert (%s rows)", len(merged))
            return
    new_df.to_parquet(path, index=False)
    logger.info("Label asset store created (%s rows)", len(new_df))
