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
    FE_DERIVED_SOURCE_KIND_PRODUCTION,
    FEATURE_CANDIDATE_REGISTRY_SNAPSHOT_FILENAME,
    MANIFEST_KEY_FE_DERIVED_SOURCE_KIND,
    MANIFEST_KEY_MID_TERM_SNAPSHOT,
    MANIFEST_KEY_SLOW_PATRON_GRAIN,
    MID_TERM_FRESHNESS_SLA_ISO8601,
    SLOW_PATRON_GRAIN_CANONICAL_ASOF,
)
from trainer_hightier.serving.candidate_registry_loader import (
    CandidateRegistrySnapshot,
    load_candidate_registry,
)
from trainer_hightier.serving.production_materialize import (
    DEFAULT_MODEL_FE_DERIVED_COLUMNS,
    DEFAULT_MODEL_SLOW_PATRON_COLUMNS,
    is_training_fe_derived_artifact,
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


def audit_feature_supplier_routes(
    snap: CandidateRegistrySnapshot,
    model_feats: tuple[str, ...],
    *,
    fe_bundled: bool,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify active model features by registry term/source and intended production supplier."""

    by_id = {r.feature_id: r for r in snap.rows}
    rows: list[dict[str, str]] = []
    term_counts: dict[str, int] = {}
    for feat in model_feats:
        row = by_id.get(feat)
        src = row.source if row is not None else "unknown"
        horizon = row.time_horizon if row is not None else "unknown"
        term_counts[horizon] = term_counts.get(horizon, 0) + 1
        if src == "baseline_model":
            supplier = "clickhouse_raw"
        elif src == "feast_trial_1h":
            supplier = "online_trial_builder"
        elif src == "feast_slow_180d":
            grain = (manifest or {}).get(MANIFEST_KEY_SLOW_PATRON_GRAIN, SLOW_PATRON_GRAIN_CANONICAL_ASOF)
            supplier = f"slow_parquet_{grain}"
        elif src == "fe_derived":
            kind = (manifest or {}).get(MANIFEST_KEY_FE_DERIVED_SOURCE_KIND, "")
            supplier = (
                "production_fe_derived_parquet"
                if kind == FE_DERIVED_SOURCE_KIND_PRODUCTION and fe_bundled
                else ("bundled_fe_derived_parquet" if fe_bundled else "missing")
            )
        else:
            supplier = "unknown"
        rows.append(
            {
                "feature_id": feat,
                "source": src,
                "time_horizon": horizon,
                "supplier": supplier,
            }
        )
    fe_cols = [f for f in model_feats if f.startswith("fe__")]
    slow_cols = [f for f in model_feats if f in DEFAULT_MODEL_SLOW_PATRON_COLUMNS]
    return {
        "features": rows,
        "term_counts": term_counts,
        "fe_derived_bundled": fe_bundled,
        "fe__column_count": len(fe_cols),
        "slow_patron_column_count": len(slow_cols),
        "manifest_slow_grain": (manifest or {}).get(MANIFEST_KEY_SLOW_PATRON_GRAIN),
        "manifest_fe_source_kind": (manifest or {}).get(MANIFEST_KEY_FE_DERIVED_SOURCE_KIND),
    }


def assert_production_feature_artifacts_or_raise(
    model_feats: tuple[str, ...],
    *,
    fe_pack_path: Path | None,
    slow_pack_path: Path | None,
    manifest: dict[str, Any] | None,
    fe_derived_columns: tuple[str, ...] | None = None,
) -> None:
    """Reject training-bundle suppliers and require production manifest metadata."""

    man = manifest or {}
    fe_needed = [f for f in model_feats if f.startswith("fe__")]
    slow_needed = [f for f in model_feats if f in DEFAULT_MODEL_SLOW_PATRON_COLUMNS]
    if fe_needed:
        kind = str(man.get(MANIFEST_KEY_FE_DERIVED_SOURCE_KIND, "") or "").strip()
        if kind != FE_DERIVED_SOURCE_KIND_PRODUCTION:
            raise ValueError(
                "[feature-supply] model requires production fe_derived but manifest "
                f"{MANIFEST_KEY_FE_DERIVED_SOURCE_KIND}!={FE_DERIVED_SOURCE_KIND_PRODUCTION!r} "
                f"(got {kind!r}). Run snapshot_updater with --production."
            )
        if fe_pack_path is None or not fe_pack_path.is_file():
            raise FileNotFoundError("[feature-supply] production fe_derived_parquet missing")
        if is_training_fe_derived_artifact(fe_pack_path):
            raise ValueError(
                f"[feature-supply] fe_derived path looks like training artifact: {fe_pack_path}. "
                "Publish production materialization via snapshot_updater --production."
            )
        cols = fe_derived_columns or tuple(f for f in fe_needed if f in DEFAULT_MODEL_FE_DERIVED_COLUMNS)
        smoke_feature_coverage_or_raise(
            fe_pack_path,
            columns=cols or tuple(fe_needed),
            label="fe_derived",
            max_null_fraction=0.95,
        )
    if slow_needed:
        grain = str(man.get(MANIFEST_KEY_SLOW_PATRON_GRAIN, "") or "").strip()
        if grain != SLOW_PATRON_GRAIN_CANONICAL_ASOF:
            raise ValueError(
                "[feature-supply] production slow patron must use "
                f"{MANIFEST_KEY_SLOW_PATRON_GRAIN}={SLOW_PATRON_GRAIN_CANONICAL_ASOF!r} "
                f"(got {grain!r})"
            )
        if slow_pack_path is None or not slow_pack_path.is_file():
            raise FileNotFoundError("[feature-supply] production slow_patron_parquet missing")
        idx = _parquet_lower_column_index(slow_pack_path)
        if "bet_id" in idx and "canonical_id" not in idx:
            raise ValueError(
                "[feature-supply] slow patron parquet is bet-grain (bet_id without canonical_id); "
                "use materialize_slow_patron_180d_canonical_asof for production"
            )
        smoke_feature_coverage_or_raise(
            slow_pack_path,
            columns=tuple(slow_needed),
            label="slow_patron",
            max_null_fraction=0.95,
        )


def smoke_feature_coverage_or_raise(
    parquet_path: Path,
    *,
    columns: Iterable[str],
    label: str,
    max_null_fraction: float = 0.95,
    sample_rows: int = 5000,
) -> dict[str, float]:
    """Fail when required feature columns are entirely or almost entirely null."""

    cols = tuple(columns)
    if not cols:
        return {}
    if max_null_fraction < 0.0 or max_null_fraction > 1.0:
        raise ValueError(f"max_null_fraction must be in [0,1], got {max_null_fraction!r}")
    path = Path(parquet_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    idx = _parquet_lower_column_index(path)
    miss = [c for c in cols if c.lower() not in idx]
    if miss:
        raise ValueError(f"[feature-supply] {label} smoke missing columns {miss} at {path}")
    read_cols = [idx[c.lower()] for c in cols]
    df = pd.read_parquet(path, columns=read_cols).head(int(max(1, sample_rows)))
    n = int(len(df))
    if n <= 0:
        raise ValueError(f"[feature-supply] {label} smoke: parquet empty at {path}")
    null_fracs: dict[str, float] = {}
    for c in cols:
        col = idx[c.lower()]
        null_n = int(df[col].isna().sum())
        frac = float(null_n) / float(n)
        null_fracs[c] = frac
        if frac >= max_null_fraction:
            raise ValueError(
                f"[feature-supply] {label} column {c!r} is {frac:.1%} null in sample (n={n}); "
                "production artifact unusable for scoring"
            )
    logger.info("[feature-supply] %s smoke ok n=%d null_fracs=%s", label, n, null_fracs)
    return null_fracs


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
        mid_rel = manifest.get(MANIFEST_KEY_MID_TERM_SNAPSHOT)
        if mid_rel:
            from trainer_hightier.serving.snapshot_freshness import (
                evaluate_mid_term_freshness,
                read_mid_term_anchor_max,
                validate_mid_term_artifact,
            )

            base_dir = None
            if fe_pack_path is not None:
                base_dir = fe_pack_path.parent
            elif slow_pack_path is not None:
                base_dir = slow_pack_path.parent
            mid_path = (base_dir / str(mid_rel)).resolve() if base_dir is not None else None
            if mid_path is None or not mid_path.is_file():
                raise FileNotFoundError(
                    "[feature-supply] model uses mid_term features but "
                    f"{MANIFEST_KEY_MID_TERM_SNAPSHOT} missing under manifest dir"
                )
            mid_val = validate_mid_term_artifact(mid_path, manifest_grain=manifest.get("mid_term_grain"))
            if mid_val.hard_failure:
                raise ValueError(f"[feature-supply] mid_term artifact invalid: {mid_val.message}")
            mid_anchor = read_mid_term_anchor_max(mid_path, manifest)
            mid_fresh = evaluate_mid_term_freshness(anchor_max=mid_anchor)
            if mid_fresh.status == "hard_cap_breached":
                raise ValueError(f"[feature-supply] mid-term hard cap breached: {mid_fresh.message}")
        else:
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
    if fe_needed or any(f in model_feats for f in DEFAULT_MODEL_SLOW_PATRON_COLUMNS):
        assert_production_feature_artifacts_or_raise(
            model_feats,
            fe_pack_path=fe_pack_path,
            slow_pack_path=slow_pack_path,
            manifest=manifest,
        )
    summary = audit_feature_supplier_routes(
        snap, model_feats, fe_bundled=fe_bundled, manifest=manifest
    )
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
