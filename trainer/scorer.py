# Re-export: make trainer.scorer resolve to the implementation (PLAN 項目 2.2 serving).
# When run as __main__ (e.g. python -m trainer.scorer), forward to the implementation's main().
import sys
from trainer.serving import scorer as _impl  # noqa: F401

sys.modules["trainer.scorer"] = _impl

# Type-checker visible re-exports (tests and callers import these)
main = _impl.main
score_once = _impl.score_once
run_scorer_loop = _impl.run_scorer_loop
build_features_for_scoring = _impl.build_features_for_scoring
_score_df = _impl._score_df
STATE_DB_PATH = _impl.STATE_DB_PATH
MODEL_DIR = _impl.MODEL_DIR
FEATURE_SPEC_PATH = _impl.FEATURE_SPEC_PATH

if __name__ == "__main__":
    _impl.main()
