"""Step 7 (row-level split) runtime helpers extracted from ``trainer.training.trainer``.

DuckDB OOM detection, sort/split orchestration, and temp-directory cleanup are shared
by the in-pipeline path and keep-on-disk fallbacks.
"""
from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

import numpy as np
import pandas as pd

from trainer.core import config as _cfg
from trainer.training.common_runtime import DATA_DIR

logger = logging.getLogger("trainer")


@dataclass
class DuckdbStep7Runtime:
    """Holds DuckDB policy telemetry for Step 7 (replaces ``nonlocal`` in pipeline)."""

    memory_gb: Optional[float] = None
    threads: Optional[int] = None


def is_duckdb_oom(exc: BaseException) -> bool:
    """Return True if *exc* is DuckDB OOM, MemoryError, or a typical allocation message."""
    try:
        import duckdb as _duckdb

        oom_cls = getattr(_duckdb, "OutOfMemoryException", None)
        if oom_cls is not None and isinstance(exc, oom_cls):
            return True
    except ImportError:
        pass
    if isinstance(exc, MemoryError):
        return True
    msg = str(exc.args[0]) if getattr(exc, "args", None) and exc.args else str(exc)
    return "unable to allocate" in msg.lower() or "out of memory" in msg.lower()


def step7_clean_duckdb_temp_dir() -> None:
    """Remove Step 7 DuckDB temp directory when it is under ``DATA_DIR`` (whitelist)."""
    get_cfg = getattr(_cfg, "get_duckdb_memory_config", None)
    if get_cfg is not None:
        temp_dir_raw = get_cfg("step7")[6] or str(DATA_DIR / "duckdb_tmp")
    else:
        temp_dir_raw = getattr(_cfg, "STEP7_DUCKDB_TEMP_DIR", None) or str(DATA_DIR / "duckdb_tmp")
    if "'" in temp_dir_raw:
        effective = DATA_DIR / "duckdb_tmp"
    else:
        effective = Path(temp_dir_raw)
    data_dir_resolved = DATA_DIR.resolve()
    effective_resolved = effective.resolve()
    allowed_duckdb_tmp = (DATA_DIR / "duckdb_tmp").resolve()
    if effective_resolved != allowed_duckdb_tmp:
        try:
            effective_resolved.relative_to(data_dir_resolved)
        except ValueError:
            logger.warning(
                "Step 7: refusing to remove DuckDB temp directory outside DATA_DIR: %s",
                effective,
            )
            return
    if effective.exists() and effective.is_dir():
        try:
            shutil.rmtree(effective)
            logger.info("Step 7: cleaned DuckDB temp directory %s", effective)
        except OSError as _e:
            logger.warning("Step 7: could not remove DuckDB temp directory %s: %s", effective, _e)


def step7_splits_pid_dir() -> Path:
    """Per-process directory under ``DATA_DIR/step7_splits`` (same layout as in-pipeline)."""
    return DATA_DIR / "step7_splits" / str(os.getpid())

def get_step7_available_ram_bytes() -> Optional[int]:
    try:
        import psutil as _psutil
        return _psutil.virtual_memory().available
    except Exception:
        return None
        
def compute_step7_duckdb_budget(available_bytes: Optional[int]) -> int:
    """Compute DuckDB memory_limit (bytes) for Step 7 sort+split via shared policy."""
    _resolve_runtime = getattr(_cfg, "resolve_duckdb_runtime_policy", None)
    if callable(_resolve_runtime):
        return int(_resolve_runtime("step7", available_bytes)["memory_limit_bytes"])
    get_limit = getattr(_cfg, "get_duckdb_memory_limit_bytes", None)
    if get_limit is not None:
        return get_limit("step7", available_bytes)
    lo = int(getattr(_cfg, "DUCKDB_MEMORY_LIMIT_MIN_GB", 1.0) * 1024**3)
    if available_bytes is None:
        return lo
    frac = getattr(_cfg, "DUCKDB_RAM_FRACTION", 0.5)
    hi = int(getattr(_cfg, "DUCKDB_MEMORY_LIMIT_MAX_GB", 24.0) * 1024**3)
    return max(lo, min(hi, int(available_bytes * frac)))
        
def configure_step7_duckdb_runtime(con: Any, *, budget_bytes: int) -> None:
    """Set Step 7 DuckDB runtime via shared policy helper when available."""
    _resolve_runtime = getattr(_cfg, "resolve_duckdb_runtime_policy", None)
    _apply_runtime = getattr(_cfg, "apply_duckdb_runtime", None)
    if callable(_resolve_runtime) and callable(_apply_runtime):
        policy = _resolve_runtime("step7", get_step7_available_ram_bytes())
        policy["memory_limit_bytes"] = int(budget_bytes)
        _apply_runtime(con, policy)
        threads = int(policy["threads"])
        temp_dir = str(policy["temp_directory"])
        budget_gb = float(policy["memory_limit_bytes"]) / 1024**3
    else:
        get_cfg = getattr(_cfg, "get_duckdb_memory_config", None)
        if get_cfg is not None:
            _tup = get_cfg("step7")
            threads = max(1, int(_tup[4]))
            preserve_order = _tup[5]
            temp_dir_raw = _tup[6] or str(DATA_DIR / "duckdb_tmp")
        else:
            threads = max(1, int(getattr(_cfg, "STEP7_DUCKDB_THREADS", 4)))
            preserve_order = False
            temp_dir_raw = getattr(_cfg, "STEP7_DUCKDB_TEMP_DIR", None) or str(DATA_DIR / "duckdb_tmp")
        if "'" in temp_dir_raw:
            temp_dir = str(DATA_DIR / "duckdb_tmp")
            logger.warning("Step 7 DuckDB temp_directory contains single quote; using fallback %s", temp_dir)
        else:
            try:
                effective_resolved = Path(temp_dir_raw).resolve()
                data_dir_resolved = DATA_DIR.resolve()
                allowed_duckdb_tmp = (DATA_DIR / "duckdb_tmp").resolve()
                if effective_resolved != allowed_duckdb_tmp:
                    effective_resolved.relative_to(data_dir_resolved)
            except (ValueError, OSError):
                temp_dir = str(DATA_DIR / "duckdb_tmp")
                logger.warning(
                    "Step 7 DuckDB temp_directory outside DATA_DIR; using fallback %s",
                    temp_dir,
                )
            else:
                temp_dir = temp_dir_raw
        budget_gb = budget_bytes / 1024**3
        temp_dir_sql = temp_dir.replace("'", "''")
        for _stmt, _label in [
            (f"SET memory_limit='{budget_gb:.2f}GB'", "memory_limit"),
            (f"SET threads={threads}", "threads"),
            (f"SET temp_directory='{temp_dir_sql}'", "temp_directory"),
        ]:
            try:
                con.execute(_stmt)
            except Exception as exc:
                logger.warning("Step 7 DuckDB SET %s failed (non-fatal): %s", _label, exc)
        if not preserve_order:
            try:
                con.execute("SET preserve_insertion_order=false")
            except Exception as exc:
                logger.warning("Step 7 DuckDB SET preserve_insertion_order failed (non-fatal): %s", exc)
    logger.info(
        "Step 7 DuckDB runtime: memory_limit=%.2fGB  threads=%d  temp_directory=%s",
        budget_gb, threads, temp_dir,
    )
        
def _duckdb_sort_and_split(
    chunk_paths: List[Path],
    train_frac: float,
    valid_frac: float,
    *,
    duckdb_runtime: Optional["DuckdbStep7Runtime"] = None,
) -> Tuple[Path, Path, Path]:
    """Sort chunk Parquets by payout_complete_dtm, canonical_id, bet_id and split into train/valid/test Parquet files.
    Uses DuckDB out-of-core; returns (train_path, valid_path, test_path).
    Creates step7_splits and DuckDB temp directory (or fallback DATA_DIR/duckdb_tmp when config path contains single quote).
    DuckDB may remove its temp directory on close; caller should not assume it exists after return.
    """
    if not chunk_paths:
        raise ValueError("chunk_paths must be non-empty")
    if not (0 < train_frac and 0 < valid_frac and train_frac + valid_frac < 1.0):
        raise ValueError(
            "train_frac and valid_frac must be in (0, 1) and train_frac + valid_frac < 1"
        )
    import duckdb
    path_list = [str(p) for p in chunk_paths]
    step7_dir = DATA_DIR / "step7_splits" / str(os.getpid())
    step7_dir.mkdir(parents=True, exist_ok=True)
    train_path = step7_dir / "split_train.parquet"
    valid_path = step7_dir / "split_valid.parquet"
    test_path = step7_dir / "split_test.parquet"
    get_cfg = getattr(_cfg, "get_duckdb_memory_config", None)
    temp_dir_raw = (get_cfg("step7")[6] if get_cfg else None) or str(DATA_DIR / "duckdb_tmp")
    if "'" in temp_dir_raw:
        effective_temp_dir = str(DATA_DIR / "duckdb_tmp")
    else:
        # DEC-027 Review #7: only use path under DATA_DIR or exactly DATA_DIR/duckdb_tmp
        try:
            effective_resolved = Path(temp_dir_raw).resolve()
            data_dir_resolved = DATA_DIR.resolve()
            allowed_duckdb_tmp = (DATA_DIR / "duckdb_tmp").resolve()
            if effective_resolved != allowed_duckdb_tmp:
                effective_resolved.relative_to(data_dir_resolved)
        except (ValueError, OSError):
            effective_temp_dir = str(DATA_DIR / "duckdb_tmp")
            logger.warning(
                "Step 7 DuckDB temp_directory outside DATA_DIR; using fallback %s",
                effective_temp_dir,
            )
        else:
            effective_temp_dir = temp_dir_raw
    Path(effective_temp_dir).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(":memory:")
    _duck_mem_gb: Optional[float] = None
    _duck_threads: Optional[int] = None
    try:
        _avail = get_step7_available_ram_bytes()
        _input_bytes = int(sum(p.stat().st_size for p in chunk_paths if p.exists()))
        _resolve_runtime = getattr(_cfg, "resolve_duckdb_runtime_policy", None)
        if callable(_resolve_runtime):
            _step7_policy = _resolve_runtime("step7", _avail, input_bytes=_input_bytes)
            budget = int(_step7_policy["memory_limit_bytes"])
            _duck_mem_gb = budget / 1024**3
            _duck_threads = int(_step7_policy["threads"])
        else:
            budget = compute_step7_duckdb_budget(_avail)
        configure_step7_duckdb_runtime(con, budget_bytes=budget)
        # Avoid prepared statement with list (Binder Error in some DuckDB builds).
        paths_escaped = [p.replace("'", "''") for p in path_list]
        paths_sql = ",".join(f"'{p}'" for p in paths_escaped)
        con.execute(f"SELECT count(*) AS n FROM read_parquet([{paths_sql}], union_by_name=true)")
        _row = con.fetchone()
        if _row is None:
            raise ValueError("No rows in chunk Parquets")
        n_rows = _row[0]
        if n_rows == 0:
            raise ValueError("No rows in chunk Parquets")
        train_end_idx = int(n_rows * train_frac)
        valid_end_idx = int(n_rows * (train_frac + valid_frac))
        col_rows = con.execute(
            f"DESCRIBE SELECT * FROM read_parquet([{paths_sql}], union_by_name=true)"
        ).fetchall()
        available_cols = {str(r[0]) for r in col_rows}
        order_cols: List[str] = ["payout_complete_dtm"]
        if "canonical_id" in available_cols:
            order_cols.append("canonical_id")
        if "bet_id" in available_cols:
            order_cols.append("bet_id")
        order_sql = ", ".join(f"{c} NULLS LAST" for c in order_cols)
        con.execute(
            f"CREATE TEMP VIEW sorted_bets AS SELECT *, ROW_NUMBER() OVER (ORDER BY {order_sql}) - 1 AS _rn FROM read_parquet([{paths_sql}], union_by_name=true)"
        )
        _tp = str(train_path).replace("'", "''")
        _vp = str(valid_path).replace("'", "''")
        _sp = str(test_path).replace("'", "''")
        try:
            con.execute(
                f"COPY (SELECT * EXCLUDE (_rn) FROM sorted_bets WHERE _rn >= 0 AND _rn < {train_end_idx}) TO '{_tp}' (FORMAT PARQUET)"
            )
            con.execute(
                f"COPY (SELECT * EXCLUDE (_rn) FROM sorted_bets WHERE _rn >= {train_end_idx} AND _rn < {valid_end_idx}) TO '{_vp}' (FORMAT PARQUET)"
            )
            con.execute(
                f"COPY (SELECT * EXCLUDE (_rn) FROM sorted_bets WHERE _rn >= {valid_end_idx}) TO '{_sp}' (FORMAT PARQUET)"
            )
        except Exception:
            for p in (train_path, valid_path, test_path):
                if p.exists():
                    p.unlink()
            raise
    finally:
        con.close()
    if duckdb_runtime is not None:
        duckdb_runtime.memory_gb = _duck_mem_gb
        duckdb_runtime.threads = _duck_threads
    return (train_path, valid_path, test_path)
        
def _step7_oom_failsafe_next_frac(current_frac: float, *, neg_sample_frac_min: float) -> Tuple[float, bool]:
    """Compute next NEG_SAMPLE_FRAC after DuckDB OOM (halve); signal whether to retry.
    Returns (new_frac, should_retry). If already at NEG_SAMPLE_FRAC_MIN, raises
    with a clear message to reduce --days or add RAM. Orchestrator is responsible
    for re-running Step 6 with the returned new_frac and retrying _duckdb_sort_and_split.
    """
    if not (0.0 < current_frac <= 1.0):
        raise ValueError(
            "current_frac must be in (0, 1], got %s" % current_frac
        )
    new_frac = max(neg_sample_frac_min, current_frac / 2.0)
    if new_frac >= current_frac:
        raise RuntimeError(
            "Step 7 DuckDB OOM and NEG_SAMPLE_FRAC already at floor (%.2f). "
            "Reduce training window (--days / --start --end) or add RAM."
            % neg_sample_frac_min
        )
    return (new_frac, True)
        
def read_parquet_head(path: Path, n: int) -> pd.DataFrame:
    """Read first n rows from a Parquet file without loading full file (PLAN B+ Step 8 sample)."""
    if n <= 0:
        return pd.DataFrame()
    import pyarrow as pa
    import pyarrow.parquet as pq
    pf = pq.ParquetFile(path)
    batches: List[Any] = []
    total = 0
    for batch in pf.iter_batches(batch_size=min(n, 100_000)):
        batches.append(batch)
        total += len(batch)
        if total >= n:
            break
    if not batches:
        return pd.DataFrame()
    table = pa.Table.from_batches(batches)
    return table.slice(0, n).to_pandas()
        
def step7_metadata_from_paths(
    _train_path: Path, _valid_path: Path, _test_path: Path
) -> Tuple[int, int, int, int, Optional[Any]]:
    """(n_train, n_valid, n_test, label1_total, train_end_max) via DuckDB (PLAN B+)."""
    import duckdb
    con = duckdb.connect(":memory:")
    try:
        def _q_count(p: Path) -> int:
            s = str(p).replace("'", "''")
            r = con.execute(f"SELECT count(*) FROM read_parquet('{s}')").fetchone()
            return int(r[0]) if r else 0
        
        def _q_label_sum(p: Path) -> int:
            s = str(p).replace("'", "''")
            r = con.execute(
                f"SELECT coalesce(sum(cast(label AS INTEGER)), 0) FROM read_parquet('{s}')"
            ).fetchone()
            return int(r[0]) if r else 0
        
        def _q_max_dtm(p: Path) -> Optional[Any]:
            s = str(p).replace("'", "''")
            r = con.execute(
                f"SELECT max(payout_complete_dtm) FROM read_parquet('{s}')"
            ).fetchone()
            if r is None or r[0] is None:
                return None
            return pd.Timestamp(r[0])
        
        n_train = _q_count(_train_path)
        n_valid = _q_count(_valid_path)
        n_test = _q_count(_test_path)
        label1_total = _q_label_sum(_train_path) + _q_label_sum(_valid_path) + _q_label_sum(_test_path)
        train_end_max = _q_max_dtm(_train_path)
        return (n_train, n_valid, n_test, label1_total, train_end_max)
    finally:
        con.close()
        
def _step7_pandas_fallback(
    chunk_paths: List[Path],
    train_frac: float,
    valid_frac: float,
    *,
    chunk_concat_ram_factor: float,
    step7_pandas_fallback_max_bytes: int,
    neg_sample_ram_safety: float,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, Optional[Path], Optional[Path], Optional[Path]]:
    """Pandas in-memory concat + sort + row-level split (Layer 3 fallback).
    Returns (train_df, valid_df, test_df, None, None, None). Caller remains responsible for
    R700 log and MIN_VALID_TEST_ROWS warnings.
    Chunk Parquets must contain column payout_complete_dtm.
    """
    def _guard_step7_pandas_fallback_memory() -> None:
        """Fail fast when pandas fallback is very likely to OOM on current RAM."""
        _chunk_total_bytes_local = sum(Path(p).stat().st_size for p in chunk_paths)
        if _chunk_total_bytes_local > step7_pandas_fallback_max_bytes:
            raise RuntimeError(
                "Step 7 pandas fallback blocked: chunk parquet total %.1f GB exceeds small-data "
                "fallback limit %.1f GB. Pandas fallback is reserved for tiny test/dev datasets; "
                "prefer STEP7_USE_DUCKDB=True, reduce --days / --start --end, or lower NEG_SAMPLE_FRAC."
                % (
                    _chunk_total_bytes_local / (1024**3),
                    step7_pandas_fallback_max_bytes / (1024**3),
                )
            )
        _estimated_peak_bytes = int(
            _chunk_total_bytes_local
            * chunk_concat_ram_factor
            * (1.0 + train_frac)
        )
        _available_bytes = get_step7_available_ram_bytes()
        _safe_budget_bytes = (
            int(_available_bytes * neg_sample_ram_safety)
            if _available_bytes is not None and _available_bytes > 0
            else None
        )
        if (
            _safe_budget_bytes is not None
            and _estimated_peak_bytes > _safe_budget_bytes
        ):
            raise RuntimeError(
                "Step 7 pandas fallback blocked: estimated peak RAM %.1f GB exceeds safe available-RAM "
                "budget %.1f GB. Prefer STEP7_USE_DUCKDB=True, reduce --days / --start --end, "
                "or lower NEG_SAMPLE_FRAC."
                % (
                    _estimated_peak_bytes / (1024**3),
                    _safe_budget_bytes / (1024**3),
                )
            )

    if not chunk_paths:
        raise ValueError("chunk_paths must be non-empty")
    if not (
        0 < train_frac and 0 < valid_frac and train_frac + valid_frac < 1.0
    ):
        raise ValueError(
            "train_frac and valid_frac must be in (0, 1) and "
            "train_frac + valid_frac < 1.0"
        )
    _guard_step7_pandas_fallback_memory()
    all_dfs = [pd.read_parquet(p) for p in chunk_paths]
    full_df = pd.concat(all_dfs, ignore_index=True)
    if "payout_complete_dtm" not in full_df.columns:
        raise ValueError(
            "chunk Parquets must contain column payout_complete_dtm"
        )
    _payout_ts = pd.to_datetime(full_df["payout_complete_dtm"])
    if _payout_ts.dt.tz is not None:
        _payout_ts = _payout_ts.dt.tz_localize(None)
    _sort_cols = ["_sort_ts_tmp"] + [
        c for c in ("canonical_id", "bet_id") if c in full_df.columns
    ]
    full_df["_sort_ts_tmp"] = _payout_ts
    full_df.sort_values(_sort_cols, kind="stable", na_position="last", inplace=True)
    full_df.drop(columns=["_sort_ts_tmp"], inplace=True)
    full_df.reset_index(drop=True, inplace=True)
    n_rows = len(full_df)
    if n_rows == 0:
        raise ValueError("chunk_paths produced no rows")
    _train_end_idx = int(n_rows * train_frac)
    _valid_end_idx = int(n_rows * (train_frac + valid_frac))
    _row_pos = np.arange(n_rows)
    full_df["_split"] = np.select(
        [_row_pos < _train_end_idx, _row_pos < _valid_end_idx],
        ["train", "valid"],
        default="test",
    )
    _split_col = full_df["_split"]
    train_df = full_df[_split_col == "train"].reset_index(drop=True)
    valid_df = full_df[_split_col == "valid"].reset_index(drop=True)
    test_df = full_df[~(_split_col.isin(("train", "valid")))].reset_index(drop=True)
    del full_df, _split_col
    return (train_df, valid_df, test_df, None, None, None)
        
def _step7_sort_and_split(
    chunk_paths: List[Path],
    train_frac: float,
    valid_frac: float,
    *,
    step6_runner: Optional[Callable[[float], List[Path]]] = None,
    current_neg_frac: Optional[float] = None,
    step7_use_duckdb: bool,
    step7_keep_train_on_disk: bool,
    step9_export_libsvm: bool,
    chunk_concat_ram_factor: float,
    step7_pandas_fallback_max_bytes: int,
    neg_sample_ram_safety: float,
    neg_sample_frac_min: float,
    duckdb_runtime: Optional["DuckdbStep7Runtime"] = None,
) -> Tuple[Optional[pd.DataFrame], Optional[pd.DataFrame], Optional[pd.DataFrame], Optional[Path], Optional[Path], Optional[Path]]:
    """Orchestrator: DuckDB sort+split (Layer 1), OOM retry (Layer 2), or pandas fallback (Layer 3).
    Returns (train_df, valid_df, test_df, train_path, valid_path, test_path). When STEP7_KEEP_TRAIN_ON_DISK
    and DuckDB succeed, train_df is None and paths are set (train not loaded). When STEP9_EXPORT_LIBSVM too,
    valid_df and test_df are not loaded (PLAN B+ 階段 6 第 2 步). Otherwise paths are None.
    When STEP7_KEEP_TRAIN_ON_DISK and DuckDB fails, raises (no pandas fallback) per PLAN B+.
    If DuckDB returns but read_parquet of the split files fails, falls back to pandas using chunk_paths.
    """
    if not step7_use_duckdb:
        if step7_keep_train_on_disk:
            raise ValueError(
                "STEP7_KEEP_TRAIN_ON_DISK=True requires STEP7_USE_DUCKDB=True. "
                "Either set STEP7_USE_DUCKDB=True or set STEP7_KEEP_TRAIN_ON_DISK=False."
            )
        logger.warning(
            "STEP7_USE_DUCKDB=False: using pandas fallback for Step 7 (high OOM risk). "
            "Prefer STEP7_USE_DUCKDB=True or reduce --days / NEG_SAMPLE_FRAC. See doc/training_oom_and_runtime_audit.md A19."
        )
        return _step7_pandas_fallback(
            chunk_paths,
            train_frac,
            valid_frac,
            chunk_concat_ram_factor=chunk_concat_ram_factor,
            step7_pandas_fallback_max_bytes=step7_pandas_fallback_max_bytes,
            neg_sample_ram_safety=neg_sample_ram_safety,
        )
    def _is_parquet_io_problem(err: Exception) -> bool:
        msg = str(err).lower()
        return (
            "no files found that match the pattern" in msg
            or "too small to be a parquet file" in msg
            or "invalid parquet" in msg
        )
    try:
        train_path, valid_path, test_path = _duckdb_sort_and_split(
            chunk_paths,
            train_frac,
            valid_frac,
            duckdb_runtime=duckdb_runtime,
        )
        if step7_keep_train_on_disk:
            if step9_export_libsvm:
                step7_clean_duckdb_temp_dir()
                return (None, None, None, train_path, valid_path, test_path)
            valid_df = pd.read_parquet(valid_path)
            test_df = pd.read_parquet(test_path)
            step7_clean_duckdb_temp_dir()
            return (None, valid_df, test_df, train_path, valid_path, test_path)
        train_df = pd.read_parquet(train_path)
        valid_df = pd.read_parquet(valid_path)
        test_df = pd.read_parquet(test_path)
        for p in (train_path, valid_path, test_path):
            if p.exists():
                p.unlink(missing_ok=True)
        step7_clean_duckdb_temp_dir()
        return (train_df, valid_df, test_df, None, None, None)
    except Exception as exc:
        if (
            is_duckdb_oom(exc)
            and step6_runner is not None
            and current_neg_frac is not None
        ):
            logger.warning(
                "Step 7 DuckDB OOM: Issue #19 removed chunk-level negative downsampling; "
                "re-running Step 6 with lower NEG_SAMPLE_FRAC does not shrink chunk Parquets. "
                "Use pandas fallback or reduce --days / add RAM."
            )
        if step7_keep_train_on_disk:
            if _is_parquet_io_problem(exc):
                logger.warning(
                    "Step 7 DuckDB parquet IO issue under keep-on-disk; "
                    "falling back to pandas for test/dev robustness: %s",
                    exc,
                )
                return _step7_pandas_fallback(
                    chunk_paths,
                    train_frac,
                    valid_frac,
                    chunk_concat_ram_factor=chunk_concat_ram_factor,
                    step7_pandas_fallback_max_bytes=step7_pandas_fallback_max_bytes,
                    neg_sample_ram_safety=neg_sample_ram_safety,
                )
            raise RuntimeError(
                "Step 7 STEP7_KEEP_TRAIN_ON_DISK is True and DuckDB failed; "
                "no pandas fallback. Reduce --days or add RAM."
            ) from exc
        if is_duckdb_oom(exc):
            logger.warning(
                "Step 7 DuckDB OOM; falling back to pandas in-memory sort+split: %s",
                exc,
            )
        else:
            logger.warning(
                "Step 7 DuckDB failed (non-OOM); falling back to pandas: %s",
                exc,
            )
        return _step7_pandas_fallback(
            chunk_paths,
            train_frac,
            valid_frac,
            chunk_concat_ram_factor=chunk_concat_ram_factor,
            step7_pandas_fallback_max_bytes=step7_pandas_fallback_max_bytes,
            neg_sample_ram_safety=neg_sample_ram_safety,
        )