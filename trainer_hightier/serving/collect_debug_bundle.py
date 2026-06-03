"""Collect production incident debug bundle (Parquet exports, audits, zip, MLflow).

Run from deploy bundle root::

    python collect_diag.py

Or::

    python -m trainer_hightier.serving.collect_debug_bundle --bundle-dir /path/to/bundle
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import sqlite3
import tempfile
import traceback
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from trainer_hightier.config import (
    DIAG_BUNDLE_EXPORTS_SUBDIR,
    DIAG_BUNDLE_RETENTION_COUNT,
    DIAG_BUNDLE_SCHEMA_VERSION,
    DIAG_SUPPLIER_RCA_MAX_BETS,
    HK_TZ,
    MLFLOW_DIAG_ARTIFACT_PREFIX,
    MLFLOW_EXPERIMENT_TRAIN_HIGHTIER,
)
from trainer_hightier.core.mlflow_adapter import (
    is_mlflow_available,
    log_artifact_to_run_safe,
    reset_availability_cache,
    resolve_mlflow_run_id_by_name,
)
from trainer_hightier.deploy.main import _load_rel_paths
from trainer_hightier.serving.audit_production_readiness import run_audit as run_production_readiness_audit
from trainer_hightier.serving.audit_supplier_root_cause import run_supplier_root_cause_audit

logger = logging.getLogger(__name__)

_PREDICTION_LOG_AUDIT_COLUMNS: tuple[str, ...] = (
    "bet_id",
    "score",
    "is_alert",
    "threshold",
    "model_features_missing",
    "missing_family_json",
    "scoring_status",
    "mid_term_freshness_status",
    "slow_freshness_status",
    "snapshot_scoring_degraded",
)

_IDENTITY_FILES: tuple[str, ...] = (
    "bundle_info.json",
    "deploy_bundle_paths.json",
)

_MODEL_IDENTITY_FILES: tuple[str, ...] = (
    "run_summary.json",
    "metrics_detailed.json",
    "model_version",
    "feature_parity_verification.json",
    "training_metrics.json",
    "run_report.json",
)


@dataclass
class StepRecord:
    """One collector step outcome for ``MANIFEST.json``."""

    name: str
    status: str
    detail: str = ""
    error: str | None = None


@dataclass
class CollectContext:
    """Mutable state while building a debug bundle."""

    bundle_root: Path
    rel: dict[str, Any]
    staging_dir: Path
    model_version: str
    steps: list[StepRecord] = field(default_factory=list)
    files: list[dict[str, Any]] = field(default_factory=list)
    partial: bool = False


def _sha256_file(path: Path) -> str:
    """Return hex sha256 digest for *path*."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _record_file(ctx: CollectContext, path: Path, *, row_count: int | None = None) -> None:
    """Append one exported artifact entry to manifest file list."""
    rel = path.relative_to(ctx.staging_dir).as_posix()
    ctx.files.append(
        {
            "path": rel,
            "sha256": _sha256_file(path),
            "size_bytes": int(path.stat().st_size),
            "row_count": row_count,
        }
    )


def _append_step(ctx: CollectContext, step: StepRecord) -> None:
    """Record step and mark bundle partial on error."""
    ctx.steps.append(step)
    if step.status == "error":
        ctx.partial = True


def _sqlite_tables(conn: sqlite3.Connection) -> list[str]:
    """List user tables in an SQLite database."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return [str(r[0]) for r in rows]


def _sqlite_connect_ro(db_path: Path) -> sqlite3.Connection:
    """Open SQLite database read-only (WAL-safe while scorer writes)."""
    uri = f"file:{db_path.resolve()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _load_model_version(bundle_root: Path, rel: dict[str, Any]) -> str:
    """Read ``models/model_version`` text from bundle."""
    model_dir = bundle_root / str(rel.get("model_bundle_dir", "models"))
    ver_path = model_dir / "model_version"
    if ver_path.is_file():
        text = ver_path.read_text(encoding="utf-8").strip()
        if text:
            return text
    return model_dir.name


def _load_mlflow_env(bundle_root: Path) -> None:
    """Load optional bundle-local MLflow env before upload."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    for candidate in (
        bundle_root / "local_state" / "mlflow.env",
        bundle_root / "credential" / "mlflow.env",
    ):
        if candidate.is_file():
            load_dotenv(candidate, override=False)
            reset_availability_cache()
            return


def _copy_if_exists(src: Path, dst: Path) -> bool:
    """Copy *src* to *dst* when source exists."""
    if not src.is_file():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def export_sqlite_db(
    ctx: CollectContext,
    db_path: Path,
    *,
    db_label: str,
) -> None:
    """Export all tables from one SQLite DB to Parquet under ``exports/``."""
    step_name = f"export_{db_label}"
    if not db_path.is_file():
        _append_step(
            ctx,
            StepRecord(step_name, "skipped", detail=f"missing {db_path}"),
        )
        return
    out_dir = ctx.staging_dir / "exports" / db_label
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        with _sqlite_connect_ro(db_path) as conn:
            tables = _sqlite_tables(conn)
            if not tables:
                _append_step(ctx, StepRecord(step_name, "skipped", detail="no tables"))
                return
            for table in tables:
                df = pd.read_sql_query(f'SELECT * FROM "{table}"', conn)
                out_path = out_dir / f"{table}.parquet"
                df.to_parquet(out_path, index=False)
                _record_file(ctx, out_path, row_count=int(len(df)))
        _append_step(
            ctx,
            StepRecord(step_name, "ok", detail=f"tables={len(tables)} path={db_path}"),
        )
    except OSError as exc:
        _append_step(
            ctx,
            StepRecord(step_name, "error", error=f"{type(exc).__name__}: {exc}"),
        )


def write_prediction_log_audit_csv(ctx: CollectContext, db_path: Path) -> Path | None:
    """Write audit CSV subset for production readiness audit."""
    step_name = "export_prediction_log_audit_csv"
    if not db_path.is_file():
        _append_step(ctx, StepRecord(step_name, "skipped", detail="prediction_log.db missing"))
        return None
    out_csv = ctx.staging_dir / "exports" / "prediction_log_audit.csv"
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    try:
        with _sqlite_connect_ro(db_path) as conn:
            have = {str(r[1]) for r in conn.execute("PRAGMA table_info(prediction_log)").fetchall()}
            cols = [c for c in _PREDICTION_LOG_AUDIT_COLUMNS if c in have]
            if not cols:
                _append_step(ctx, StepRecord(step_name, "error", error="no audit columns on prediction_log"))
                return None
            col_sql = ", ".join(f'"{c}"' for c in cols)
            df = pd.read_sql_query(f"SELECT {col_sql} FROM prediction_log", conn)
        df.to_csv(out_csv, index=False)
        _record_file(ctx, out_csv, row_count=int(len(df)))
        _append_step(ctx, StepRecord(step_name, "ok", detail=f"rows={len(df)}"))
        return out_csv
    except OSError as exc:
        _append_step(
            ctx,
            StepRecord(step_name, "error", error=f"{type(exc).__name__}: {exc}"),
        )
        return None


def copy_identity_and_runtime(ctx: CollectContext) -> None:
    """Copy bundle identity, runtime JSON, and deploy log into staging."""
    identity_dir = ctx.staging_dir / "identity"
    runtime_dir = ctx.staging_dir / "runtime"
    logs_dir = ctx.staging_dir / "logs"
    identity_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for name in _IDENTITY_FILES:
        if _copy_if_exists(ctx.bundle_root / name, identity_dir / name):
            copied += 1
            _record_file(ctx, identity_dir / name)
    model_dir = ctx.bundle_root / str(ctx.rel.get("model_bundle_dir", "models"))
    for name in _MODEL_IDENTITY_FILES:
        if _copy_if_exists(model_dir / name, identity_dir / name):
            copied += 1
            _record_file(ctx, identity_dir / name)
    feast_rel = str(
        ctx.rel.get("feast_readiness_path", "artifacts/feast/feast_online_readiness.json")
    )
    if _copy_if_exists(ctx.bundle_root / feast_rel, runtime_dir / "feast_online_readiness.json"):
        copied += 1
        _record_file(ctx, runtime_dir / "feast_online_readiness.json")
    snap = ctx.bundle_root / str(ctx.rel.get("snapshot_manifest_dir", "snapshots")) / "active_manifest.json"
    if _copy_if_exists(snap, runtime_dir / "active_manifest.json"):
        copied += 1
        _record_file(ctx, runtime_dir / "active_manifest.json")
    ls = str(ctx.rel.get("local_state_dir", "local_state"))
    deploy_log = ctx.bundle_root / ls / "logs" / "deploy_main.log"
    if _copy_if_exists(deploy_log, logs_dir / "deploy_main.log"):
        copied += 1
        _record_file(ctx, logs_dir / "deploy_main.log")
    _append_step(ctx, StepRecord("copy_identity_runtime", "ok", detail=f"files={copied}"))


def run_production_audit(ctx: CollectContext, audit_csv: Path | None) -> None:
    """Run production readiness audit (fail-open)."""
    step_name = "audit_production_readiness"
    reports_dir = ctx.staging_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    out_json = reports_dir / "production_audit.json"
    if audit_csv is None or not audit_csv.is_file():
        _append_step(ctx, StepRecord(step_name, "skipped", detail="no prediction_log_audit.csv"))
        return
    argv = [
        "--bundle-dir",
        str(ctx.bundle_root),
        "--prediction-log",
        str(audit_csv),
        "--output-json",
        str(out_json),
    ]
    try:
        run_production_readiness_audit(argv)
        if out_json.is_file():
            _record_file(ctx, out_json)
            _append_step(ctx, StepRecord(step_name, "ok", detail=str(out_json)))
        else:
            _append_step(ctx, StepRecord(step_name, "error", error="output json missing"))
    except Exception as exc:
        _append_step(
            ctx,
            StepRecord(step_name, "error", error=f"{type(exc).__name__}: {exc}"),
        )


def copy_flight_recording_pointer(ctx: CollectContext) -> None:
    """Copy flight recorder ``MANIFEST.json`` into staging when present (fail-open)."""
    step_name = "copy_flight_recording_manifest"
    ls = str(ctx.rel.get("local_state_dir", "local_state"))
    src = ctx.bundle_root / ls / "flight_recording" / "MANIFEST.json"
    if not src.is_file():
        _append_step(ctx, StepRecord(step_name, "skipped", detail="no flight_recording MANIFEST"))
        return
    dst = ctx.staging_dir / "flight_recording" / "MANIFEST.json"
    if _copy_if_exists(src, dst):
        _record_file(ctx, dst)
        _append_step(ctx, StepRecord(step_name, "ok", detail=str(src)))
    else:
        _append_step(ctx, StepRecord(step_name, "skipped", detail="copy failed"))


def run_supplier_audit(ctx: CollectContext, audit_csv: Path | None) -> None:
    """Run supplier root-cause audit (fail-open)."""
    step_name = "audit_supplier_root_cause"
    reports_dir = ctx.staging_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    out_json = reports_dir / "supplier_root_cause.json"
    if audit_csv is None or not audit_csv.is_file():
        _append_step(ctx, StepRecord(step_name, "skipped", detail="no prediction_log_audit.csv"))
        return
    try:
        report = run_supplier_root_cause_audit(
            bundle_dir=ctx.bundle_root,
            prediction_log=audit_csv,
            max_bets=DIAG_SUPPLIER_RCA_MAX_BETS,
            only_null_features=False,
        )
        out_json.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        _record_file(ctx, out_json)
        _append_step(ctx, StepRecord(step_name, "ok", detail=str(out_json)))
    except Exception as exc:
        err_payload = {
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(limit=20),
        }
        out_json.write_text(json.dumps(err_payload, indent=2), encoding="utf-8")
        _record_file(ctx, out_json)
        _append_step(
            ctx,
            StepRecord(step_name, "error", error=f"{type(exc).__name__}: {exc}"),
        )


def build_manifest(
    ctx: CollectContext,
    *,
    zip_path: Path,
    mlflow_upload_status: str,
    mlflow_run_id: str | None,
) -> dict[str, Any]:
    """Build manifest dict and write ``MANIFEST.json`` into staging."""
    manifest: dict[str, Any] = {
        "schema_version": DIAG_BUNDLE_SCHEMA_VERSION,
        "collected_at_utc": datetime.now(timezone.utc).isoformat(),
        "collected_at_hk": datetime.now(ZoneInfo(HK_TZ)).isoformat(),
        "bundle_dir": str(ctx.bundle_root),
        "model_version": ctx.model_version,
        "profile": "full",
        "partial": bool(ctx.partial),
        "mlflow_upload_status": mlflow_upload_status,
        "mlflow_run_id": mlflow_run_id,
        "zip_path": str(zip_path),
        "steps": [
            {"name": s.name, "status": s.status, "detail": s.detail, "error": s.error}
            for s in ctx.steps
        ],
        "files": ctx.files,
    }
    manifest_path = ctx.staging_dir / "MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    ctx.files = [f for f in ctx.files if f.get("path") != "MANIFEST.json"]
    ctx.files.append(
        {
            "path": "MANIFEST.json",
            "sha256": _sha256_file(manifest_path),
            "size_bytes": int(manifest_path.stat().st_size),
            "row_count": None,
        }
    )
    return manifest


def zip_staging_dir(staging_dir: Path, zip_path: Path) -> None:
    """Zip entire staging tree to *zip_path*."""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(staging_dir.rglob("*")):
            if path.is_file():
                zf.write(path, arcname=path.relative_to(staging_dir).as_posix())


def prune_old_zips(exports_dir: Path, *, keep: int) -> None:
    """Keep only the newest *keep* ``prod_diag_*.zip`` files."""
    zips = sorted(exports_dir.glob("prod_diag_*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in zips[keep:]:
        old.unlink(missing_ok=True)


def upload_to_mlflow(ctx: CollectContext, zip_path: Path, *, skip: bool) -> tuple[str, str | None]:
    """Try MLflow upload; return status and optional run_id."""
    if skip:
        return "mlflow_upload_skipped", None
    _load_mlflow_env(ctx.bundle_root)
    if not is_mlflow_available():
        return "mlflow_upload_skipped", None
    run_id = resolve_mlflow_run_id_by_name(
        MLFLOW_EXPERIMENT_TRAIN_HIGHTIER,
        ctx.model_version,
    )
    if run_id is None:
        return "mlflow_upload_skipped", None
    artifact_name = zip_path.name
    ok = log_artifact_to_run_safe(
        run_id,
        zip_path,
        artifact_path=f"{MLFLOW_DIAG_ARTIFACT_PREFIX}/{artifact_name}",
    )
    if ok:
        return "uploaded", run_id
    return "mlflow_upload_failed", run_id


def collect_debug_bundle(
    *,
    bundle_dir: Path,
    output_zip: Path | None = None,
    skip_mlflow_upload: bool = False,
) -> Path:
    """Build incident debug zip under bundle ``local_state/diag_exports/``."""
    bundle_root = Path(bundle_dir).expanduser().resolve()
    rel = _load_rel_paths(bundle_root)
    model_version = _load_model_version(bundle_root, rel)
    ts = datetime.now(ZoneInfo(HK_TZ)).strftime("%Y%m%d_%H%M%S")
    ls = str(rel.get("local_state_dir", "local_state"))
    exports_dir = bundle_root / ls / DIAG_BUNDLE_EXPORTS_SUBDIR
    exports_dir.mkdir(parents=True, exist_ok=True)
    if output_zip is None:
        output_zip = exports_dir / f"prod_diag_{model_version}_{ts}.zip"
    else:
        output_zip = Path(output_zip).resolve()

    staging_dir = Path(tempfile.mkdtemp(prefix="prod_diag_staging_", dir=str(exports_dir)))
    ctx = CollectContext(
        bundle_root=bundle_root,
        rel=rel,
        staging_dir=staging_dir,
        model_version=model_version,
    )
    try:
        ls_path = bundle_root / ls
        export_sqlite_db(ctx, ls_path / "prediction_log.db", db_label="prediction_log")
        export_sqlite_db(ctx, ls_path / "state.db", db_label="state")
        export_sqlite_db(ctx, ls_path / "feature_state.db", db_label="feature_state")
        audit_csv = write_prediction_log_audit_csv(ctx, ls_path / "prediction_log.db")
        copy_identity_and_runtime(ctx)
        copy_flight_recording_pointer(ctx)
        run_production_audit(ctx, audit_csv)
        run_supplier_audit(ctx, audit_csv)
        build_manifest(
            ctx,
            zip_path=output_zip,
            mlflow_upload_status="pending",
            mlflow_run_id=None,
        )
        zip_staging_dir(staging_dir, output_zip)
        mlflow_status, mlflow_run_id = upload_to_mlflow(
            ctx,
            output_zip,
            skip=skip_mlflow_upload,
        )
        build_manifest(
            ctx,
            zip_path=output_zip,
            mlflow_upload_status=mlflow_status,
            mlflow_run_id=mlflow_run_id,
        )
        zip_staging_dir(staging_dir, output_zip)
        prune_old_zips(exports_dir, keep=DIAG_BUNDLE_RETENTION_COUNT)
        logger.info("[collect_debug_bundle] wrote %s partial=%s mlflow=%s", output_zip, ctx.partial, mlflow_status)
        return output_zip
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


def run_collect_debug_bundle(argv: list[str] | None = None) -> int:
    """CLI entry for debug bundle collector."""
    pr = argparse.ArgumentParser(description="Collect production incident debug bundle zip")
    pr.add_argument(
        "--bundle-dir",
        type=Path,
        default=Path.cwd(),
        help="deploy bundle root (default: current directory)",
    )
    pr.add_argument(
        "--profile",
        choices=("full",),
        default="full",
        help="collection profile (only full supported)",
    )
    pr.add_argument("--output-zip", type=Path, default=None, help="override output zip path")
    pr.add_argument(
        "--skip-mlflow-upload",
        action="store_true",
        help="skip MLflow artifact upload",
    )
    args = pr.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.profile != "full":
        raise SystemExit("only profile=full is supported")
    zip_path = collect_debug_bundle(
        bundle_dir=Path(args.bundle_dir),
        output_zip=args.output_zip,
        skip_mlflow_upload=bool(args.skip_mlflow_upload),
    )
    print(json.dumps({"zip_path": str(zip_path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_collect_debug_bundle())
