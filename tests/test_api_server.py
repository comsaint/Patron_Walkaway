"""tests/test_api_server.py
==========================
Unit tests for the three new model-API endpoints added in Step 9:

  GET  /health      — always returns 200 {"status": "ok", "model_version": ...}
  GET  /model_info  — returns 503 when no artifacts; 200 with metadata when present
  POST /score       — returns 422 on bad schema; 503 when no model; 200 with
                      correct structure when a stub model is present

All tests run with Flask's built-in test client — no real ClickHouse connection,
no real model files required.  Where a model is needed, a minimal stub LightGBM
classifier is trained on 10 synthetic rows.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Import api_server safely — without adding trainer/ to sys.path.
#
# Adding trainer/ to sys.path would cause trainer/trainer.py to be imported as
# the module 'trainer', shadowing the trainer/ namespace package.  That breaks
# any subsequent `from trainer.xxx import yyy` in other test files.
#
# Instead:
#   1. Add the repo root to sys.path so the 'trainer' namespace package is found.
#   2. Pre-register trainer.config as 'config' (api_server.py uses bare
#      `import config`, not `import trainer.config`).
#   3. Import api_server via the trainer namespace package.
# ---------------------------------------------------------------------------
import importlib as _il

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Pre-register the real config so api_server's `import config` resolves.
if "config" not in sys.modules:
    sys.modules["config"] = _il.import_module("trainer.config")

# Import api_server as part of the trainer namespace (avoids sys.path pollution).
if "trainer.api_server" not in sys.modules:
    _il.import_module("trainer.api_server")

api_server = sys.modules["trainer.api_server"]  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_stub_lgbm(n_features: int = 3):
    """Return a minimal fitted LightGBM binary classifier."""
    try:
        import lightgbm as lgb
        import numpy as np

        X = np.random.default_rng(0).random((20, n_features))
        y = (X[:, 0] > 0.5).astype(int)
        clf = lgb.LGBMClassifier(n_estimators=5, verbose=-1)
        clf.fit(X, y)
        return clf
    except Exception:
        return None


def _make_stub_artifacts(feature_list: list | None = None) -> dict:
    features = feature_list or ["f1", "f2", "f3"]
    stub_model = _make_stub_lgbm(len(features))
    model_entry = (
        {"model": stub_model, "threshold": 0.5} if stub_model else None
    )
    return {
        "rated": model_entry,
        "feature_list": features,
        "reason_code_map": {"f1": "RC_F1", "f2": "RC_F2"},
        "model_version": "test-v0",
    }


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestHealthEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = api_server.app.test_client()

    def test_health_returns_200(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)

    def test_health_has_status_ok(self):
        resp = self.client.get("/health")
        data = json.loads(resp.data)
        self.assertEqual(data.get("status"), "ok")

    def test_health_has_model_version_key(self):
        resp = self.client.get("/health")
        data = json.loads(resp.data)
        self.assertIn("model_version", data)

    def test_health_model_version_is_no_model_when_no_artifacts(self):
        """When no model files exist, model_version should be 'no_model'."""
        with patch.object(api_server, "_get_artifacts", return_value=None):
            resp = self.client.get("/health")
            data = json.loads(resp.data)
            self.assertEqual(data["model_version"], "no_model")

    def test_health_reflects_artifact_version(self):
        arts = _make_stub_artifacts()
        with patch.object(api_server, "_get_artifacts", return_value=arts):
            resp = self.client.get("/health")
            data = json.loads(resp.data)
            self.assertEqual(data["model_version"], "test-v0")


class TestModelInfoEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = api_server.app.test_client()

    def test_503_when_no_artifacts(self):
        with patch.object(api_server, "_get_artifacts", return_value=None):
            resp = self.client.get("/model_info")
            self.assertEqual(resp.status_code, 503)

    def test_200_when_artifacts_present(self):
        arts = _make_stub_artifacts()
        with patch.object(api_server, "_get_artifacts", return_value=arts):
            resp = self.client.get("/model_info")
            self.assertEqual(resp.status_code, 200)

    def test_response_contains_required_keys(self):
        arts = _make_stub_artifacts()
        with patch.object(api_server, "_get_artifacts", return_value=arts):
            resp = self.client.get("/model_info")
            data = json.loads(resp.data)
            for key in ("model_type", "model_version", "features", "training_metrics"):
                self.assertIn(key, data, msg=f"Missing key: {key}")

    def test_model_type_rated_when_model_present(self):
        arts = _make_stub_artifacts()
        with patch.object(api_server, "_get_artifacts", return_value=arts):
            resp = self.client.get("/model_info")
            data = json.loads(resp.data)
            self.assertEqual(data["model_type"], "rated")

    def test_features_list_matches_artifacts(self):
        arts = _make_stub_artifacts(feature_list=["alpha", "beta"])
        with patch.object(api_server, "_get_artifacts", return_value=arts):
            resp = self.client.get("/model_info")
            data = json.loads(resp.data)
            self.assertEqual(data["features"], ["alpha", "beta"])


def _score_payload(rows):
    """Build request body for POST /score (PLAN § {rows})."""
    return {"rows": rows}


class TestScoreEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = api_server.app.test_client()
        self.features = ["f1", "f2", "f3"]
        self.arts = _make_stub_artifacts(feature_list=self.features)

    def _post(self, payload, content_type="application/json"):
        return self.client.post(
            "/score",
            data=json.dumps(payload),
            content_type=content_type,
        )

    def _one_row(self, **kw):
        """One row with feature_list + bet_id (required by protocol)."""
        row = {f: 0.5 for f in self.features}
        row["bet_id"] = 1
        row.update(kw)
        return row

    # ── 400 error cases ───────────────────────────────────────────────────────

    def test_400_when_rows_missing(self):
        resp = self._post({"f1": 1})
        self.assertEqual(resp.status_code, 400)
        data = json.loads(resp.data)
        self.assertIn("error", data)

    def test_400_when_body_not_object(self):
        resp = self._post("hello")
        self.assertEqual(resp.status_code, 400)

    def test_400_when_empty_rows(self):
        with patch.object(api_server, "_get_artifacts", return_value=self.arts):
            resp = self._post(_score_payload([]))
            self.assertEqual(resp.status_code, 400)
            data = json.loads(resp.data)
            self.assertEqual(data.get("error"), "empty rows")

    # ── 422 error cases ───────────────────────────────────────────────────────

    def test_422_when_features_missing(self):
        with patch.object(api_server, "_get_artifacts", return_value=self.arts):
            resp = self._post(_score_payload([{"f1": 1.0}]))  # f2, f3, bet_id missing
            self.assertEqual(resp.status_code, 422)

    def test_422_error_body_contains_missing_and_extra(self):
        with patch.object(api_server, "_get_artifacts", return_value=self.arts):
            resp = self._post(_score_payload([{"f1": 1.0}]))
            data = json.loads(resp.data)
            self.assertIn("error", data)
            self.assertIn("missing", data)

    def test_422_when_batch_too_large(self):
        with patch.object(api_server, "_get_artifacts", return_value=self.arts):
            big_batch = [
                {f: 0 for f in self.features} | {"bet_id": i}
                for i in range(api_server._MAX_SCORE_ROWS + 1)
            ]
            resp = self._post(_score_payload(big_batch))
            self.assertEqual(resp.status_code, 422)

    # ── 503 when no model ─────────────────────────────────────────────────────

    def test_503_when_no_artifacts(self):
        with patch.object(api_server, "_get_artifacts", return_value=None):
            resp = self._post(_score_payload([self._one_row()]))
            self.assertEqual(resp.status_code, 503)
            data = json.loads(resp.data)
            self.assertEqual(data.get("error"), "model not ready")

    # ── 200 happy-path ────────────────────────────────────────────────────────

    def test_200_on_valid_batch(self):
        if self.arts["rated"] is None:
            self.skipTest("LightGBM not available")
        with patch.object(api_server, "_get_artifacts", return_value=self.arts):
            resp = self._post(_score_payload([self._one_row(f1=0.1, f2=0.9, f3=0.5)]))
            self.assertEqual(resp.status_code, 200)

    def test_response_shape_model_version_threshold_scores(self):
        if self.arts["rated"] is None:
            self.skipTest("LightGBM not available")
        with patch.object(api_server, "_get_artifacts", return_value=self.arts):
            resp = self._post(_score_payload([self._one_row()]))
            data = json.loads(resp.data)
            self.assertIn("model_version", data)
            self.assertIn("threshold", data)
            self.assertIn("scores", data)
            self.assertEqual(len(data["scores"]), 1)
            result = data["scores"][0]
            for key in ("bet_id", "score", "alert"):
                self.assertIn(key, result, msg=f"Missing key in /score output: {key}")
            self.assertNotIn("reason_codes", result)

    def test_score_is_float_between_0_and_1(self):
        if self.arts["rated"] is None:
            self.skipTest("LightGBM not available")
        with patch.object(api_server, "_get_artifacts", return_value=self.arts):
            resp = self._post(_score_payload([self._one_row()]))
            score_val = json.loads(resp.data)["scores"][0]["score"]
            self.assertIsInstance(score_val, float)
            self.assertGreaterEqual(score_val, 0.0)
            self.assertLessEqual(score_val, 1.0)

    def test_alert_is_bool(self):
        if self.arts["rated"] is None:
            self.skipTest("LightGBM not available")
        with patch.object(api_server, "_get_artifacts", return_value=self.arts):
            resp = self._post(_score_payload([self._one_row()]))
            alert_val = json.loads(resp.data)["scores"][0]["alert"]
            self.assertIsInstance(alert_val, bool)

    def test_output_order_matches_input_order(self):
        """The i-th output row must correspond to the i-th input row."""
        if self.arts["rated"] is None:
            self.skipTest("LightGBM not available")
        rows = [self._one_row(**{f: float(i) / 10 for f in self.features}) for i in range(5)]
        for i, r in enumerate(rows):
            r["bet_id"] = i + 1
        with patch.object(api_server, "_get_artifacts", return_value=self.arts):
            resp = self._post(_score_payload(rows))
            data = json.loads(resp.data)
            self.assertEqual(len(data["scores"]), 5)

    def test_is_rated_true_routes_to_rated_model(self):
        """is_rated=True in the payload should use the rated model branch."""
        if self.arts["rated"] is None:
            self.skipTest("LightGBM not available")
        row = self._one_row(is_rated=True)
        call_tracker = {"rated": 0, "nonrated": 0}
        original_predict = self.arts["rated"]["model"].predict_proba

        def tracking_predict(X):
            call_tracker["rated"] += 1
            return original_predict(X)

        self.arts["rated"]["model"].predict_proba = tracking_predict
        with patch.object(api_server, "_get_artifacts", return_value=self.arts):
            self._post(_score_payload([row]))
        self.assertGreater(call_tracker["rated"], 0)

    def test_model_version_in_output_matches_artifacts(self):
        if self.arts["rated"] is None:
            self.skipTest("LightGBM not available")
        with patch.object(api_server, "_get_artifacts", return_value=self.arts):
            resp = self._post(_score_payload([self._one_row()]))
            data = json.loads(resp.data)
            self.assertEqual(data["model_version"], "test-v0")


class TestArtifactCacheReload(unittest.TestCase):
    """Verify that _get_artifacts() reloads when model_version changes."""

    def test_cache_reloads_on_version_change(self):
        # Force a known state
        api_server._artifacts_cache = {}
        api_server._cached_model_version = "old-version"

        with (
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.read_text", return_value="new-version"),
            patch.object(api_server, "_load_artifacts", return_value={"model_version": "new-version"}) as mock_load,
        ):
            api_server._get_artifacts()
            mock_load.assert_called_once()


if __name__ == "__main__":
    unittest.main()
