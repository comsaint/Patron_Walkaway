"""Tests for production incident debug bundle collector."""

from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from trainer_hightier.config import DIAG_BUNDLE_RETENTION_COUNT
from trainer_hightier.serving import collect_debug_bundle as cdb


def _write_min_bundle(root: Path) -> None:
    """Create minimal deploy bundle layout for collector tests."""
    (root / "deploy_bundle_paths.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "model_bundle_dir": "models",
                "snapshot_manifest_dir": "snapshots",
                "canonical_mapping_parquet": "mapping/canonical_player_mapping.parquet",
                "local_state_dir": "local_state",
                "feast_repo_dir": "feast_repo",
                "feast_artifacts_dir": "artifacts/feast",
                "feast_readiness_path": "artifacts/feast/feast_online_readiness.json",
                "adt_allowlist_parquet": "mapping/adt_allowed_players_q0p99.parquet",
            }
        ),
        encoding="utf-8",
    )
    (root / "bundle_info.json").write_text('{"model_version":"test-model-v1"}', encoding="utf-8")
    models = root / "models"
    models.mkdir(parents=True)
    (models / "model_version").write_text("test-model-v1", encoding="utf-8")
    ls = root / "local_state"
    ls.mkdir(parents=True)
    pl_db = ls / "prediction_log.db"
    with sqlite3.connect(str(pl_db)) as conn:
        conn.execute(
            """
            CREATE TABLE prediction_log (
                prediction_id INTEGER PRIMARY KEY,
                bet_id TEXT,
                score REAL,
                is_alert INTEGER,
                threshold REAL,
                model_features_missing INTEGER,
                missing_family_json TEXT,
                scoring_status TEXT,
                mid_term_freshness_status TEXT,
                slow_freshness_status TEXT,
                snapshot_scoring_degraded INTEGER
            )
            """
        )
        conn.execute(
            """
            INSERT INTO prediction_log(
                bet_id, score, is_alert, threshold, model_features_missing,
                missing_family_json, scoring_status, mid_term_freshness_status,
                slow_freshness_status, snapshot_scoring_degraded
            ) VALUES ('101', 0.8, 1, 0.57, 0, '{}', 'ok', 'fresh', 'fresh', 0)
            """
        )
    st_db = ls / "state.db"
    with sqlite3.connect(str(st_db)) as conn:
        conn.execute("CREATE TABLE alerts (alert_id INTEGER PRIMARY KEY, bet_id TEXT)")
        conn.execute("INSERT INTO alerts(bet_id) VALUES ('101')")
    fs_db = ls / "feature_state.db"
    with sqlite3.connect(str(fs_db)) as conn:
        conn.execute(
            "CREATE TABLE feature_state_meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        conn.execute(
            "INSERT INTO feature_state_meta(key, value) VALUES ('k', 'v')"
        )


@patch("trainer_hightier.serving.collect_debug_bundle.run_supplier_root_cause_audit")
@patch("trainer_hightier.serving.collect_debug_bundle.run_production_readiness_audit")
@patch("trainer_hightier.serving.collect_debug_bundle.upload_to_mlflow")
def test_collect_debug_bundle_writes_zip(
    mock_upload: object,
    mock_prod_audit: object,
    mock_supplier_audit: object,
    tmp_path: Path,
) -> None:
    """Collector produces zip with exports, manifest, and audit CSV."""
    root = tmp_path / "bundle"
    root.mkdir()
    _write_min_bundle(root)
    mock_prod_audit.return_value = 0
    mock_supplier_audit.return_value = {"n_bets_diagnosed": 1}
    mock_upload.return_value = ("mlflow_upload_skipped", None)

    out = cdb.collect_debug_bundle(bundle_dir=root, skip_mlflow_upload=True)
    assert out.is_file()
    with zipfile.ZipFile(out, "r") as zf:
        names = set(zf.namelist())
        assert "MANIFEST.json" in names
        assert "exports/prediction_log_audit.csv" in names
        assert "exports/prediction_log/prediction_log.parquet" in names
        assert "exports/state/alerts.parquet" in names
        assert "exports/feature_state/feature_state_meta.parquet" in names
        manifest = json.loads(zf.read("MANIFEST.json").decode("utf-8"))
        assert manifest["model_version"] == "test-model-v1"
        assert manifest["mlflow_upload_status"] == "mlflow_upload_skipped"


@patch("trainer_hightier.serving.collect_debug_bundle.run_supplier_root_cause_audit")
@patch("trainer_hightier.serving.collect_debug_bundle.run_production_readiness_audit")
@patch("trainer_hightier.serving.collect_debug_bundle.upload_to_mlflow")
def test_collect_debug_bundle_partial_on_audit_error(
    mock_upload: object,
    mock_prod_audit: object,
    mock_supplier_audit: object,
    tmp_path: Path,
) -> None:
    """Audit failures mark manifest partial but still emit zip."""
    root = tmp_path / "bundle"
    root.mkdir()
    _write_min_bundle(root)
    mock_prod_audit.side_effect = RuntimeError("feast down")
    mock_supplier_audit.side_effect = RuntimeError("ch down")
    mock_upload.return_value = ("mlflow_upload_skipped", None)

    out = cdb.collect_debug_bundle(bundle_dir=root, skip_mlflow_upload=True)
    with zipfile.ZipFile(out, "r") as zf:
        manifest = json.loads(zf.read("MANIFEST.json").decode("utf-8"))
        assert manifest["partial"] is True
        statuses = {s["name"]: s["status"] for s in manifest["steps"]}
        assert statuses["audit_production_readiness"] == "error"
        assert statuses["audit_supplier_root_cause"] == "error"


def test_write_prediction_log_audit_csv(tmp_path: Path) -> None:
    """Audit CSV contains expected subset columns."""
    root = tmp_path / "bundle"
    root.mkdir()
    _write_min_bundle(root)
    staging = tmp_path / "staging"
    staging.mkdir()
    ctx = cdb.CollectContext(
        bundle_root=root,
        rel={"local_state_dir": "local_state"},
        staging_dir=staging,
        model_version="test-model-v1",
    )
    csv_path = cdb.write_prediction_log_audit_csv(ctx, root / "local_state" / "prediction_log.db")
    assert csv_path is not None
    text = csv_path.read_text(encoding="utf-8")
    assert "bet_id" in text
    assert "score" in text


@patch("trainer_hightier.serving.collect_debug_bundle.upload_to_mlflow")
@patch("trainer_hightier.serving.collect_debug_bundle.run_supplier_root_cause_audit")
@patch("trainer_hightier.serving.collect_debug_bundle.run_production_readiness_audit")
def test_prune_old_zips(
    mock_prod_audit: object,
    mock_supplier_audit: object,
    mock_upload: object,
    tmp_path: Path,
) -> None:
    """Retention keeps only newest N prod_diag zip files."""
    root = tmp_path / "bundle"
    root.mkdir()
    _write_min_bundle(root)
    mock_prod_audit.return_value = 0
    mock_supplier_audit.return_value = {}
    mock_upload.return_value = ("mlflow_upload_skipped", None)
    exports = root / "local_state" / "diag_exports"
    for idx in range(DIAG_BUNDLE_RETENTION_COUNT + 2):
        stale = exports / f"prod_diag_test-model-v1_old{idx}.zip"
        exports.mkdir(parents=True, exist_ok=True)
        stale.write_bytes(b"stale")
    cdb.collect_debug_bundle(bundle_dir=root, skip_mlflow_upload=True)
    zips = list(exports.glob("prod_diag_*.zip"))
    assert len(zips) == DIAG_BUNDLE_RETENTION_COUNT


def test_upload_to_mlflow_skipped_when_unavailable(tmp_path: Path) -> None:
    """Missing MLflow credentials yields mlflow_upload_skipped."""
    ctx = cdb.CollectContext(
        bundle_root=tmp_path,
        rel={},
        staging_dir=tmp_path / "staging",
        model_version="mv1",
    )
    fake_zip = tmp_path / "out.zip"
    fake_zip.write_bytes(b"zip")
    with patch("trainer_hightier.serving.collect_debug_bundle.is_mlflow_available", return_value=False):
        status, run_id = cdb.upload_to_mlflow(ctx, fake_zip, skip=False)
    assert status == "mlflow_upload_skipped"
    assert run_id is None
