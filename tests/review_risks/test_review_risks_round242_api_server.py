"""Round 242 Code Review — api_server 風險點轉成最小可重現測試。

STATUS.md Round 242 Code Review: convert reviewer risk points to minimal
reproducible tests only. No production code changes.

Reference: PLAN § api_server 對齊 model_api_protocol, STATUS Round 242 Review.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(reason="api_server reverted to DB-only; model API removed")

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

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

from tests.integration.test_api_server import _make_stub_artifacts, _score_payload  # noqa: E402


def _one_row(features: list, **kw):
    """One row with feature_list + bet_id (required by protocol)."""
    row = {f: 0.5 for f in features}
    row["bet_id"] = 1
    row.update(kw)
    return row


# ---------------------------------------------------------------------------
# R242 #1 / #10 — GET /model_info 應含 threshold、feature_count（PLAN §4, protocol §3.3）
# ---------------------------------------------------------------------------

class TestR242_1_ModelInfoThresholdAndFeatureCount(unittest.TestCase):
    """Review #1/#10: 鎖定 GET /model_info 契約 — 目前未回傳 threshold/feature_count（PLAN §4 規定應有）。"""

    def test_model_info_200_response_keys_locked(self):
        """Lock contract: GET /model_info 200 時目前回傳 model_type, model_version, features, training_metrics.
        Review #1/#10: PLAN §4 規定應含 threshold、feature_count；實作補上後請改斷言為 assertIn('threshold', data) 等。"""
        arts = _make_stub_artifacts(feature_list=["f1", "f2", "f3"])
        with patch.object(api_server, "_get_artifacts", return_value=arts):
            resp = api_server.app.test_client().get("/model_info")
        self.assertEqual(resp.status_code, 200, "Stub artifacts present => 200")
        data = resp.get_json()
        for key in ("model_type", "model_version", "features", "training_metrics"):
            self.assertIn(key, data, "model_info must include %s" % key)
        self.assertEqual(len(data["features"]), 3)
        # Current implementation does NOT return threshold / feature_count (Review #1/#10 gap)
        self.assertNotIn("threshold", data, "Current: no threshold; add assertIn when production adds it")
        self.assertNotIn("feature_count", data, "Current: no feature_count; add assertIn when production adds it")


# ---------------------------------------------------------------------------
# R242 #2 — 特徵值為 NumPy 純量時應接受（不 422）
# ---------------------------------------------------------------------------

class TestR242_2_ScoreAcceptsNumpyScalars(unittest.TestCase):
    """Review #2: 鎖定行為 — 特徵值為 NumPy 純量時目前回 422（預期應接受並回 200）。"""

    def test_score_numpy_scalar_in_feature_returns_422_currently(self):
        """Lock behavior: when a feature value is np.int64/np.float64, server currently returns 422.
        Review #2: production should accept numpy scalars and return 200; then change to assertEqual(resp.status_code, 200)."""
        arts = _make_stub_artifacts(feature_list=["f1", "f2", "f3"])
        payload = {
            "rows": [
                {
                    "f1": np.int64(1),
                    "f2": np.float64(0.5),
                    "f3": 0.5,
                    "bet_id": 1,
                }
            ]
        }
        from flask.wrappers import Request
        client = api_server.app.test_client()
        with patch.object(api_server, "_get_artifacts", return_value=arts), patch.object(
            Request, "get_json", return_value=payload
        ):
            resp = client.post("/score", data="{}", content_type="application/json")
        self.assertEqual(resp.status_code, 422, "Current: NumPy scalars rejected with 422 (Review #2)")
        data = resp.get_json()
        self.assertEqual(data.get("error"), "invalid feature types")


# ---------------------------------------------------------------------------
# R242 #3 — bet_id 非 int 時目前為 echo 原樣（文件化行為）
# ---------------------------------------------------------------------------

class TestR242_3_BetIdTypeEchoBehavior(unittest.TestCase):
    """Review #3 (optional): Document that service echoes bet_id as-is when not int."""

    def test_score_echoes_bet_id_as_sent_when_non_int(self):
        """When bet_id is a string, response scores[0] echoes it (document current behavior)."""
        arts = _make_stub_artifacts(feature_list=["f1", "f2", "f3"])
        if arts["rated"] is None:
            self.skipTest("LightGBM not available")
        row = _one_row(["f1", "f2", "f3"], bet_id="abc")
        with patch.object(api_server, "_get_artifacts", return_value=arts):
            resp = api_server.app.test_client().post(
                "/score",
                data=json.dumps(_score_payload([row])),
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["scores"][0]["bet_id"], "abc", "Service echoes bet_id as-is (documented behavior)")


# ---------------------------------------------------------------------------
# R242 #4 — feature_list 含重複鍵時 required 無法由單一 dict 滿足（文件化）
# ---------------------------------------------------------------------------

class TestR242_4_DuplicateFeatureListMissingBehavior(unittest.TestCase):
    """Review #4 (optional): feature_list 含重複鍵時，production 可能 422（missing）或 500（DataFrame 錯誤）。"""

    def test_score_duplicate_feature_list_either_422_or_500(self):
        """feature_list=['f1','f1','f2'] with row {'f1':0,'f2':0,'bet_id':1}: current code can 500 on df[feature_list].
        Accept either 422 (missing) or 500 to document the risk until production dedupes feature_list."""
        arts = _make_stub_artifacts(feature_list=["f1", "f1", "f2"])
        row = {"f1": 0, "f2": 0, "bet_id": 1}
        with patch.object(api_server, "_get_artifacts", return_value=arts):
            resp = api_server.app.test_client().post(
                "/score",
                data=json.dumps(_score_payload([row])),
                content_type="application/json",
            )
        self.assertIn(
            resp.status_code,
            (422, 500),
            "Duplicate feature_list must not yield 200 (either 422 missing or 500 DataFrame error)",
        )
        if resp.status_code == 422:
            data = resp.get_json()
            self.assertIn("error", data)


# ---------------------------------------------------------------------------
# R242 #5 — rows 中單筆非 dict 時應回 400/422 且 body 含可辨識 error
# ---------------------------------------------------------------------------

class TestR242_5_RowNotDictErrorBody(unittest.TestCase):
    """Review #5: When rows[i] is not a dict, status 400 or 422 and body has identifiable error."""

    def test_score_row_not_dict_returns_400_or_422_with_identifiable_error(self):
        """Body with second element string 'not-a-dict' => 400 or 422 and body contains 'error'."""
        features = ["f1", "f2", "f3"]
        arts = _make_stub_artifacts(feature_list=features)
        payload = {"rows": [_one_row(features), "not-a-dict"]}
        with patch.object(api_server, "_get_artifacts", return_value=arts):
            resp = api_server.app.test_client().post(
                "/score",
                data=json.dumps(payload),
                content_type="application/json",
            )
        self.assertIn(resp.status_code, (400, 422), "Non-dict row must yield 400 or 422")
        data = resp.get_json()
        self.assertIn("error", data, "Body must contain identifiable error description")


# ---------------------------------------------------------------------------
# R242 #7 — POST /score body 大小未限（期望逾限回 413，目前可能未實作）
# ---------------------------------------------------------------------------

class TestR242_7_OversizedBodyShouldReturn413(unittest.TestCase):
    """Review #7 (optional): 鎖定行為 — 目前未限制 body 大小，逾 10MB 仍回 200."""

    def test_score_oversized_body_currently_returns_200(self):
        """Lock behavior: request body > 10MB currently returns 200 (no MAX_CONTENT_LENGTH).
        Review #7: production should return 413 when over limit; then change to assertEqual(resp.status_code, 413)."""
        arts = _make_stub_artifacts(feature_list=["f1", "f2", "f3"])
        huge = "x" * (512 * 1024)  # 512KB per row
        row = _one_row(["f1", "f2", "f3"], session_id=huge)
        rows = [row] * 21  # ~10.5MB
        payload = _score_payload(rows)
        with patch.object(api_server, "_get_artifacts", return_value=arts):
            resp = api_server.app.test_client().post(
                "/score",
                data=json.dumps(payload),
                content_type="application/json",
            )
        self.assertEqual(
            resp.status_code,
            200,
            "Current: no body size limit (Review #7); when 413 is implemented, assert 413 here.",
        )


if __name__ == "__main__":
    unittest.main()
