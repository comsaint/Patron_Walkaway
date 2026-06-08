"""Tests for optional pre-Step-5 feature screening hook."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trainer_hightier.config import FeatureScreeningPolicy, validate_feature_screening_policy
from trainer_hightier.utils.cache_invalidation_v1 import feature_screening_change_invalidates_layers
from trainer_hightier.utils.feature_screening_hook import (
    FEATURE_SCREENING_MANIFEST_KIND,
    FEATURE_SCREENING_MANIFEST_SCHEMA_VERSION,
    feature_selection_manifest_fingerprint,
    load_feature_screening_manifest,
    resolve_selected_features_from_manifest,
    resolve_step5_feature_columns,
)


def _write_manifest(path: Path, *, selected_features: list[str], method: str = "fqg_allowlist_v0") -> None:
    payload = {
        "schema_version": FEATURE_SCREENING_MANIFEST_SCHEMA_VERSION,
        "kind": FEATURE_SCREENING_MANIFEST_KIND,
        "selected_features": selected_features,
        "method": method,
        "fqg_version": "v0",
        "evidence_refs": [
            {"kind": "feature_quality_report", "path": "out/feature_quality_report.json"},
        ],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def test_resolve_step5_feature_columns_noop_when_disabled() -> None:
    baseline = ("bet__bets_cnt__w1h", "fe__bets_cnt__w15m", "fe__wager_sum__w15m")
    cols, meta = resolve_step5_feature_columns(
        baseline_features=baseline,
        policy=FeatureScreeningPolicy(enabled=False),
    )
    assert cols == baseline
    assert meta["noop"] is True
    assert meta["selected_features"] == list(baseline)


def test_resolve_step5_feature_columns_applies_manifest_subset(tmp_path: Path) -> None:
    baseline = ("bet__bets_cnt__w1h", "fe__bets_cnt__w15m", "fe__wager_sum__w15m")
    manifest_p = tmp_path / "selected_features.json"
    _write_manifest(manifest_p, selected_features=["fe__bets_cnt__w15m", "bet__bets_cnt__w1h"])
    cols, meta = resolve_step5_feature_columns(
        baseline_features=baseline,
        policy=FeatureScreeningPolicy(enabled=True, manifest_path=manifest_p),
    )
    assert cols == ("fe__bets_cnt__w15m", "bet__bets_cnt__w1h")
    assert meta["noop"] is False
    assert meta["selected_feature_count"] == 2
    assert meta["registry_baseline_preserved"] is True
    assert meta["feature_selection_manifest_fingerprint"]


def test_manifest_unknown_feature_rejected(tmp_path: Path) -> None:
    manifest_p = tmp_path / "bad.json"
    _write_manifest(manifest_p, selected_features=["fe__does_not_exist"])
    manifest = load_feature_screening_manifest(manifest_p)
    with pytest.raises(ValueError, match="not in registry baseline"):
        resolve_selected_features_from_manifest(
            manifest,
            baseline_features=("fe__bets_cnt__w15m",),
        )


def test_validate_feature_screening_policy_requires_manifest_when_enabled() -> None:
    with pytest.raises(ValueError, match="manifest_path"):
        validate_feature_screening_policy(FeatureScreeningPolicy(enabled=True, manifest_path=None))


def test_validate_feature_screening_policy_requires_existing_file(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(FileNotFoundError, match="manifest not found"):
        validate_feature_screening_policy(FeatureScreeningPolicy(enabled=True, manifest_path=missing))


def test_feature_selection_manifest_fingerprint_stable(tmp_path: Path) -> None:
    manifest_p = tmp_path / "selected.json"
    _write_manifest(manifest_p, selected_features=["fe__a", "fe__b"])
    manifest = load_feature_screening_manifest(manifest_p)
    fp1 = feature_selection_manifest_fingerprint(manifest)
    fp2 = feature_selection_manifest_fingerprint(manifest)
    assert fp1 == fp2
    assert len(fp1) == 64


def test_feature_screening_change_invalidates_manifest_and_model_only() -> None:
    layers = feature_screening_change_invalidates_layers()
    assert layers == ("selected_feature_manifest", "model_artifacts")
    assert "sampled_train_cache" not in layers
    assert "short_term_pit_cache" not in layers
