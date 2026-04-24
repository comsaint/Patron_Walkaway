from __future__ import annotations

import os
from pathlib import Path
from typing import Tuple

"""Internal env / path / prediction-export config shard."""

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def bootstrap_dotenv(load_dotenv_func: object, logger: object) -> None:
    """Load dotenv files using the historical config import contract."""
    try:
        load_dotenv = load_dotenv_func  # local alias for readability
        _env_credential = _REPO_ROOT / "credential" / ".env"
        if _env_credential.is_file():
            load_dotenv(str(_env_credential), override=False)
        load_dotenv(_REPO_ROOT / ".env", override=False)
        load_dotenv(override=False)
    except Exception as e:  # pragma: no cover - behavior validated via config import tests
        logger.warning("could not load .env (credential/repo/cwd): %s", type(e).__name__)


NUMEXPR_MAX_THREADS = 12

DEFAULT_MODEL_DIR: Path = _REPO_ROOT / "out" / "models"
DEFAULT_BACKTEST_OUT: Path = _REPO_ROOT / "out" / "backtest"

PREDICTION_LOG_DB_PATH: str = os.getenv(
    "PREDICTION_LOG_DB_PATH",
    str(_REPO_ROOT / "local_state" / "prediction_log.db"),
)
PREDICTION_EXPORT_SAFETY_LAG_MINUTES: int = int(
    os.getenv("PREDICTION_EXPORT_SAFETY_LAG_MINUTES", "5"),
)
PREDICTION_EXPORT_BATCH_ROWS: int = int(
    os.getenv("PREDICTION_EXPORT_BATCH_ROWS", "10000"),
)
PREDICTION_LOG_RETENTION_DAYS: int = int(
    os.getenv("PREDICTION_LOG_RETENTION_DAYS", "30"),
)
PREDICTION_LOG_RETENTION_DELETE_BATCH: int = int(
    os.getenv("PREDICTION_LOG_RETENTION_DELETE_BATCH", "5000"),
)
PREDICTION_LOG_SUMMARY_WINDOW_MINUTES: int = int(
    os.getenv("PREDICTION_LOG_SUMMARY_WINDOW_MINUTES", "60"),
)
PREDICTION_LOG_BET_SIZE_EDGES_HKD: Tuple[float, ...] = (
    0.0,
    100.0,
    500.0,
    2000.0,
    10000.0,
)

