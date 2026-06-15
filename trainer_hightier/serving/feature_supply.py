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
    MID_TERM_ANCHOR_AUDIT_COLUMN,
    MID_TERM_FRESHNESS_SLA_ISO8601,
    MID_TERM_GRAIN_CANONICAL_DAILY_ASOF,
    MID_TERM_SNAPSHOT_AGE_AUDIT_COLUMN,
    MID_TERM_SNAPSHOT_MISSING_AUDIT_COLUMN,
    SLOW_PATRON_GRAIN_CANONICAL_ASOF,
    TXN_LITE_FEATURE_COLUMNS,
)
from trainer_hightier.feature_experiment.feature_cadence import (
    MID_TERM_COMPOSITE_FEATURE_COLUMNS,
    classify_model_fe_features,
    runtime_inputs_from_registry,
    short_term_enrich_columns_with_dependencies,
)

_MID_TERM_AUDIT_MODEL_COLUMNS: frozenset[str] = frozenset(
    {
        MID_TERM_ANCHOR_AUDIT_COLUMN,
        MID_TERM_SNAPSHOT_AGE_AUDIT_COLUMN,
        MID_TERM_SNAPSHOT_MISSING_AUDIT_COLUMN,
    }
)
from trainer_hightier.feature_experiment.materialize_mid_term_daily_snapshot import (
    MID_TERM_SNAPSHOT_OUTPUT_COLUMNS,
)
from trainer_hightier.serving.feast_production_constants import (
    PRODUCTION_LONG_TERM_FEATURE_COLUMNS,
    PRODUCTION_MID_TERM_FEATURE_COLUMNS,
)
from trainer_hightier.serving.candidate_registry_loader import (
    CandidateRegistrySnapshot,
    FeatureRegistryEntryRow,
    load_candidate_registry,
)
from trainer_hightier.feature_experiment.materialize_mid_term_daily_snapshot import (
    mid_term_snapshot_production_safe,
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
        "t_casino_txn",
    }
)

_TXN_LITE_COLUMN_SET: frozenset[str] = frozenset(TXN_LITE_FEATURE_COLUMNS)


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
            supplier = "short_term_pit_builder"
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
        elif src == "t_casino_txn":
            supplier = "txn_lite_builder"
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
            supplier = "short_term_pit_builder"
        elif src == "feast_slow_180d":
            supplier = "bundled_slow_parquet"
        elif src == "fe_derived":
            supplier = "bundled_fe_derived_parquet" if fe_bundled else "missing"
        elif src == "t_casino_txn":
            supplier = "txn_lite_builder"
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
    scorer_v2_feast_mode: bool = False,
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
            if not scorer_v2_feast_mode and (slow_pack_path is None or not slow_pack_path.is_file()):
                raise FileNotFoundError(
                    f"[feature-supply] model expects {feat!r} (feast_slow_180d) but slow parquet missing"
                )
        elif src == "fe_derived":
            fe_needed.append(feat)
        elif src == "feast_trial_1h":
            # Production primary supplier is online attach_trial_bet_behavior_1h; optional trial parquet
            # is not a substitute for readiness.
            pass
        elif src == "t_casino_txn":
            if require_runtime_artifacts:
                _assert_ch_txn_supplier_ready_or_raise(
                    n_txn_features=sum(
                        1 for f in model_feats if by_id.get(f) and by_id[f].source == "t_casino_txn"
                    ),
                    cfg=None,
                )

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

    if short_cols and not scorer_v2_feast_mode:
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

    if mid_term_needed and not scorer_v2_feast_mode:
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

    if mid_term_needed and not scorer_v2_feast_mode and manifest is not None and mid_term_pack_path is not None:
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
    elif mid_term_needed and not scorer_v2_feast_mode and manifest is not None and mid_term_pack_path is None:
        if require_runtime_artifacts:
            assert_mid_term_freshness_or_raise(
                manifest,
                mid_term_feature_count=len(mid_term_needed),
                sla_iso=sla,
            )
    elif mid_term_needed and not scorer_v2_feast_mode and manifest is None:
        raise ValueError(
            "[feature-supply] model uses mid_term registry features but no manifest was provided "
            "for freshness gate; pass active_manifest dict (coverage_end_exclusive). "
            f"mid_term columns example: {', '.join(mid_term_needed[:12])}",
        )

    fe_bundled = (fe_short_term_pack_path is not None and fe_short_term_pack_path.is_file()) or (
        fe_pack_path is not None and fe_pack_path.is_file()
    )
    if (fe_needed or any(f in model_feats for f in DEFAULT_MODEL_SLOW_PATRON_COLUMNS)) and not scorer_v2_feast_mode:
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


_SPIKE_MID_TERM_COLUMN_SET: frozenset[str] = frozenset(PRODUCTION_MID_TERM_FEATURE_COLUMNS)
_SPIKE_SLOW_COLUMN_SET: frozenset[str] = frozenset(PRODUCTION_LONG_TERM_FEATURE_COLUMNS)
_MATERIALIZER_MID_COLUMNS: frozenset[str] = frozenset(
    c for c in MID_TERM_SNAPSHOT_OUTPUT_COLUMNS if c not in ("canonical_id", "anchor_gaming_day_event")
)


@dataclass(frozen=True)
class RuntimeDependencyClosure:
    """Expanded runtime suppliers for one active model (model outputs + upstream deps)."""

    model_output_cols: tuple[str, ...]
    baseline_cols: tuple[str, ...]
    feast_trial_cols: tuple[str, ...]
    feast_mid_cols: tuple[str, ...]
    feast_slow_cols: tuple[str, ...]
    short_term_cols: tuple[str, ...]
    mid_composite_cols: tuple[str, ...]
    dependency_only_cols: tuple[str, ...]
    unknown_cols: tuple[str, ...]
    txn_cols: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScorerSupplierPlan:
    """Runtime supplier routing for one active model (scorer v2)."""

    baseline_cols: tuple[str, ...]
    feast_trial_cols: tuple[str, ...]
    feast_mid_cols: tuple[str, ...]
    mid_composite_cols: tuple[str, ...]
    feast_slow_cols: tuple[str, ...]
    short_term_cols: tuple[str, ...]
    unknown_cols: tuple[str, ...]
    dependency_only_cols: tuple[str, ...] = ()
    txn_cols: tuple[str, ...] = ()


def _infer_runtime_supplier(
    row: FeatureRegistryEntryRow | None,
    feature_id: str,
    *,
    mid_set: set[str],
    short_set: set[str],
) -> str | None:
    """Infer runtime supplier when registry row omits ``runtime_supplier``."""

    if feature_id in _TXN_LITE_COLUMN_SET:
        return "txn_lite_builder"
    if row is not None and row.runtime_supplier:
        return row.runtime_supplier
    if row is not None:
        from trainer_hightier.feature_experiment.feature_cadence import (
            SUPPLIER_MID_TERM_DAILY,
            SUPPLIER_SHORT_TERM_PIT,
            SUPPLIER_TXN_LITE,
        )

        ats = str(row.allowed_training_supplier or "").strip()
        if ats == SUPPLIER_SHORT_TERM_PIT:
            return "short_term_pit_builder"
        if ats == SUPPLIER_TXN_LITE:
            return "txn_lite_builder"
        if ats == SUPPLIER_MID_TERM_DAILY:
            if feature_id in MID_TERM_COMPOSITE_FEATURE_COLUMNS:
                return "composite"
            if feature_id in mid_set and feature_id in _SPIKE_MID_TERM_COLUMN_SET:
                return "feast_online_mid"
    if row is None:
        if feature_id in _SPIKE_MID_TERM_COLUMN_SET:
            return "feast_online_mid"
        if feature_id in _SPIKE_SLOW_COLUMN_SET:
            return "feast_online_slow"
        return None
    src = row.source
    if src == "baseline_model":
        return "clickhouse_raw"
    if src == "t_casino_txn":
        return "txn_lite_builder"
    if src == "feast_trial_1h":
        return "short_term_pit_builder"
    if src == "feast_slow_180d" or feature_id in DEFAULT_MODEL_SLOW_PATRON_COLUMNS:
        return "feast_online_slow"
    if src == "fe_derived":
        if feature_id in MID_TERM_COMPOSITE_FEATURE_COLUMNS:
            return "composite"
        if feature_id in mid_set and feature_id in _SPIKE_MID_TERM_COLUMN_SET:
            return "feast_online_mid"
        if feature_id in short_set:
            return "short_term_pit_builder"
        if feature_id in mid_set:
            return "composite" if feature_id in MID_TERM_COMPOSITE_FEATURE_COLUMNS else None
    return None


def _collect_closure_feature_ids(
    model_feats: tuple[str, ...],
    by_id: dict[str, FeatureRegistryEntryRow],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Expand model features via registry ``runtime_inputs`` (fail on cycles)."""

    needed: list[str] = []
    seen: set[str] = set()
    unknown: list[str] = []
    stack: list[tuple[str, frozenset[str]]] = [(f, frozenset()) for f in model_feats]

    while stack:
        fid, ancestors = stack.pop()
        if fid in ancestors:
            raise ValueError(f"[feature-supply] cyclic runtime dependency involving {fid!r}")
        if fid in seen:
            continue
        row = by_id.get(fid)
        if row is None and fid not in _SPIKE_MID_TERM_COLUMN_SET and fid not in _SPIKE_SLOW_COLUMN_SET:
            if fid in model_feats and fid in _MID_TERM_AUDIT_MODEL_COLUMNS:
                seen.add(fid)
                needed.append(fid)
                continue
            if fid in model_feats and fid in _TXN_LITE_COLUMN_SET:
                seen.add(fid)
                needed.append(fid)
                continue
            if fid in model_feats:
                unknown.append(fid)
            seen.add(fid)
            needed.append(fid)
            continue
        seen.add(fid)
        needed.append(fid)
        if row is None:
            continue
        next_anc = ancestors | {fid}
        for _, deps in runtime_inputs_from_registry(row, fid):
            for dep in deps:
                if dep not in seen:
                    stack.append((dep, next_anc))

    return tuple(dict.fromkeys(needed)), tuple(dict.fromkeys(unknown))


def build_runtime_dependency_closure(
    snap: CandidateRegistrySnapshot,
    model_feats: tuple[str, ...],
) -> RuntimeDependencyClosure:
    """Build supplier buckets from model outputs plus registry dependency closure."""

    by_id = {r.feature_id: r for r in snap.rows}
    model_set = set(model_feats)
    fe_split = classify_model_fe_features(snap, model_feats)
    mid_set = set(fe_split["mid_term"])
    short_set = set(fe_split["short_term"])
    all_feats, unknown_from_closure = _collect_closure_feature_ids(model_feats, by_id)

    baseline: list[str] = []
    trial: list[str] = []
    mid: list[str] = []
    mid_comp: list[str] = []
    slow: list[str] = []
    short: list[str] = []
    txn: list[str] = []
    unknown: list[str] = list(unknown_from_closure)

    def _append_unique(bucket: list[str], fid: str) -> None:
        if fid not in bucket:
            bucket.append(fid)

    def _classify_into_buckets(fid: str, *, is_model_output: bool) -> None:
        row = by_id.get(fid)
        supplier = _infer_runtime_supplier(row, fid, mid_set=mid_set, short_set=short_set)
        if supplier is None:
            if is_model_output and fid in _MID_TERM_AUDIT_MODEL_COLUMNS:
                return
            if is_model_output:
                unknown.append(fid)
            return
        if supplier == "clickhouse_raw" and is_model_output:
            _append_unique(baseline, fid)
        elif supplier == "short_term_pit_builder" and is_model_output:
            _append_unique(short, fid)
        elif supplier == "feast_online_mid":
            _append_unique(mid, fid)
        elif supplier == "feast_online_slow":
            _append_unique(slow, fid)
        elif supplier == "short_term_pit_builder":
            _append_unique(short, fid)
        elif supplier == "composite" and is_model_output:
            _append_unique(mid_comp, fid)
            for sup_key, deps in runtime_inputs_from_registry(row, fid):
                if sup_key == "feast_online_mid":
                    for dep in deps:
                        _append_unique(mid, dep)
                elif sup_key == "feast_online_slow":
                    for dep in deps:
                        _append_unique(slow, dep)
                elif sup_key == "short_term_pit_builder":
                    for dep in deps:
                        _append_unique(short, dep)
        elif supplier == "txn_lite_builder" and is_model_output:
            _append_unique(txn, fid)

    for fid in model_feats:
        _classify_into_buckets(fid, is_model_output=True)

    for fid in all_feats:
        if fid in model_set:
            continue
        _classify_into_buckets(fid, is_model_output=False)

    feast_mid = tuple(dict.fromkeys(c for c in mid if c in _SPIKE_MID_TERM_COLUMN_SET))
    feast_slow = tuple(dict.fromkeys(c for c in slow if c in _SPIKE_SLOW_COLUMN_SET))
    short_out = tuple(dict.fromkeys(short))
    dep_only_out = tuple(c for c in all_feats if c not in model_set and c not in mid_comp)
    audit_model_cols = _MID_TERM_AUDIT_MODEL_COLUMNS & model_set
    unknown_out = tuple(
        u for u in dict.fromkeys(unknown) if u not in audit_model_cols
    )
    return RuntimeDependencyClosure(
        model_output_cols=tuple(model_feats),
        baseline_cols=tuple(dict.fromkeys(baseline)),
        feast_trial_cols=tuple(dict.fromkeys(trial)),
        feast_mid_cols=feast_mid,
        feast_slow_cols=feast_slow,
        short_term_cols=short_out,
        mid_composite_cols=tuple(dict.fromkeys(mid_comp)),
        dependency_only_cols=dep_only_out,
        unknown_cols=unknown_out,
        txn_cols=tuple(dict.fromkeys(txn)),
    )


def build_scorer_supplier_plan(
    snap: CandidateRegistrySnapshot,
    model_feats: tuple[str, ...],
) -> ScorerSupplierPlan:
    """Map ``model.pkl`` feature columns to scorer v2 runtime suppliers."""
    closure = build_runtime_dependency_closure(snap, model_feats)
    return ScorerSupplierPlan(
        baseline_cols=closure.baseline_cols,
        feast_trial_cols=closure.feast_trial_cols,
        feast_mid_cols=closure.feast_mid_cols,
        mid_composite_cols=closure.mid_composite_cols,
        feast_slow_cols=closure.feast_slow_cols,
        short_term_cols=closure.short_term_cols,
        unknown_cols=closure.unknown_cols,
        dependency_only_cols=closure.dependency_only_cols,
        txn_cols=closure.txn_cols,
    )


def assert_composite_implementations_or_raise(plan: ScorerSupplierPlan) -> None:
    """Every composite model column must have a Python scorer implementation."""

    missing = [c for c in plan.mid_composite_cols if c not in MID_TERM_COMPOSITE_FEATURE_COLUMNS]
    if missing:
        raise ValueError(
            "[feature-supply] composite features lack scorer implementation: "
            f"{missing[:12]}"
        )


def assert_feast_plan_schema_support_or_raise(plan: ScorerSupplierPlan) -> None:
    """Fail when plan-required Feast columns are absent from production schema lists."""

    for col in plan.feast_mid_cols:
        if col not in _SPIKE_MID_TERM_COLUMN_SET:
            raise ValueError(
                f"[feature-supply] model requires feast_online_mid column {col!r} "
                "but it is not in PRODUCTION_MID_TERM_FEATURE_COLUMNS"
            )
        if col not in _MATERIALIZER_MID_COLUMNS:
            raise ValueError(
                f"[feature-supply] model requires feast_online_mid column {col!r} "
                "but it is not in MID_TERM_SNAPSHOT_OUTPUT_COLUMNS"
            )
    for col in plan.feast_slow_cols:
        if col not in _SPIKE_SLOW_COLUMN_SET:
            raise ValueError(
                f"[feature-supply] model requires feast_online_slow column {col!r} "
                "but it is not in PRODUCTION_LONG_TERM_FEATURE_COLUMNS"
            )


def assert_scorer_supplier_plan_or_raise(plan: ScorerSupplierPlan) -> None:
    """Fail fast when scorer v2 cannot supply every model column."""
    if plan.unknown_cols:
        tip = ", ".join(plan.unknown_cols[:12])
        ellipsis = "" if len(plan.unknown_cols) <= 12 else ", …"
        txn_unknown = [c for c in plan.unknown_cols if c in _TXN_LITE_COLUMN_SET]
        if txn_unknown:
            raise ValueError(
                "[feature-supply] installed trainer_hightier lacks txn_lite_builder routing "
                f"for model columns [{', '.join(txn_unknown)}]. "
                "Reinstall the bundle wheel from the bundle root: "
                "pip install --force-reinstall wheels/trainer_hightier-*.whl "
                "(or pip install --force-reinstall -r requirements.txt). "
                "Do not rely on an older conda/site-packages copy of the same version."
            )
        raise ValueError(
            "[feature-supply] scorer v2 supplier plan has unknown columns: "
            f"[{tip}{ellipsis}]"
        )
    assert_composite_implementations_or_raise(plan)
    assert_feast_plan_schema_support_or_raise(plan)


def _assert_ch_txn_supplier_ready_or_raise(
    *,
    n_txn_features: int,
    cfg: Any | None = None,
) -> dict[str, Any]:
    """Verify ClickHouse ``t_casino_txn`` is ready for production txn_lite scoring."""

    from trainer_hightier.serving.txn_lite_ch_runtime import assert_ch_txn_supplier_ready_or_raise

    detail = assert_ch_txn_supplier_ready_or_raise(cfg=cfg)
    detail["n_txn_features"] = int(n_txn_features)
    return detail


def _assert_cleaned_casino_txn_partitions_or_raise(
    txn_root: Path,
    *,
    n_txn_features: int,
) -> dict[str, Any]:
    """Verify L0 cleaned ``t_casino_txn`` partitions exist for txn_lite scoring."""

    from trainer_hightier.feature_experiment.materialize_txn_lite import discover_cleaned_txn_partitions

    root = Path(txn_root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(
            "[feature-supply] model requires txn__* "
            f"({n_txn_features} feature(s)) but cleaned casino_txn root missing: {root}. "
            "Populate bundle source_mirror/cleaned_casino_txn/ or set CLEANED_CASINO_TXN_ROOT in .env"
        )
    paths, included, excluded = discover_cleaned_txn_partitions(root, exclude_partial=True)
    if not paths:
        raise FileNotFoundError(
            "[feature-supply] model expects txn__* but no eligible cleaned txn partitions "
            f"under {root} (included={included}, excluded_partial={excluded})"
        )
    return {
        "cleaned_casino_txn_root": str(root),
        "cleaned_casino_txn_partition_count": len(included),
        "cleaned_casino_txn_excluded_partial": list(excluded),
    }


def assert_deploy_external_data_roots_or_raise(
    plan: ScorerSupplierPlan,
    *,
    cfg: Any | None = None,
) -> dict[str, Any]:
    """Verify deploy-host supplier readiness for the active model plan.

    Scorer v2 mid/long features come from bundle-local Feast online store (refreshed from
    ClickHouse at startup). Baseline, short-term PIT, and txn_lite features come from
    ClickHouse at score time. This gate covers **additional** external roots not bundled
    in the wheel (legacy cleaned txn root checks removed for production).
    """
    from trainer_hightier.config import default_hightier_serving_config

    _ = cfg or default_hightier_serving_config()
    out: dict[str, Any] = {
        "clickhouse_required": bool(
            plan.baseline_cols or plan.short_term_cols or plan.feast_trial_cols or plan.txn_cols
        ),
        "feast_online_required": bool(
            plan.feast_mid_cols or plan.feast_slow_cols or plan.mid_composite_cols
        ),
    }
    if plan.txn_cols:
        out["txn_lite"] = _assert_ch_txn_supplier_ready_or_raise(
            n_txn_features=len(plan.txn_cols),
            cfg=cfg,
        )
    return out


def scorer_supplier_route_counts(plan: ScorerSupplierPlan) -> dict[str, int]:
    """Count model columns routed to each scorer v2 runtime supplier."""
    return {
        "baseline_model": len(plan.baseline_cols),
        "feast_trial_1h": len(plan.feast_trial_cols),
        "feast_online_mid": len(plan.feast_mid_cols),
        "mid_term_composite": len(plan.mid_composite_cols),
        "feast_online_slow": len(plan.feast_slow_cols),
        "short_term_pit_builder": len(plan.short_term_cols),
        "txn_lite_builder": len(plan.txn_cols),
    }
