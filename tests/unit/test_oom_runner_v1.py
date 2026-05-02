"""Unit tests for ``oom_runner_v1`` (LDA-E1-07 / implementation plan §7.1)."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from layered_data_assets.oom_runner_v1 import (
    is_likely_oom_exception,
    memory_thread_plan,
    run_duckdb_job_with_oom_retries,
    sum_input_paths_bytes,
    suggest_initial_memory_limit_mb,
)


def test_sum_input_paths_bytes_counts_existing(tmp_path: Path) -> None:
    a = tmp_path / "a.bin"
    a.write_bytes(b"abcd")
    assert sum_input_paths_bytes([a, tmp_path / "missing"]) == 4


def test_suggest_initial_memory_limit_mb_none_without_available() -> None:
    assert suggest_initial_memory_limit_mb(available_bytes=None, input_total_bytes=1_000_000) is None


def test_suggest_initial_memory_limit_mb_positive_with_available() -> None:
    mb = suggest_initial_memory_limit_mb(available_bytes=4 * 1024**3, input_total_bytes=100 * 1024**2)
    assert mb is not None and mb >= 64


def test_memory_thread_plan_first_row_respects_initial() -> None:
    plan = memory_thread_plan(initial_memory_limit_mb=800, max_attempts=3)
    assert plan[0][0] == 800
    assert plan[0][1] >= 1


def test_is_likely_oom_exception() -> None:
    assert is_likely_oom_exception(MemoryError())
    assert is_likely_oom_exception(RuntimeError("Out of Memory Error"))
    assert not is_likely_oom_exception(ValueError("not oom"))
    assert not is_likely_oom_exception(ValueError("bad sql"))


def test_run_duckdb_job_retries_then_succeeds(tmp_path: Path) -> None:
    """Second attempt succeeds: determinism of returned value; both attempts logged."""
    log = tmp_path / "run.jsonl"
    calls: list[int] = []

    def connect() -> MagicMock:
        return MagicMock()

    def work(con: MagicMock) -> int:
        calls.append(1)
        if len(calls) == 1:
            raise MemoryError()
        return 42

    out = run_duckdb_job_with_oom_retries(
        connect=connect,
        work=work,
        input_paths=[tmp_path / "x.parquet"],
        job_name="unit_test_job",
        run_log_path=log,
        failure_context_path=None,
        max_attempts=4,
        initial_memory_limit_mb=None,
    )
    assert out == 42
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["success"] is False
    assert json.loads(lines[1])["success"] is True


def test_run_duckdb_job_non_oom_fails_fast(tmp_path: Path) -> None:
    log = tmp_path / "run.jsonl"

    def connect() -> MagicMock:
        return MagicMock()

    def work(_con: MagicMock) -> None:
        raise ValueError("not oom")

    with pytest.raises(ValueError, match="not oom"):
        run_duckdb_job_with_oom_retries(
            connect=connect,
            work=work,
            input_paths=[tmp_path / "y.parquet"],
            job_name="unit_fail_fast",
            run_log_path=log,
            failure_context_path=None,
            max_attempts=4,
        )
    assert len(log.read_text(encoding="utf-8").strip().splitlines()) == 1


def test_run_duckdb_job_writes_failure_context(tmp_path: Path) -> None:
    ctx = tmp_path / "fail.json"

    def connect() -> MagicMock:
        return MagicMock()

    def work(_con: MagicMock) -> None:
        raise RuntimeError("Out of Memory Error: test")

    with pytest.raises(RuntimeError):
        run_duckdb_job_with_oom_retries(
            connect=connect,
            work=work,
            input_paths=[tmp_path / "z.parquet"],
            job_name="unit_exhaust",
            run_log_path=None,
            failure_context_path=ctx,
            max_attempts=2,
        )
    blob = json.loads(ctx.read_text(encoding="utf-8"))
    assert blob["job_name"] == "unit_exhaust"
    assert len(blob["attempts"]) == 2
    assert "last_error" in blob


def test_max_attempts_invalid() -> None:
    def connect() -> MagicMock:
        return MagicMock()

    with pytest.raises(ValueError, match="max_attempts"):
        run_duckdb_job_with_oom_retries(
            connect=connect,
            work=lambda c: 1,
            input_paths=[],
            job_name="x",
            run_log_path=None,
            failure_context_path=None,
            max_attempts=0,
        )
