"""run_scorer_loop first_cycle_done signaling (deploy / SQLite lock mitigation)."""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest

from trainer.serving import scorer as sc


@pytest.fixture
def tmp_state_db(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    monkeypatch.setattr(sc, "STATE_DB_PATH", db)
    return db


def test_first_cycle_done_set_after_once_with_mocked_score_once(tmp_state_db):
    ev = threading.Event()
    assert not ev.is_set()
    fake_art = {
        "model_version": "t",
        "rated": None,
        "feature_list": [],
        "reason_code_map": {},
        "feature_spec": None,
    }
    with (
        patch.object(sc, "_check_numba_runtime_once"),
        patch.object(sc, "load_dual_artifacts", return_value=fake_art),
        patch.object(sc, "score_once") as m_score,
        patch.object(sc, "load_alert_history", return_value=set()),
    ):
        sc.run_scorer_loop(
            interval_seconds=1,
            lookback_hours=1,
            once=True,
            first_cycle_done=ev,
        )
    assert ev.is_set()
    assert m_score.call_count == 1


def test_first_cycle_done_set_even_when_score_once_raises(tmp_state_db):
    ev = threading.Event()
    fake_art = {
        "model_version": "t",
        "rated": None,
        "feature_list": [],
        "reason_code_map": {},
        "feature_spec": None,
    }
    with (
        patch.object(sc, "_check_numba_runtime_once"),
        patch.object(sc, "load_dual_artifacts", return_value=fake_art),
        patch.object(sc, "score_once", side_effect=RuntimeError("boom")),
        patch.object(sc, "load_alert_history", return_value=set()),
    ):
        sc.run_scorer_loop(
            interval_seconds=1,
            lookback_hours=1,
            once=True,
            first_cycle_done=ev,
        )
    assert ev.is_set()


def test_first_cycle_done_none_no_crash(tmp_state_db):
    fake_art = {
        "model_version": "t",
        "rated": None,
        "feature_list": [],
        "reason_code_map": {},
        "feature_spec": None,
    }
    with (
        patch.object(sc, "_check_numba_runtime_once"),
        patch.object(sc, "load_dual_artifacts", return_value=fake_art),
        patch.object(sc, "score_once"),
        patch.object(sc, "load_alert_history", return_value=set()),
    ):
        sc.run_scorer_loop(
            interval_seconds=1,
            lookback_hours=1,
            once=True,
            first_cycle_done=None,
        )
