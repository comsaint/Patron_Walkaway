"""Tests for :mod:`trainer_hightier.serving.candidate_registry_loader` (via experiment re-export)."""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest
import yaml

from trainer_hightier.feature_experiment.candidate_registry_loader import (
    baseline_features_for_main_trainer,
    default_registry_path,
    load_candidate_registry,
)
from trainer_hightier.feature_experiment.feature_registry import (
    candidate_registry_snapshot,
    set_candidate_registry_path,
)

# Golden order: YAML baseline slot rows in declaration order (v0.5+ includes KEEP fe__*).
_EXPECTED_DEFAULT_BASELINE: Final[tuple[str, ...]] = (
    "wager",
    "casino_win",
    "is_back_bet",
    "bet_type",
    "type_of_bet",
    "bet__bets_cnt__w1h",
    "bet__wager_sum__w1h",
    "bet__back_bet_ratio__w1h",
    "bet__payout_odds_avg__w1h",
    "patron__theo_win_sum__w180d_m1snap",
    "patron__gaming_days_cnt__w180d_m1snap",
    "patron__adt__w180d_m1snap",
    "fe__wager_sum__w15m",
    "fe__bets_cnt__w15m",
    "fe__canonical__bets_cnt__today",
    "fe__canonical__wager_sum__today",
    "fe__canonical__avg_wager__today",
    "fe__canonical__elapsed_sec_since_first_bet__today",
    "fe__interarrival__lag2_sec",
    "fe__interarrival__last_gap_to_recent_mean_ratio__w1h",
    "fe__interarrival__cv__w1h",
    "fe__odds__payout_odds_z__w1h",
    "fe__odds__payout_odds_to_recent_max_ratio__w1h",
    "fe__odds__payout_odds_step_ratio",
)


def test_default_registry_baselines_yaml_order() -> None:
    """Default registry baseline list follows YAML and is non-empty."""

    snap = load_candidate_registry(None)
    assert snap.model_feature_columns == _EXPECTED_DEFAULT_BASELINE
    assert len(snap.experimental_numeric_columns) >= 1
    assert set(snap.model_feature_columns).issubset(set(snap.full_candidate_feature_columns))


def test_baseline_features_for_main_trainer_matches_snapshot() -> None:
    """Main-trainer helper returns the ordered baseline tuple from the snapshot."""

    snap = load_candidate_registry(None)
    assert baseline_features_for_main_trainer(snap) == snap.model_feature_columns


def test_duplicate_feature_id_rejected(tmp_path: Path) -> None:
    blob = yaml.safe_load(default_registry_path().read_text(encoding="utf-8"))
    feats = blob["features"]
    blob["features"] = feats + [feats[-1]]
    p = tmp_path / "dup.yaml"
    p.write_text(yaml.safe_dump(blob, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate feature_id entries"):
        load_candidate_registry(p)


def test_disabled_requires_drop_reason_code(tmp_path: Path) -> None:
    blob = yaml.safe_load(default_registry_path().read_text(encoding="utf-8"))
    for row in blob["features"]:
        if str(row.get("feature_id", "")).startswith("fe__"):
            row["status"] = "disabled"
            row["drop_reason_code"] = None
            break
    else:
        raise AssertionError("expected at least one fe__ row in default registry")
    p = tmp_path / "nodrop.yaml"
    p.write_text(yaml.safe_dump(blob, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValueError, match="drop_reason_code is required"):
        load_candidate_registry(p)


def test_invalid_status_rejected(tmp_path: Path) -> None:
    blob = yaml.safe_load(default_registry_path().read_text(encoding="utf-8"))
    blob["features"][0]["status"] = "bogus"
    p = tmp_path / "badstat.yaml"
    p.write_text(yaml.safe_dump(blob, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValueError, match="status must be one of"):
        load_candidate_registry(p)


def test_fe_with_baseline_slot_allowed() -> None:
    """Default contract may list ``baseline`` for ``fe__*`` (main trainer Step 3.5 enrich)."""

    snap = load_candidate_registry(None)
    fe_in_baseline = [c for c in snap.model_feature_columns if c.startswith("fe__")]
    assert len(fe_in_baseline) >= 1


def test_no_baseline_rows_rejected(tmp_path: Path) -> None:
    """Registry must expose at least one selectable ``baseline`` feature row."""

    p = tmp_path / "no_baseline.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "registry_version": "v.test",
                "features": [
                    {
                        "feature_id": "fe__only",
                        "group_id": "group_z",
                        "source": "fe_derived",
                        "status": "active",
                        "enabled_for": ["candidate", "ablation"],
                        "time_horizon": "mid_term",
                        "max_lookback": "P1D",
                    },
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="at least one baseline feature"):
        load_candidate_registry(p)


def test_set_candidate_registry_path_reloads_snapshot(tmp_path: Path) -> None:
    """YAML path configured before reads must drive :func:`candidate_registry_snapshot`."""

    blob = yaml.safe_load(default_registry_path().read_text(encoding="utf-8"))
    blob["registry_version"] = "v0.unit_test_registry_path"
    p = tmp_path / "marked.yaml"
    p.write_text(yaml.safe_dump(blob, sort_keys=False), encoding="utf-8")
    try:
        set_candidate_registry_path(p)
        snap = candidate_registry_snapshot()
        assert snap.registry_version == "v0.unit_test_registry_path"
        assert snap.path.resolve() == p.resolve()
    finally:
        set_candidate_registry_path(None)
