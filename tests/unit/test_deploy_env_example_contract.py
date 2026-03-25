"""
Contract: package/deploy/.env.example documents deploy-relevant env vars so bundles
stay aligned with trainer/core/config.py and package/deploy/main.py (no silent drift).
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEPLOY_ENV_EXAMPLE = _REPO_ROOT / "package" / "deploy" / ".env.example"


def test_deploy_env_example_exists_and_covers_key_vars() -> None:
    assert _DEPLOY_ENV_EXAMPLE.is_file(), f"Missing {_DEPLOY_ENV_EXAMPLE}"
    text = _DEPLOY_ENV_EXAMPLE.read_text(encoding="utf-8")
    for token in (
        "CH_USER",
        "CH_PASS",
        "DEPLOY_LOG_LEVEL",
        "LOGLEVEL",
        "SCORER_COLD_START_WINDOW_HOURS",
        "SCORER_LOOKBACK_HOURS",
        "PREDICTION_LOG_DB_PATH",
        "STATE_DB_PATH",
        "MODEL_DIR",
        "SCORER_ENABLE_SHAP_REASON_CODES",
        "PORT",
        "ML_API_PORT",
        "trainer/core/config.py",
    ):
        assert token in text, f".env.example should mention {token!r} for deploy operators"


def test_deploy_main_defaults_prediction_log_before_config_import() -> None:
    """Bundle main.py must default PREDICTION_LOG_DB_PATH next to state before walkaway_ml.config import."""
    main_py = _REPO_ROOT / "package" / "deploy" / "main.py"
    src = main_py.read_text(encoding="utf-8")
    head, _, _ = src.partition("from walkaway_ml import config")
    head_n = head.replace("\r\n", "\n")
    assert "PREDICTION_LOG_DB_PATH" in head_n, "main.py should mention PREDICTION_LOG_DB_PATH before config import"
    assert 'os.environ.setdefault(\n    "PREDICTION_LOG_DB_PATH"' in head_n, (
        "main.py should os.environ.setdefault PREDICTION_LOG_DB_PATH before config import"
    )
