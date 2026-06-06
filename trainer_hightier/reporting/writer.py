"""Central bundle report builders and writer for trainer_hightier training runs."""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from trainer_hightier.core.model_bundle_paths import (
    RUN_REPORT_FILENAME,
    RUN_REPORT_SCHEMA,
    SPLIT_REPORT_FILENAME,
    TRAINING_METRICS_SCHEMA,
)

logger = logging.getLogger(__name__)


def _repo_root() -> Path:
    """Repository root (parent of ``trainer_hightier`` package)."""

    return Path(__file__).resolve().parents[2]


def _epoch_ms_to_iso_z(epoch_ms: int | None) -> str | None:
    """Render epoch milliseconds as UTC ISO-8601 ``…Z`` string."""

    if epoch_ms is None:
        return None
    try:
        ms = int(epoch_ms)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_short_head(repo_root: Path) -> str:
    """Return ``git rev-parse --short HEAD`` or ``nogit``."""

    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(repo_root),
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return "nogit"


def _resolved_patron_sampling_ratio(args: Any) -> tuple[float | None, str]:
    """Return ``(ratio, source)`` for run-summary ``data_scope``."""

    if args.patron_sampling_ratio is not None:
        return float(args.patron_sampling_ratio), "explicit"
    if args.filter_bets_by_adt_quantile:
        q = float(args.objective.theo_train_quantile)
        if 0.0 < q < 1.0:
            return round(1.0 - q, 8), "adt_quantile_derived"
    return None, "unknown"


def _feature_list_sha256_hex(columns: object) -> str | None:
    """Stable SHA256 over sorted feature column names (hex digest)."""

    if not isinstance(columns, list):
        return None
    names = [str(x) for x in columns]
    payload = json.dumps(sorted(names), separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def path_relative_to_repo(path_str: str | None, *, repo_root: Path) -> str | None:
    """Best-effort repo-relative path string for logs."""

    if path_str is None:
        return None
    raw = str(path_str).strip()
    if not raw:
        return None
    try:
        p = Path(raw).resolve()
        rel = p.relative_to(repo_root.resolve())
        return str(rel).replace("\\", "/")
    except ValueError:
        return raw.replace("\\", "/")


def build_training_metrics_document(
    step5_report: dict[str, Any],
    *,
    patches: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble ``training_metrics.json`` body from Step 5 report dict."""

    body = dict(step5_report)
    body["schema"] = TRAINING_METRICS_SCHEMA
    if patches:
        body.update(patches)
    return body


def build_run_summary(metrics: dict[str, Any], args: Any) -> dict[str, Any]:
    """Build cross-run comparison summary (nested under ``run_report.json``)."""

    repo_root = _repo_root()
    run_id = str(metrics.get("model_version") or "").strip() or "unknown_run"
    started = _epoch_ms_to_iso_z(metrics.get("start_epoch_ms")) if isinstance(metrics.get("start_epoch_ms"), int) else None
    finished = _epoch_ms_to_iso_z(metrics.get("finish_epoch_ms")) if isinstance(metrics.get("finish_epoch_ms"), int) else None
    duration_sec = metrics.get("run_training_total_seconds")
    patron_ratio, patron_src = _resolved_patron_sampling_ratio(args)
    warn_keys = ("val_ap", "val_precision", "test_ap", "test_precision")
    for wk in warn_keys:
        if wk not in metrics:
            logger.warning("[run_report.summary] missing metrics[%r]; cross-run comparison degraded.", wk)
    feat_cols = metrics.get("step5_feature_columns")
    n_feat = len(feat_cols) if isinstance(feat_cols, list) else None
    feat_hash = _feature_list_sha256_hex(feat_cols) if isinstance(feat_cols, list) else None
    thr = metrics.get("step5_threshold")
    if "step5_optuna_skipped" not in metrics:
        opt_enabled = False
    else:
        opt_enabled = not bool(metrics.get("step5_optuna_skipped"))
    summary = {
        "run_id": run_id,
        "started_at": started,
        "finished_at": finished,
        "duration_sec": float(duration_sec) if isinstance(duration_sec, (int, float)) else None,
        "data_scope": {
            "population_scope": "adt_filtered_bets_when_enabled",
            "filter_bets_by_adt_quantile": bool(args.filter_bets_by_adt_quantile),
            "theo_train_quantile": float(args.objective.theo_train_quantile),
            "patron_sampling_ratio": patron_ratio,
            "patron_sampling_ratio_source": patron_src,
        },
        "model": {"algorithm": "lightgbm", "n_features_used": n_feat, "feature_list_sha256_hex": feat_hash},
        "thresholding": {
            "policy": "min_precision",
            "policy_param": {"min_precision": float(args.objective.min_precision)},
            "selected_threshold": float(thr) if isinstance(thr, (int, float)) else None,
        },
        "metrics": {
            "val": {
                "ap": metrics.get("val_ap"),
                "precision": metrics.get("val_precision"),
                "recall": metrics.get("val_recall"),
                "f1": metrics.get("val_f1"),
                "samples": metrics.get("val_samples"),
                "positives": metrics.get("val_positives"),
                "alerts": metrics.get("val_alerts"),
                "alerts_per_hour": metrics.get("val_alerts_per_hour"),
            },
            "test": {
                "ap": metrics.get("test_ap"),
                "precision": metrics.get("test_precision"),
                "recall": metrics.get("test_recall"),
                "f1": metrics.get("test_f1"),
                "samples": metrics.get("test_samples"),
                "positives": metrics.get("test_positives"),
                "alerts": metrics.get("test_alerts"),
                "alerts_per_hour": metrics.get("test_alerts_per_hour"),
            },
        },
        "optimization": {
            "enabled": opt_enabled,
            "backend": "optuna",
            "max_time_sec_configured": metrics.get("optuna_max_time_sec_configured"),
            "max_trials_configured": metrics.get("optuna_max_trials_configured"),
            "wall_time_sec_actual": metrics.get("optuna_wall_time_sec_actual"),
            "trials_completed": metrics.get("optuna_trials_completed"),
            "trials_total": metrics.get("optuna_trials_total"),
            "stopping_reason": metrics.get("optuna_stopping_reason"),
            "best_value": metrics.get("optuna_best_value"),
        },
        "git_commit_short": _git_short_head(repo_root),
        "run_profile": str(args.run_profile_name),
        "split_periods": metrics.get("step4_split_periods"),
    }
    if patron_ratio is None and patron_src == "unknown":
        logger.warning(
            "[run_report.summary] patron_sampling_ratio unknown; set patron_sampling_ratio "
            "or enable filter_bets_by_adt_quantile with theo_train_quantile in (0,1).",
        )
    return summary


def build_metrics_detailed(metrics: dict[str, Any]) -> dict[str, Any]:
    """Build evaluation detail block (nested under ``run_report.json``)."""

    run_id = str(metrics.get("model_version") or "").strip() or "unknown_run"

    def _split(prefix: str) -> dict[str, Any]:
        keys = ("ap", "precision", "recall", "f1")
        return {k: metrics.get(f"{prefix}_{k}") for k in keys}

    return {
        "run_id": run_id,
        "split_metrics": {"train": _split("train"), "val": _split("val"), "test": _split("test")},
        "threshold_analysis": {
            "selection_policy": f"min_precision={metrics.get('step5_min_precision')}",
            "selected_threshold": metrics.get("step5_threshold"),
            "val_pick_feasible": metrics.get("step5_val_pick_feasible"),
        },
        "budget_points": {
            "alerts_per_hour": {
                "train": metrics.get("train_alerts_per_hour"),
                "val": metrics.get("val_alerts_per_hour"),
                "test": metrics.get("test_alerts_per_hour"),
            }
        },
        "feature_columns": metrics.get("step5_feature_columns"),
        "candidate_registry": metrics.get("candidate_registry"),
        "split_periods": metrics.get("step4_split_periods"),
    }


def build_pipeline_debug(metrics: dict[str, Any]) -> dict[str, Any]:
    """Build pipeline debug block (nested under ``run_report.json``)."""

    repo_root = _repo_root()
    run_id = str(metrics.get("model_version") or "").strip() or "unknown_run"
    return {
        "run_id": run_id,
        "cache": {
            "session_clean_cache_hit": metrics.get("session_clean_cache_hit"),
            "bet_base_clean_cache_hit": metrics.get("bet_base_clean_cache_hit"),
            "bet_segment_clean_cache_hit": metrics.get("bet_segment_clean_cache_hit"),
            "bet_clean_cache_hit": metrics.get("bet_clean_cache_hit"),
        },
        "partition": {
            "inventory_fingerprint_sha256_hex": metrics.get("partition_inventory_fingerprint_sha256_hex"),
            "recompute_months": metrics.get("partition_recompute_months"),
            "snapshot_dir_effective": metrics.get("partition_snapshot_dir_effective"),
            "inventory_baseline_path": metrics.get("partition_inventory_baseline_path"),
        },
        "source_manifest_v2": {
            "elapsed_seconds": metrics.get("source_manifest_v2_elapsed_seconds"),
            "hashed_bytes": metrics.get("source_manifest_v2_hashed_bytes"),
            "hash_elapsed_seconds": metrics.get("source_manifest_v2_hash_elapsed_seconds"),
            "diff_summary": metrics.get("source_manifest_v2_diff_summary"),
            "changed_partitions": metrics.get("source_manifest_v2_changed_partitions"),
            "change_set_path": metrics.get("source_manifest_v2_change_set_path"),
            "cache_report_path": metrics.get("source_manifest_v2_cache_report_path"),
            "current_path": metrics.get("source_manifest_v2_current_path"),
        },
        "timings_sec": {
            "prepare_training_frame": metrics.get("prepare_training_frame_seconds"),
            "build_training_dataset": metrics.get("build_training_dataset_seconds"),
            "step4": metrics.get("step4_seconds"),
            "step5": metrics.get("step5_seconds"),
            "run_training_total": metrics.get("run_training_total_seconds"),
            "main_trainer_fe_materialize": metrics.get("main_trainer_fe_materialize_sec"),
            "main_trainer_fe_enrich": metrics.get("main_trainer_fe_enrich_sec"),
        },
        "feast_auto_apply": metrics.get("feast_auto_apply"),
        "split_periods": metrics.get("step4_split_periods"),
        "artifacts": {
            "model_path": path_relative_to_repo(
                str(metrics.get("model_path") or metrics.get("step5_model_path") or ""),
                repo_root=repo_root,
            ),
            "training_metrics_path": path_relative_to_repo(
                str(metrics.get("training_metrics_path") or metrics.get("step5_training_metrics_path") or ""),
                repo_root=repo_root,
            ),
            "step4_split_report": path_relative_to_repo(
                str(metrics.get("step4_split_report_bundle") or metrics.get("step4_split_report") or ""),
                repo_root=repo_root,
            ),
            "step4_splits_dir": path_relative_to_repo(str(metrics.get("step4_splits_dir") or ""), repo_root=repo_root),
            "main_trainer_training_parquet_for_step4": path_relative_to_repo(
                str(metrics.get("main_trainer_training_parquet_for_step4") or ""),
                repo_root=repo_root,
            ),
        },
        "session_dedup_hash_buckets_effective": metrics.get("session_dedup_hash_buckets_effective"),
        "bet_dedup_hash_buckets_effective": metrics.get("bet_dedup_hash_buckets_effective"),
    }


def _build_gates_block(metrics: dict[str, Any]) -> dict[str, Any]:
    """Summarize gate JSON paths and verdicts for ``run_report.json``."""

    gates: dict[str, Any] = {}
    ptg = metrics.get("pre_train_feature_gate_json")
    if isinstance(ptg, str) and ptg.strip():
        gates["pre_train_feature_gate"] = {
            "path": ptg,
            "verdict": metrics.get("pre_train_feature_gate_verdict"),
        }
    parity = metrics.get("feature_parity_verification_json")
    if isinstance(parity, str) and parity.strip():
        gates["step6_parity"] = {
            "path": parity,
            "n_failed_slow_gate": metrics.get("step6_slow_gate_failed"),
            "n_failed_all_feature_gate": metrics.get("step6_all_feature_gate_failed"),
        }
    e2e = metrics.get("deploy_e2e_gate_report_json")
    if isinstance(e2e, str) and e2e.strip():
        gates["step6_deploy_e2e"] = {
            "path": e2e,
            "verdict": metrics.get("step6_deploy_e2e_verdict"),
            "exit_code": metrics.get("step6_deploy_e2e_exit_code"),
        }
    return gates


def _build_artifacts_block(metrics: dict[str, Any]) -> dict[str, Any]:
    """Artifact pointer block for ``run_report.json``."""

    return {
        "training_metrics_path": metrics.get("training_metrics_path") or metrics.get("step5_training_metrics_path"),
        "model_path": metrics.get("model_path") or metrics.get("step5_model_path"),
        "split_report_path": metrics.get("step4_split_report_bundle") or metrics.get("step4_split_report"),
        "model_bundle_dir": metrics.get("model_bundle_dir"),
        "run_report_path": metrics.get("run_report_json"),
    }


def build_run_report(
    metrics: dict[str, Any],
    args: Any,
    *,
    status: str,
    error: str | None = None,
) -> dict[str, Any]:
    """Build nested ``run_report.json`` document."""

    run_id = str(metrics.get("model_version") or "").strip() or "unknown_run"
    return {
        "schema": RUN_REPORT_SCHEMA,
        "run_id": run_id,
        "status": status,
        "error": error,
        "summary": build_run_summary(metrics, args),
        "evaluation_detail": build_metrics_detailed(metrics),
        "pipeline_debug": build_pipeline_debug(metrics),
        "gates": _build_gates_block(metrics),
        "artifacts": _build_artifacts_block(metrics),
    }


class BundleReportWriter:
    """Write and register JSON reports under a training output directory."""

    TRAINING_METRICS_BASENAME: Final[str] = "training_metrics.json"

    def __init__(self, output_dir: Path) -> None:
        """Prepare writer for *output_dir* (bundle dir or versions root when Step 5 skipped)."""

        self.output_dir = Path(output_dir).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._training_metrics_body: dict[str, Any] | None = None
        self._registered_json: list[Path] = []

    def training_metrics_path(self) -> Path:
        """Return canonical ``training_metrics.json`` path under :attr:`output_dir`."""

        return self.output_dir / self.TRAINING_METRICS_BASENAME

    def run_report_path(self) -> Path:
        """Return canonical ``run_report.json`` path."""

        return self.output_dir / RUN_REPORT_FILENAME

    def write_training_metrics(self, step5_report: dict[str, Any]) -> Path:
        """Write ``training_metrics.json`` from Step 5 report."""

        path = self.training_metrics_path()
        self._training_metrics_body = build_training_metrics_document(step5_report)
        self._write_json(path, self._training_metrics_body)
        return path

    def patch_training_metrics(self, patch: dict[str, Any]) -> Path | None:
        """Shallow-merge *patch* into training metrics and rewrite disk JSON."""

        if self._training_metrics_body is None:
            path = self.training_metrics_path()
            if path.is_file():
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    self._training_metrics_body = raw if isinstance(raw, dict) else {}
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    logger.warning("[report_writer] skip training_metrics patch: %s", exc)
                    return None
            else:
                logger.warning("[report_writer] skip training_metrics patch: no body yet")
                return None
        self._training_metrics_body.update(patch)
        path = self.training_metrics_path()
        self._write_json(path, self._training_metrics_body)
        return path

    def copy_split_report(self, source: str | Path | None) -> Path | None:
        """Copy Step 4 ``split_report.json`` into :attr:`output_dir`."""

        if source is None:
            return None
        src = Path(source).resolve()
        if not src.is_file():
            logger.warning("[report_writer] split report missing at %s; skip copy.", src)
            return None
        dest = self.output_dir / SPLIT_REPORT_FILENAME
        shutil.copy2(src, dest)
        self._register_json(dest)
        logger.info("[report_writer] copied split report to %s", dest.resolve())
        return dest

    def write_gate_report(self, filename: str, body: dict[str, Any]) -> Path:
        """Write a gate JSON report and register it for MLflow."""

        path = self.output_dir / filename
        self._write_json(path, body)
        return path

    def finalize(
        self,
        metrics: dict[str, Any],
        args: Any,
        *,
        status: str,
        error: str | None = None,
    ) -> Path:
        """Write nested ``run_report.json`` and register it."""

        report = build_run_report(metrics, args, status=status, error=error)
        path = self.run_report_path()
        self._write_json(path, report)
        metrics["run_report_json"] = str(path.resolve())
        return path

    def registered_json_paths(self) -> tuple[Path, ...]:
        """Return all JSON artifacts written through this writer (for MLflow upload)."""

        return tuple(self._registered_json)

    def register_existing_json(self, path: Path) -> None:
        """Track an externally written JSON artifact (e.g. deploy E2E CLI output)."""

        if path.is_file():
            self._register_json(path)

    def _write_json(self, path: Path, body: dict[str, Any]) -> None:
        """Persist JSON and track registration."""

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body, indent=2, default=str), encoding="utf-8")
        self._register_json(path)

    def _register_json(self, path: Path) -> None:
        """Track *path* if not already registered."""

        resolved = path.resolve()
        if resolved not in self._registered_json:
            self._registered_json.append(resolved)
