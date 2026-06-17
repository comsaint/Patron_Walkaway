"""Feature supplier deploy contract: requirements, validators, and ``deploy_contract.json``."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from trainer_hightier.config import (
    DEPLOY_CONTRACT_FILENAME,
    DEPLOY_CONTRACT_SCHEMA_VERSION,
    FEATURE_CONTRACT_DEPLOY_STRICT,
    FEATURE_CONTRACT_PACKAGE_STRICT,
)
from trainer_hightier.serving.feature_supply import (
    ScorerSupplierPlan,
    assert_composite_implementations_or_raise,
    assert_scorer_supplier_plan_or_raise,
)

logger = logging.getLogger(__name__)

SupplierStage = Literal["package", "deploy_e2e", "deploy_preflight"]
ValidatorFn = Callable[..., dict[str, Any]]

BUNDLE_STATIC_SUPPLIER_ID: str = "bundle_static_artifact"
BUNDLE_STATIC_MAPPING_REL: str = "mapping/canonical_player_mapping.parquet"
BUNDLE_STATIC_ALLOWLIST_REL: str = "mapping/adt_allowed_players_q0p99.parquet"


@dataclass(frozen=True)
class SupplierRequirement:
    """Runtime resource contract for one production supplier."""

    supplier_id: str
    taxonomy: str
    required_clickhouse_tables: tuple[str, ...] = ()
    required_bundle_paths: tuple[str, ...] = ()
    required_feast_layers: tuple[str, ...] = ()
    validator_id: str = ""
    always_include: bool = False


@dataclass(frozen=True)
class SupplierValidatorSpec:
    """Registered validator for one supplier id."""

    validator_id: str
    stages: tuple[SupplierStage, ...]
    validate: ValidatorFn


@dataclass
class DeployFeatureContract:
    """Machine-readable per-model deploy supplier contract."""

    schema_version: str
    generated_at: str
    model_version: str
    feature_count: int
    registry_fingerprint: str
    supplier_plan: dict[str, Any]
    requirements: list[dict[str, Any]]
    validators: list[dict[str, Any]]
    flags: dict[str, bool]
    contract_fingerprint: str


SUPPLIER_REQUIREMENTS: dict[str, SupplierRequirement] = {
    "clickhouse_raw": SupplierRequirement(
        supplier_id="clickhouse_raw",
        taxonomy="clickhouse_raw",
        required_clickhouse_tables=("t_bet",),
        validator_id="validate_clickhouse_bet_source",
    ),
    "short_term_pit_builder": SupplierRequirement(
        supplier_id="short_term_pit_builder",
        taxonomy="short_term_pit",
        required_clickhouse_tables=("t_bet",),
        validator_id="validate_clickhouse_bet_source",
    ),
    "txn_lite_builder": SupplierRequirement(
        supplier_id="txn_lite_builder",
        taxonomy="short_term_pit",
        required_clickhouse_tables=("t_casino_txn",),
        validator_id="validate_ch_txn_supplier",
    ),
    "feast_online_mid": SupplierRequirement(
        supplier_id="feast_online_mid",
        taxonomy="feast_online_mid",
        required_feast_layers=("mid",),
        validator_id="validate_feast_online_mid",
    ),
    "feast_online_slow": SupplierRequirement(
        supplier_id="feast_online_slow",
        taxonomy="feast_online_slow",
        required_feast_layers=("slow",),
        validator_id="validate_feast_online_slow",
    ),
    "composite": SupplierRequirement(
        supplier_id="composite",
        taxonomy="mid_composite",
        validator_id="validate_mid_composite",
    ),
    BUNDLE_STATIC_SUPPLIER_ID: SupplierRequirement(
        supplier_id=BUNDLE_STATIC_SUPPLIER_ID,
        taxonomy="bundle_static_artifact",
        required_bundle_paths=(BUNDLE_STATIC_MAPPING_REL, BUNDLE_STATIC_ALLOWLIST_REL),
        validator_id="validate_bundle_static_artifacts",
        always_include=True,
    ),
}


def _stable_json_hash(payload: dict[str, Any]) -> str:
    """Return sha256 hex digest for a JSON-serializable payload."""

    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _supplier_plan_payload(plan: ScorerSupplierPlan) -> dict[str, Any]:
    """Serialize supplier buckets for contract storage and fingerprinting."""

    buckets = {
        "baseline_cols": list(plan.baseline_cols),
        "feast_trial_cols": list(plan.feast_trial_cols),
        "short_term_cols": list(plan.short_term_cols),
        "txn_cols": list(plan.txn_cols),
        "feast_mid_cols": list(plan.feast_mid_cols),
        "feast_slow_cols": list(plan.feast_slow_cols),
        "mid_composite_cols": list(plan.mid_composite_cols),
        "unknown_cols": list(plan.unknown_cols),
    }
    return {
        **buckets,
        "bucket_hashes": {name: _stable_json_hash({name: cols}) for name, cols in buckets.items()},
    }


def assert_no_legacy_feast_trial_cols(plan: ScorerSupplierPlan) -> None:
    """Fail when legacy ``feast_trial_1h`` bucket is non-empty."""

    if plan.feast_trial_cols:
        tip = ", ".join(plan.feast_trial_cols[:12])
        ellipsis = "" if len(plan.feast_trial_cols) <= 12 else ", …"
        raise ValueError(
            "[feature-contract] scorer v2 contract requires empty feast_trial_cols; "
            f"got [{tip}{ellipsis}]. Route features to short_term_pit_builder."
        )


def _active_supplier_ids(plan: ScorerSupplierPlan) -> set[str]:
    """Return runtime supplier ids required by the active model plan."""

    active: set[str] = set()
    if plan.baseline_cols:
        active.add("clickhouse_raw")
    if plan.short_term_cols:
        active.add("short_term_pit_builder")
    if plan.txn_cols:
        active.add("txn_lite_builder")
    if plan.feast_mid_cols:
        active.add("feast_online_mid")
    if plan.feast_slow_cols:
        active.add("feast_online_slow")
    if plan.mid_composite_cols:
        active.add("composite")
    return active


def resolve_supplier_requirements(
    plan: ScorerSupplierPlan,
    *,
    include_bundle_static: bool = True,
) -> tuple[SupplierRequirement, ...]:
    """Resolve model-scoped supplier requirements plus always-on bundle artifacts."""

    req_by_id: dict[str, SupplierRequirement] = {}
    for sid in _active_supplier_ids(plan):
        req = SUPPLIER_REQUIREMENTS.get(sid)
        if req is None:
            raise ValueError(f"[feature-contract] no requirement map for supplier {sid!r}")
        req_by_id[sid] = req
    if include_bundle_static:
        static_req = SUPPLIER_REQUIREMENTS[BUNDLE_STATIC_SUPPLIER_ID]
        req_by_id[static_req.supplier_id] = static_req
    return tuple(req_by_id[sid] for sid in sorted(req_by_id))


def assert_all_requirements_have_validators(
    requirements: tuple[SupplierRequirement, ...],
) -> None:
    """Fail when any required supplier lacks a registered validator."""

    missing = sorted({r.supplier_id for r in requirements if r.supplier_id not in SUPPLIER_VALIDATORS})
    if missing:
        raise ValueError(
            "[feature-contract] suppliers missing validator registry entries: "
            f"{missing}"
        )


def _contract_flags(plan: ScorerSupplierPlan) -> dict[str, bool]:
    """Derive deploy requirement flags from the supplier plan."""

    return {
        "deploy_requires_clickhouse": bool(
            plan.baseline_cols or plan.short_term_cols or plan.txn_cols,
        ),
        "deploy_requires_feast_online": bool(
            plan.feast_mid_cols or plan.feast_slow_cols or plan.mid_composite_cols,
        ),
        "deploy_requires_ch_txn": bool(plan.txn_cols),
    }


def build_deploy_contract(
    *,
    plan: ScorerSupplierPlan,
    requirements: tuple[SupplierRequirement, ...],
    model_version: str,
    feature_count: int,
    registry_fingerprint: str,
) -> DeployFeatureContract:
    """Build a versioned deploy supplier contract from plan + requirements."""

    assert_scorer_supplier_plan_or_raise(plan)
    assert_no_legacy_feast_trial_cols(plan)
    assert_all_requirements_have_validators(requirements)
    supplier_plan = _supplier_plan_payload(plan)
    req_payload = [asdict(r) for r in requirements]
    validators = [
        {
            "validator_id": SUPPLIER_REQUIREMENTS[r.supplier_id].validator_id,
            "supplier_id": r.supplier_id,
            "stages": list(SUPPLIER_VALIDATORS[r.supplier_id].stages),
        }
        for r in requirements
    ]
    flags = _contract_flags(plan)
    fingerprint = _stable_json_hash(
        {
            "schema_version": DEPLOY_CONTRACT_SCHEMA_VERSION,
            "feature_count": feature_count,
            "registry_fingerprint": registry_fingerprint,
            "supplier_plan": supplier_plan,
            "requirements": req_payload,
            "flags": flags,
        },
    )
    return DeployFeatureContract(
        schema_version=DEPLOY_CONTRACT_SCHEMA_VERSION,
        generated_at=datetime.now(timezone.utc).isoformat(),
        model_version=model_version,
        feature_count=feature_count,
        registry_fingerprint=registry_fingerprint,
        supplier_plan=supplier_plan,
        requirements=req_payload,
        validators=validators,
        flags=flags,
        contract_fingerprint=fingerprint,
    )


def write_deploy_contract_json(path: Path, contract: DeployFeatureContract) -> None:
    """Atomically write ``deploy_contract.json``."""

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(contract), indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def load_deploy_contract_json(path: Path) -> DeployFeatureContract:
    """Load and validate ``deploy_contract.json``."""

    if not path.is_file():
        raise FileNotFoundError(f"[feature-contract] deploy contract missing: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"[feature-contract] deploy contract root must be object: {path}")
    return DeployFeatureContract(**raw)


def assert_contract_matches_recomputed(
    contract: DeployFeatureContract,
    *,
    plan: ScorerSupplierPlan,
    requirements: tuple[SupplierRequirement, ...],
    registry_fingerprint: str,
    feature_count: int,
) -> None:
    """Fail when on-disk contract drifts from live registry + plan builder."""

    if contract.feature_count != feature_count:
        raise ValueError(
            "[feature-contract] deploy contract feature_count mismatch: "
            f"on_disk={contract.feature_count} live={feature_count}"
        )
    if contract.registry_fingerprint != registry_fingerprint:
        raise ValueError(
            "[feature-contract] deploy contract registry_fingerprint mismatch"
        )
    expected = build_deploy_contract(
        plan=plan,
        requirements=requirements,
        model_version=contract.model_version,
        feature_count=feature_count,
        registry_fingerprint=registry_fingerprint,
    )
    if contract.contract_fingerprint != expected.contract_fingerprint:
        raise ValueError(
            "[feature-contract] deploy contract fingerprint mismatch: "
            f"on_disk={contract.contract_fingerprint!r} "
            f"recomputed={expected.contract_fingerprint!r}"
        )


def build_and_write_deploy_contract(
    *,
    plan: ScorerSupplierPlan,
    model_bundle_dir: Path,
    model_version: str,
    registry_fingerprint: str,
    feature_count: int,
    bundle_root: Path | None = None,
    mapping: Path | None = None,
    allowlist: Path | None = None,
    strict: bool = FEATURE_CONTRACT_PACKAGE_STRICT,
) -> dict[str, Any]:
    """Package-time helper: validate, build, and write deploy contract."""

    if strict:
        assert_scorer_supplier_plan_or_raise(plan)
        assert_no_legacy_feast_trial_cols(plan)
    requirements = resolve_supplier_requirements(plan, include_bundle_static=True)
    if strict:
        assert_all_requirements_have_validators(requirements)
    contract = build_deploy_contract(
        plan=plan,
        requirements=requirements,
        model_version=model_version,
        feature_count=feature_count,
        registry_fingerprint=registry_fingerprint,
    )
    out_path = Path(model_bundle_dir) / DEPLOY_CONTRACT_FILENAME
    write_deploy_contract_json(out_path, contract)
    validator_detail = run_supplier_validators(
        requirements=requirements,
        stage="package",
        plan=plan,
        bundle_root=bundle_root,
        mapping=mapping,
        allowlist=allowlist,
    )
    detail = {
        "deploy_contract_path": str(out_path.resolve()),
        "contract_fingerprint": contract.contract_fingerprint,
        "requirement_count": len(requirements),
        "flags": contract.flags,
        "package_validators": validator_detail,
    }
    logger.info("[feature-contract] wrote deploy contract %s", detail)
    return detail


def registry_fingerprint_from_model_bundle(model_bundle: Path) -> str:
    """Resolve frozen registry fingerprint from bundle metrics or snapshot bytes."""

    metrics_path = Path(model_bundle) / "training_metrics.json"
    if metrics_path.is_file():
        raw = json.loads(metrics_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            sha = raw.get("feature_candidate_registry_sha256")
            if isinstance(sha, str) and sha.strip():
                return sha.strip()
    from trainer_hightier.config import FEATURE_CANDIDATE_REGISTRY_SNAPSHOT_FILENAME

    snap = Path(model_bundle) / FEATURE_CANDIDATE_REGISTRY_SNAPSHOT_FILENAME
    if snap.is_file():
        return hashlib.sha256(snap.read_bytes()).hexdigest()
    return ""


def _validate_bundle_static_artifacts(
    *,
    bundle_root: Path,
    plan: ScorerSupplierPlan,
    cfg: Any | None = None,
    mapping: Path | None = None,
    allowlist: Path | None = None,
) -> dict[str, Any]:
    """Verify bundle-local mapping and ADT allowlist artifacts."""

    _ = (plan, cfg)
    mapping_path = mapping or (Path(bundle_root) / BUNDLE_STATIC_MAPPING_REL)
    allowlist_path = allowlist or (Path(bundle_root) / BUNDLE_STATIC_ALLOWLIST_REL)
    for role, path, cols in (
        ("canonical_mapping", mapping_path, ("player_id", "canonical_id")),
        ("adt_allowlist", allowlist_path, ("player_id",)),
    ):
        if not path.is_file():
            raise FileNotFoundError(
                f"[feature-contract] {role} missing for bundle_static_artifact: {path}"
            )
        import pyarrow.parquet as pq

        names = {str(c).lower() for c in pq.read_schema(path).names}
        miss = [c for c in cols if c.lower() not in names]
        if miss:
            raise ValueError(
                f"[feature-contract] {role} parquet missing columns {miss} at {path}"
            )
    return {
        "mapping": str(mapping_path.resolve()),
        "allowlist": str(allowlist_path.resolve()),
    }


def _validate_mid_composite(
    *,
    plan: ScorerSupplierPlan,
    cfg: Any | None = None,
    bundle_root: Path | None = None,
    mapping: Path | None = None,
    allowlist: Path | None = None,
) -> dict[str, Any]:
    """Verify composite implementations exist for the active model."""

    _ = (cfg, bundle_root, mapping, allowlist)
    assert_composite_implementations_or_raise(plan)
    return {"mid_composite_cols": list(plan.mid_composite_cols)}


def _validate_ch_txn_supplier(
    *,
    plan: ScorerSupplierPlan,
    cfg: Any | None = None,
    bundle_root: Path | None = None,
    mapping: Path | None = None,
    allowlist: Path | None = None,
) -> dict[str, Any]:
    """Verify ClickHouse ``t_casino_txn`` readiness when model uses txn features."""

    _ = (bundle_root, mapping, allowlist)
    if not plan.txn_cols:
        return {"skipped": True}
    from trainer_hightier.serving.feature_supply import assert_deploy_external_data_roots_or_raise

    return assert_deploy_external_data_roots_or_raise(plan, cfg=cfg)


def _validate_clickhouse_bet_source(
    *,
    plan: ScorerSupplierPlan,
    cfg: Any | None = None,
    bundle_root: Path | None = None,
    mapping: Path | None = None,
    allowlist: Path | None = None,
) -> dict[str, Any]:
    """Verify ClickHouse bet source is configured when baseline/short suppliers are active."""

    _ = (bundle_root, mapping, allowlist)
    if not (plan.baseline_cols or plan.short_term_cols):
        return {"skipped": True}
    from trainer_hightier.config import default_hightier_serving_config

    serving = cfg or default_hightier_serving_config()
    if not str(serving.source_db or "").strip():
        raise ValueError("[feature-contract] source_db missing for ClickHouse bet suppliers")
    return {"source_db": str(serving.source_db)}


def _validate_feast_online_mid(
    *,
    plan: ScorerSupplierPlan,
    cfg: Any | None = None,
    mapping: Path | None = None,
    allowlist: Path | None = None,
    bundle_root: Path | None = None,
) -> dict[str, Any]:
    """Mid Feast readiness is validated by deploy smoke; contract records requirement only."""

    _ = (plan, cfg, mapping, allowlist, bundle_root)
    return {"delegated_to": "deploy_smoke_gate"}


def _validate_feast_online_slow(
    *,
    plan: ScorerSupplierPlan,
    cfg: Any | None = None,
    mapping: Path | None = None,
    allowlist: Path | None = None,
    bundle_root: Path | None = None,
) -> dict[str, Any]:
    """Slow Feast readiness is validated by deploy smoke; contract records requirement only."""

    _ = (plan, cfg, mapping, allowlist, bundle_root)
    return {"delegated_to": "deploy_smoke_gate"}


SUPPLIER_VALIDATORS: dict[str, SupplierValidatorSpec] = {
    "clickhouse_raw": SupplierValidatorSpec(
        validator_id="validate_clickhouse_bet_source",
        stages=("deploy_e2e", "deploy_preflight"),
        validate=_validate_clickhouse_bet_source,
    ),
    "short_term_pit_builder": SupplierValidatorSpec(
        validator_id="validate_clickhouse_bet_source",
        stages=("deploy_e2e", "deploy_preflight"),
        validate=_validate_clickhouse_bet_source,
    ),
    "txn_lite_builder": SupplierValidatorSpec(
        validator_id="validate_ch_txn_supplier",
        stages=("deploy_e2e", "deploy_preflight"),
        validate=_validate_ch_txn_supplier,
    ),
    "feast_online_mid": SupplierValidatorSpec(
        validator_id="validate_feast_online_mid",
        stages=("deploy_e2e", "deploy_preflight"),
        validate=_validate_feast_online_mid,
    ),
    "feast_online_slow": SupplierValidatorSpec(
        validator_id="validate_feast_online_slow",
        stages=("deploy_e2e", "deploy_preflight"),
        validate=_validate_feast_online_slow,
    ),
    "composite": SupplierValidatorSpec(
        validator_id="validate_mid_composite",
        stages=("package", "deploy_e2e", "deploy_preflight"),
        validate=_validate_mid_composite,
    ),
    BUNDLE_STATIC_SUPPLIER_ID: SupplierValidatorSpec(
        validator_id="validate_bundle_static_artifacts",
        stages=("package", "deploy_e2e", "deploy_preflight"),
        validate=_validate_bundle_static_artifacts,
    ),
}


def run_supplier_validators(
    *,
    requirements: tuple[SupplierRequirement, ...],
    stage: SupplierStage,
    plan: ScorerSupplierPlan,
    cfg: Any | None = None,
    bundle_root: Path | None = None,
    mapping: Path | None = None,
    allowlist: Path | None = None,
) -> dict[str, Any]:
    """Run registered validators for one gate stage."""

    out: dict[str, Any] = {}
    for req in requirements:
        spec = SUPPLIER_VALIDATORS.get(req.supplier_id)
        if spec is None or stage not in spec.stages:
            continue
        out[req.supplier_id] = spec.validate(
            plan=plan,
            cfg=cfg,
            bundle_root=bundle_root,
            mapping=mapping,
            allowlist=allowlist,
        )
    return out


def run_supplier_contract_gate(
    *,
    bundle_root: Path,
    model_bundle: Path,
    plan: ScorerSupplierPlan,
    registry_fingerprint: str,
    feature_count: int,
    cfg: Any | None = None,
    mapping: Path | None = None,
    allowlist: Path | None = None,
    stage: SupplierStage,
    strict: bool | None = None,
) -> dict[str, Any]:
    """Cross-check on-disk contract and run stage validators."""

    from trainer_hightier.config import FEATURE_CONTRACT_DEPLOY_STRICT

    effective_strict = FEATURE_CONTRACT_DEPLOY_STRICT if strict is None else strict
    contract_path = Path(model_bundle) / DEPLOY_CONTRACT_FILENAME
    requirements = resolve_supplier_requirements(plan, include_bundle_static=True)
    detail: dict[str, Any] = {
        "stage": stage,
        "strict": effective_strict,
        "contract_path": str(contract_path),
    }
    if contract_path.is_file():
        contract = load_deploy_contract_json(contract_path)
        detail["on_disk_fingerprint"] = contract.contract_fingerprint
        try:
            assert_contract_matches_recomputed(
                contract,
                plan=plan,
                requirements=requirements,
                registry_fingerprint=registry_fingerprint,
                feature_count=feature_count,
            )
            detail["cross_check"] = "pass"
        except ValueError as exc:
            detail["cross_check"] = "fail"
            detail["cross_check_error"] = str(exc)
            if effective_strict:
                raise
            logger.warning("[feature-contract] contract drift (report-only): %s", exc)
    else:
        detail["cross_check"] = "missing_contract"
        if effective_strict:
            raise FileNotFoundError(
                f"[feature-contract] strict {stage} requires {contract_path}"
            )
        logger.warning("[feature-contract] deploy contract missing at %s", contract_path)
    detail["validators"] = run_supplier_validators(
        requirements=requirements,
        stage=stage,
        plan=plan,
        cfg=cfg,
        bundle_root=bundle_root,
        mapping=mapping,
        allowlist=allowlist,
    )
    return detail
