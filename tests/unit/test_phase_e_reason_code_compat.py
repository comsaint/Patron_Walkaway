"""Phase E gate tests: SHAP reason-code removal + compat guarantees.

Locks four contracts established by the Phase E working plan
(``c:/Users/longp/.cursor/plans/phase_e_working_plan_*.plan.md``):

* Gate-E1: ``reason_code_map.json`` is **not** a hard dependency for any path.
* Gate-E2: ``reason_codes`` column / API field stays a parseable JSON list
  (canonical empty = ``"[]"``) when SHAP is disabled.
* Gate-E3: admission ``skip_reason_code`` semantics are untouched.
* Gate-E4: SHAP-off path short-circuits with no SHAP / numpy work.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd
import pytest


class _PicklableStubModel:
    """Module-level stub so ``joblib.dump`` / pickle can serialise it."""

    supports_shap_reason_codes = False


# ---------------------------------------------------------------------------
# Gate-E1 / E2: canonical empty value
# ---------------------------------------------------------------------------


def test_canonical_empty_constant_is_json_empty_list() -> None:
    """``SCORER_REASON_CODES_DEFAULT_EMPTY`` must round-trip JSON to []."""
    from trainer.core import config

    assert config.SCORER_REASON_CODES_DEFAULT_EMPTY == "[]"
    assert json.loads(config.SCORER_REASON_CODES_DEFAULT_EMPTY) == []


def test_compute_reason_codes_short_circuits_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase E Gate-E4: SHAP gate off => no SHAP import, returns canonical empty list per row."""
    from trainer.core import config
    from trainer.serving import scorer

    monkeypatch.setattr(config, "SCORER_ENABLE_SHAP_REASON_CODES", False, raising=False)

    class _BoomModel:
        supports_shap_reason_codes = True

        def predict(self, *_: Any, **__: Any) -> Any:
            raise AssertionError("model.predict must NOT be called when SHAP gate is off")

    df = pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]})
    out = scorer._compute_reason_codes(_BoomModel(), df, reason_code_map={})
    assert out == ["[]", "[]", "[]"]


def test_compute_reason_codes_short_circuits_when_model_disables(monkeypatch: pytest.MonkeyPatch) -> None:
    """SHAP gate on but ``supports_shap_reason_codes=False`` => still empty list."""
    from trainer.core import config
    from trainer.serving import scorer

    monkeypatch.setattr(config, "SCORER_ENABLE_SHAP_REASON_CODES", True, raising=False)

    class _Model:
        supports_shap_reason_codes = False

    df = pd.DataFrame({"a": [1, 2]})
    out = scorer._compute_reason_codes(_Model(), df, reason_code_map={})
    assert out == ["[]", "[]"]


def test_load_dual_artifacts_handles_missing_reason_code_map(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Gate-E1: ``load_dual_artifacts`` returns empty ``reason_code_map`` and logs the policy line.

    A minimal ``model.pkl`` is written so the loader proceeds past the strict
    artifact checks; the test focuses on the reason-code-map branch only.
    """
    import joblib

    from trainer.serving import scorer

    (tmp_path / "feature_list.json").write_text("[]", encoding="utf-8")
    (tmp_path / "model_version").write_text("v_phase_e_test", encoding="utf-8")

    joblib.dump(
        {
            "model": _PicklableStubModel(),
            "threshold": 0.5,
            "features": [],
            "model_kind": "stub",
        },
        tmp_path / "model.pkl",
    )

    with caplog.at_level(logging.INFO, logger=scorer.logger.name):
        artifacts = scorer.load_dual_artifacts(tmp_path)

    assert artifacts["reason_code_map"] == {}
    assert artifacts["model_version"] == "v_phase_e_test"
    assert any(
        "reason_code_map.json not present" in rec.message for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# Gate-E1: deploy bundle copy treats reason_code_map.json as optional
# ---------------------------------------------------------------------------


def test_optional_bundle_files_includes_reason_code_map() -> None:
    from package import build_deploy_package as bdp

    assert "reason_code_map.json" in bdp.OPTIONAL_BUNDLE_FILES


def test_copy_model_bundle_succeeds_without_reason_code_map(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from package import build_deploy_package as bdp

    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
    (src / "model.pkl").write_bytes(b"\x80\x04N.")  # minimal bytes; not loaded here
    (src / "feature_list.json").write_text("[]", encoding="utf-8")
    (src / "feature_spec.yaml").write_text("track_llm: {}\n", encoding="utf-8")
    (src / "model_version").write_text("v", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger=bdp.logger.name):
        bdp.copy_model_bundle(src, dest)

    assert (dest / "model.pkl").exists()
    assert not (dest / "reason_code_map.json").exists()
    assert any("reason_code_map.json" in rec.message for rec in caplog.records), (
        "Phase E expects an explicit warning when reason_code_map.json is omitted"
    )


# ---------------------------------------------------------------------------
# Gate-E3: admission contract unchanged
# ---------------------------------------------------------------------------


def test_admission_skip_reason_code_distinct_from_shap_reason_codes() -> None:
    """Re-affirm Gate-C4 invariant that Phase E must not regress."""
    from trainer.features import layered as L

    assert "reason_codes" not in L.VALID_SKIP_REASON_CODES
