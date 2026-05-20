"""Feast online lookup adapter for scorer v2 (mid/long feature supplier)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import pandas as pd

from trainer_hightier.config import TRAINER_HIGHTIER_PACKAGE_DIR
from trainer_hightier.feature_experiment.feast_long_term_spike import (
    SPIKE_LONG_TERM_FEATURE_COLUMNS,
    SPIKE_ONLINE_FEATURE_REFS as LONG_SPIKE_ONLINE_FEATURE_REFS,
    SPIKE_FEATURE_VIEW_NAME as LONG_SPIKE_FEATURE_VIEW_NAME,
)
from trainer_hightier.feature_experiment.feast_mid_term_spike import (
    SPIKE_MID_TERM_FEATURE_COLUMNS,
    SPIKE_ONLINE_FEATURE_REFS as MID_SPIKE_ONLINE_FEATURE_REFS,
    SPIKE_FEATURE_SERVICE_NAME as MID_SPIKE_FEATURE_SERVICE_NAME,
    SPIKE_FEATURE_VIEW_NAME as MID_SPIKE_FEATURE_VIEW_NAME,
    _feast_entity_rows,
)
from trainer_hightier.serving.production_materialize import DEFAULT_MODEL_SLOW_PATRON_COLUMNS

logger = logging.getLogger(__name__)

FEAST_CANONICAL_ENTITY_NAME: str = "canonical_patron"
FEAST_CANONICAL_JOIN_KEY: str = "canonical_id"
FEAST_LONG_TERM_FEATURE_SERVICE_NAME: str = "walkaway_canonical_long_term_spike_v1"


@dataclass(frozen=True)
class RowMissingAudit:
    """Per-row missing-feature counts for prediction log audit."""

    model_features_missing: int
    fe_features_missing: int
    feast_mid_missing: int
    feast_slow_missing: int
    short_term_missing: int

    def family_summary(self) -> dict[str, int]:
        """JSON-serializable family missing counts."""
        return {
            "model_features_missing": self.model_features_missing,
            "fe_features_missing": self.fe_features_missing,
            "feast_mid_missing": self.feast_mid_missing,
            "feast_slow_missing": self.feast_slow_missing,
            "short_term_missing": self.short_term_missing,
        }


@dataclass(frozen=True)
class ScorerCycleReadinessSummary:
    """One scoring cycle Feast / supplier readiness snapshot."""

    supplier_routes: dict[str, int]
    feast_mid_columns: tuple[str, ...]
    feast_slow_columns: tuple[str, ...]
    short_term_columns: tuple[str, ...]
    n_requested: int
    n_scored: int
    n_skipped_entity_missing: int
    entity_missing_rate: float
    entity_missing_fail_fraction: float
    lookup_latency_ms: float
    cell_null_counts: dict[str, int]

    def to_log_dict(self) -> dict[str, Any]:
        """Compact dict for structured scorer logs."""
        return {
            "supplier_routes": dict(self.supplier_routes),
            "feast_mid_n": len(self.feast_mid_columns),
            "feast_slow_n": len(self.feast_slow_columns),
            "short_term_n": len(self.short_term_columns),
            "n_requested": self.n_requested,
            "n_scored": self.n_scored,
            "n_skipped_entity_missing": self.n_skipped_entity_missing,
            "entity_missing_rate": round(self.entity_missing_rate, 4),
            "entity_missing_fail_fraction": self.entity_missing_fail_fraction,
            "lookup_latency_ms": self.lookup_latency_ms,
            "cell_null_top": dict(sorted(self.cell_null_counts.items(), key=lambda kv: -kv[1])[:8]),
        }


def _is_null_cell(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def compute_row_missing_audits(
    features: pd.DataFrame,
    feature_columns: tuple[str, ...],
    *,
    feast_mid_cols: tuple[str, ...],
    feast_slow_cols: tuple[str, ...],
    short_term_cols: tuple[str, ...],
) -> list[RowMissingAudit]:
    """Count null model-input cells per row, split by feature family."""
    if features.empty:
        return []
    mid_set = set(feast_mid_cols)
    slow_set = set(feast_slow_cols)
    short_set = set(short_term_cols)
    out: list[RowMissingAudit] = []
    for pos in range(len(features)):
        row = features.iloc[pos]
        model_miss = 0
        fe_miss = 0
        mid_miss = 0
        slow_miss = 0
        short_miss = 0
        for col in feature_columns:
            null = _is_null_cell(row.get(col))
            if not null:
                continue
            model_miss += 1
            if str(col).startswith("fe__"):
                fe_miss += 1
            if col in mid_set:
                mid_miss += 1
            elif col in slow_set:
                slow_miss += 1
            elif col in short_set:
                short_miss += 1
        out.append(
            RowMissingAudit(
                model_features_missing=model_miss,
                fe_features_missing=fe_miss,
                feast_mid_missing=mid_miss,
                feast_slow_missing=slow_miss,
                short_term_missing=short_miss,
            )
        )
    return out


def build_cycle_readiness_summary(
    *,
    supplier_routes: dict[str, int],
    feast_mid_columns: tuple[str, ...],
    feast_slow_columns: tuple[str, ...],
    short_term_columns: tuple[str, ...],
    n_requested: int,
    n_scored: int,
    n_skipped_entity_missing: int,
    entity_missing_fail_fraction: float,
    feast_diag: FeastLookupDiagnostics,
) -> ScorerCycleReadinessSummary:
    """Build per-cycle readiness summary after Feast lookup."""
    rate = float(n_skipped_entity_missing) / float(n_requested) if n_requested else 0.0
    return ScorerCycleReadinessSummary(
        supplier_routes=supplier_routes,
        feast_mid_columns=feast_mid_columns,
        feast_slow_columns=feast_slow_columns,
        short_term_columns=short_term_columns,
        n_requested=n_requested,
        n_scored=n_scored,
        n_skipped_entity_missing=n_skipped_entity_missing,
        entity_missing_rate=rate,
        entity_missing_fail_fraction=entity_missing_fail_fraction,
        lookup_latency_ms=feast_diag.lookup_latency_ms,
        cell_null_counts=dict(feast_diag.cell_null_counts),
    )


def format_entity_missing_failure(
    *,
    n_missing: int,
    n_total: int,
    fail_fraction: float,
    feast_mid_columns: tuple[str, ...],
    feast_slow_columns: tuple[str, ...],
) -> str:
    """Human-readable hard-fail message for batch entity-missing threshold."""
    rate = float(n_missing) / float(n_total) if n_total else 0.0
    return (
        "[feast_adapter] entity row missing rate "
        f"{rate:.1%} exceeds fail_fraction={fail_fraction:.1%} "
        f"(missing={n_missing}/{n_total}); "
        f"supplier=feast_online mid_cols={len(feast_mid_columns)} slow_cols={len(feast_slow_columns)}"
    )


@dataclass(frozen=True)
class FeastLookupDiagnostics:
    """Lookup audit counters for one scoring batch."""

    lookup_latency_ms: float
    n_requested: int
    n_mid_present: int
    n_slow_present: int
    n_entity_missing: int
    cell_null_counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class FeastLookupResult:
    """Aligned Feast feature columns plus entity-missing mask (same index as input staged)."""

    feature_columns: tuple[str, ...]
    values: pd.DataFrame
    entity_missing: pd.Series
    diagnostics: FeastLookupDiagnostics


class OnlineFeastAdapter(Protocol):
    """Batch Feast online lookup for mid-term ``fe__*`` and long-term ``patron__*`` columns."""

    def lookup_mid_slow(
        self,
        canonical_ids: list[str],
        *,
        mid_columns: tuple[str, ...],
        slow_columns: tuple[str, ...],
    ) -> pd.DataFrame:
        """Return lookup frame keyed by ``canonical_id`` with requested feature columns."""


@dataclass
class MockFeastOnlineAdapter:
    """In-memory Feast substitute for unit tests and mock end-to-end slices."""

    features_by_canonical: dict[str, dict[str, Any]]
    absent_canonical: frozenset[str] = frozenset()
    mid_family_columns: frozenset[str] = frozenset(SPIKE_MID_TERM_FEATURE_COLUMNS)
    slow_family_columns: frozenset[str] = frozenset(DEFAULT_MODEL_SLOW_PATRON_COLUMNS)

    def lookup_mid_slow(
        self,
        canonical_ids: list[str],
        *,
        mid_columns: tuple[str, ...],
        slow_columns: tuple[str, ...],
    ) -> pd.DataFrame:
        """Return one row per requested canonical id (absent ids omitted)."""
        cols = tuple(dict.fromkeys([*mid_columns, *slow_columns]))
        rows: list[dict[str, Any]] = []
        for cid in canonical_ids:
            key = str(cid).strip()
            if not key or key in self.absent_canonical:
                continue
            payload = dict(self.features_by_canonical.get(key, {}))
            row = {"canonical_id": key}
            for col in cols:
                row[col] = payload.get(col, pd.NA)
            rows.append(row)
        if not rows:
            return pd.DataFrame(columns=["canonical_id", *cols])
        return pd.DataFrame(rows)


@dataclass(frozen=True)
class FeastSdkOnlineAdapter:
    """Production Feast SDK wrapper (dict-of-lists ``entity_rows`` batch lookup)."""

    feast_repo: Path
    online_feature_refs: tuple[str, ...] = tuple(
        dict.fromkeys([*MID_SPIKE_ONLINE_FEATURE_REFS, *LONG_SPIKE_ONLINE_FEATURE_REFS])
    )

    def lookup_mid_slow(
        self,
        canonical_ids: list[str],
        *,
        mid_columns: tuple[str, ...],
        slow_columns: tuple[str, ...],
    ) -> pd.DataFrame:
        """Call ``FeatureStore.get_online_features`` and return a canonical-id keyed frame."""
        from feast import FeatureStore

        ids = [str(x).strip() for x in canonical_ids if str(x).strip()]
        wanted = tuple(dict.fromkeys([*mid_columns, *slow_columns]))
        if not ids:
            return pd.DataFrame(columns=["canonical_id", *wanted])
        store = FeatureStore(repo_path=str(Path(self.feast_repo).resolve()))
        refs = list(resolve_online_feature_refs(mid_columns, slow_columns))
        if not refs:
            raise ValueError(
                f"[feast_adapter] no online feature refs resolved for columns={wanted!r}"
            )
        t0 = time.perf_counter()
        out = store.get_online_features(
            features=list(refs),
            entity_rows=_feast_entity_rows(ids),
        ).to_df()
        latency_ms = round((time.perf_counter() - t0) * 1000.0, 3)
        logger.info(
            "[feast_adapter] lookup n=%d cols=%d latency_ms=%.3f",
            len(ids),
            len(wanted),
            latency_ms,
        )
        if out.empty:
            return pd.DataFrame(columns=["canonical_id", *wanted])
        if "canonical_id" not in out.columns:
            raise ValueError("[feast_adapter] Feast response missing canonical_id column")
        keep = ["canonical_id", *[c for c in wanted if c in out.columns]]
        return out[keep].copy()


def default_feast_repo_path() -> Path:
    """Default Feast repo bundled with ``trainer_hightier``."""
    return TRAINER_HIGHTIER_PACKAGE_DIR / "feast_repo"


def _feast_registry_db_path(feast_repo: Path) -> Path:
    return Path(feast_repo).resolve() / "data" / "registry.db"


def resolve_online_feature_refs(
    mid_columns: tuple[str, ...],
    slow_columns: tuple[str, ...],
) -> tuple[str, ...]:
    """Map model Feast columns to ``feature_view:column`` online refs."""
    mid_by_col = {r.split(":", 1)[-1]: r for r in MID_SPIKE_ONLINE_FEATURE_REFS}
    slow_by_col = {r.split(":", 1)[-1]: r for r in LONG_SPIKE_ONLINE_FEATURE_REFS}
    refs: list[str] = []
    unknown: list[str] = []
    for col in mid_columns:
        ref = mid_by_col.get(col)
        if ref is None:
            unknown.append(col)
        else:
            refs.append(ref)
    for col in slow_columns:
        ref = slow_by_col.get(col)
        if ref is None:
            unknown.append(col)
        else:
            refs.append(ref)
    if unknown:
        tip = ", ".join(unknown[:12])
        ellipsis = "" if len(unknown) <= 12 else ", …"
        raise ValueError(
            "[feast_smoke] model Feast columns have no spike online feature ref: "
            f"[{tip}{ellipsis}]"
        )
    return tuple(dict.fromkeys(refs))


@dataclass(frozen=True)
class FeastScorerSmokeResult:
    """Outcome of startup Feast schema / online probe smoke."""

    feast_repo: Path
    entity_name: str
    entity_join_key: str
    mid_feature_view: str | None
    slow_feature_view: str | None
    mid_feature_service: str | None
    slow_feature_service: str | None
    feature_refs_checked: tuple[str, ...]
    online_probe_ok: bool

    def to_log_dict(self) -> dict[str, Any]:
        """Compact dict for scorer startup logs."""
        return {
            "feast_repo": str(self.feast_repo),
            "entity": self.entity_name,
            "join_key": self.entity_join_key,
            "mid_feature_view": self.mid_feature_view,
            "slow_feature_view": self.slow_feature_view,
            "mid_feature_service": self.mid_feature_service,
            "slow_feature_service": self.slow_feature_service,
            "feature_refs_checked": len(self.feature_refs_checked),
            "online_probe_ok": self.online_probe_ok,
        }


def _check_feast_entity_key(store: Any) -> None:
    """Validate canonical patron entity name, join key, and STRING value type."""
    from feast.value_type import ValueType

    try:
        entity = store.get_entity(FEAST_CANONICAL_ENTITY_NAME)
    except Exception as exc:
        raise RuntimeError(
            f"[feast_smoke] entity {FEAST_CANONICAL_ENTITY_NAME!r} missing from Feast registry"
        ) from exc
    join_keys = [str(k) for k in list(getattr(entity, "join_keys", []) or [])]
    if FEAST_CANONICAL_JOIN_KEY not in join_keys:
        raise RuntimeError(
            "[feast_smoke] entity key mismatch: "
            f"expected join key {FEAST_CANONICAL_JOIN_KEY!r}, registry join_keys={join_keys!r}"
        )
    value_type = getattr(entity, "value_type", None)
    if value_type is not None and value_type != ValueType.STRING:
        raise RuntimeError(
            "[feast_smoke] entity key type mismatch: "
            f"expected ValueType.STRING for {FEAST_CANONICAL_JOIN_KEY!r}, got {value_type!r}"
        )


def _check_feast_feature_view_columns(
    store: Any,
    *,
    view_name: str,
    required_columns: tuple[str, ...],
) -> None:
    """Ensure a feature view exists and exposes all required column names."""
    if not required_columns:
        return
    try:
        feature_view = store.get_feature_view(view_name)
    except Exception as exc:
        raise RuntimeError(f"[feast_smoke] feature view {view_name!r} missing from registry") from exc
    schema_names = {str(field.name) for field in list(getattr(feature_view, "schema", []) or [])}
    missing = [c for c in required_columns if c not in schema_names]
    if missing:
        sample = sorted(schema_names)[:12]
        raise RuntimeError(
            f"[feast_smoke] feature name mismatch in {view_name!r}: missing {missing}; "
            f"registry schema sample={sample}"
        )


def _check_feast_feature_service(store: Any, service_name: str) -> None:
    """Ensure a feature service is registered."""
    try:
        store.get_feature_service(service_name)
    except Exception as exc:
        raise RuntimeError(f"[feast_smoke] feature service {service_name!r} missing from registry") from exc


def run_feast_scorer_schema_smoke_check(
    feast_repo: Path,
    *,
    mid_columns: tuple[str, ...],
    slow_columns: tuple[str, ...],
    probe_canonical_id: str = "__feast_scorer_smoke_probe__",
    run_online_probe: bool = True,
) -> FeastScorerSmokeResult:
    """Fail fast when Feast registry schema does not match scorer v2 mid/long requirements."""
    if not mid_columns and not slow_columns:
        return FeastScorerSmokeResult(
            feast_repo=Path(feast_repo).resolve(),
            entity_name=FEAST_CANONICAL_ENTITY_NAME,
            entity_join_key=FEAST_CANONICAL_JOIN_KEY,
            mid_feature_view=None,
            slow_feature_view=None,
            mid_feature_service=None,
            slow_feature_service=None,
            feature_refs_checked=(),
            online_probe_ok=False,
        )
    repo = Path(feast_repo).resolve()
    if not repo.is_dir():
        raise FileNotFoundError(f"[feast_smoke] feast repo directory missing: {repo}")
    registry_path = _feast_registry_db_path(repo)
    if not registry_path.is_file():
        raise FileNotFoundError(
            f"[feast_smoke] Feast registry missing at {registry_path}; "
            f"run `feast apply` under {repo}"
        )
    from feast import FeatureStore

    store = FeatureStore(repo_path=str(repo))
    _check_feast_entity_key(store)
    if mid_columns:
        _check_feast_feature_view_columns(
            store,
            view_name=MID_SPIKE_FEATURE_VIEW_NAME,
            required_columns=mid_columns,
        )
        _check_feast_feature_service(store, MID_SPIKE_FEATURE_SERVICE_NAME)
    if slow_columns:
        _check_feast_feature_view_columns(
            store,
            view_name=LONG_SPIKE_FEATURE_VIEW_NAME,
            required_columns=slow_columns,
        )
        _check_feast_feature_service(store, FEAST_LONG_TERM_FEATURE_SERVICE_NAME)
    feature_refs = resolve_online_feature_refs(mid_columns, slow_columns)
    online_probe_ok = False
    if run_online_probe and feature_refs:
        probe_id = str(probe_canonical_id).strip() or "__feast_scorer_smoke_probe__"
        try:
            probe_out = store.get_online_features(
                features=list(feature_refs),
                entity_rows=_feast_entity_rows([probe_id]),
            ).to_df()
        except Exception as exc:
            raise RuntimeError(
                "[feast_smoke] online probe lookup failed for "
                f"entity={FEAST_CANONICAL_ENTITY_NAME!r} refs={len(feature_refs)}: {exc}"
            ) from exc
        if not probe_out.empty and FEAST_CANONICAL_JOIN_KEY not in probe_out.columns:
            raise RuntimeError(
                "[feast_smoke] online probe response missing "
                f"{FEAST_CANONICAL_JOIN_KEY!r} column; got columns={list(probe_out.columns)}"
            )
        online_probe_ok = True
    return FeastScorerSmokeResult(
        feast_repo=repo,
        entity_name=FEAST_CANONICAL_ENTITY_NAME,
        entity_join_key=FEAST_CANONICAL_JOIN_KEY,
        mid_feature_view=MID_SPIKE_FEATURE_VIEW_NAME if mid_columns else None,
        slow_feature_view=LONG_SPIKE_FEATURE_VIEW_NAME if slow_columns else None,
        mid_feature_service=MID_SPIKE_FEATURE_SERVICE_NAME if mid_columns else None,
        slow_feature_service=FEAST_LONG_TERM_FEATURE_SERVICE_NAME if slow_columns else None,
        feature_refs_checked=feature_refs,
        online_probe_ok=online_probe_ok,
    )


def build_production_feast_adapter() -> OnlineFeastAdapter:
    """Construct the production Feast SDK adapter."""
    return FeastSdkOnlineAdapter(feast_repo=default_feast_repo_path())


def join_feast_lookup(
    staged: pd.DataFrame,
    lookup_df: pd.DataFrame,
    *,
    feature_columns: tuple[str, ...],
    mid_columns: tuple[str, ...],
    slow_columns: tuple[str, ...],
) -> FeastLookupResult:
    """Left-join Feast values onto *staged* and classify entity-missing rows."""
    if staged.empty:
        empty = pd.DataFrame(columns=list(feature_columns))
        diag = FeastLookupDiagnostics(
            lookup_latency_ms=0.0,
            n_requested=0,
            n_mid_present=0,
            n_slow_present=0,
            n_entity_missing=0,
        )
        return FeastLookupResult(
            feature_columns=feature_columns,
            values=empty,
            entity_missing=pd.Series(dtype=bool),
            diagnostics=diag,
        )
    if "canonical_id" not in staged.columns:
        raise ValueError("[feast_adapter] staged frame missing canonical_id before Feast join")
    out = staged.copy()
    cols = tuple(dict.fromkeys(feature_columns))
    if lookup_df is None or lookup_df.empty:
        for col in cols:
            out[col] = pd.NA
        merged = out
        present_ids: set[str] = set()
    else:
        lk = lookup_df.copy()
        lk["canonical_id"] = lk["canonical_id"].astype(str).str.strip()
        present_ids = set(lk["canonical_id"].tolist())
        merged = out.merge(lk, on="canonical_id", how="left", suffixes=("", "_feast_dup"))
        for col in cols:
            if col not in merged.columns:
                merged[col] = pd.NA
    entity_missing = pd.Series(False, index=merged.index)
    n_mid_present = 0
    n_slow_present = 0
    cell_null_counts: dict[str, int] = {c: 0 for c in cols}
    for idx, row in merged.iterrows():
        cid = str(row.get("canonical_id", "")).strip()
        mid_ok = (not mid_columns) or (cid in present_ids)
        slow_ok = (not slow_columns) or (cid in present_ids)
        if mid_columns and mid_ok:
            n_mid_present += 1
        if slow_columns and slow_ok:
            n_slow_present += 1
        missing_entity = (bool(mid_columns) and not mid_ok) or (bool(slow_columns) and not slow_ok)
        entity_missing.at[idx] = missing_entity
        for col in cols:
            if col not in row.index or pd.isna(row[col]):
                cell_null_counts[col] = cell_null_counts.get(col, 0) + 1
    n_entity_missing = int(entity_missing.sum())
    diag = FeastLookupDiagnostics(
        lookup_latency_ms=0.0,
        n_requested=int(len(staged)),
        n_mid_present=n_mid_present,
        n_slow_present=n_slow_present,
        n_entity_missing=n_entity_missing,
        cell_null_counts=cell_null_counts,
    )
    return FeastLookupResult(
        feature_columns=cols,
        values=merged,
        entity_missing=entity_missing,
        diagnostics=diag,
    )


def apply_entity_missing_policy(
    staged: pd.DataFrame,
    lookup: FeastLookupResult,
    *,
    fail_fraction: float,
    mid_columns: tuple[str, ...] = (),
    slow_columns: tuple[str, ...] = (),
) -> tuple[pd.DataFrame, pd.DataFrame, FeastLookupDiagnostics]:
    """Split *staged* into scorable vs entity-missing skipped rows; hard-fail high missing rates."""
    if fail_fraction < 0.0 or fail_fraction > 1.0:
        raise ValueError(f"fail_fraction must be in [0,1], got {fail_fraction!r}")
    if staged.empty:
        return staged.copy(), staged.iloc[0:0].copy(), lookup.diagnostics
    mask = lookup.entity_missing.reindex(staged.index, fill_value=False)
    n = int(len(staged))
    n_missing = int(mask.sum())
    rate = float(n_missing) / float(n) if n else 0.0
    if n > 0 and rate > fail_fraction:
        raise RuntimeError(
            format_entity_missing_failure(
                n_missing=n_missing,
                n_total=n,
                fail_fraction=fail_fraction,
                feast_mid_columns=mid_columns,
                feast_slow_columns=slow_columns,
            )
        )
    skipped = staged.loc[mask].copy()
    scorable = staged.loc[~mask].copy()
    diag = FeastLookupDiagnostics(
        lookup_latency_ms=lookup.diagnostics.lookup_latency_ms,
        n_requested=lookup.diagnostics.n_requested,
        n_mid_present=lookup.diagnostics.n_mid_present,
        n_slow_present=lookup.diagnostics.n_slow_present,
        n_entity_missing=n_missing,
        cell_null_counts=dict(lookup.diagnostics.cell_null_counts),
    )
    if n_missing:
        logger.warning(
            "[feast_adapter] skipped %d/%d rows with entity-missing Feast families (rate=%.1f%%)",
            n_missing,
            n,
            rate * 100.0,
        )
    return scorable, skipped, diag
