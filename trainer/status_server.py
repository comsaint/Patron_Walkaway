# Re-export: make trainer.status_server resolve to the implementation (PLAN 項目 2.2 serving).
# When run as __main__ (e.g. python -m trainer.status_server), forward to the implementation's main().
import sys
from trainer.serving import status_server as _impl  # noqa: F401

sys.modules["trainer.status_server"] = _impl

# Type-checker visible re-exports
main = _impl.main
STATE_DB_PATH = _impl.STATE_DB_PATH

if __name__ == "__main__":
    _impl.main()
