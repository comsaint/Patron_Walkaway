# Re-export: make trainer.time_fold resolve to the implementation module (PLAN 項目 2.2 training).
# When loaded as bare "time_fold" (trainer/ on sys.path), expose API for test_time_fold_risks.
import sys
from trainer.training import time_fold as _impl  # noqa: F401

sys.modules["trainer.time_fold"] = _impl
get_monthly_chunks = _impl.get_monthly_chunks
get_single_window_chunk = _impl.get_single_window_chunk
partition_windows_for_train_end_cutoff = _impl.partition_windows_for_train_end_cutoff
