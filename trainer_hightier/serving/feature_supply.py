"""Feature supplyability: map model columns to runtime suppliers (build + deploy preflight)."""

from __future__ import annotations

import json
import logging
import pickle
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import pyarrow.parquet as pq

from trainer_hightier.config import (
    FE_DERIVED_SOURCE_KIND_PRODUCTION,
    FE_DERIVED_SOURCE_KIND_SHIPPED,
    FEATURE_CANDIDATE_REGISTRY_SNAPSHOT_FILENAME,
    MANIFEST_KEY_FE_DERIVED_SOURCE_KIND,
    MANIFEST_KEY_FE_SHORT_TERM,
    MANIFEST_KEY_MID_TERM_GRAIN,
    MANIFEST_KEY_MID_TERM_SNAPSHOT,
    MANIFEST_KEY_SLOW_PATRON_GRAIN,
    MID_TERM_FRESHNESS_SLA_ISO8601,
    MID_TERM_GRAIN_CANONICAL_DAILY_ASOF,
    SLOW_PATRON_GRAIN_CANONICAL_ASOF,
)
from trainer_hightier.feature_experiment.feature_cadence import (
    classify_model_fe_features,
    short_term_enrich_columns_with_dependencies,
)
from trainer_hightier.feature_experiment.materialize_mid_term_daily_snapshot import (
    mid_term_snapshot_production_safe,
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


def _read_json_sidecar(path: Path) -> dict[str, Any] | None:
    """Load JSON sidecar when present; return None on missing or parse errors."""

    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _read_fe_production_sidecar(parquet_path: Path) -> dict[str, Any] | None:
    sidecar = parquet_path.resolve().parent / f"{parquet_path.stem}.production_meta.json"
    return _read_json_sidecar(sidecar)


def _read_mid_term_snapshot_meta(parquet_path: Path) -> dict[str, Any] | None:
    sidecar = parquet_path.resolve().parent / f"{parquet_path.stem}.meta.json"
    return _read_json_sidecar(sidecar)


def _manifest_layer_base_dir(
    *,
    fe_short_term_pack_path: Path | None,
    mid_term_pack_path: Path | None,
    fe_pack_path: Path | None,
    slow_pack_path: Path | None,
) -> Path | None:
    for candidate in (fe_short_term_pack_path, mid_term_pack_path, fe_pack_path, slow_pack_path):
        if candidate is not None and candidate.is_file():
            return candidate.parent
    return None


def _resolve_manifest_layer_path(
    manifest: dict[str, Any] | None,
    *,
    base_dir: Path | None,
    manifest_key: str,
) -> Path | None:
    if manifest is None or base_dir is None:
        return None
    rel = manifest.get(manifest_key)
    if not isinstance(rel, str) or not rel.strip():
        return None
    candidate = (base_dir / rel).resolve()
    return candidate if candidate.is_file() else None


def _fe_source_kind_production(manifest: dict[str, Any] | None, parquet_path: Path | None) -> bool:
    man = manifest or {}
    kind = str(man.get(MANIFEST_KEY_FE_DERIVED_SOURCE_KIND, "") or "").strip()
    if kind == FE_DERIVED_SOURCE_KIND_PRODUCTION:
        return True
    if parquet_path is None:
        return False
    sidecar = _read_fe_production_sidecar(parquet_path)
    sidecar_kind = str((sidecar or {}).get("fe_derived_source_kind", "") or "").strip()
    return sidecar_kind == FE_DERIVED_SOURCE_KIND_PRODUCTION


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
            fe_split = classify_model_fe_features(snap, (feat,))
            if fe_split["mid_term"]:
                supplier = "mid_term_snapshot_parquet"
            elif fe_split["short_term"]:
                supplier = "fe_short_term_parquet"
            elif fe_bundled:
                supplier = "legacy_fe_derived_parquet"
            else:
                supplier = "missing"
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
    snap: CandidateRegistrySnapshot,
    fe_short_term_pack_path: Path | None,
    mid_term_pack_path: Path | None,
    fe_pack_path: Path | None,
    slow_pack_path: Path | None,
    manifest: dict[str, Any] | None,
    require_fe_artifacts: bool = True,
) -> None:
    """Reject training-bundle suppliers and require production manifest metadata."""

    man = manifest or {}
    fe_split = classify_model_fe_features(snap, model_feats)
    short_cols = fe_split["short_term"]
    mid_cols = fe_split["mid_term"]
    slow_needed = [f for f in model_feats if f in DEFAULT_MODEL_SLOW_PATRON_COLUMNS]

    if short_cols:
        short_path = fe_short_term_pack_path
        if short_path is None or not short_path.is_file():
            if fe_pack_path is not None and fe_pack_path.is_file() and not mid_cols:
                short_path = fe_pack_path
        if short_path is None or not short_path.is_file():
            if not require_fe_artifacts:
                short_path = None
            else:
                raise FileNotFoundError(
                    "[feature-supply] production fe_short_term_parquet missing for short-term fe__*"
                )
        if short_path is None:
            pass
        elif not require_fe_artifacts:
            pass
        elif is_training_fe_derived_artifact(short_path):
            raise ValueError(
                f"[feature-supply] fe_short_term path looks like training artifact: {short_path}. "
                "Publish production short-term materialization via snapshot_updater --production."
            )
        elif not _fe_source_kind_production(man, short_path):
            kind = str(man.get(MANIFEST_KEY_FE_DERIVED_SOURCE_KIND, "") or "").strip()
            raise ValueError(
                "[feature-supply] short-term fe__* requires production supplier metadata "
                f"({MANIFEST_KEY_FE_DERIVED_SOURCE_KIND}={FE_DERIVED_SOURCE_KIND_PRODUCTION!r} "
                f"or production sidecar); got manifest kind={kind!r}"
            )
        else:
            smoke_feature_coverage_or_raise(
                short_path,
                columns=short_term_enrich_columns_with_dependencies(short_cols, mid_cols),
                label="fe_short_term",
                max_null_fraction=0.95,
            )

    if mid_cols:
        if mid_term_pack_path is None or not mid_term_pack_path.is_file():
            if require_fe_artifacts:
                raise FileNotFoundError(
                    "[feature-supply] production mid_term_snapshot_parquet missing for mid-term fe__*"
                )
            mid_term_pack_path = None
        if mid_term_pack_path is not None:
            meta = _read_mid_term_snapshot_meta(mid_term_pack_path)
            if not mid_term_snapshot_production_safe(meta):
                scope = str((meta or {}).get("snapshot_scope", "") or "").strip()
                raise ValueError(
                    "[feature-supply] mid_term_snapshot_parquet is not production-safe "
                    f"(snapshot_scope={scope!r}, expected production). "
                    "Training-scoped snapshots cannot satisfy production packaging."
                )
        if mid_term_pack_path is None and not require_fe_artifacts:
            pass
        kind = str(man.get(MANIFEST_KEY_FE_DERIVED_SOURCE_KIND, "") or "").strip()
        if (
            mid_term_pack_path is not None
            and kind == FE_DERIVED_SOURCE_KIND_SHIPPED
            and not _fe_source_kind_production(man, fe_short_term_pack_path)
        ):
            raise ValueError(
                "[feature-supply] manifest fe_derived_source_kind=shipped_training_bundle cannot "
                "satisfy production mid-term fe__* serving without production short-term supplier metadata"
            )

    if slow_needed and not require_fe_artifacts:
        return
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
    fe_short_term_pack_path: Path | None = None,
    mid_term_pack_path: Path | None = None,
    manifest: dict[str, Any] | None = None,
    mid_term_freshness_sla_iso: str | None = None,
    validation_stage: str = "deploy",
) -> dict[str, Any]:
    """Fail fast when any model column cannot be supplied in production serving."""

    if validation_stage not in {"package", "deploy"}:
        raise ValueError(f"validation_stage must be 'package' or 'deploy', got {validation_stage!r}")
    require_runtime_artifacts = validation_stage == "deploy"
    refresh_required_layers: list[str] = []

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

    fe_split = classify_model_fe_features(snap, tuple(fe_needed) if fe_needed else model_feats)
    short_cols = fe_split["short_term"]
    mid_cols = fe_split["mid_term"]
    other_fe_cols = fe_split["other"]
    if other_fe_cols:
        tip = ", ".join(other_fe_cols[:12])
        ellipsis = "" if len(other_fe_cols) <= 12 else ", …"
        raise ValueError(
            "[feature-supply] model fe__* columns lack a supported cadence supplier: "
            f"[{tip}{ellipsis}]. Update frozen registry allowed_training_supplier."
        )

    base_dir = _manifest_layer_base_dir(
        fe_short_term_pack_path=fe_short_term_pack_path,
        mid_term_pack_path=mid_term_pack_path,
        fe_pack_path=fe_pack_path,
        slow_pack_path=slow_pack_path,
    )
    if fe_short_term_pack_path is None or not fe_short_term_pack_path.is_file():
        fe_short_term_pack_path = _resolve_manifest_layer_path(
            manifest, base_dir=base_dir, manifest_key=MANIFEST_KEY_FE_SHORT_TERM
        )
    if mid_term_pack_path is None or not mid_term_pack_path.is_file():
        mid_term_pack_path = _resolve_manifest_layer_path(
            manifest, base_dir=base_dir, manifest_key=MANIFEST_KEY_MID_TERM_SNAPSHOT
        )

    mid_term_registry = model_feats_requiring_mid_term_snapshot(snap, model_feats)
    mid_term_needed = tuple(dict.fromkeys([*mid_term_registry, *mid_cols]))
    sla = mid_term_freshness_sla_iso or MID_TERM_FRESHNESS_SLA_ISO8601

    if short_cols:
        short_path = fe_short_term_pack_path
        if short_path is None or not short_path.is_file():
            if fe_pack_path is not None and fe_pack_path.is_file():
                short_path = fe_pack_path
        if short_path is None or not short_path.is_file():
            tip = ", ".join(short_cols[:24])
            ellipsis = "" if len(short_cols) <= 24 else ", …"
            refresh_required_layers.append(MANIFEST_KEY_FE_SHORT_TERM)
            if require_runtime_artifacts:
                raise ValueError(
                    "[feature-supply] model requires short-term fe__* but bundle has no "
                    f"{MANIFEST_KEY_FE_SHORT_TERM} (legacy {MANIFEST_KEY_FE_DERIVED} is debug-only for "
                    "mid-term). "
                    f"Missing example columns: [{tip}{ellipsis}]. "
                    "Publish production fe_short_term_parquet via snapshot_updater --production."
                )
        else:
            short_required = short_term_enrich_columns_with_dependencies(short_cols, mid_cols)
            _ensure_parquet_columns(short_path, role="fe_short_term", required=short_required)

    if mid_term_needed:
        if mid_term_pack_path is None or not mid_term_pack_path.is_file():
            tip = ", ".join(mid_cols[:24] or mid_term_needed[:24])
            ellipsis = "" if len(mid_cols or mid_term_needed) <= 24 else ", …"
            refresh_required_layers.append(MANIFEST_KEY_MID_TERM_SNAPSHOT)
            if require_runtime_artifacts:
                raise ValueError(
                    "[feature-supply] model requires mid-term fe__* but bundle has no "
                    f"{MANIFEST_KEY_MID_TERM_SNAPSHOT} (legacy {MANIFEST_KEY_FE_DERIVED} does not satisfy "
                    "mid-term production gate). "
                    f"Missing example columns: [{tip}{ellipsis}]. "
                    "Publish production mid_term_snapshot_parquet with snapshot_scope=production."
                )
        if mid_cols and fe_pack_path is not None and fe_pack_path.is_file() and (
            fe_short_term_pack_path is None or not fe_short_term_pack_path.is_file()
        ):
            refresh_required_layers.append(MANIFEST_KEY_FE_SHORT_TERM)
            if require_runtime_artifacts:
                raise ValueError(
                    "[feature-supply] legacy fe_derived_parquet alone cannot satisfy mid-term fe__*; "
                    f"require {MANIFEST_KEY_MID_TERM_SNAPSHOT} and {MANIFEST_KEY_FE_SHORT_TERM}."
                )

    if mid_term_needed and manifest is not None and mid_term_pack_path is not None:
        from trainer_hightier.serving.snapshot_freshness import (
            evaluate_mid_term_freshness,
            read_mid_term_anchor_max,
            validate_mid_term_artifact,
        )

        grain = str(manifest.get(MANIFEST_KEY_MID_TERM_GRAIN, "") or "").strip()
        if grain and grain != MID_TERM_GRAIN_CANONICAL_DAILY_ASOF:
            raise ValueError(
                "[feature-supply] mid_term_grain must be "
                f"{MID_TERM_GRAIN_CANONICAL_DAILY_ASOF!r} (got {grain!r})"
            )
        mid_val = validate_mid_term_artifact(
            mid_term_pack_path,
            manifest_grain=manifest.get(MANIFEST_KEY_MID_TERM_GRAIN),
        )
        if mid_val.hard_failure:
            raise ValueError(f"[feature-supply] mid_term artifact invalid: {mid_val.message}")
        mid_anchor = read_mid_term_anchor_max(mid_term_pack_path, manifest)
        mid_fresh = evaluate_mid_term_freshness(anchor_max=mid_anchor)
        if require_runtime_artifacts and mid_fresh.status == "hard_cap_breached":
            raise ValueError(f"[feature-supply] mid-term hard cap breached: {mid_fresh.message}")
    elif mid_term_needed and manifest is not None and mid_term_pack_path is None:
        if require_runtime_artifacts:
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

    fe_bundled = (fe_short_term_pack_path is not None and fe_short_term_pack_path.is_file()) or (
        fe_pack_path is not None and fe_pack_path.is_file()
    )
    if fe_needed or any(f in model_feats for f in DEFAULT_MODEL_SLOW_PATRON_COLUMNS):
        assert_production_feature_artifacts_or_raise(
            model_feats,
            snap=snap,
            fe_short_term_pack_path=fe_short_term_pack_path,
            mid_term_pack_path=mid_term_pack_path,
            fe_pack_path=fe_pack_path,
            slow_pack_path=slow_pack_path,
            manifest=manifest,
            require_fe_artifacts=require_runtime_artifacts,
        )
    summary = audit_feature_supplier_routes(
        snap, model_feats, fe_bundled=fe_bundled, manifest=manifest
    )
    summary["fe_short_term_column_count"] = len(short_cols)
    summary["fe_mid_term_column_count"] = len(mid_cols)
    summary["mid_term_model_feature_count"] = len(mid_term_needed)
    summary["validation_stage"] = validation_stage
    summary["refresh_required_layers"] = sorted(set(refresh_required_layers))
    if manifest is not None:
        summary["coverage_end_exclusive"] = manifest.get("coverage_end_exclusive")
    logger.info(
        "[feature-supply] ok model_features=%d fe_derived=%d short_fe=%d mid_fe=%d bundled_fe=%s",
        len(model_feats),
        len(fe_needed),
        len(short_cols),
        len(mid_cols),
        fe_bundled,
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


@dataclass(frozen=True)
class ScorerSupplierPlan:
    """Runtime supplier routing for one active model (scorer v2)."""

    baseline_cols: tuple[str, ...]
    feast_trial_cols: tuple[str, ...]
    feast_mid_cols: tuple[str, ...]
    feast_slow_cols: tuple[str, ...]
    short_term_cols: tuple[str, ...]
    unknown_cols: tuple[str, ...]


def build_scorer_supplier_plan(
    snap: CandidateRegistrySnapshot,
    model_feats: tuple[str, ...],
) -> ScorerSupplierPlan:
    """Map ``model.pkl`` feature columns to scorer v2 runtime suppliers."""
    by_id = {r.feature_id: r for r in snap.rows}
    fe_split = classify_model_fe_features(snap, model_feats)
    mid_set = set(fe_split["mid_term"])
    short_set = set(fe_split["short_term"])
    slow_set = set(DEFAULT_MODEL_SLOW_PATRON_COLUMNS)
    baseline: list[str] = []
    trial: list[str] = []
    mid: list[str] = []
    slow: list[str] = []
    short: list[str] = []
    unknown: list[str] = []
    for feat in model_feats:
        row = by_id.get(feat)
        if row is None:
            unknown.append(feat)
            continue
        src = row.source
        if src == "baseline_model":
            baseline.append(feat)
        elif src == "feast_trial_1h":
            trial.append(feat)
        elif src == "feast_slow_180d" or feat in slow_set:
            slow.append(feat)
        elif src == "fe_derived":
            if feat in mid_set:
                mid.append(feat)
            elif feat in short_set:
                short.append(feat)
            else:
                unknown.append(feat)
        else:
            unknown.append(feat)
    return ScorerSupplierPlan(
        baseline_cols=tuple(baseline),
        feast_trial_cols=tuple(trial),
        feast_mid_cols=tuple(mid),
        feast_slow_cols=tuple(slow),
        short_term_cols=tuple(short),
        unknown_cols=tuple(unknown),
    )


def assert_scorer_supplier_plan_or_raise(plan: ScorerSupplierPlan) -> None:
    """Fail fast when scorer v2 cannot supply every model column."""
    if plan.unknown_cols:
        tip = ", ".join(plan.unknown_cols[:12])
        ellipsis = "" if len(plan.unknown_cols) <= 12 else ", …"
        raise ValueError(
            "[feature-supply] scorer v2 supplier plan has unknown columns: "
            f"[{tip}{ellipsis}]"
        )


def scorer_supplier_route_counts(plan: ScorerSupplierPlan) -> dict[str, int]:
    """Count model columns routed to each scorer v2 runtime supplier."""
    return {
        "baseline_model": len(plan.baseline_cols),
        "feast_trial_1h": len(plan.feast_trial_cols),
        "feast_online_mid": len(plan.feast_mid_cols),
        "feast_online_slow": len(plan.feast_slow_cols),
        "fe_short_term_parquet": len(plan.short_term_cols),
    }
