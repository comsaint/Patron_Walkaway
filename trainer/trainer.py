# Re-export: make trainer.trainer resolve to the implementation module (PLAN 項目 2.2 training).
# When run as __main__ (e.g. python -m trainer.trainer), forward to the implementation's main().
# Re-exports below for type checker; runtime resolution is via sys.modules overwrite.
import sys
from trainer.training import trainer as _impl  # noqa: F401

sys.modules["trainer.trainer"] = _impl

# Type-checker visible re-exports (STATUS Code Review 2.2 training §2; backtester + scripts use these)
MODEL_DIR = _impl.MODEL_DIR
CHUNK_DIR = _impl.CHUNK_DIR
LOCAL_PARQUET_DIR = _impl.LOCAL_PARQUET_DIR
HISTORY_BUFFER_DAYS = _impl.HISTORY_BUFFER_DAYS
load_clickhouse_data = _impl.load_clickhouse_data
load_local_parquet = _impl.load_local_parquet
apply_dq = _impl.apply_dq
add_track_human_features = _impl.add_track_human_features
compute_track_llm_features = _impl.compute_track_llm_features
load_feature_spec = _impl.load_feature_spec
load_player_profile = _impl.load_player_profile
join_player_profile = _impl.join_player_profile
_to_hk = _impl._to_hk
main = _impl.main

if __name__ == "__main__":
    _impl.main()
