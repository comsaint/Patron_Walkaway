"""DuckDB OOM 階梯重試與 run log（implementation plan §7.1；LDA-E1-07）。

僅調整 DuckDB ``memory_limit``／``threads`` 等執行參數；不改變 L1 SQL 語義。
"""
from __future__ import annotations

import json
import os
from argparse import ArgumentParser
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, NoReturn, TypeVar

T = TypeVar("T")

_MIN_MEMORY_LIMIT_MB = 64


def add_duckdb_oom_cli_args(parser: ArgumentParser) -> None:
    """Register optional §7.1 run log / retry CLI flags on an ``ArgumentParser``."""
    parser.add_argument(
        "--duckdb-run-log",
        type=Path,
        default=None,
        help="Append JSONL: each DuckDB attempt (memory_limit_mb, threads, rss, outcome)",
    )
    parser.add_argument(
        "--duckdb-oom-failure-context",
        type=Path,
        default=None,
        help="If all attempts fail, write one JSON file with inputs + last error + attempt trail",
    )
    parser.add_argument(
        "--duckdb-oom-max-attempts",
        type=int,
        default=6,
        help="Max connect/configure/work cycles before fail-fast (default: 6)",
    )
    parser.add_argument(
        "--duckdb-initial-memory-limit-mb",
        type=int,
        default=None,
        help="Optional first-attempt SET memory_limit (MB); omit to keep DuckDB default",
    )


def sum_input_paths_bytes(paths: Sequence[Path]) -> int:
    """Sum file sizes in bytes; missing files contribute 0."""
    total = 0
    for p in paths:
        try:
            if p.is_file():
                total += int(p.stat().st_size)
        except OSError:
            continue
    return total


def virtual_memory_available_bytes() -> int | None:
    """Best-effort available RAM; ``None`` if not measurable."""
    try:
        import psutil

        return int(psutil.virtual_memory().available)
    except Exception:
        return None


def suggest_initial_memory_limit_mb(*, available_bytes: int | None, input_total_bytes: int) -> int | None:
    """Heuristic first-attempt cap (MB); ``None`` means caller should omit ``SET memory_limit``."""
    if available_bytes is None:
        return None
    avail_mb = max(1, available_bytes // (1024 * 1024))
    need_mb = max(64, min(8192, int(input_total_bytes * 3 / (1024 * 1024)) + 128))
    cap = max(_MIN_MEMORY_LIMIT_MB, min(avail_mb // 2, need_mb))
    return int(cap)


def memory_thread_plan(
    *,
    initial_memory_limit_mb: int | None,
    max_attempts: int,
) -> list[tuple[int | None, int]]:
    """Build (memory_limit_mb, threads) steps: halve effective MB each step; shrink threads."""
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts!r}")
    threads0 = min(4, max(1, (os.cpu_count() or 2)))
    rows: list[tuple[int | None, int]] = []
    mem: int | None = initial_memory_limit_mb
    thr = threads0
    for _ in range(max_attempts):
        rows.append((mem, thr))
        base = mem if mem is not None else 1024
        mem = max(_MIN_MEMORY_LIMIT_MB, int(base) // 2)
        thr = max(1, thr // 2) if thr > 1 else 1
    return rows


def is_likely_oom_exception(exc: BaseException) -> bool:
    """Return True if ``exc`` is probably an out-of-memory condition."""
    if isinstance(exc, MemoryError):
        return True
    msg = str(exc).lower()
    return (
        "out of memory" in msg
        or "cannot allocate memory" in msg
        or "bad_alloc" in msg
        or "failed to allocate" in msg
    )


def apply_duckdb_resource_pragmas(con: Any, *, memory_limit_mb: int | None, threads: int) -> None:
    """Apply DuckDB resource settings for one attempt."""
    if threads < 1:
        raise ValueError(f"threads must be >= 1, got {threads!r}")
    con.execute(f"SET threads TO {int(threads)}")
    if memory_limit_mb is not None:
        if memory_limit_mb < _MIN_MEMORY_LIMIT_MB:
            raise ValueError(
                f"memory_limit_mb must be >= {_MIN_MEMORY_LIMIT_MB}, got {memory_limit_mb!r}"
            )
        con.execute(f"SET memory_limit='{int(memory_limit_mb)}MB'")


def current_process_rss_bytes() -> int | None:
    """Best-effort RSS of this process."""
    try:
        import psutil

        return int(psutil.Process().memory_info().rss)
    except Exception:
        return None


def append_jsonl_record(path: Path, record: dict[str, Any]) -> None:
    """Append one JSON object as a line (UTF-8)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True, ensure_ascii=True) + "\n")


def write_failure_context(path: Path, payload: dict[str, Any]) -> None:
    """Write a single JSON diagnostics blob (UTF-8, stable key order)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")


def _prepare_oom_run(
    *,
    input_paths: Sequence[Path],
    initial_memory_limit_mb: int | None,
    max_attempts: int,
) -> tuple[list[tuple[int | None, int]], int | None, int, int | None, list[Path]]:
    """Resolve paths, estimate bytes/RAM hint, and build the resource plan."""
    resolved = [p.resolve() for p in input_paths]
    total_b = sum_input_paths_bytes(resolved)
    avail = virtual_memory_available_bytes()
    hint_mb = suggest_initial_memory_limit_mb(available_bytes=avail, input_total_bytes=total_b)
    plan = memory_thread_plan(
        initial_memory_limit_mb=initial_memory_limit_mb,
        max_attempts=max_attempts,
    )
    return plan, hint_mb, total_b, avail, resolved


def _duckdb_attempt_with_pragmas(
    *,
    connect: Callable[[], Any],
    work: Callable[[Any], T],
    memory_limit_mb: int | None,
    threads: int,
) -> tuple[T, int | None, int | None]:
    """One connection: apply pragmas, run ``work``, return value and RSS before/after."""
    con = connect()
    try:
        apply_duckdb_resource_pragmas(con, memory_limit_mb=memory_limit_mb, threads=threads)
        rss_before = current_process_rss_bytes()
        result = work(con)
        rss_after = current_process_rss_bytes()
        return result, rss_before, rss_after
    finally:
        con.close()


def _push_attempt_record(
    attempt_records: list[dict[str, Any]],
    run_log_path: Path | None,
    record: dict[str, Any],
) -> None:
    """Append to in-memory trail and optional JSONL run log."""
    attempt_records.append(dict(record))
    if run_log_path is not None:
        append_jsonl_record(run_log_path, record)


def _oom_success_record(
    *,
    attempt: int,
    hint_mb: int | None,
    total_b: int,
    job_name: str,
    mem_mb: int | None,
    rss_b: int | None,
    rss_a: int | None,
    thr: int,
) -> dict[str, Any]:
    """JSON-serializable dict for a successful attempt."""
    return {
        "attempt": attempt,
        "estimated_memory_limit_hint_mb": hint_mb,
        "input_total_bytes": total_b,
        "job_name": job_name,
        "memory_limit_mb": mem_mb,
        "rss_bytes_after": rss_a,
        "rss_bytes_before": rss_b,
        "success": True,
        "threads": thr,
    }


def _oom_fail_record(
    *,
    attempt: int,
    hint_mb: int | None,
    total_b: int,
    job_name: str,
    mem_mb: int | None,
    thr: int,
    exc: BaseException,
) -> dict[str, Any]:
    """JSON-serializable dict for a failed attempt."""
    return {
        "attempt": attempt,
        "error": repr(exc),
        "estimated_memory_limit_hint_mb": hint_mb,
        "input_total_bytes": total_b,
        "job_name": job_name,
        "memory_limit_mb": mem_mb,
        "rss_bytes_at_fail": current_process_rss_bytes(),
        "success": False,
        "threads": thr,
    }


def _oom_run_successful_plan_step(
    *,
    idx: int,
    mem_mb: int | None,
    thr: int,
    connect: Callable[[], Any],
    work: Callable[[Any], T],
    hint_mb: int | None,
    total_b: int,
    job_name: str,
    run_log_path: Path | None,
    attempt_records: list[dict[str, Any]],
) -> T:
    """Execute one plan step, push success record, return ``work`` result."""
    out, rss_b, rss_a = _duckdb_attempt_with_pragmas(
        connect=connect, work=work, memory_limit_mb=mem_mb, threads=thr
    )
    _push_attempt_record(
        attempt_records,
        run_log_path,
        _oom_success_record(
            attempt=idx,
            hint_mb=hint_mb,
            total_b=total_b,
            job_name=job_name,
            mem_mb=mem_mb,
            rss_b=rss_b,
            rss_a=rss_a,
            thr=thr,
        ),
    )
    return out


def _oom_retry_execution_loop(
    *,
    plan: list[tuple[int | None, int]],
    connect: Callable[[], Any],
    work: Callable[[Any], T],
    hint_mb: int | None,
    total_b: int,
    job_name: str,
    run_log_path: Path | None,
) -> tuple[T | None, list[dict[str, Any]], BaseException | None]:
    """Run each plan step until success, non-OOM error, or OOM exhaustion."""
    attempt_records: list[dict[str, Any]] = []
    last_exc: BaseException | None = None
    for idx, (mem_mb, thr) in enumerate(plan):
        try:
            out = _oom_run_successful_plan_step(
                idx=idx,
                mem_mb=mem_mb,
                thr=thr,
                connect=connect,
                work=work,
                hint_mb=hint_mb,
                total_b=total_b,
                job_name=job_name,
                run_log_path=run_log_path,
                attempt_records=attempt_records,
            )
            return out, attempt_records, None
        except BaseException as exc:
            last_exc = exc
            _push_attempt_record(
                attempt_records,
                run_log_path,
                _oom_fail_record(
                    attempt=idx,
                    hint_mb=hint_mb,
                    total_b=total_b,
                    job_name=job_name,
                    mem_mb=mem_mb,
                    thr=thr,
                    exc=exc,
                ),
            )
            if not is_likely_oom_exception(exc):
                raise
            if idx == len(plan) - 1:
                break
    return None, attempt_records, last_exc


def _raise_after_oom_exhausted(
    *,
    attempt_records: list[dict[str, Any]],
    resolved: list[Path],
    total_b: int,
    job_name: str,
    last_exc: BaseException | None,
    hint_mb: int | None,
    avail: int | None,
    failure_context_path: Path | None,
) -> NoReturn:
    """Write failure context (optional) and re-raise the last exception."""
    payload = {
        "attempts": attempt_records,
        "input_paths": [str(p) for p in resolved],
        "input_total_bytes": total_b,
        "job_name": job_name,
        "last_error": repr(last_exc) if last_exc else None,
        "recommended_memory_limit_hint_mb": hint_mb,
        "virtual_memory_available_bytes": avail,
    }
    if failure_context_path is not None:
        write_failure_context(failure_context_path, payload)
    assert last_exc is not None
    raise last_exc


def run_duckdb_job_with_oom_retries(
    *,
    connect: Callable[[], Any],
    work: Callable[[Any], T],
    input_paths: Sequence[Path],
    job_name: str,
    run_log_path: Path | None,
    failure_context_path: Path | None,
    max_attempts: int,
    initial_memory_limit_mb: int | None = None,
) -> T:
    """Run ``work(con)`` with §7.1 tiered retries on likely OOM; fail-fast on other errors."""
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts!r}")
    plan, hint_mb, total_b, avail, resolved = _prepare_oom_run(
        input_paths=input_paths,
        initial_memory_limit_mb=initial_memory_limit_mb,
        max_attempts=max_attempts,
    )
    out, attempt_records, last_exc = _oom_retry_execution_loop(
        plan=plan,
        connect=connect,
        work=work,
        hint_mb=hint_mb,
        total_b=total_b,
        job_name=job_name,
        run_log_path=run_log_path,
    )
    if out is not None:
        return out
    _raise_after_oom_exhausted(
        attempt_records=attempt_records,
        resolved=resolved,
        total_b=total_b,
        job_name=job_name,
        last_exc=last_exc,
        hint_mb=hint_mb,
        avail=avail,
        failure_context_path=failure_context_path,
    )
