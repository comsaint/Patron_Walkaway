"""Feature missing provenance artifacts for scorer cycles."""

from __future__ import annotations

from typing import Any, Sequence

import numpy as np
import pandas as pd

from trainer_hightier.serving.feast_online_adapter import RowMissingAudit
from trainer_hightier.serving.feature_supply import ScorerSupplierPlan


def _null_reason_for_feature(
    feature_id: str,
    *,
    plan: ScorerSupplierPlan,
) -> str:
    """Heuristic null reason from supplier plan membership."""
    if feature_id in plan.short_term_cols:
        return "short_term_missing"
    if feature_id in plan.feast_mid_cols or feature_id in plan.mid_composite_cols:
        return "feast_mid_missing"
    if feature_id in plan.feast_slow_cols:
        return "feast_slow_missing"
    if feature_id in ("wager", "player_id", "game_id", "bet_id"):
        return "raw_bet_missing"
    return "unknown"


def _source_layer_for_feature(
    feature_id: str,
    *,
    plan: ScorerSupplierPlan,
) -> str:
    """Map feature id to recorder source layer label."""
    if feature_id in plan.short_term_cols:
        return "short_term_pit"
    if feature_id in plan.feast_mid_cols:
        return "feast_mid"
    if feature_id in plan.mid_composite_cols:
        return "composite"
    if feature_id in plan.feast_slow_cols:
        return "feast_slow"
    return "raw_bet"


def build_feature_missing_provenance(
    staged: pd.DataFrame,
    features: pd.DataFrame,
    *,
    feature_columns: Sequence[str],
    supplier_plan: ScorerSupplierPlan,
    row_audits: Sequence[RowMissingAudit] | None = None,
) -> pd.DataFrame:
    """One row per (bet, feature) with null flag and source layer."""
    if staged.empty or features.empty:
        return pd.DataFrame()
    n = min(len(staged), len(features))
    bet_ids = staged["bet_id"].astype(str).head(n).tolist() if "bet_id" in staged.columns else [""] * n
    canonical = (
        staged["canonical_id"].astype(str).head(n).tolist()
        if "canonical_id" in staged.columns
        else [""] * n
    )
    rows: list[dict[str, Any]] = []
    for i in range(n):
        audit = row_audits[i] if row_audits and i < len(row_audits) else None
        for feat in feature_columns:
            if feat not in features.columns:
                val = np.nan
            else:
                val = features.iloc[i][feat]
            is_null = pd.isna(val)
            rows.append(
                {
                    "bet_id": bet_ids[i],
                    "canonical_id": canonical[i],
                    "feature_id": feat,
                    "feature_value": None if is_null else float(val)
                    if isinstance(val, (int, float, np.floating))
                    else val,
                    "is_null": bool(is_null),
                    "null_reason": _null_reason_for_feature(feat, plan=supplier_plan)
                    if is_null
                    else "",
                    "source_layer": _source_layer_for_feature(feat, plan=supplier_plan),
                    "model_features_missing": audit.model_features_missing if audit else None,
                    "feast_mid_missing": audit.feast_mid_missing if audit else None,
                    "feast_slow_missing": audit.feast_slow_missing if audit else None,
                    "short_term_missing": audit.short_term_missing if audit else None,
                }
            )
    return pd.DataFrame(rows)
