"""Apply DuckDB connection settings for ``trainer_hightier`` (isolated from ``trainer.core``)."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable, TypeVar

from trainer_hightier.config import DuckDbRuntimeConfig

T = TypeVar("T")


def _path_posix(path: Path) -> str:
    return str(Path(path).resolve()).replace("\\", "/")


def apply_duckdb_runtime_pragmas(con: Any, cfg: DuckDbRuntimeConfig) -> None:
    """Apply ``DuckDbRuntimeConfig`` PRAGMAs / session variables on *con*."""
    if cfg.temp_directory is not None:
        td = _path_posix(Path(cfg.temp_directory))
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
