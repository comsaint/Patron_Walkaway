"""Round 232 Code Review — api_server steps 1–2 risk points as tests.

STATUS.md Round 232 Code Review: convert reviewer risk points to minimal
reproducible tests only. No production code changes.

Reference: PLAN § api_server 對齊 model_api_protocol, STATUS Round 232 Review.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Pre-register config so api_server's bare `import config` resolves.
if "config" not in sys.modules:
    import importlib as _il
    sys.modules["config"] = _il.import_module("trainer.config")
if "trainer.api_server" not in sys.modules:
    import importlib as _il
    _il.import_module("trainer.api_server")

import trainer.api_server as api_server  # noqa: E402


def _stub_artifacts(training_metrics=None):
    """Minimal artifacts dict for /model_info and /health tests."""
    arts = {
        "rated": None,
        "feature_list": ["f1", "f2"],
        "reason_code_map": {},
        "model_version": "test-v0",
    }
    if training_metrics is not None:
        arts["training_metrics"] = training_metrics
    return arts


class TestR232_1_TrainingMetricsTypeContract(unittest.TestCase):
    """Review #1: training_metrics in /model_info should be a dict for downstream safety."""

    def test_model_info_training_metrics_is_dict_when_arts_has_dict(self):
        """When arts[\"training_metrics\"] is a dict, response[\"training_metrics\"] is a dict."""
        arts = _stub_artifacts(training_metrics={"test_ap": 0.5, "test_f1": 0.3})
        with patch.object(api_server, "_get_artifacts", return_value=arts):
            resp = api_server.app.test_client().get("/model_info")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn("training_metrics", data)
        self.assertIsInstance(
            data["training_metrics"],
            dict,
            "Downstream may assume training_metrics is a dict (PLAN § model_info).",
        )

    def test_model_info_training_metrics_becomes_dict_when_arts_has_list(self):
        """When arts[\"training_metrics\"] is [] (e.g. file was \"[]\"), response uses {} (falsy → or {})."""
        arts = _stub_artifacts(training_metrics=[])
        with patch.object(api_server, "_get_artifacts", return_value=arts):
            resp = api_server.app.test_client().get("/model_info")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIsInstance(data["training_metrics"], dict)
        self.assertEqual(data["training_metrics"], {})

    def test_model_info_training_metrics_is_dict_when_arts_has_string(self):
        """When arts[\"training_metrics\"] is truthy non-dict (e.g. string), response coerces to {}."""
        arts = _stub_artifacts(training_metrics="some string")
        with patch.object(api_server, "_get_artifacts", return_value=arts):
            resp = api_server.app.test_client().get("/model_info")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIsInstance(data["training_metrics"], dict)
        self.assertEqual(data["training_metrics"], {})


class TestR232_8_HealthContractModelLoaded(unittest.TestCase):
    """Review #8: GET /health must include model_loaded and it must be bool."""

    def test_health_response_has_required_keys_including_model_loaded(self):
        """Response must contain status, model_version, model_loaded (PLAN § GET /health)."""
        client = api_server.app.test_client()
        resp = client.get("/health")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        for key in ("status", "model_version", "model_loaded"):
            self.assertIn(key, data, msg=f"Missing key: {key}")

    def test_health_model_loaded_is_bool(self):
        """model_loaded must be a boolean."""
        client = api_server.app.test_client()
        resp = client.get("/health")
        data = json.loads(resp.data)
        self.assertIn("model_loaded", data)
        self.assertIsInstance(
            data["model_loaded"],
            bool,
            "model_loaded must be bool (PLAN § api_server 對齊 model_api_protocol).",
        )

    def test_health_model_loaded_false_when_no_artifacts(self):
        """When no artifacts, model_loaded should be false."""
        with patch.object(api_server, "_get_artifacts", return_value=None):
            resp = api_server.app.test_client().get("/health")
        data = json.loads(resp.data)
        self.assertFalse(data["model_loaded"])
        self.assertEqual(data["model_version"], "no_model")

    def test_health_model_loaded_true_when_rated_present(self):
        """When arts[\"rated\"] is present, model_loaded should be true."""
        arts = _stub_artifacts()
        arts["rated"] = {"model": None, "threshold": 0.5}
        with patch.object(api_server, "_get_artifacts", return_value=arts):
            resp = api_server.app.test_client().get("/health")
        data = json.loads(resp.data)
        self.assertTrue(data["model_loaded"])
        self.assertEqual(data["model_version"], "test-v0")


if __name__ == "__main__":
    unittest.main()
