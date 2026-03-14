# Re-export: make trainer.backtester resolve to the implementation module (PLAN 項目 2.2 training).
# When run as __main__ (e.g. python -m trainer.backtester), forward to the implementation's main().
import sys
from trainer.training import backtester as _impl  # noqa: F401

sys.modules["trainer.backtester"] = _impl
main = _impl.main

if __name__ == "__main__":
    _impl.main()
