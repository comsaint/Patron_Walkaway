# Re-export: make trainer.validator resolve to the implementation (PLAN 項目 2.2 serving).
# When run as __main__ (e.g. python -m trainer.validator), forward to the implementation's main().
import sys
from trainer.serving import validator as _impl  # noqa: F401

sys.modules["trainer.validator"] = _impl

# Type-checker visible re-exports
main = _impl.main
STATE_DB_PATH = _impl.STATE_DB_PATH
OUT_DIR = _impl.OUT_DIR

if __name__ == "__main__":
    _impl.main()
