"""Round 235 Code Review — POST /score 步驟 4 risk points as tests.

STATUS.md Round 235 Code Review: convert reviewer risk points to minimal
reproducible tests only. No production code changes.

Reference: PLAN § api_server 對齊 model_api_protocol, STATUS Round 235 Review.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(reason="api_server reverted to DB-only; model API removed")

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

if "config" not in sys.modules:
    import importlib as _il
    sys.modules["config"] = _il.import_module("trainer.config")
if "trainer.api_server" not in sys.modules:
    import importlib as _il
    _il.import_module("trainer.api_server")

import trainer.api_server as api_server  # noqa: E402

# Reuse helpers from test_api_server for stub artifacts and payload shape
from test_api_server import _make_stub_artifacts, _score_payload  # noqa: E402


def _one_row(features: list, **kw):
    """One row with feature_list + bet_id (required by protocol)."""
    row = {f: 0.5 for f in features}
    row["bet_id"] = 1
    row.update(kw)
    return row


class TestR235_1_PassThroughMustNotOverwriteScoreAlert(unittest.TestCase):
    """Review #1: Server-computed score/alert must not be overwritten by client pass-through keys."""

    def test_score_alert_must_be_server_computed_when_row_has_extra_score_alert_keys(self):
        """When row contains extra keys \"score\": 999 and \"alert\": true, response must still
        have scores[0][\"score\"] as model-computed float in [0,1] and scores[0][\"alert\"] as computed bool.
        Current production overwrites with client values; this test documents desired behavior."""
        features = ["f1", "f2", "f3"]
        arts = _make_stub_artifacts(feature_list=features)
        if arts["rated"] is None:
            self.skipTest("LightGBM not available")
        # Send is_rated=False so server must compute alert=False; client also sends alert=True as pass-through
        row = _one_row(features, score=999, alert=True, is_rated=False)
        with patch.object(api_server, "_get_artifacts", return_value=arts):
            resp = api_server.app.test_client().post(
                "/score",
                data=json.dumps(_score_payload([row])),
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 200, "Request is valid; server should return 200")
        data = resp.get_json()
        self.assertIn("scores", data)
        self.assertEqual(len(data["scores"]), 1)
        s0 = data["scores"][0]
        self.assertIn("score", s0)
        self.assertIn("alert", s0)
        self.assertIsInstance(s0["score"], (int, float), "score must be numeric from model")
        if s0["score"] is not None:
            self.assertGreaterEqual(s0["score"], 0.0)
            self.assertLessEqual(s0["score"], 1.0)
        self.assertIsInstance(s0["alert"], bool, "alert must be bool from server logic")
        # Server must use computed values, not client pass-through: score in [0,1], alert=False when is_rated=False
        self.assertNotEqual(s0["score"], 999, "score must not be client-supplied pass-through value")
        self.assertFalse(s0["alert"], "alert must be server-computed (is_rated=False => alert=False)")


class TestR235_2_RowsElementMustBeDict(unittest.TestCase):
    """Review #2: When rows[i] is not a dict, server returns 400 with error body."""

    def test_400_when_rows_contains_non_dict_element(self):
        """Send {\"rows\": [{\"f1\": 1, \"f2\": 0, \"f3\": 0, \"bet_id\": 1}, 123]}; expect 400."""
        features = ["f1", "f2", "f3"]
        arts = _make_stub_artifacts(feature_list=features)
        payload = {"rows": [_one_row(features), 123]}
        with patch.object(api_server, "_get_artifacts", return_value=arts):
            resp = api_server.app.test_client().post(
                "/score",
                data=json.dumps(payload),
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertIn("error", data)


class TestR235_7_BatchResponseLengthAndOrder(unittest.TestCase):
    """Review #7: Multi-row request must return scores in same length and order."""

    def test_multi_row_returns_scores_same_length_and_order(self):
        """Send 10 rows; assert response scores length 10 and each score is float."""
        features = ["f1", "f2", "f3"]
        arts = _make_stub_artifacts(feature_list=features)
        if arts["rated"] is None:
            self.skipTest("LightGBM not available")
        rows = [_one_row(features, **{f: float(i) / 10 for f in features}) for i in range(10)]
        for i, r in enumerate(rows):
            r["bet_id"] = i + 1
        with patch.object(api_server, "_get_artifacts", return_value=arts):
            resp = api_server.app.test_client().post(
                "/score",
                data=json.dumps(_score_payload(rows)),
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("scores", data)
        self.assertEqual(len(data["scores"]), 10)
        for i, s in enumerate(data["scores"]):
            self.assertIn("score", s)
            self.assertIn("bet_id", s)
            self.assertEqual(s["bet_id"], i + 1)
            self.assertIsInstance(s["score"], (int, float))


class TestR235_8_BatchTooLargeResponseBody(unittest.TestCase):
    """Review #8: 422 when batch over limit must include 'error' and 'limit' in body."""

    def test_422_batch_too_large_body_contains_error_and_limit(self):
        """When rows exceed _MAX_SCORE_ROWS, response body must contain 'error' and 'limit'."""
        features = ["f1", "f2", "f3"]
        arts = _make_stub_artifacts(feature_list=features)
        big_batch = [
            {f: 0 for f in features} | {"bet_id": i}
            for i in range(api_server._MAX_SCORE_ROWS + 1)
        ]
        with patch.object(api_server, "_get_artifacts", return_value=arts):
            resp = api_server.app.test_client().post(
                "/score",
                data=json.dumps(_score_payload(big_batch)),
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 422)
        data = resp.get_json()
        self.assertIn("error", data)
        self.assertIn("limit", data)
        self.assertEqual(data["limit"], api_server._MAX_SCORE_ROWS)


if __name__ == "__main__":
    unittest.main()
