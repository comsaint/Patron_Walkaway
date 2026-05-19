"""Feature supplyability: map model columns to runtime suppliers (build + deploy preflight)."""

from __future__ import annotations

import logging
import pickle
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import pyarrow.parquet as pq

from trainer_hightier.config import (
    FEATURE_CANDIDATE_REGISTRY_SNAPSHOT_FILENAME,
    MID_TERM_FRESHNESS_SLA_ISO8601,
)
from trainer_hightier.serving.candidate_registry_loader import (
    CandidateRegistrySnapshot,
    load_candidate_registry,
)

logger = logging.getLogger(__name__)

MANIFEST_KEY_FE_DERIVED: str = "fe_derived_parquet"

_KNOWN_SOURCES: frozenset[str] = frozenset(
    {
        "baseline_model",
        "feast_trial_1h",
        "feast_slow_180d",
        "fe_derived",
    }
)


def _parquet_lower_column_index(path: Path) -> dict[str, str]:
    names = pq.read_schema(path).names
    return {str(c).lower(): str(c) for c in names}


def _parse_manifest_iso_datetime(value: str) -> datetime:
    """Parse manifest ISO-8601 timestamp to timezone-aware UTC."""

    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def model_feats_requiring_mid_term_snapshot(
    snap: CandidateRegistrySnapshot,
    model_feats: tuple[str, ...],
) -> tuple[str, ...]:
    """Return feature ids in ``model_feats`` whose registry ``time_horizon`` is ``mid_term``."""

    by_id = {r.feature_id: r for r in snap.rows}
    out: list[str] = []
    for feat in model_feats:
        row = by_id.get(feat)
        if row is not None and row.time_horizon == "mid_term":
            out.append(feat)
    return tuple(out)


def assert_mid_term_freshness_or_raise(
    manifest: dict[str, Any],
    *,
    mid_term_feature_count: int,
    sla_iso: str = MID_TERM_FRESHNESS_SLA_ISO8601,
) -> None:
    """If the model uses mid-term features, enforce ``coverage_end_exclusive`` vs SLA."""

    if mid_term_feature_count <= 0:
        return
    cov_raw = manifest.get("coverage_end_exclusive")
    if not isinstance(cov_raw, str) or not cov_raw.strip():
        raise ValueError(
            "[feature-supply] model uses mid_term features but active_manifest.json "
            "missing coverage_end_exclusive (ISO-8601). "
            f"mid_term_feature_count={mid_term_feature_count}",
        )
    cov = _parse_manifest_iso_datetime(cov_raw)
    try:
        sla_td = pd.Timedelta(str(sla_iso).strip())
    except (ValueError, TypeError) as exc:
        raise ValueError(f"[feature-supply] invalid mid_term_freshness_sla_iso={sla_iso!r}") from exc
    now = datetime.now(timezone.utc)
    threshold = now - timedelta(seconds=float(sla_td.total_seconds()))
    if cov < threshold:
        raise ValueError(
            "[feature-supply] mid-term snapshot stale: "
            f"coverage_end_exclusive={cov_raw!r} is older than SLA {sla_iso!r} "
            f"(threshold_utc={threshold.isoformat()}, now_utc={now.isoformat()}). "
            f"mid_term features in model≈{mid_term_feature_count}",
        )


def _ensure_parquet_columns(path: Path, *, role: str, required: Iterable[str]) -> None:
    idx = _parquet_lower_column_index(path)
    miss = sorted({c for c in required if c.lower() not in idx})
    if miss:
        sample = sorted(idx.keys())
        tip = ", ".join(sample[:50])
        ellipsis = "" if len(sample) <= 50 else ", …"
        raise ValueError(
            f"[feature-supply] {role} parquet missing columns {miss} at {path}. "
            f"schema(lowercase-sample)=[{tip}{ellipsis}]"
        )


def model_feature_columns_from_pickle(model_bundle_dir: Path) -> tuple[str, ...]:
    """Ordered feature names from ``model.pkl`` under *model_bundle_dir*."""

    pkl = Path(model_bundle_dir) / "model.pkl"
    raw = pickle.loads(pkl.read_bytes())
    if not isinstance(raw, dict):
        raise ValueError(f"{pkl}: expected dict pickle payload")
    feat = raw.get("feature_columns") or raw.get("feature_cols")
    if not feat:
        raise ValueError(f"{pkl} missing feature_columns/feature_cols")
    return tuple(str(x) for x in list(feat))


def fe_derived_features_for_model(
    snap: CandidateRegistrySnapshot,
    model_feats: tuple[str, ...],
) -> tuple[str, ...]:
    """Return model feature ids whose frozen registry ``source`` is ``fe_derived``."""

    by_id = {r.feature_id: r for r in snap.rows}
    out: list[str] = []
    for feat in model_feats:
        row = by_id.get(feat)
        if row is not None and row.source == "fe_derived":
            out.append(feat)
    return tuple(out)


def build_feature_supplier_summary(
    snap: CandidateRegistrySnapshot,
    model_feats: tuple[str, ...],
    *,
    fe_bundled: bool,
) -> dict[str, Any]:
    """Summarize how each model feature is expected to be supplied at runtime."""

    by_id = {r.feature_id: r for r in snap.rows}
    rows: list[dict[str, str]] = []
    for feat in model_feats:
        row = by_id.get(feat)
        src = row.source if row is not None else "unknown"
        if src == "baseline_model":
            supplier = "clickhouse_raw"
        elif src == "feast_trial_1h":
            supplier = "online_trial_builder"
        elif src == "feast_slow_180d":
            supplier = "bundled_slow_parquet"
        elif src == "fe_derived":
            supplier = "bundled_fe_derived_parquet" if fe_bundled else "missing"
        else:
            supplier = "unknown"
        rows.append({"feature_id": feat, "source": src, "supplier": supplier})
    return {"features": rows, "fe_derived_bundled": fe_bundled}


def assert_feature_supplyability_or_raise(
    snap: CandidateRegistrySnapshot,
    model_feats: tuple[str, ...],
    *,
    slow_pack_path: Path | None,
    trial_pack_path: Path | None,
    fe_pack_path: Path | None,
    manifest: dict[str, Any] | None = None,
    mid_term_freshness_sla_iso: str | None = None,
) -> dict[str, Any]:
    """Fail fast when any model column cannot be supplied in production serving."""

    by_id = {r.feature_id: r for r in snap.rows}
    fe_needed: list[str] = []
    unknown: list[str] = []

    for feat in model_feats:
        row = by_id.get(feat)
        if row is None:
            raise ValueError(
                f"[feature-supply] model.pkl lists feature_columns={feat!r} "
                "not present in frozen feature_candidate_registry snapshot"
            )
        src = row.source
        if src not in _KNOWN_SOURCES:
            unknown.append(f"{feat}({src})")
            continue
        if src == "feast_slow_180d":
            if slow_pack_path is None or not slow_pack_path.is_file():
                raise FileNotFoundError(
                    f"[feature-supply] model expects {feat!r} (feast_slow_180d) but slow parquet missing"
                )
        elif src == "fe_derived":
            fe_needed.append(feat)
        elif src == "feast_trial_1h":
            # Production primary supplier is online attach_trial_bet_behavior_1h; optional trial parquet
            # is not a substitute for readiness.
            pass

    if unknown:
        raise ValueError(
            "[feature-supply] model uses registry sources without a production supplier: "
            + ", ".join(unknown)
        )

    mid_term_needed = model_feats_requiring_mid_term_snapshot(snap, model_feats)
    sla = mid_term_freshness_sla_iso or MID_TERM_FRESHNESS_SLA_ISO8601

    if fe_needed:
        if fe_pack_path is None or not fe_pack_path.is_file():
            tip = ", ".join(fe_needed[:24])
            ellipsis = "" if len(fe_needed) <= 24 else ", …"
            raise ValueError(
                "[feature-supply] model requires fe_derived features but bundle has no "
                f"{MANIFEST_KEY_FE_DERIVED} (or file missing). "
                f"Missing example columns: [{tip}{ellipsis}]. "
                "Retrain with fe__* in baseline, ensure Step 5 copies fe_derived_features.parquet "
                "into deploy_inputs, then rebuild the deploy bundle."
            )
        _ensure_parquet_columns(fe_pack_path, role="fe_derived", required=tuple(fe_needed))

    if mid_term_needed and manifest is not None:
        assert_mid_term_freshness_or_raise(
            manifest,
            mid_term_feature_count=len(mid_term_needed),
            sla_iso=sla,
        )
    elif mid_term_needed and manifest is None:
        raise ValueError(
            "[feature-supply] model uses mid_term registry features but no manifest was provided "
            "for freshness gate; pass active_manifest dict (coverage_end_exclusive). "
            f"mid_term columns example: {', '.join(mid_term_needed[:12])}",
        )

    fe_bundled = fe_pack_path is not None and fe_pack_path.is_file()
    summary = build_feature_supplier_summary(snap, model_feats, fe_bundled=fe_bundled)
    summary["mid_term_model_feature_count"] = len(mid_term_needed)
    if manifest is not None:
        summary["coverage_end_exclusive"] = manifest.get("coverage_end_exclusive")
    logger.info(
        "[feature-supply] ok model_features=%d fe_derived=%d bundled_fe=%s mid_term_in_model=%d",
        len(model_feats),
        len(fe_needed),
        fe_bundled,
        len(mid_term_needed),
    )
    return summary


def load_frozen_registry_for_bundle(model_bundle_dir: Path) -> CandidateRegistrySnapshot:
    """Load frozen registry YAML next to ``model.pkl``."""

    snap_p = Path(model_bundle_dir) / FEATURE_CANDIDATE_REGISTRY_SNAPSHOT_FILENAME
    if not snap_p.is_file():
        raise FileNotFoundError(
            f"[feature-supply] missing {FEATURE_CANDIDATE_REGISTRY_SNAPSHOT_FILENAME} under {model_bundle_dir}"
        )
    return load_candidate_registry(snap_p)
