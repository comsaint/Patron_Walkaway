"""End-to-end fixture: recording layout + full replay analysis."""

from __future__ import annotations

import json
import pickle
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from sklearn.dummy import DummyClassifier

from trainer_hightier.config import (
    FEATURE_CANDIDATE_REGISTRY_SNAPSHOT_FILENAME,
    HK_TZ,
)
from trainer_hightier.serving.flight_recorder.init_recording import init_recording_root
from trainer_hightier.serving.flight_recorder.config import FlightRecorderConfig
from trainer_hightier.serving.replay_recording_bundle import run_full_analysis
from trainer_hightier.serving.validator import validate_alert_row


def _write_model_bundle(bundle_dir: Path, *, feature_cols: tuple[str, ...]) -> None:
    """Write minimal ``model.pkl`` under *bundle_dir*."""
    model = DummyClassifier(strategy="constant", constant=1)
    model.fit([[0.0], [1.0]], [0, 1])
    payload = {
        "model": model,
        "feature_columns": list(feature_cols),
        "threshold": 0.5,
        "categorical_columns": [],
        "category_categories": {},
    }
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "model.pkl").write_bytes(pickle.dumps(payload))
    (bundle_dir / "model_version").write_text("e2e-fixture-v1\n", encoding="utf-8")
    (bundle_dir / FEATURE_CANDIDATE_REGISTRY_SNAPSHOT_FILENAME).write_text(
        "registry_version: e2e\nfeatures: []\n",
        encoding="utf-8",
    )


def _build_validator_match_row(now_hk: datetime) -> tuple[pd.Series, dict, dict]:
    """Build alert row and bet_cache that finalize to MATCH under bundle validator."""
    bet_ts = now_hk - timedelta(hours=2)
    score_ts = bet_ts + timedelta(minutes=5)
    canonical_id = "1001"
    player_id = 42
    # Single bet at alert time: tail gap to LABEL_LOOKAHEAD horizon yields MATCH
    # (a second bet inside (ALERT_HORIZON, LABEL_LOOKAHEAD] would finalize as MISS).
    bet_cache = {canonical_id: [bet_ts]}
    alert = pd.Series(
        {
            "bet_id": "90001",
            "player_id": player_id,
            "canonical_id": canonical_id,
            "ts": score_ts.isoformat(),
            "bet_ts": bet_ts,
            "score": 0.92,
            "model_version": "e2e-fixture-v1",
            "casino_player_id": "CP1001",
        }
    )
    prod = validate_alert_row(alert, bet_cache, {}, force_finalize=True)
    assert prod.get("result") is True, f"expected MATCH, got {prod!r}"
    assert prod.get("reason") == "MATCH"
    return alert, bet_cache, prod


def build_e2e_recording_fixture(root: Path, bundle_dir: Path) -> Path:
    """Populate *root* with scorer + validator cycles consistent with replay."""
    recording_root = root / "flight_recording"
    config = FlightRecorderConfig(recording_root=str(recording_root))
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "bundle_info.json").write_text("{}", encoding="utf-8")
    (bundle_dir / "deploy_bundle_paths.json").write_text(
        json.dumps({"local_state_dir": "local_state", "model_bundle_dir": "models"}),
        encoding="utf-8",
    )
    _write_model_bundle(bundle_dir / "models", feature_cols=("wager", "player_id"))
    init_recording_root(bundle_dir, config, write_default_config=False, export_sqlite=False)

    model_payload = pickle.loads((bundle_dir / "models" / "model.pkl").read_bytes())
    model = model_payload["model"]
    features = pd.DataFrame({"wager": [50.0, 80.0], "player_id": [1, 2]})
    prob = model.predict_proba(features)[:, 1]

    scorer_cycle = recording_root / "cycles" / "scorer" / "cycle_000001"
    stages = scorer_cycle / "stages"
    stages.mkdir(parents=True, exist_ok=True)
    features.to_parquet(stages / "stage_08_model_feature_matrix.parquet", index=False)
    features.assign(score=prob).to_parquet(stages / "stage_09_scores.parquet", index=False)
    (scorer_cycle / "clickhouse").mkdir(exist_ok=True)
    (scorer_cycle / "audits" / "row_counts.json").parent.mkdir(parents=True, exist_ok=True)
    (scorer_cycle / "audits" / "row_counts.json").write_text(
        json.dumps({"n_batch_rows": 2, "n_alerts": 0}),
        encoding="utf-8",
    )

    now_hk = datetime.now(ZoneInfo(HK_TZ))
    alert, bet_cache, prod = _build_validator_match_row(now_hk)
    val_cycle = recording_root / "cycles" / "validator" / "cycle_000001"
    (val_cycle / "alerts").mkdir(parents=True, exist_ok=True)
    (val_cycle / "clickhouse").mkdir(parents=True, exist_ok=True)
    pd.DataFrame([alert]).to_parquet(val_cycle / "alerts" / "pending_alerts.parquet", index=False)
    ch_rows = [
        {"player_id": int(alert["player_id"]), "payout_complete_dtm": ts}
        for ts in bet_cache[str(alert["canonical_id"])]
    ]
    pd.DataFrame(ch_rows).to_parquet(
        val_cycle / "clickhouse" / "fetch_bets_by_canonical_id.final.parquet",
        index=False,
    )
    trace = pd.DataFrame([{k: prod.get(k) for k in (
        "bet_id", "result", "reason", "gap_start", "gap_minutes", "validated_at",
        "alert_ts", "bet_ts", "canonical_id", "player_id", "score", "model_version",
    )}])
    (val_cycle / "decisions").mkdir(parents=True, exist_ok=True)
    trace.to_parquet(val_cycle / "decisions" / "decision_trace.parquet", index=False)

    from trainer_hightier.serving.flight_recorder.manifest import RecordingRoot

    rec = RecordingRoot(
        root=recording_root,
        bundle_dir=bundle_dir,
        model_version="e2e-fixture-v1",
    )
    rec.write_manifest()
    return recording_root


@pytest.fixture
def e2e_fixture_dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Bundle dir + recording root + analysis output dir."""
    bundle = tmp_path / "deploy_bundle"
    recording = build_e2e_recording_fixture(tmp_path, bundle)
    analysis = tmp_path / "analysis"
    return bundle, recording, analysis


def test_e2e_full_analysis_score_and_validator_match(
    e2e_fixture_dirs: tuple[Path, Path, Path],
) -> None:
    """Full analysis on fixture achieves 100% score and validator replay match."""
    bundle_dir, recording_root, analysis_dir = e2e_fixture_dirs
    reports = run_full_analysis(
        recording_root,
        analysis_dir,
        model_bundle_dir=bundle_dir / "models",
    )
    score = reports["score_replay"]
    assert score["overall_match_rate"] == 1.0
    assert score["total_rows"] == 2
    val = reports["validator_replay"]
    assert val["total_compared"] == 1
    assert val["overall_match_rate"] == 1.0
    assert (analysis_dir / "score_replay_diff_report.json").is_file()
    assert (analysis_dir / "validator_replay_diff_report.json").is_file()
    assert (analysis_dir / "analysis_summary.json").is_file()


def test_collect_debug_bundle_includes_flight_recording_pointer(
    e2e_fixture_dirs: tuple[Path, Path, Path],
    tmp_path: Path,
) -> None:
    """Debug bundle collector copies flight_recording MANIFEST when present."""
    from trainer_hightier.serving.collect_debug_bundle import collect_debug_bundle

    bundle_dir, recording_root, _ = e2e_fixture_dirs
    ls = bundle_dir / "local_state"
    ls.mkdir(parents=True, exist_ok=True)
    target = ls / "flight_recording"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(recording_root, target)
    out_zip = tmp_path / "diag.zip"
    collect_debug_bundle(bundle_dir=bundle_dir, output_zip=out_zip, skip_mlflow_upload=True)
    import zipfile

    with zipfile.ZipFile(out_zip, "r") as zf:
        names = zf.namelist()
    assert any(n.endswith("flight_recording/MANIFEST.json") for n in names)
