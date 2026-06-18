"""WalkawayLabelContract parity across training materialize and serving config."""

from __future__ import annotations

from dataclasses import replace

import pandas as pd
import pytest

from trainer_hightier.config import (
    ALERT_HORIZON_MIN,
    HightierServingConfig,
    default_hightier_serving_config,
    hightier_serving_config_for_deploy_bundle,
    walkaway_label_contract_for_gap_min,
)
from trainer_hightier.utils.walkaway_labels import (
    label_window_ends_from_max_payout,
    write_walkaway_labels_from_joined_dataframe,
)


@pytest.mark.parametrize("gap_min", [30, 60, 1440])
def test_label_lookahead_min_is_gap_plus_horizon(gap_min: int) -> None:
    contract = walkaway_label_contract_for_gap_min(gap_min)
    assert contract.label_lookahead_min == gap_min + ALERT_HORIZON_MIN


@pytest.mark.parametrize("gap_min", [30, 60, 1440])
def test_serving_config_label_lookahead_matches_contract(gap_min: int) -> None:
    cfg = replace(HightierServingConfig(), walkaway_gap_min=gap_min)
    assert cfg.label_lookahead_min == cfg.walkaway_label_contract.label_lookahead_min


def test_default_serving_config_gap30_lookahead() -> None:
    cfg = default_hightier_serving_config()
    assert cfg.label_lookahead_min == 45
    assert cfg.walkaway_label_contract.contract_id == "walkaway_v1_gap30"


def test_label_window_ends_from_max_payout_uses_contract_lookahead() -> None:
    max_pcd = pd.Timestamp("2026-06-01 10:00:00")
    contract = walkaway_label_contract_for_gap_min(60)
    window_end, extended_end = label_window_ends_from_max_payout(max_pcd, label_contract=contract)
    assert window_end == max_pcd
    assert extended_end == max_pcd + pd.Timedelta(minutes=75)


def test_write_walkaway_labels_default_extended_end_uncensors_terminal_bet(tmp_path) -> None:
    """Terminal bets at window_end must not be censored when extended_end includes lookahead."""
    t0 = pd.Timestamp("2024-06-01 12:00:00")
    joined = pd.DataFrame(
        [{"bet_id": 1.0, "canonical_id": "c1", "payout_complete_dtm": t0}],
    )
    contract = walkaway_label_contract_for_gap_min(30)
    out_default = tmp_path / "default_extended.parquet"
    write_walkaway_labels_from_joined_dataframe(
        joined,
        out_default,
        label_contract=contract,
    )
    got_default = pd.read_parquet(out_default)
    assert bool(got_default["censored"].iloc[0]) is False

    out_legacy = tmp_path / "legacy_extended.parquet"
    write_walkaway_labels_from_joined_dataframe(
        joined,
        out_legacy,
        window_end=t0,
        extended_end=t0,
        label_contract=contract,
    )
    got_legacy = pd.read_parquet(out_legacy)
    assert bool(got_legacy["censored"].iloc[0]) is True


def test_deploy_bundle_rel_wires_walkaway_gap(tmp_path) -> None:
    rel = {
        "local_state_dir": "local_state",
        "walkaway_gap_min": 60,
        "alert_horizon_min": 15,
    }
    cfg = hightier_serving_config_for_deploy_bundle(tmp_path, rel)
    assert cfg.walkaway_gap_min == 60
    assert cfg.label_lookahead_min == 75
