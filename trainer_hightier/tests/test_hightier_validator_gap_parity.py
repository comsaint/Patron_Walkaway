"""Validator gap logic parity vs ``trainer`` (same code lineage)."""

from __future__ import annotations

from datetime import datetime, timedelta

from zoneinfo import ZoneInfo

from trainer.serving import validator as tv
from trainer_hightier.serving import validator as hv


def test_find_gap_within_window_matches_trainer() -> None:
    hk = ZoneInfo("Asia/Hong_Kong")
    alert_ts = datetime(2024, 1, 1, 12, 0, tzinfo=hk)
    bets = [
        alert_ts + timedelta(minutes=5),
        alert_ts + timedelta(minutes=50),
    ]
    a = tv.find_gap_within_window(alert_ts, bets)
    b = hv.find_gap_within_window(alert_ts, bets)
    assert a == b
