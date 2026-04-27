from __future__ import annotations

from pathlib import Path

"""Early dotenv loader: no ``os.getenv`` / env-derived constants.

``trainer.core.config`` calls :func:`bootstrap_dotenv` before importing shards
that bind ``CH_*`` and other settings from the environment.
"""

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def bootstrap_dotenv(load_dotenv_func: object, logger: object) -> None:
    """Load dotenv files using the historical config import contract.

    Loads ``credential/.env``, repo ``.env``, then cwd (via ``load_dotenv()``).
    Uses ``override=False`` so existing process environment wins.

    Args:
        load_dotenv_func: ``dotenv.load_dotenv`` or compatible callable.
        logger: Object with a ``warning`` method for load failures.
    """
    try:
        load_dotenv = load_dotenv_func  # local alias for readability
        _env_credential = _REPO_ROOT / "credential" / ".env"
        if _env_credential.is_file():
            load_dotenv(str(_env_credential), override=False)
        load_dotenv(_REPO_ROOT / ".env", override=False)
        load_dotenv(override=False)
    except Exception as e:  # pragma: no cover - behavior validated via config import tests
        logger.warning("could not load .env (credential/repo/cwd): %s", type(e).__name__)
