"""Validator gap_within_window deterministic checks (legacy trainer parity lineage)."""

from __future__ import annotations

from datetime import datetime, timedelta

from zoneinfo import ZoneInfo

from trainer_hightier.serving import validator as hv


def test_find_gap_within_window_known_case() -> None:
    hk = ZoneInfo("Asia/Hong_Kong")
    alert_ts = datetime(2024, 1, 1, 12, 0, tzinfo=hk)
    bets = [
        alert_ts + timedelta(minutes=5),
        alert_ts + timedelta(minutes=50),
    ]
    out = hv.find_gap_within_window(alert_ts, bets)
    gap_start_expected = bets[0]
    horizon_end = alert_ts + timedelta(minutes=int(hv.config.LABEL_LOOKAHEAD_MIN))
    gap_minutes_expected = (horizon_end - gap_start_expected).total_seconds() / 60.0
    assert out[0] is True
    assert out[1] == gap_start_expected
    assert abs(float(out[2]) - gap_minutes_expected) < 1e-6
