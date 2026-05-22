"""Production-like deploy E2E gate (local_cleaned source).

Orchestrates bundle startup refresh, deploy readiness/smoke, and minimal scorer replay
without duplicating ``deploy.main`` or ``feast_online_refresh`` business logic.

Example (default: recreate ``bundle/.venv``, pip install ``requirements.txt``, reset Feast runtime)::

    python -m trainer_hightier.serving.deploy_e2e_gate \\
        --bundle-dir out/deploy_hightier/20260522-124003-245bd1f \\
        --local-cleaned-bet trainer_hightier/artifacts/cleaned/cleaned__gmwds_t_bet \\
        --local-cleaned-session trainer_hightier/artifacts/cleaned/cleaned__gmwds_t_session.parquet \\
        --output-json artifacts/feast/deploy_e2e_gate_report.json

Use ``--no-provision-venv`` only when already running inside the bundle venv (faster dev loop).
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
import traceback
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import trainer_hightier
from trainer_hightier.config import (
    HK_TZ,
    HightierServingConfig,
    apply_hightier_serving_environ_overrides,
    set_hightier_serving_deploy_override,
)
from trainer_hightier.deploy import main as deploy_main
from trainer_hightier.serving.feature_supply import (
    ScorerSupplierPlan,
    assert_scorer_supplier_plan_or_raise,
    build_scorer_supplier_plan,
    load_frozen_registry_for_bundle,
    model_feature_columns_from_pickle,
    scorer_supplier_route_counts,
)

logger = logging.getLogger(__name__)

REPORT_SCHEMA_VERSION: str = "deploy_e2e_gate_v1"
SPLIT_REPORT_FILENAME: str = "split_report.json"


@dataclass(frozen=True)
class DeployE2EGateOptions:
    """CLI options for one deploy E2E gate run."""

    bundle_dir: Path
    local_cleaned_bet: Path
    local_cleaned_session: Path
    output_json: Path | None
    gaming_day_start: date | None
    gaming_day_end: date | None
    gaming_day_source: str | None
    max_bets: int
    force_feast_refresh: bool
    reuse_readiness: bool
    strict_smoke: bool
    warn_only: bool
    provision_venv: bool
    recreate_venv: bool
    reset_feast_runtime: bool
    skip_venv_provision: bool


@dataclass
class GateStepResult:
    """One gate step outcome."""

    name: str
    ok: bool
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class DeployE2EGateReport:
    """JSON-serializable deploy E2E gate report."""

    schema_version: str
    generated_at: str
    verdict: Literal["pass", "fail", "warn"]
    bundle_dir: str
    runtime: dict[str, Any]
    supplier_routes: dict[str, int]
    steps: list[GateStepResult]
    artifact_paths: dict[str, str]
    failure_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-friendly dict."""
        payload = asdict(self)
        payload["steps"] = [asdict(s) for s in self.steps]
        return payload


def _parse_gaming_day(value: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"expected gaming day YYYY-MM-DD, got {value!r}") from exc


def _test_split_gaming_day_range_from_report(report: dict[str, Any]) -> tuple[date, date] | None:
    """Return test-split ``min_gaming_day`` / ``max_gaming_day`` from Step 4 ``split_report.json`` body."""
    splits = report.get("splits")
    if not isinstance(splits, list):
        return None
    for row in splits:
        if not isinstance(row, dict) or str(row.get("split") or "").strip().lower() != "test":
            continue
        raw_min = row.get("min_gaming_day")
        raw_max = row.get("max_gaming_day")
        if raw_min is None or raw_max is None:
            return None
        try:
            return _parse_gaming_day(str(raw_min)), _parse_gaming_day(str(raw_max))
        except ValueError:
            return None
    return None


def resolve_model_bundle_test_gaming_days(model_bundle: Path) -> tuple[date, date]:
    """Load test-split gaming-day window from ``split_report.json`` in the model bundle."""
    report_path = Path(model_bundle).resolve() / SPLIT_REPORT_FILENAME
    if not report_path.is_file():
        raise FileNotFoundError(
            f"{SPLIT_REPORT_FILENAME} missing under model bundle {model_bundle}; "
            "re-run Step 4/5 training or pass --gaming-day-start/--gaming-day-end",
        )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise ValueError(f"{report_path} must be a JSON object")
    rng = _test_split_gaming_day_range_from_report(report)
    if rng is None:
        raise ValueError(
            f"{report_path} has no test split with min_gaming_day/max_gaming_day; "
            "pass --gaming-day-start/--gaming-day-end explicitly",
        )
    return rng


def apply_default_scorability_gaming_days(
    opts: DeployE2EGateOptions,
    model_bundle: Path,
) -> DeployE2EGateOptions:
    """Fill scorer replay window from model test split when CLI omits gaming-day flags."""
    if opts.gaming_day_start is not None and opts.gaming_day_end is not None:
        return opts
    start, end = resolve_model_bundle_test_gaming_days(model_bundle)
    logger.info(
        "[deploy_e2e_gate] scorability gaming days from %s test split: %s .. %s",
        SPLIT_REPORT_FILENAME,
        start.isoformat(),
        end.isoformat(),
    )
    return replace(
        opts,
        gaming_day_start=start,
        gaming_day_end=end,
        gaming_day_source=f"{SPLIT_REPORT_FILENAME}#test",
    )


def parse_gate_args(argv: list[str] | None = None) -> DeployE2EGateOptions:
    """Parse CLI arguments."""
    pr = argparse.ArgumentParser(
        description="Production-like deploy E2E gate (local_cleaned Feast refresh + scorer smoke)",
    )
    pr.add_argument("--bundle-dir", type=Path, required=True, help="deploy bundle root")
    pr.add_argument(
        "--local-cleaned-bet",
        type=Path,
        required=True,
        help="hive-partitioned cleaned bet root (required for mid layer)",
    )
    pr.add_argument(
        "--local-cleaned-session",
        type=Path,
        required=True,
        help="cleaned session parquet (required for slow layer)",
    )
    pr.add_argument("--output-json", type=Path, default=None, help="write JSON report path")
    pr.add_argument(
        "--gaming-day-start",
        type=str,
        default=None,
        help="YYYY-MM-DD for scorer replay (default: test split min from model bundle split_report.json)",
    )
    pr.add_argument(
        "--gaming-day-end",
        type=str,
        default=None,
        help="YYYY-MM-DD for scorer replay (default: test split max from model bundle split_report.json)",
    )
    pr.add_argument("--max-bets", type=int, default=500, help="max bets for scorer replay")
    pr.add_argument(
        "--no-force-feast-refresh",
        action="store_true",
        help="do not force startup Feast refresh when readiness looks fresh",
    )
    pr.add_argument(
        "--reuse-readiness",
        action="store_true",
        help="skip refresh when readiness looks fresh (production-like skip path)",
    )
    pr.add_argument(
        "--no-strict-smoke",
        dest="strict_smoke",
        action="store_false",
        help="do not fail scorer replay on post_join_feature_smoke failures",
    )
    pr.add_argument(
        "--warn-only",
        action="store_true",
        help="emit verdict=warn on failure but exit 0 (CI migration mode)",
    )
    pr.add_argument(
        "--no-provision-venv",
        action="store_true",
        help="skip creating bundle .venv and pip install -r requirements.txt (dev/CI only)",
    )
    pr.add_argument(
        "--reuse-venv",
        action="store_true",
        help="reuse existing bundle .venv if present (default: recreate .venv each run)",
    )
    pr.add_argument(
        "--no-reset-feast-runtime",
        action="store_true",
        help="do not clear feast_repo registry/online_store and feast_online_readiness.json",
    )
    pr.add_argument(
        "--skip-venv-provision",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    pr.set_defaults(strict_smoke=True)
    args = pr.parse_args(argv)
    g_start = _parse_gaming_day(args.gaming_day_start) if args.gaming_day_start else None
    g_end = _parse_gaming_day(args.gaming_day_end) if args.gaming_day_end else None
    if (g_start is None) != (g_end is None):
        pr.error("provide both --gaming-day-start and --gaming-day-end, or neither")
    return DeployE2EGateOptions(
        bundle_dir=Path(args.bundle_dir).resolve(),
        local_cleaned_bet=Path(args.local_cleaned_bet).resolve(),
        local_cleaned_session=Path(args.local_cleaned_session).resolve(),
        output_json=Path(args.output_json).resolve() if args.output_json else None,
        gaming_day_start=g_start,
        gaming_day_end=g_end,
        gaming_day_source="cli" if g_start is not None else None,
        max_bets=int(args.max_bets),
        force_feast_refresh=not bool(args.no_force_feast_refresh),
        reuse_readiness=bool(args.reuse_readiness),
        strict_smoke=bool(args.strict_smoke),
        warn_only=bool(args.warn_only),
        provision_venv=not bool(args.no_provision_venv),
        recreate_venv=not bool(args.reuse_venv),
        reset_feast_runtime=not bool(args.no_reset_feast_runtime),
        skip_venv_provision=bool(args.skip_venv_provision),
    )


def bundle_venv_python(bundle_root: Path) -> Path:
    """Return bundle-local venv interpreter path."""
    root = Path(bundle_root).resolve()
    if sys.platform == "win32":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def bundle_venv_pip(bundle_root: Path) -> Path:
    """Return bundle-local venv pip path."""
    root = Path(bundle_root).resolve()
    if sys.platform == "win32":
        return root / ".venv" / "Scripts" / "pip.exe"
    return root / ".venv" / "bin" / "pip"


def running_in_bundle_venv(bundle_root: Path) -> bool:
    """True when the current process uses the bundle ``.venv`` interpreter."""
    try:
        return Path(sys.executable).resolve() == bundle_venv_python(bundle_root).resolve()
    except OSError:
        return False


def reset_bundle_feast_runtime(bundle_root: Path, rel: dict[str, Any]) -> dict[str, str]:
    """Clear Feast registry/online DB and readiness JSON (cold-bundle simulation)."""
    from trainer_hightier.serving.feast_online_adapter import reset_feast_repo_runtime_state

    feast_repo = (bundle_root / rel.get("feast_repo_dir", "feast_repo")).resolve()
    reset_feast_repo_runtime_state(feast_repo)
    removed: list[str] = []
    readiness_rel = rel.get("feast_readiness_path", "artifacts/feast/feast_online_readiness.json")
    readiness = (bundle_root / readiness_rel).resolve()
    if readiness.is_file():
        readiness.unlink()
        removed.append(str(readiness))
    return {
        "feast_repo": str(feast_repo),
        "removed_readiness": str(readiness) if removed else "",
    }


def provision_bundle_venv(bundle_root: Path, *, recreate: bool) -> GateStepResult:
    """Create ``bundle_root/.venv`` and ``pip install -r requirements.txt`` (production-like)."""
    root = Path(bundle_root).resolve()
    req = root / "requirements.txt"
    if not req.is_file():
        return GateStepResult(
            name="venv_provision",
            ok=False,
            error=f"requirements.txt missing under bundle root: {req}",
        )
    venv_py = bundle_venv_python(root)
    detail: dict[str, Any] = {
        "bundle_dir": str(root),
        "requirements": str(req),
        "recreate": recreate,
        "venv_python": str(venv_py),
    }
    try:
        if recreate and (root / ".venv").exists():
            shutil.rmtree(root / ".venv")
            detail["removed_existing_venv"] = True
        if not venv_py.is_file():
            subprocess.run(
                [sys.executable, "-m", "venv", str(root / ".venv")],
                cwd=str(root),
                check=True,
                capture_output=True,
                text=True,
            )
            detail["venv_created"] = True
        else:
            detail["venv_created"] = False
        pip = bundle_venv_pip(root)
        proc = subprocess.run(
            [str(pip), "install", "-r", "requirements.txt"],
            cwd=str(root),
            check=False,
            capture_output=True,
            text=True,
        )
        detail["pip_exit_code"] = proc.returncode
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "")[-2000:]
            return GateStepResult(
                name="venv_provision",
                ok=False,
                detail=detail,
                error=f"pip install failed (exit {proc.returncode}): {tail}",
            )
        verify = subprocess.run(
            [
                str(venv_py),
                "-c",
                "import trainer_hightier; print(trainer_hightier.__file__)",
            ],
            cwd=str(root),
            check=False,
            capture_output=True,
            text=True,
        )
        detail["verify_exit_code"] = verify.returncode
        if verify.returncode != 0:
            tail = (verify.stderr or verify.stdout or "")[-1000:]
            return GateStepResult(
                name="venv_provision",
                ok=False,
                detail=detail,
                error=f"venv import trainer_hightier failed: {tail}",
            )
        detail["trainer_hightier_from_venv"] = (verify.stdout or "").strip()
        detail["host_python"] = str(Path(sys.executable).resolve())
        return GateStepResult(name="venv_provision", ok=True, detail=detail)
    except subprocess.CalledProcessError as exc:
        detail["traceback"] = traceback.format_exc()
        return GateStepResult(
            name="venv_provision",
            ok=False,
            detail=detail,
            error=f"venv provision subprocess failed: {exc}",
        )
    except OSError as exc:
        return GateStepResult(name="venv_provision", ok=False, detail=detail, error=str(exc))


def _argv_for_venv_reexec(argv: list[str] | None) -> list[str]:
    """Copy argv and ensure re-invoked child skips venv provisioning."""
    out = list(argv or sys.argv[1:])
    if "--skip-venv-provision" not in out:
        out.append("--skip-venv-provision")
    return out


def _argv_with_absolute_bundle_dir(argv: list[str] | None, bundle_dir: Path) -> list[str]:
    """Normalize ``--bundle-dir`` to an absolute path for venv re-exec (cwd becomes bundle root)."""
    out = list(argv or sys.argv[1:])
    bundle_abs = str(Path(bundle_dir).resolve())
    for i, tok in enumerate(out):
        if tok == "--bundle-dir" and i + 1 < len(out):
            out[i + 1] = bundle_abs
            return out
        if tok.startswith("--bundle-dir="):
            out[i] = f"--bundle-dir={bundle_abs}"
            return out
    return out


def validate_bundle_contract(bundle_root: Path) -> dict[str, Any]:
    """Fail fast when deploy bundle layout is incomplete."""
    rel = deploy_main._load_rel_paths(bundle_root)
    deploy_main._preflight_frozen_artifacts(bundle_root, rel)
    model_bundle = bundle_root / str(rel.get("model_bundle_dir", "models"))
    mapping = bundle_root / rel["canonical_mapping_parquet"]
    allowlist = deploy_main._bundle_allowlist_path(bundle_root, rel)
    feast_repo = bundle_root / rel.get("feast_repo_dir", "feast_repo")
    return {
        "rel": rel,
        "model_bundle": model_bundle,
        "mapping": mapping,
        "allowlist": allowlist,
        "feast_repo": feast_repo,
    }


def collect_runtime_info(*, bundle_dir: Path, cfg: HightierServingConfig) -> dict[str, Any]:
    """Record interpreter and package load paths for environment audit."""
    from trainer_hightier.serving.feast_online_adapter import feast_registry_missing

    feast_repo = Path(cfg.scorer_feast_repo_path or bundle_dir / "feast_repo").resolve()
    venv_py = bundle_venv_python(bundle_dir)
    return {
        "python_executable": sys.executable,
        "bundle_venv_python": str(venv_py),
        "running_in_bundle_venv": running_in_bundle_venv(bundle_dir),
        "trainer_hightier_file": str(Path(trainer_hightier.__file__).resolve()),
        "trainer_hightier_version": getattr(trainer_hightier, "__version__", None),
        "bundle_dir": str(bundle_dir.resolve()),
        "feast_repo": str(feast_repo),
        "registry_missing_before": feast_registry_missing(feast_repo),
    }


def resolve_supplier_plan(model_bundle: Path) -> ScorerSupplierPlan:
    """Load frozen registry and build scorer supplier plan."""
    snap = load_frozen_registry_for_bundle(model_bundle)
    model_feats = model_feature_columns_from_pickle(model_bundle)
    plan = build_scorer_supplier_plan(snap, model_feats)
    assert_scorer_supplier_plan_or_raise(plan)
    return plan


def _resolve_refresh_layers(plan: ScorerSupplierPlan) -> tuple[str, ...]:
    layers: list[str] = []
    if plan.feast_mid_cols or plan.mid_composite_cols:
        layers.append("mid")
    if plan.feast_slow_cols:
        layers.append("slow")
    if not layers:
        layers = ["mid", "slow"]
    return tuple(layers)


def run_startup_refresh_gate(
    *,
    bundle_root: Path,
    rel: dict[str, Any],
    cfg: HightierServingConfig,
    plan: ScorerSupplierPlan,
    mapping: Path,
    allowlist: Path,
    local_cleaned_bet: Path,
    local_cleaned_session: Path,
    force_refresh: bool,
) -> GateStepResult:
    """Run production-like startup Feast refresh with ``local_cleaned`` source."""
    from trainer_hightier.serving import feast_online_refresh as refresh_mod
    from trainer_hightier.serving.feast_online_adapter import feast_registry_missing

    feast_repo = Path(cfg.scorer_feast_repo_path or (bundle_root / "feast_repo")).resolve()
    require_mid = bool(plan.feast_mid_cols or plan.mid_composite_cols)
    require_slow = bool(plan.feast_slow_cols)
    registry_was_missing = feast_registry_missing(feast_repo)
    need_refresh, reason = deploy_main._needs_feast_startup_refresh(
        cfg,
        force=force_refresh,
        require_mid=require_mid,
        require_slow=require_slow,
    )
    detail: dict[str, Any] = {
        "need_refresh": need_refresh,
        "refresh_reason": reason,
        "registry_missing_before": registry_was_missing,
        "layers": list(_resolve_refresh_layers(plan)),
        "source": "local_cleaned",
    }
    if not need_refresh:
        detail["skipped"] = True
        return GateStepResult(name="startup_refresh", ok=True, detail=detail)

    if "mid" in detail["layers"] and not local_cleaned_bet.exists():
        return GateStepResult(
            name="startup_refresh",
            ok=False,
            detail=detail,
            error=f"local cleaned bet missing: {local_cleaned_bet}",
        )
    if "slow" in detail["layers"] and not local_cleaned_session.is_file():
        return GateStepResult(
            name="startup_refresh",
            ok=False,
            detail=detail,
            error=f"local cleaned session missing: {local_cleaned_session}",
        )

    bootstrap_mid = deploy_main._mid_feast_needs_bootstrap(
        cfg,
        require_mid=require_mid,
        allowlist=allowlist,
        mapping=mapping,
        force=force_refresh,
    )
    registry_missing = feast_registry_missing(feast_repo)
    apply_before = registry_missing or bootstrap_mid or force_refresh
    detail["bootstrap_mid"] = bootstrap_mid
    detail["apply_expected_before_materialize"] = apply_before

    fd = deploy_main._try_acquire_feast_refresh_lock(cfg)
    if fd is None:
        return GateStepResult(
            name="startup_refresh",
            ok=False,
            detail=detail,
            error="Feast refresh lock held by another process",
        )
    try:
        opts = refresh_mod._resolve_refresh_options(
            layers=",".join(detail["layers"]),
            source="local_cleaned",
            skip_apply=(not bootstrap_mid) and not registry_missing,
            skip_materialize=False,
            smoke_only=False,
            dry_run=False,
            feast_repo=feast_repo,
            readiness_path=cfg.scorer_feast_readiness_path,
            canonical_mapping=mapping,
            adt_allowlist=allowlist,
            local_cleaned_bet=local_cleaned_bet if "mid" in detail["layers"] else None,
            local_cleaned_session=local_cleaned_session if "slow" in detail["layers"] else None,
            max_smoke_entities=int(cfg.scorer_feast_deploy_lookup_smoke_sample_size),
            summary_path=(Path(cfg.scorer_feast_readiness_path).parent / "feast_online_refresh_report.json"),
            bootstrap_mid=bootstrap_mid,
            apply_schema=bootstrap_mid or registry_missing,
        )
        summary = refresh_mod.run_feast_online_refresh(opts)
        detail["refresh_summary"] = summary
        detail["registry_missing_after"] = feast_registry_missing(feast_repo)
        if summary.get("verdict") != "ok":
            return GateStepResult(
                name="startup_refresh",
                ok=False,
                detail=detail,
                error=f"feast_online_refresh verdict={summary.get('verdict')!r}",
            )
        if registry_was_missing and detail["registry_missing_after"]:
            return GateStepResult(
                name="startup_refresh",
                ok=False,
                detail=detail,
                error="registry still missing after refresh (feast apply likely skipped)",
            )
        return GateStepResult(name="startup_refresh", ok=True, detail=detail)
    except Exception as exc:
        detail["traceback"] = traceback.format_exc()
        return GateStepResult(name="startup_refresh", ok=False, detail=detail, error=str(exc))
    finally:
        deploy_main._release_feast_refresh_lock(cfg, fd)


def run_deploy_smoke_gate(
    *,
    cfg: HightierServingConfig,
    mapping: Path,
    allowlist: Path,
    plan: ScorerSupplierPlan,
) -> GateStepResult:
    """Run deploy Feast readiness + allowlist lookup smoke."""
    from trainer_hightier.serving.feast_readiness import run_deploy_feast_readiness_check

    try:
        gate = run_deploy_feast_readiness_check(
            require_mid=bool(plan.feast_mid_cols or plan.mid_composite_cols),
            require_slow=bool(plan.feast_slow_cols),
            allowlist_parquet=allowlist,
            canonical_mapping_parquet=mapping,
            mid_columns=plan.feast_mid_cols,
            slow_columns=plan.feast_slow_cols,
            run_lookup_smoke=True,
        )
        detail = gate.to_log_dict()
        if not gate.ok:
            return GateStepResult(
                name="deploy_smoke",
                ok=False,
                detail=detail,
                error=gate.hard_failure_reason or "deploy readiness smoke failed",
            )
        return GateStepResult(name="deploy_smoke", ok=True, detail=detail)
    except Exception as exc:
        return GateStepResult(
            name="deploy_smoke",
            ok=False,
            detail={"traceback": traceback.format_exc()},
            error=str(exc),
        )


def run_scorability_gate(
    *,
    opts: DeployE2EGateOptions,
    plan: ScorerSupplierPlan,
) -> GateStepResult:
    """Minimal production scorer replay after startup succeeds."""
    from trainer_hightier.serving.offline_serving_backtest import run_offline_serving_backtest

    if opts.gaming_day_start is None or opts.gaming_day_end is None:
        return GateStepResult(
            name="scorability",
            ok=False,
            detail={"skipped": True, "reason": "gaming-day window unresolved"},
            error="gaming_day_start/gaming_day_end missing after defaults",
        )
    try:
        summary = run_offline_serving_backtest(
            bundle_dir=opts.bundle_dir,
            local_cleaned_bet=opts.local_cleaned_bet,
            gaming_day_start=opts.gaming_day_start,
            gaming_day_end=opts.gaming_day_end,
            max_bets=opts.max_bets,
            strict_smoke=opts.strict_smoke,
        )
        smoke_failures = summary.get("post_join_smoke_failures") or []
        n_scored = int(summary.get("n_scored") or 0)
        ok = n_scored > 0 and not smoke_failures
        detail = {
            "gaming_day_start": opts.gaming_day_start.isoformat(),
            "gaming_day_end": opts.gaming_day_end.isoformat(),
            "gaming_day_source": opts.gaming_day_source,
            "n_bets": summary.get("n_bets"),
            "n_scored": n_scored,
            "n_skipped_entity_missing": summary.get("n_skipped_entity_missing"),
            "post_join_smoke_failures": smoke_failures,
            "feast_diagnostics": summary.get("feast_diagnostics"),
            "readiness_gate": summary.get("readiness_gate"),
        }
        if not ok:
            err = "no rows scored" if n_scored <= 0 else f"post_join_smoke failed: {smoke_failures[:3]}"
            return GateStepResult(name="scorability", ok=False, detail=detail, error=err)
        return GateStepResult(name="scorability", ok=True, detail=detail)
    except Exception as exc:
        return GateStepResult(
            name="scorability",
            ok=False,
            detail={"traceback": traceback.format_exc()},
            error=str(exc),
        )


def write_gate_report(path: Path, report: DeployE2EGateReport) -> None:
    """Write JSON report atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True, default=str), encoding="utf-8")
    tmp.replace(path)


def run_deploy_e2e_gate(opts: DeployE2EGateOptions) -> DeployE2EGateReport:
    """Execute full deploy E2E gate and return structured report."""
    bundle_root = opts.bundle_dir
    deploy_main._load_dotenv_if_present(bundle_root)
    contract = validate_bundle_contract(bundle_root)
    rel = contract["rel"]
    cfg = apply_hightier_serving_environ_overrides(
        deploy_main._serving_config_for_bundle(bundle_root, rel),
    )
    set_hightier_serving_deploy_override(cfg)
    import trainer_hightier.serving.runtime_config  # noqa: F401

    model_bundle = contract["model_bundle"]
    opts = apply_default_scorability_gaming_days(opts, model_bundle)
    mapping = contract["mapping"]
    allowlist = contract["allowlist"]
    plan = resolve_supplier_plan(model_bundle)
    runtime = collect_runtime_info(bundle_dir=bundle_root, cfg=cfg)

    steps: list[GateStepResult] = []
    if opts.reset_feast_runtime:
        steps.append(
            GateStepResult(
                name="feast_runtime_reset",
                ok=True,
                detail=reset_bundle_feast_runtime(bundle_root, rel),
            ),
        )
    steps.append(
        GateStepResult(
            name="bundle_contract",
            ok=True,
            detail={
                "model_bundle": str(model_bundle),
                "mapping": str(mapping),
                "allowlist": str(allowlist),
            },
        ),
    )
    steps.append(
        run_startup_refresh_gate(
            bundle_root=bundle_root,
            rel=rel,
            cfg=cfg,
            plan=plan,
            mapping=mapping,
            allowlist=allowlist,
            local_cleaned_bet=opts.local_cleaned_bet,
            local_cleaned_session=opts.local_cleaned_session,
            force_refresh=opts.force_feast_refresh and not opts.reuse_readiness,
        ),
    )
    if steps[-1].ok:
        steps.append(
            run_deploy_smoke_gate(cfg=cfg, mapping=mapping, allowlist=allowlist, plan=plan),
        )
    if all(s.ok for s in steps):
        steps.append(run_scorability_gate(opts=opts, plan=plan))

    hard_fail = next((s for s in steps if not s.ok), None)
    if hard_fail is None:
        verdict: Literal["pass", "fail", "warn"] = "pass"
        failure_reason = None
    elif opts.warn_only:
        verdict = "warn"
        failure_reason = hard_fail.error
    else:
        verdict = "fail"
        failure_reason = hard_fail.error

    artifact_paths = {
        "feast_repo": str(Path(cfg.scorer_feast_repo_path or bundle_root / "feast_repo").resolve()),
        "feast_readiness": str(Path(cfg.scorer_feast_readiness_path).resolve()),
        "feature_state_db": str(Path(cfg.feature_state_db_path).resolve()),
        "refresh_report": str(
            Path(cfg.scorer_feast_readiness_path).parent / "feast_online_refresh_report.json",
        ),
    }
    report = DeployE2EGateReport(
        schema_version=REPORT_SCHEMA_VERSION,
        generated_at=datetime.now(ZoneInfo(HK_TZ)).isoformat(),
        verdict=verdict,
        bundle_dir=str(bundle_root),
        runtime=runtime,
        supplier_routes=scorer_supplier_route_counts(plan),
        steps=steps,
        artifact_paths=artifact_paths,
        failure_reason=failure_reason,
    )
    if opts.output_json is not None:
        write_gate_report(opts.output_json, report)
    return report


def _exit_code_from_report(report: DeployE2EGateReport) -> int:
    if report.verdict == "pass":
        return 0
    if report.verdict == "warn":
        return 0
    return 1


def run_cli(argv: list[str] | None = None) -> int:
    """CLI entry: provision bundle venv (default), then run gate and return process exit code."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    opts = parse_gate_args(argv)
    if opts.provision_venv and not opts.skip_venv_provision and not running_in_bundle_venv(opts.bundle_dir):
        logger.info(
            "[deploy_e2e_gate] provisioning bundle venv recreate=%s at %s",
            opts.recreate_venv,
            opts.bundle_dir / ".venv",
        )
        vstep = provision_bundle_venv(opts.bundle_dir, recreate=opts.recreate_venv)
        if not vstep.ok:
            logger.error("[deploy_e2e_gate] venv provision failed: %s", vstep.error)
            return 1
        child = bundle_venv_python(opts.bundle_dir)
        child_argv = [
            "-m",
            "trainer_hightier.serving.deploy_e2e_gate",
            *_argv_for_venv_reexec(_argv_with_absolute_bundle_dir(argv, opts.bundle_dir)),
        ]
        logger.info("[deploy_e2e_gate] re-exec gate with bundle venv: %s", child)
        proc = subprocess.run(
            [str(child), *child_argv],
            cwd=str(opts.bundle_dir),
            check=False,
        )
        return int(proc.returncode)
    report = run_deploy_e2e_gate(opts)
    logger.info("[deploy_e2e_gate] verdict=%s failure=%s", report.verdict, report.failure_reason)
    return _exit_code_from_report(report)


if __name__ == "__main__":
    raise SystemExit(run_cli())
