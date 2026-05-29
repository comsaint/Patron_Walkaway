"""Deploy preflight helpers for production snapshot bootstrap."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from trainer_hightier.config import (
    FE_DERIVED_SOURCE_KIND_SHIPPED,
    MANIFEST_KEY_FE_SHORT_TERM,
    MANIFEST_KEY_MID_TERM_ANCHOR_MAX,
    MANIFEST_KEY_MID_TERM_COVERAGE_END,
    MANIFEST_KEY_MID_TERM_GENERATED_AT,
    MANIFEST_KEY_MID_TERM_GRAIN,
    MANIFEST_KEY_MID_TERM_SNAPSHOT,
    MANIFEST_KEY_MID_TERM_STALE_HARD_CAP_DAYS,
    MANIFEST_KEY_SLOW_ANCHOR_MAX,
    MANIFEST_KEY_SLOW_GENERATED_AT,
    MANIFEST_KEY_SLOW_MONTHLY_GRACE_DAYS,
    MANIFEST_KEY_SLOW_STALE_HARD_CAP_DAYS,
    MID_TERM_GRAIN_CANONICAL_DAILY_ASOF,
    MID_TERM_STALE_HARD_CAP_DAYS,
    SLOW_MONTHLY_GRACE_DAYS,
    SLOW_PATRON_GRAIN_CANONICAL_ASOF,
    SLOW_STALE_HARD_CAP_DAYS,
    default_hightier_serving_config,
)
from trainer_hightier.serving.feature_state_store import publish_manifest_atomic, read_active_manifest
from trainer_hightier.serving.feature_supply import MANIFEST_KEY_FE_DERIVED
from trainer_hightier.serving.snapshot_freshness import (
    build_scoring_snapshot_gate,
    evaluate_mid_term_freshness,
    evaluate_slow_freshness,
    read_mid_term_anchor_max,
    read_slow_anchor_max,
    validate_mid_term_artifact,
    validate_slow_artifact,
)

logger = logging.getLogger(__name__)


def preflight_validate_shipped_snapshots(
    manifest: dict[str, Any],
    *,
    manifest_dir: Path,
) -> dict[str, Any]:
    """Validate bundled snapshot artifacts and return per-layer freshness summary."""

    cfg = default_hightier_serving_config()
    snap_root = Path(manifest_dir).resolve()

    def _resolve(key: str) -> Path | None:
        rel = manifest.get(key)
        if not rel:
            return None
        fp = (snap_root / str(rel)).resolve()
        return fp if fp.is_file() else None

    mid_path = _resolve(MANIFEST_KEY_MID_TERM_SNAPSHOT)
    slow_path = _resolve("slow_patron_parquet")
    mid_val = validate_mid_term_artifact(
        mid_path,
        manifest_grain=str(manifest.get(MANIFEST_KEY_MID_TERM_GRAIN) or MID_TERM_GRAIN_CANONICAL_DAILY_ASOF),
    )
    slow_val = validate_slow_artifact(
        slow_path,
        manifest_grain=str(manifest.get("slow_patron_grain") or SLOW_PATRON_GRAIN_CANONICAL_ASOF),
    )
    mid_anchor = read_mid_term_anchor_max(mid_path, manifest)
    slow_anchor = read_slow_anchor_max(slow_path, manifest)
    mid_fresh = evaluate_mid_term_freshness(
        anchor_max=mid_anchor,
        hard_cap_days=int(manifest.get(MANIFEST_KEY_MID_TERM_STALE_HARD_CAP_DAYS) or cfg.mid_term_stale_hard_cap_days),
        close_hour=int(cfg.gaming_day_close_hour),
    )
    slow_fresh = evaluate_slow_freshness(
        anchor_max=slow_anchor,
        monthly_grace_days=int(manifest.get(MANIFEST_KEY_SLOW_MONTHLY_GRACE_DAYS) or cfg.slow_monthly_grace_days),
        hard_cap_days=int(manifest.get(MANIFEST_KEY_SLOW_STALE_HARD_CAP_DAYS) or cfg.slow_stale_hard_cap_days),
        close_hour=int(cfg.gaming_day_close_hour),
    )
    gate = build_scoring_snapshot_gate(
        mid_term=mid_fresh,
        slow=slow_fresh,
        mid_validation=mid_val,
        slow_validation=slow_val,
    )
    if not gate.allow_scoring:
        raise ValueError(
            "[deploy-preflight] shipped snapshots failed validation: "
            f"{gate.hard_failure_reason}"
        )
    return {
        "mid_term_status": mid_fresh.status,
        "slow_status": slow_fresh.status,
        "degraded": gate.degraded,
        "mid_term_anchor_max": mid_anchor.isoformat() if mid_anchor else None,
        "slow_anchor_max": slow_anchor.isoformat() if slow_anchor else None,
    }


def bootstrap_manifest_from_deploy_inputs(
    *,
    deploy_inputs_dir: Path,
    snapshot_manifest_dir: Path,
    model_version: str,
) -> Path:
    """Copy shipped deploy_inputs snapshots into serving manifest dir and publish manifest."""

    di = Path(deploy_inputs_dir).resolve()
    snap_root = Path(snapshot_manifest_dir).resolve()
    snap_root.mkdir(parents=True, exist_ok=True)
    man_path = di / "active_manifest.json"
    if not man_path.is_file():
        raise FileNotFoundError(f"deploy_inputs active_manifest.json missing: {man_path}")
    man = json.loads(man_path.read_text(encoding="utf-8"))
    if not isinstance(man, dict):
        raise ValueError("deploy_inputs active_manifest.json must be a JSON object")

    copied: dict[str, str] = {}
    for key in (
        "slow_patron_parquet",
        MANIFEST_KEY_FE_DERIVED,
        MANIFEST_KEY_FE_SHORT_TERM,
        MANIFEST_KEY_MID_TERM_SNAPSHOT,
        "adt_allowlist_parquet",
        "trial_bet_behavior_parquet",
    ):
        rel = man.get(key)
        if not rel:
            continue
        src = (di / str(rel)).resolve()
        if not src.is_file():
            raise FileNotFoundError(f"deploy_inputs missing artifact for {key}: {src}")
        dest = snap_root / src.name
        if not dest.is_file() or dest.stat().st_mtime < src.stat().st_mtime:
            dest.write_bytes(src.read_bytes())
        copied[key] = dest.name

    payload = dict(man)
    payload["version"] = str(man.get("version") or model_version)
    payload["model_version"] = model_version
    for key, name in copied.items():
        payload[key] = name
    if MANIFEST_KEY_FE_DERIVED in copied and MANIFEST_KEY_FE_SHORT_TERM not in copied:
        payload[MANIFEST_KEY_FE_SHORT_TERM] = copied[MANIFEST_KEY_FE_DERIVED]
    payload.setdefault(MANIFEST_KEY_MID_TERM_GRAIN, MID_TERM_GRAIN_CANONICAL_DAILY_ASOF)
    payload.setdefault("slow_patron_grain", SLOW_PATRON_GRAIN_CANONICAL_ASOF)
    payload.setdefault(MANIFEST_KEY_MID_TERM_STALE_HARD_CAP_DAYS, MID_TERM_STALE_HARD_CAP_DAYS)
    payload.setdefault(MANIFEST_KEY_SLOW_MONTHLY_GRACE_DAYS, SLOW_MONTHLY_GRACE_DAYS)
    payload.setdefault(MANIFEST_KEY_SLOW_STALE_HARD_CAP_DAYS, SLOW_STALE_HARD_CAP_DAYS)
    payload.setdefault("fe_derived_source_kind", FE_DERIVED_SOURCE_KIND_SHIPPED)

    preflight_validate_shipped_snapshots(payload, manifest_dir=snap_root)
    return publish_manifest_atomic(payload)
