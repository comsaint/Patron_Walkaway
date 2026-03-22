# Re-export stub (PLAN 項目 2.2 training). __main__ parses CLI before loading implementation.
import sys

if __name__ == "__main__":
    from trainer.training.trainer_argparse import build_trainer_argparser

    _args = build_trainer_argparser().parse_args()
    from trainer.training import trainer as _impl

    sys.modules["trainer.trainer"] = _impl
    _impl.run_pipeline(_args)
else:
    from trainer.training import trainer as _impl

    sys.modules["trainer.trainer"] = _impl
    MODEL_DIR, CHUNK_DIR, LOCAL_PARQUET_DIR, HISTORY_BUFFER_DAYS, load_clickhouse_data, load_local_parquet, apply_dq, add_track_human_features, compute_track_llm_features, load_feature_spec, load_player_profile, join_player_profile, _to_hk, main = _impl.MODEL_DIR, _impl.CHUNK_DIR, _impl.LOCAL_PARQUET_DIR, _impl.HISTORY_BUFFER_DAYS, _impl.load_clickhouse_data, _impl.load_local_parquet, _impl.apply_dq, _impl.add_track_human_features, _impl.compute_track_llm_features, _impl.load_feature_spec, _impl.load_player_profile, _impl.join_player_profile, _impl._to_hk, _impl.main  # noqa: E501
