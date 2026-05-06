"""Unit tests for ``parallel_lda_mvp.run_mvp`` orchestration helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import duckdb
import pandas as pd

from parallel_lda_mvp.run_mvp import (
    _FINGERPRINT_ALGO_LEGACY,
    _FINGERPRINT_ALGO_ROLLING,
    _compute_month_bet_shas_for_span,
    _materialize_cleaned_bets_with_canonical_id,
    _read_month_bet_sha_cache,
    _rolling_month_fingerprints_for_yms,
    _t_bet_month_content_sha256,
    _t_bet_paths_input_fingerprint,
    _t_bet_read_parquet_rp_list,
    _tail_and_frozen_month_lists,
    _target_month_bet_fingerprint_algo,
    _trip_argv,
    _write_month_bet_sha_cache,
)


def canonical_parquet_digest_sorted(parquet_path: Path) -> str:
    """Return lowercase SHA-256 hex over canonical JSON for Parquet row content.

    Sorts columns lexicographically, sorts rows by those columns (mergesort),
    then JSON-serializes rows with sorted keys. Intended for **small** outputs
    only (loads entire file into memory).

    Args:
        parquet_path: Existing Parquet file.

    Returns:
        64-character hex digest.
    """
    p = Path(parquet_path)
    if not p.is_file():
        raise FileNotFoundError(f"parquet not found: {p}")
    df = pd.read_parquet(p)
    cols = sorted(df.columns)
    df2 = df[cols].sort_values(by=cols, na_position="first", kind="mergesort").reset_index(drop=True)

    def _cell(v: Any) -> Any:
        if v is None or v is pd.NA:
            return None
        if isinstance(v, pd.Timestamp):
            if pd.isna(v):
                return None
            return v.isoformat()
        if isinstance(v, float) and pd.isna(v):
            return None
        if hasattr(v, "item") and not isinstance(v, (bytes, str)):
            try:
                return v.item()
            except (ValueError, TypeError):
                return str(v)
        return v

    rows: list[dict[str, Any]] = []
    for _, row in df2.iterrows():
        rows.append({c: _cell(row[c]) for c in cols})
    payload = json.dumps(rows, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def test_trip_argv_includes_span_run_facts_and_coverage_end(tmp_path: Path) -> None:
    """Trip CLI must receive all span ``run_fact`` inputs and explicit ``--coverage-end``."""
    data_root = tmp_path / "data"
    data_root.mkdir()
    span = [tmp_path / f"run_fact__2026-10-{d:02d}.parquet" for d in (30, 31)]
    span.append(tmp_path / "run_fact__2026-11-01.parquet")
    for p in span:
        p.write_bytes(b"x")
    tf = tmp_path / "trip_fact__2026-10-30.parquet"
    tm = tmp_path / "trip_run_map__2026-10-30.parquet"
    cmd = _trip_argv(
        py="python",
        trip_start_day="2026-10-30",
        trip_fact_parquet=tf,
        trip_run_map_parquet=tm,
        run_fact_paths=span,
        snap="snap_test",
        data_root=data_root,
        coverage_end_day="2026-11-30",
    )
    assert cmd.count("--input-run-fact") == len(span)
    assert "--coverage-end" in cmd
    i = cmd.index("--coverage-end")
    assert cmd[i + 1] == "2026-11-30"
    assert "--trip-fact-output-parquet" in cmd
    assert "--trip-run-map-output-parquet" in cmd


def test_t_bet_paths_input_fingerprint_order_invariant(tmp_path: Path) -> None:
    """Fingerprint must not depend on path list order."""
    a = tmp_path / "a.parquet"
    b = tmp_path / "b.parquet"
    a.write_bytes(b"x")
    b.write_bytes(b"y")
    fp1 = _t_bet_paths_input_fingerprint([a, b])
    fp2 = _t_bet_paths_input_fingerprint([b, a])
    assert fp1 == fp2


def test_month_bet_sha_cache_merge_roundtrip(tmp_path: Path) -> None:
    """On-disk cache merges months under the same ``t_bet`` fingerprint."""
    scratch = tmp_path / "mvp_scratch"
    fp = hashlib.sha256(b"inputs").hexdigest()
    h1 = hashlib.sha256(b"one").hexdigest()
    h2 = hashlib.sha256(b"two").hexdigest()
    _write_month_bet_sha_cache(
        scratch,
        t_bet_inputs_fingerprint=fp,
        span_by_ym={"2024-07": h1},
        fingerprint_algo=_FINGERPRINT_ALGO_LEGACY,
    )
    _write_month_bet_sha_cache(
        scratch,
        t_bet_inputs_fingerprint=fp,
        span_by_ym={"2024-08": h2},
        fingerprint_algo=_FINGERPRINT_ALGO_LEGACY,
    )
    got = _read_month_bet_sha_cache(scratch)
    assert got is not None
    assert got[0] == fp
    assert got[1]["2024-07"] == h1
    assert got[1]["2024-08"] == h2
    assert got[2] == _FINGERPRINT_ALGO_LEGACY


def test_compute_month_bet_shas_matches_legacy(tmp_path: Path) -> None:
    """Batch + cache path must match per-month SHA for a tiny Parquet."""
    pq = tmp_path / "t_bet.parquet"
    scratch = tmp_path / "scratch"
    con = duckdb.connect()
    try:
        con.execute(
            f"""
            COPY (
              SELECT
                CAST('2024-07-03' AS DATE) AS gaming_day,
                CAST(10 AS BIGINT) AS bet_id,
                CAST(20 AS BIGINT) AS player_id
            ) TO '{str(pq.resolve()).replace(chr(39), chr(39) + chr(39))}' (FORMAT PARQUET)
            """
        )
    finally:
        con.close()
    ym = "2024-07"
    legacy = _t_bet_month_content_sha256(ym, [pq], scratch)
    cov = "2024-07-31"
    by_ym, stats = _compute_month_bet_shas_for_span(
        [ym], [pq], scratch, force=True, coverage_end_gaming_day=cov
    )
    assert by_ym[ym] == legacy
    assert stats["recomputed"] == 1
    assert stats["late_arrival_window_days"] == 45
    by2, st2 = _compute_month_bet_shas_for_span(
        [ym], [pq], scratch, force=False, coverage_end_gaming_day=cov
    )
    assert by2[ym] == legacy
    assert st2["cache_hits"] == 1
    assert st2["recomputed"] == 0


def test_compute_month_bet_shas_fallback_mid_loop_raises_runtimeerror_not_keyerror(
    tmp_path: Path,
) -> None:
    """Batch failure then partial per-month fallback must not surface as KeyError on subset."""
    pq = tmp_path / "t_bet.parquet"
    pq.write_bytes(b"x")
    scratch = tmp_path / "scratch"
    yms = ["2024-07", "2024-08"]

    def _batch_fail(
        months: object,
        paths: object,
        sd: object,
    ) -> dict[str, str]:
        raise RuntimeError("simulated batch duckdb failure")

    def _per_month(ym: str, paths: object, sd: object) -> str:
        if ym == "2024-07":
            return "a" * 64
        raise ValueError("simulated second month failure")

    with (
        patch(
            "parallel_lda_mvp.run_mvp._recompute_month_bet_shas_one_connection",
            side_effect=_batch_fail,
        ),
        patch(
            "parallel_lda_mvp.run_mvp._t_bet_month_content_sha256",
            side_effect=_per_month,
        ),
    ):
        try:
            _compute_month_bet_shas_for_span(
                yms, [pq], scratch, force=True, coverage_end_gaming_day="2024-08-31"
            )
        except KeyError as exc:
            raise AssertionError(f"unexpected KeyError: {exc}") from exc
        except RuntimeError as exc:
            assert "per-month fallback" in str(exc)
            assert exc.__cause__ is not None
        else:
            raise AssertionError("expected RuntimeError")


def test_tail_and_frozen_month_lists_splits_tail_two() -> None:
    """Last two calendar months are tail; earlier months are frozen."""
    tail, frozen = _tail_and_frozen_month_lists(["2024-01", "2024-02", "2024-03", "2024-04"], 2)
    assert tail == ["2024-03", "2024-04"]
    assert frozen == ["2024-01", "2024-02"]


def test_read_month_bet_sha_cache_rejects_schema_v1(tmp_path: Path) -> None:
    """Schema v1 cache files must be ignored (forces rebuild under v2 policy)."""
    scratch = tmp_path / "mvp_scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    legacy = {
        "algo_version": "t_bet_month_extract_sha_v1",
        "by_ym": {"2024-07": "a" * 64},
        "cache_schema_version": 1,
        "t_bet_inputs_fingerprint": "b" * 64,
    }
    (scratch / "month_bet_sha_cache.v1.json").write_text(
        json.dumps(legacy) + "\n", encoding="utf-8"
    )
    assert _read_month_bet_sha_cache(scratch) is None


def test_tail_only_requeues_last_two_months(tmp_path: Path) -> None:
    """Warm cache: only tail months are passed to DuckDB batch recompute."""
    pq = tmp_path / "t_bet.parquet"
    scratch = tmp_path / "scratch"
    con = duckdb.connect()
    try:
        con.execute(
            f"""
            COPY (
              SELECT
                CAST('2024-07-03' AS DATE) AS gaming_day,
                CAST(1 AS BIGINT) AS bet_id,
                CAST(2 AS BIGINT) AS player_id
            ) TO '{str(pq.resolve()).replace(chr(39), chr(39) + chr(39))}' (FORMAT PARQUET)
            """
        )
    finally:
        con.close()
    fp = _t_bet_paths_input_fingerprint([pq])
    _write_month_bet_sha_cache(
        scratch,
        t_bet_inputs_fingerprint=fp,
        span_by_ym={"2024-05": "0" * 64},
        fingerprint_algo=_FINGERPRINT_ALGO_LEGACY,
    )
    seen: list[str] = []

    def _capture(months: object, paths: object, sd: object) -> dict[str, str]:
        seen.extend(list(months))
        return {ym: "f" * 64 for ym in months}

    yms = ["2024-05", "2024-06", "2024-07"]
    with patch(
        "parallel_lda_mvp.run_mvp._recompute_month_bet_shas_one_connection",
        side_effect=_capture,
    ):
        by_ym, stats = _compute_month_bet_shas_for_span(
            yms, [pq], scratch, force=False, coverage_end_gaming_day="2024-07-31"
        )
    assert seen == ["2024-06", "2024-07"]
    assert stats["mode"] == "tail_only_recompute"
    assert stats["cache_hits"] == 1
    assert by_ym["2024-05"] == "0" * 64
    assert by_ym["2024-06"] == "f" * 64
    assert by_ym["2024-07"] == "f" * 64


def test_escalate_full_span_when_frozen_missing_from_cache(tmp_path: Path) -> None:
    """If any frozen month is absent from cache, recompute entire span."""
    pq = tmp_path / "t_bet.parquet"
    scratch = tmp_path / "scratch"
    pq.write_bytes(b"x")
    fp = _t_bet_paths_input_fingerprint([pq])
    _write_month_bet_sha_cache(
        scratch,
        t_bet_inputs_fingerprint=fp,
        span_by_ym={"2024-05": "0" * 64},
        fingerprint_algo=_FINGERPRINT_ALGO_LEGACY,
    )
    seen: list[str] = []

    def _capture(months: object, paths: object, sd: object) -> dict[str, str]:
        seen.extend(list(months))
        return {ym: "e" * 64 for ym in months}

    yms = ["2024-05", "2024-06", "2024-07", "2024-08"]
    with patch(
        "parallel_lda_mvp.run_mvp._recompute_month_bet_shas_one_connection",
        side_effect=_capture,
    ):
        _compute_month_bet_shas_for_span(
            yms, [pq], scratch, force=False, coverage_end_gaming_day="2024-08-31"
        )
    assert seen == yms
    assert len(seen) == 4


def test_read_month_bet_sha_cache_rejects_missing_fingerprint_algo(tmp_path: Path) -> None:
    """Policy without ``fingerprint_algo`` must invalidate cache."""
    scratch = tmp_path / "mvp_scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    bad = {
        "algo_version": "t_bet_month_fp_v2",
        "by_ym": {"2024-07": "a" * 64},
        "cache_schema_version": 2,
        "policy": {
            "late_arrival_window_days": 45,
            "policy_version": "tail2_late45_v1",
            "tail_months": 2,
        },
        "t_bet_inputs_fingerprint": "b" * 64,
    }
    (scratch / "month_bet_sha_cache.v1.json").write_text(json.dumps(bad) + "\n", encoding="utf-8")
    assert _read_month_bet_sha_cache(scratch) is None


def test_rolling_month_fingerprints_deterministic(tmp_path: Path) -> None:
    """Rolling aggregate path must be stable across repeated queries."""
    pq = tmp_path / "t_bet.parquet"
    con = duckdb.connect()
    try:
        con.execute(
            f"""
            COPY (
              SELECT
                CAST('2024-07-03' AS DATE) AS gaming_day,
                CAST(10 AS BIGINT) AS bet_id,
                CAST(20 AS BIGINT) AS player_id,
                CAST('2024-07-04 12:00:00' AS TIMESTAMP) AS payout_complete_dtm,
                CAST(1.0 AS DOUBLE) AS turnover,
                CAST(2.0 AS DOUBLE) AS valid_stake,
                CAST(0.5 AS DOUBLE) AS net_win
            ) TO '{str(pq.resolve()).replace(chr(39), chr(39) + chr(39))}' (FORMAT PARQUET)
            """
        )
    finally:
        con.close()
    con2 = duckdb.connect()
    try:
        rp = _t_bet_read_parquet_rp_list([pq])
        a = _rolling_month_fingerprints_for_yms(con2, rp_list=rp, yms=["2024-07"])
        b = _rolling_month_fingerprints_for_yms(con2, rp_list=rp, yms=["2024-07"])
    finally:
        con2.close()
    assert a == b
    assert len(a["2024-07"]) == 64


def test_compute_month_bet_sha_rolling_fallback_to_legacy(tmp_path: Path) -> None:
    """When rolling aggregate raises, legacy extract+hash must still complete."""
    pq = tmp_path / "t_bet.parquet"
    scratch = tmp_path / "scratch"
    con = duckdb.connect()
    try:
        con.execute(
            f"""
            COPY (
              SELECT
                CAST('2024-07-03' AS DATE) AS gaming_day,
                CAST(10 AS BIGINT) AS bet_id,
                CAST(20 AS BIGINT) AS player_id,
                CAST('2024-07-04 12:00:00' AS TIMESTAMP) AS payout_complete_dtm,
                CAST(1.0 AS DOUBLE) AS turnover,
                CAST(2.0 AS DOUBLE) AS valid_stake,
                CAST(0.5 AS DOUBLE) AS net_win
            ) TO '{str(pq.resolve()).replace(chr(39), chr(39) + chr(39))}' (FORMAT PARQUET)
            """
        )
    finally:
        con.close()
    ym = "2024-07"
    legacy = _t_bet_month_content_sha256(ym, [pq], scratch)
    with patch(
        "parallel_lda_mvp.run_mvp._rolling_month_fingerprints_for_yms",
        side_effect=RuntimeError("simulated rolling failure"),
    ):
        by_ym, stats = _compute_month_bet_shas_for_span(
            [ym], [pq], scratch, force=True, coverage_end_gaming_day="2024-07-31"
        )
    assert by_ym[ym] == legacy
    assert stats["month_bet_sha_fingerprint_algo"] == _FINGERPRINT_ALGO_LEGACY
    assert stats["month_bet_sha_strategy"] == _FINGERPRINT_ALGO_LEGACY


def test_rolling_fingerprint_accepts_gmwds_raw_t_bet_columns(tmp_path: Path) -> None:
    """GMWDS ``t_bet`` (``GDP_GMWDS_Raw_Schema_Dictionary`` §4) uses ``wager`` / ``casino_win``, not turnover trio."""
    pq = tmp_path / "t_bet.parquet"
    con = duckdb.connect()
    try:
        con.execute(
            f"""
            COPY (
              SELECT
                CAST('2024-07-03' AS DATE) AS gaming_day,
                CAST(101001 AS BIGINT) AS bet_id,
                CAST(2 AS BIGINT) AS player_id,
                CAST('2024-07-03 12:00:00' AS TIMESTAMP) AS payout_complete_dtm,
                CAST(100.0 AS DECIMAL(19,4)) AS wager,
                CAST(-10.0 AS DECIMAL(19,4)) AS casino_win
            ) TO '{str(pq.resolve()).replace(chr(39), chr(39) + chr(39))}' (FORMAT PARQUET)
            """
        )
    finally:
        con.close()
    assert _target_month_bet_fingerprint_algo([pq]) == _FINGERPRINT_ALGO_ROLLING
    con2 = duckdb.connect()
    try:
        rp = _t_bet_read_parquet_rp_list([pq])
        out = _rolling_month_fingerprints_for_yms(con2, rp_list=rp, yms=["2024-07"])
    finally:
        con2.close()
    assert len(out["2024-07"]) == 64


def test_force_recompute_used_in_stats(tmp_path: Path) -> None:
    """``force=True`` must surface in stats for mvp_summary wiring."""
    pq = tmp_path / "t_bet.parquet"
    scratch = tmp_path / "scratch"
    con = duckdb.connect()
    try:
        con.execute(
            f"""
            COPY (
              SELECT
                CAST('2024-07-03' AS DATE) AS gaming_day,
                CAST(1 AS BIGINT) AS bet_id,
                CAST(2 AS BIGINT) AS player_id
            ) TO '{str(pq.resolve()).replace(chr(39), chr(39) + chr(39))}' (FORMAT PARQUET)
            """
        )
    finally:
        con.close()
    _, stats = _compute_month_bet_shas_for_span(
        ["2024-07"],
        [pq],
        scratch,
        force=True,
        coverage_end_gaming_day="2024-07-31",
    )
    assert stats["force_recompute_used"] is True
    assert stats["mode"] == "force_full"


def test_materialize_cleaned_bets_with_canonical_id_is_deterministic(tmp_path: Path) -> None:
    """For duplicate mapping rows per player, canonical_id selection must be deterministic."""
    cleaned = tmp_path / "cleaned.parquet"
    mapping = tmp_path / "mapping.parquet"
    pd.DataFrame(
        {
            "bet_id": [11, 12],
            "player_id": [1001, 1002],
        }
    ).to_parquet(cleaned, index=False)
    pd.DataFrame(
        {
            "player_id": [1001, 1001, 1002],
            "canonical_id": ["canon_z", "canon_a", "canon_k"],
        }
    ).to_parquet(mapping, index=False)

    out_paths = _materialize_cleaned_bets_with_canonical_id(
        cleaned_paths=[cleaned],
        mapping_parquet=mapping,
    )
    out = pd.read_parquet(out_paths[0]).sort_values("player_id").reset_index(drop=True)
    assert list(out["player_id"]) == [1001, 1002]
    assert list(out["canonical_id"]) == ["canon_a", "canon_k"]


def test_canonical_parquet_digest_row_order_invariant(tmp_path: Path) -> None:
    """Digest must ignore row order for identical logical content."""
    a = tmp_path / "a.parquet"
    b = tmp_path / "b.parquet"
    df1 = pd.DataFrame({"z": [1, 2], "a": ["x", "y"]})
    df2 = pd.DataFrame({"z": [2, 1], "a": ["y", "x"]})
    df1.to_parquet(a, index=False)
    df2.to_parquet(b, index=False)
    assert canonical_parquet_digest_sorted(a) == canonical_parquet_digest_sorted(b)


def test_canonical_parquet_digest_empty_frame(tmp_path: Path) -> None:
    """Empty Parquet must yield a stable non-empty digest string."""
    p = tmp_path / "empty.parquet"
    pd.DataFrame({"k": pd.Series([], dtype="int64")}).iloc[:0].to_parquet(p, index=False)
    d = canonical_parquet_digest_sorted(p)
    assert len(d) == 64
    d2 = canonical_parquet_digest_sorted(p)
    assert d == d2
