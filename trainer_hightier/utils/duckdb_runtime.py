"""Apply DuckDB connection settings for ``trainer_hightier`` (isolated from ``trainer.core``)."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, TypeVar

import duckdb

from trainer_hightier.config import DuckDbRuntimeConfig

T = TypeVar("T")

_LOG = logging.getLogger("trainer_hightier")


def degraded_runtime_config_after_oom(cfg: DuckDbRuntimeConfig) -> DuckDbRuntimeConfig | None:
    """Return more conservative DuckDB threads after ``OutOfMemoryException``, or ``None`` if capped at 1."""

    threads = cfg.threads
    if threads is None:
        return replace(cfg, threads=8, preserve_insertion_order=False)
    if int(threads) <= 1:
        return None
    next_t = max(1, int(threads) // 2)
    if next_t >= int(threads):
        next_t = 1
    return replace(cfg, threads=next_t, preserve_insertion_order=False)


def run_with_fresh_duck_connections_oom_retry(
    initial_cfg: DuckDbRuntimeConfig,
    fn: Callable[[Any, int], T],
    *,
    max_attempts: int = 12,
) -> tuple[T, DuckDbRuntimeConfig]:
    """Run *fn(con, attempt_ix)* on fresh in-memory DuckDB connections; degrade threads on each OOM.

    *fn* receives *con* after :func:`apply_duckdb_runtime_pragmas`. *attempt_ix* is the 0-based
    loop index (``0`` on first try, ``1`` after one OOM, …) for progress labels only.
    """

    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
    cfg = initial_cfg
    last_oom: duckdb.OutOfMemoryException | None = None

    for attempt_ix in range(int(max_attempts)):
        con = duckdb.connect(database=":memory:")
        try:
            apply_duckdb_runtime_pragmas(con, cfg)
            out = fn(con, attempt_ix)
            return out, cfg
        except duckdb.OutOfMemoryException as exc:
            last_oom = exc
            next_cfg = degraded_runtime_config_after_oom(cfg)
            if next_cfg is None:
                _LOG.warning(
                    "trainer_hightier DuckDB OOM with threads<=1 (%s): %s; giving up.",
                    getattr(cfg, "memory_limit", None),
                    exc,
                )
                raise exc
            _LOG.warning(
                "trainer_hightier DuckDB OOM (attempt %d/%d): %s → retry threads %s→%s, memory_limit=%s",
                attempt_ix + 1,
                max_attempts,
                exc,
                cfg.threads,
                next_cfg.threads,
                next_cfg.memory_limit,
            )
            cfg = next_cfg
        finally:
            con.close()

    if last_oom is not None:
        raise last_oom
    raise RuntimeError("duckdb oom retry: internal error")



def _path_posix(path: Path) -> str:
    return str(Path(path).resolve()).replace("\\", "/")


def apply_duckdb_runtime_pragmas(con: Any, cfg: DuckDbRuntimeConfig) -> None:
    """Apply ``DuckDbRuntimeConfig`` PRAGMAs / session variables on *con*."""
    if cfg.temp_directory is not None:
        td_path = Path(cfg.temp_directory).resolve()
        td_path.mkdir(parents=True, exist_ok=True)
        td = _path_posix(td_path)
        con.execute(f"PRAGMA temp_directory='{td}'")
    if cfg.max_temp_directory_size is not None:
        con.execute(f"PRAGMA max_temp_directory_size='{cfg.max_temp_directory_size}'")
    con.execute(f"PRAGMA memory_limit='{cfg.memory_limit}'")
    if cfg.threads is not None:
        con.execute(f"PRAGMA threads={int(cfg.threads)}")
    pio = "true" if cfg.preserve_insertion_order else "false"
    con.execute(f"SET preserve_insertion_order={pio}")


def _poll_duckdb_progress_bar(con: Any, stop: threading.Event, desc: str, t0: float) -> None:
    """Background loop: refresh tqdm using ``con.query_progress()`` until *stop* is set."""
    try:
        from tqdm import tqdm
        from tqdm.contrib.logging import logging_redirect_tqdm
    except ImportError:
        while not stop.wait(1.0):
            pass
        return

    with logging_redirect_tqdm():
        with tqdm(
            total=100.0,
            desc=desc,
            unit="%",
            bar_format="{desc}: {percentage:3.0f}%|{bar}| {elapsed} {postfix}",
            mininterval=0.15,
        ) as pbar:
            while True:
                if stop.wait(0.25):
                    pbar.n = 100.0
                    pbar.refresh()
                    break
                qp = con.query_progress()
                elapsed = time.perf_counter() - t0
                if qp >= 0.0:
                    pbar.n = max(pbar.n, min(100.0, qp * 100.0))
                    pbar.set_postfix_str("")
                else:
                    pbar.set_postfix_str(f"…{elapsed:.0f}s")
                pbar.refresh()


def run_with_query_progress(
    con: Any,
    fn: Callable[[], T],
    *,
    desc: str,
    join_timeout_s: float = 120.0,
) -> T:
    """Run *fn* while a tqdm bar polls ``con.query_progress()`` on a background thread."""
    try:
        con.execute("PRAGMA enable_progress_bar_print=false")
    except Exception:
        pass
    stop = threading.Event()
    t0 = time.perf_counter()
    th = threading.Thread(
        target=_poll_duckdb_progress_bar,
        args=(con, stop, desc, t0),
        daemon=True,
    )
    th.start()
    try:
        return fn()
    finally:
        stop.set()
        th.join(timeout=float(join_timeout_s))


def execute_sql_with_progress(
    con: Any,
    sql: str,
    *,
    desc: str,
    join_timeout_s: float = 120.0,
) -> None:
    """Execute *sql*; tqdm reflects ``query_progress()`` while the query runs (same connection)."""

    def _run() -> None:
        con.execute(sql)

    run_with_query_progress(con, _run, desc=desc, join_timeout_s=join_timeout_s)


def execute_sql_with_progress_oom_retry(
    initial_cfg: DuckDbRuntimeConfig,
    sql: str,
    *,
    desc: str,
    join_timeout_s: float = 7200.0,
    max_attempts: int = 12,
) -> DuckDbRuntimeConfig:
    """Like :func:`execute_sql_with_progress` but reopen DuckDB after each ``OutOfMemoryException``."""

    def _fn(con: Any, attempt_ix: int) -> None:
        dd = desc if attempt_ix == 0 else f"{desc} (OOM retry {attempt_ix})"
        execute_sql_with_progress(con, sql, desc=dd, join_timeout_s=join_timeout_s)

    _, cfg = run_with_fresh_duck_connections_oom_retry(
        initial_cfg,
        _fn,
        max_attempts=max_attempts,
    )
    return cfg


def execute_query_df_with_progress(
    con: Any,
    sql: str,
    *,
    desc: str,
    join_timeout_s: float = 600.0,
) -> Any:
    """Execute *sql* and return ``pandas`` DataFrame with the same progress UX."""

    def _run() -> Any:
        return con.execute(sql).df()

    return run_with_query_progress(con, _run, desc=desc, join_timeout_s=join_timeout_s)
