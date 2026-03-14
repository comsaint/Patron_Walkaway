# Re-export: make trainer.api_server resolve to the implementation (PLAN 項目 2.2 serving).
# When run as __main__ (e.g. python -m trainer.api_server), forward to the implementation's main().
import sys
from trainer.serving import api_server as _impl  # noqa: F401

sys.modules["trainer.api_server"] = _impl

# Type-checker visible re-exports
app = _impl.app
STATE_DB_PATH = _impl.STATE_DB_PATH
BASE_DIR = _impl.BASE_DIR


def run() -> None:
    """Entry point for console_scripts (e.g. walkaway-api). Uses ML_API_PORT, default 8001."""
    import os
    port = int(os.environ.get("ML_API_PORT", "8001"))
    _impl.app.run(host="0.0.0.0", port=port, debug=True)


if __name__ == "__main__":
    run()
