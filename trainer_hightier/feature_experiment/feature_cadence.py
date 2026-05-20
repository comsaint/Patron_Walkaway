"""Feature cadence contract defaults, audit, and training-time gates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from trainer_hightier.serving.candidate_registry_loader import (
    CandidateRegistrySnapshot,
    FeatureRegistryEntryRow,
)

CADENCE_EVENT_LEVEL: Final[str] = "event_level"
CADENCE_DAILY_GAMING_DAY: Final[str] = "daily_gaming_day"
CADENCE_MONTHLY: Final[str] = "monthly"

ANCHOR_TARGET_PREDICTION_TIME: Final[str] = "target_prediction_time"
ANCHOR_PRIOR_GAMING_DAY_END: Final[str] = "prior_gaming_day_end"
ANCHOR_MONTHLY_SNAPSHOT: Final[str] = "monthly_snapshot_anchor"

GRAIN_BET_ID: Final[str] = "bet_id"
GRAIN_CANONICAL_ANCHOR_DAY: Final[str] = "canonical_id + anchor_gaming_day"
GRAIN_CANONICAL_ANCHOR_MONTH: Final[str] = "canonical_id + anchor_month"

SUPPLIER_SHORT_TERM_PIT: Final[str] = "short_term_pit_builder"
SUPPLIER_MID_TERM_DAILY: Final[str] = "mid_term_daily_snapshot"
SUPPLIER_LONG_TERM_MONTHLY: Final[str] = "long_term_monthly_snapshot"
SUPPLIER_RAW: Final[str] = "clickhouse_raw"
SUPPLIER_FEAST_TRIAL: Final[str] = "feast_trial_1h"
SUPPLIER_FEAST_SLOW: Final[str] = "feast_slow_180d"

_LEGACY_FE_DERIVED_OWNER: Final[str] = "trainer_hightier/feature_experiment/materialize_fe_derived.py"

_MID_TERM_COMPOSITE_SHORT_DEPS: Final[dict[str, tuple[str, ...]]] = {
    "fe__wager_sum__w15m_over_w1d": ("fe__wager_sum__w15m",),
    "fe__interarrival__last_gap_z__w7d": ("fe__time_since_last_bet_sec",),
}

MID_TERM_COMPOSITE_FEATURE_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        "fe__wager_sum__w15m_over_w1d",
        "fe__wager_cv_w7d",
        "fe__payout_odds_z_prior_w30d",
        "fe__interarrival__last_gap_z__w7d",
    }
)


@dataclass(frozen=True)
class ResolvedFeatureCadence:
    """Effective cadence contract for one registry row."""

    feature_id: str
    time_horizon: str
    cadence: str
    anchor_rule: str
    grain: str
    allowed_training_supplier: str
    source: str


def default_cadence_for_horizon(time_horizon: str) -> str:
    """Map ``time_horizon`` to default cadence."""

    h = str(time_horizon).strip()
    if h == "short_term":
        return CADENCE_EVENT_LEVEL
    if h == "mid_term":
        return CADENCE_DAILY_GAMING_DAY
    if h == "long_term":
        return CADENCE_MONTHLY
    return CADENCE_EVENT_LEVEL


def default_anchor_rule_for_horizon(time_horizon: str) -> str:
    """Map ``time_horizon`` to default anchor rule."""

    h = str(time_horizon).strip()
    if h == "short_term":
        return ANCHOR_TARGET_PREDICTION_TIME
    if h == "mid_term":
        return ANCHOR_PRIOR_GAMING_DAY_END
    if h == "long_term":
        return ANCHOR_MONTHLY_SNAPSHOT
    return ANCHOR_TARGET_PREDICTION_TIME


def default_grain_for_horizon(time_horizon: str) -> str:
    """Map ``time_horizon`` to default artifact grain."""

    h = str(time_horizon).strip()
    if h == "short_term":
        return GRAIN_BET_ID
    if h == "mid_term":
        return GRAIN_CANONICAL_ANCHOR_DAY
    if h == "long_term":
        return GRAIN_CANONICAL_ANCHOR_MONTH
    return GRAIN_BET_ID


def default_training_supplier_for_row(row: FeatureRegistryEntryRow) -> str:
    """Infer default training supplier from registry ``source`` and horizon."""

    src = str(row.source).strip()
    h = str(row.time_horizon).strip()
    if src == "baseline_model":
        return SUPPLIER_RAW
    if src == "feast_trial_1h":
        return SUPPLIER_FEAST_TRIAL
    if src == "feast_slow_180d":
        return SUPPLIER_LONG_TERM_MONTHLY
    if src == "fe_derived":
        if h == "short_term":
            return SUPPLIER_SHORT_TERM_PIT
        if h == "mid_term":
            return SUPPLIER_MID_TERM_DAILY
    return SUPPLIER_SHORT_TERM_PIT


def _optional_str(raw: dict[str, Any], key: str) -> str | None:
    val = raw.get(key)
    if val is None:
        return None
    s = str(val).strip()
    return s or None


def resolve_feature_cadence(row: FeatureRegistryEntryRow, raw: dict[str, Any] | None = None) -> ResolvedFeatureCadence:
    """Resolve cadence fields with YAML overrides or horizon defaults."""

    raw = raw or {}
    cadence = row.cadence or _optional_str(raw, "cadence") or default_cadence_for_horizon(row.time_horizon)
    anchor = row.anchor_rule or _optional_str(raw, "anchor_rule") or default_anchor_rule_for_horizon(row.time_horizon)
    grain = row.grain or _optional_str(raw, "grain") or default_grain_for_horizon(row.time_horizon)
    supplier = (
        row.allowed_training_supplier
        or _optional_str(raw, "allowed_training_supplier")
        or default_training_supplier_for_row(row)
    )
    return ResolvedFeatureCadence(
        feature_id=row.feature_id,
        time_horizon=row.time_horizon,
        cadence=cadence,
        anchor_rule=anchor,
        grain=grain,
        allowed_training_supplier=supplier,
        source=row.source,
    )


def classify_model_fe_features(
    snapshot: CandidateRegistrySnapshot,
    model_features: tuple[str, ...],
    *,
    raw_rows: list[dict[str, Any]] | None = None,
) -> dict[str, tuple[str, ...]]:
    """Split model ``fe__*`` columns by resolved training supplier."""

    by_id = {r.feature_id: r for r in snapshot.rows}
    raw_by_id: dict[str, dict[str, Any]] = {}
    if raw_rows is not None:
        for item in raw_rows:
            if isinstance(item, dict) and item.get("feature_id"):
                raw_by_id[str(item["feature_id"])] = item
    short_cols: list[str] = []
    mid_cols: list[str] = []
    other_cols: list[str] = []
    for feat in model_features:
        row = by_id.get(feat)
        if row is None:
            if str(feat).startswith("fe__"):
                other_cols.append(feat)
            continue
        is_fe_derived = row.source == "fe_derived" or str(feat).startswith("fe__")
        if not is_fe_derived:
            continue
        resolved = resolve_feature_cadence(row, raw_by_id.get(feat))
        if resolved.allowed_training_supplier == SUPPLIER_MID_TERM_DAILY:
            mid_cols.append(feat)
        elif resolved.allowed_training_supplier == SUPPLIER_SHORT_TERM_PIT:
            short_cols.append(feat)
        else:
            other_cols.append(feat)
    return {
        "short_term": tuple(short_cols),
        "mid_term": tuple(mid_cols),
        "other": tuple(other_cols),
    }


def short_term_enrich_columns_with_dependencies(
    short_term_columns: tuple[str, ...],
    mid_term_columns: tuple[str, ...],
) -> tuple[str, ...]:
    """Return short-term columns plus composite dependency columns required at enrich."""

    deps: list[str] = []
    for mid_col in mid_term_columns:
        deps.extend(_MID_TERM_COMPOSITE_SHORT_DEPS.get(mid_col, ()))
    return tuple(dict.fromkeys([*short_term_columns, *deps]))


def build_feature_cadence_audit(
    snapshot: CandidateRegistrySnapshot,
    model_features: tuple[str, ...],
    *,
    raw_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Audit model features against cadence contract and legacy supplier drift."""

    by_id = {r.feature_id: r for r in snapshot.rows}
    raw_by_id: dict[str, dict[str, Any]] = {}
    if raw_rows is not None:
        for item in raw_rows:
            if isinstance(item, dict) and item.get("feature_id"):
                raw_by_id[str(item["feature_id"])] = item

    rows_out: list[dict[str, str]] = []
    violations: list[str] = []
    term_counts: dict[str, int] = {}
    cadence_counts: dict[str, int] = {}

    for feat in model_features:
        row = by_id.get(feat)
        if row is None:
            violations.append(f"{feat}: missing from registry")
            continue
        raw = raw_by_id.get(feat, {})
        resolved = resolve_feature_cadence(row, raw)
        term_counts[resolved.time_horizon] = term_counts.get(resolved.time_horizon, 0) + 1
        cadence_counts[resolved.cadence] = cadence_counts.get(resolved.cadence, 0) + 1
        rows_out.append(
            {
                "feature_id": feat,
                "source": resolved.source,
                "time_horizon": resolved.time_horizon,
                "cadence": resolved.cadence,
                "anchor_rule": resolved.anchor_rule,
                "grain": resolved.grain,
                "allowed_training_supplier": resolved.allowed_training_supplier,
            }
        )
        owner = (row.semantic_owner or "").strip()
        if (
            resolved.allowed_training_supplier == SUPPLIER_MID_TERM_DAILY
            and _LEGACY_FE_DERIVED_OWNER in owner
        ):
            violations.append(
                f"{feat}: mid_term daily snapshot still owned by legacy bet-grain materializer ({owner})",
            )

    fe_split = classify_model_fe_features(snapshot, model_features, raw_rows=raw_rows)
    return {
        "model_feature_count": len(model_features),
        "term_counts": term_counts,
        "cadence_counts": cadence_counts,
        "features": rows_out,
        "fe_short_term_columns": list(fe_split["short_term"]),
        "fe_mid_term_columns": list(fe_split["mid_term"]),
        "violations": violations,
        "violation_count": len(violations),
    }


def assert_feature_cadence_contract_or_raise(
    snapshot: CandidateRegistrySnapshot,
    model_features: tuple[str, ...],
    *,
    raw_rows: list[dict[str, Any]] | None = None,
    fail_on_legacy_mid_term_owner: bool = True,
) -> dict[str, Any]:
    """Fail fast when cadence audit finds contract violations."""

    audit = build_feature_cadence_audit(snapshot, model_features, raw_rows=raw_rows)
    if fail_on_legacy_mid_term_owner and audit["violation_count"] > 0:
        sample = "; ".join(audit["violations"][:8])
        raise ValueError(
            f"[feature-cadence] {audit['violation_count']} cadence violation(s) for model features: {sample}",
        )
    return audit
