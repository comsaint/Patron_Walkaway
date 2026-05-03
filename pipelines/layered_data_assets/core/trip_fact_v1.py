"""L1 ``trip_fact`` / ``trip_run_map`` from ``run_fact`` (Phase 2 MVP, SSOT trip v1 + impl plan §4.3).

Trip close: **3 complete ``gaming_day`` without bet**; MVP uses ``run_fact`` only and the
``run_start_gaming_day`` / ``run_end_gaming_day`` calendar gap (no-run ⇔ no-bet equivalence).
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from pipelines.layered_data_assets.core.preprocess_bet_v1 import (
    _manifest_hashes_for_output,
    manifest_output_relative_uri,
)
from pipelines.layered_data_assets.core.trip_id_v1 import derive_trip_id
from pipelines.layered_data_assets.io.ingestion_delay_summary_v1 import manifest_ingestion_delay_placeholder
from pipelines.layered_data_assets.io.l0_paths import validate_source_snapshot_id

TRIP_DEFINITION_VERSION_DEFAULT = "trip_boundary_v1"
SOURCE_NAMESPACE_DEFAULT = "layered_data_assets_l1"
_TRIP_FACT_TRANSFORM_VERSION = "v1"
_TRIP_RUN_MAP_TRANSFORM_VERSION = "v1"


def _validate_gaming_day(value: str, *, param_name: str) -> str:
    s = value.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        raise ValueError(f"{param_name} must be YYYY-MM-DD, got {value!r}")
    return s


def _parse_gaming_day(s: str) -> date:
    return date.fromisoformat(_validate_gaming_day(s, param_name="gaming_day"))


def _empty_days_between_runs(last_end: date, next_start: date) -> int:
    """Count strict calendar ``gaming_day`` gaps between ``last_end`` and ``next_start``."""
    if next_start <= last_end:
        return 0
    return (next_start - last_end).days - 1


def _empty_days_after_last_run(last_end: date, coverage_end: date) -> int:
    """Count empty ``gaming_day`` from day after ``last_end`` through ``coverage_end`` inclusive."""
    if coverage_end <= last_end:
        return 0
    return (coverage_end - last_end).days


def _tag_runs_with_trip_seq(runs: pd.DataFrame) -> pd.DataFrame:
    """Return ``runs`` with ``trip_seq`` per ``player_id`` (0-based trip index within player)."""
    if runs.empty:
        return runs.assign(trip_seq=pd.Series(dtype="int64"))
    need = {
        "player_id",
        "run_id",
        "run_start_ts",
        "run_start_gaming_day",
        "run_end_gaming_day",
    }
    missing = need - set(runs.columns)
    if missing:
        raise ValueError(f"runs frame missing columns {sorted(missing)}")
    out_parts: list[pd.DataFrame] = []
    for pid, grp in runs.groupby("player_id", sort=False):
        g = grp.sort_values(["run_start_ts", "run_id"], kind="mergesort").copy()
        trip_seq = 0
        last_end: date | None = None
        seqs: list[int] = []
        for _, row in g.iterrows():
            sd = _parse_gaming_day(str(row["run_start_gaming_day"]))
            ed = _parse_gaming_day(str(row["run_end_gaming_day"]))
            if last_end is not None and _empty_days_between_runs(last_end, sd) >= 3:
                trip_seq += 1
            seqs.append(trip_seq)
            last_end = ed
        g["trip_seq"] = seqs
        out_parts.append(g)
    return pd.concat(out_parts, axis=0)


def _coverage_end_date(runs: pd.DataFrame) -> date:
    """Upper bound calendar day from all run start/end gaming days in ``runs``."""
    if runs.empty:
        raise ValueError("runs must be non-empty to derive coverage_end")
    mx = runs["run_end_gaming_day"].astype(str).map(_parse_gaming_day).max()
    ms = runs["run_start_gaming_day"].astype(str).map(_parse_gaming_day).max()
    return max(mx, ms)


def source_partitions_from_runs(runs: pd.DataFrame) -> list[str]:
    """Stable ``l1/run_fact/run_end_gaming_day=...`` keys from ``runs``."""
    if runs.empty:
        return []
    u = sorted({str(x) for x in runs["run_end_gaming_day"].tolist()})
    return [f"l1/run_fact/run_end_gaming_day={d}" for d in u]


def _align_hashes_to_partitions(hashes: list[str], n: int) -> list[str]:
    if n <= 0:
        return []
    if not hashes:
        return ["sha256:unknown"] * n
    base = list(hashes)
    out = base[:n]
    while len(out) < n:
        out.append(base[0])
    return out


def _one_trip_and_maps(
    g: pd.DataFrame,
    *,
    pid: int,
    cov: date,
    mseq: int,
    tsq: int,
    source_snapshot_id: str,
    trip_definition_version: str,
    source_namespace: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build one ``trip_fact`` row and its ``trip_run_map`` rows for a tagged run group."""
    g = g.sort_values(["run_start_ts", "run_id"], kind="mergesort")
    first = g.iloc[0]
    last = g.iloc[-1]
    last_end = _parse_gaming_day(str(last["run_end_gaming_day"]))
    is_last_trip = int(tsq) == mseq
    tail_empty = _empty_days_after_last_run(last_end, cov)
    is_closed = (not is_last_trip) or (is_last_trip and tail_empty >= 3)
    tsg = str(first["run_start_gaming_day"])
    first_run_id = str(first["run_id"])
    trip_id = derive_trip_id(
        player_id=pid,
        trip_start_gaming_day=tsg,
        first_run_id=first_run_id,
        trip_definition_version=trip_definition_version,
        source_namespace=source_namespace,
        source_snapshot_id=source_snapshot_id,
    )
    trip_end_gd = (last_end + timedelta(days=3)).isoformat() if is_closed else None
    trip_row = {
        "trip_id": trip_id,
        "player_id": int(pid),
        "trip_start_gaming_day": tsg,
        "trip_start_ts": first["run_start_ts"],
        "trip_end_ts": None,
        "trip_end_gaming_day": trip_end_gd,
        "is_trip_closed": bool(is_closed),
        "first_run_id": first_run_id,
        "last_run_id": str(last["run_id"]),
        "run_count": int(len(g)),
        "trip_definition_version": trip_definition_version,
        "source_namespace": source_namespace,
    }
    maps: list[dict[str, Any]] = []
    for o, (_, row) in enumerate(g.iterrows()):
        maps.append(
            {
                "trip_id": trip_id,
                "run_id": str(row["run_id"]),
                "player_id": int(pid),
                "run_ord_in_trip": int(o),
                "trip_start_gaming_day": tsg,
            }
        )
    return trip_row, maps


def build_trip_fact_and_run_map_frames(
    runs: pd.DataFrame,
    *,
    source_snapshot_id: str,
    trip_definition_version: str = TRIP_DEFINITION_VERSION_DEFAULT,
    source_namespace: str = SOURCE_NAMESPACE_DEFAULT,
    coverage_end: date | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build ``trip_fact`` and ``trip_run_map`` rows (full snapshot) from concatenated ``run_fact`` rows."""
    validate_source_snapshot_id(source_snapshot_id)
    if runs.empty:
        return pd.DataFrame(), pd.DataFrame()
    r = runs.copy()
    cov = coverage_end if coverage_end is not None else _coverage_end_date(r)
    tagged = _tag_runs_with_trip_seq(r)
    tagged["player_id"] = tagged["player_id"].astype("int64")
    trip_rows: list[dict[str, Any]] = []
    map_rows: list[dict[str, Any]] = []
    max_seq = tagged.groupby("player_id", sort=False)["trip_seq"].max()
    for (pid, tsq), g in tagged.groupby(["player_id", "trip_seq"], sort=False):
        mseq = int(max_seq.loc[int(pid)])
        tr, mp = _one_trip_and_maps(
            g,
            pid=int(pid),
            cov=cov,
            mseq=mseq,
            tsq=int(tsq),
            source_snapshot_id=source_snapshot_id,
            trip_definition_version=trip_definition_version,
            source_namespace=source_namespace,
        )
        trip_rows.append(tr)
        map_rows.extend(mp)
    return pd.DataFrame(trip_rows), pd.DataFrame(map_rows)


def load_run_fact_dataframe(con: Any, paths: list[Path]) -> pd.DataFrame:
    """Load and sort all ``run_fact`` Parquet files into one frame."""
    if not paths:
        raise ValueError("paths must be non-empty")
    parts: list[str] = []
    for p in paths:
        s = p.resolve().as_posix().replace("'", "''")
        parts.append(f"'{s}'")
    lst = ", ".join(parts)
    q = f"""
    SELECT * FROM read_parquet([{lst}])
    ORDER BY player_id, run_start_ts, run_id
    """
    return con.execute(q).df()


def _time_range_from_trips(trip_df: pd.DataFrame) -> tuple[str, str]:
    if trip_df.empty:
        z = "1970-01-01T00:00:00Z"
        return z, z
    mn = trip_df["trip_start_ts"].min()
    mx = trip_df["trip_start_ts"].max()
    return str(mn), str(mx)


def materialize_trip_partition_parquets(
    *,
    con: Any,
    run_fact_paths: list[Path],
    trip_start_gaming_day: str,
    trip_fact_out: Path,
    trip_run_map_out: Path,
    source_snapshot_id: str,
    trip_definition_version: str = TRIP_DEFINITION_VERSION_DEFAULT,
    source_namespace: str = SOURCE_NAMESPACE_DEFAULT,
    coverage_end: date | None = None,
) -> dict[str, Any]:
    """Compute trips from all ``run_fact`` inputs; write one ``trip_start_gaming_day`` partition.

    Returns ``row_count_trip_fact``, ``row_count_trip_run_map``, ``time_range_min``, ``time_range_max``.
    """
    day = _validate_gaming_day(trip_start_gaming_day, param_name="trip_start_gaming_day")
    runs = load_run_fact_dataframe(con, run_fact_paths)
    trip_all, map_all = build_trip_fact_and_run_map_frames(
        runs,
        source_snapshot_id=source_snapshot_id,
        trip_definition_version=trip_definition_version,
        source_namespace=source_namespace,
        coverage_end=coverage_end,
    )
    trip_part = trip_all[trip_all["trip_start_gaming_day"] == day].copy()
    map_part = map_all[map_all["trip_start_gaming_day"] == day].copy()
    trip_fact_out.parent.mkdir(parents=True, exist_ok=True)
    trip_run_map_out.parent.mkdir(parents=True, exist_ok=True)
    con.register("_trip_fact_part", trip_part)
    con.register("_trip_run_map_part", map_part)
    tf = str(trip_fact_out.resolve()).replace("\\", "/").replace("'", "''")
    tm = str(trip_run_map_out.resolve()).replace("\\", "/").replace("'", "''")
    con.execute(f"COPY _trip_fact_part TO '{tf}' (FORMAT PARQUET)")
    con.execute(f"COPY _trip_run_map_part TO '{tm}' (FORMAT PARQUET)")
    con.unregister("_trip_fact_part")
    con.unregister("_trip_run_map_part")
    t0, t1 = _time_range_from_trips(trip_part)
    return {
        "row_count_trip_fact": int(len(trip_part)),
        "row_count_trip_run_map": int(len(map_part)),
        "time_range_min": t0,
        "time_range_max": t1,
        "source_partitions": source_partitions_from_runs(runs),
    }


def _manifest_time_range(stats: dict[str, Any]) -> tuple[str, str]:
    return str(stats.get("time_range_min") or "1970-01-01T00:00:00Z"), str(
        stats.get("time_range_max") or "1970-01-01T00:00:00Z"
    )


def build_trip_fact_manifest(
    *,
    source_snapshot_id: str,
    trip_start_gaming_day: str,
    l0_fingerprint_path: Path | None,
    output_parquet: Path,
    manifest_uri_anchor: Path,
    stats: dict[str, Any],
    source_partitions: list[str],
    ingestion_delay_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Manifest dict for ``trip_fact`` (``artifact_kind`` = ``trip_fact``)."""
    validate_source_snapshot_id(source_snapshot_id)
    day = _validate_gaming_day(trip_start_gaming_day, param_name="trip_start_gaming_day")
    parts = list(source_partitions)
    hashes = _align_hashes_to_partitions(_manifest_hashes_for_output(l0_fingerprint_path), len(parts))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    t0, t1 = _manifest_time_range(stats)
    out_uri = manifest_output_relative_uri(output_parquet, manifest_uri_anchor)
    ids = ingestion_delay_summary if ingestion_delay_summary is not None else manifest_ingestion_delay_placeholder()
    return {
        "artifact_kind": "trip_fact",
        "partition_keys": {"source_snapshot_id": source_snapshot_id.strip(), "trip_start_gaming_day": day},
        "definition_version": TRIP_DEFINITION_VERSION_DEFAULT,
        "feature_version": "na_l1_trip_fact",
        "transform_version": _TRIP_FACT_TRANSFORM_VERSION,
        "source_partitions": parts,
        "source_hashes": hashes,
        "source_snapshot_id": source_snapshot_id.strip(),
        "preprocessing_rule_id": "preprocess_bet_v1",
        "preprocessing_rule_version": "v1",
        "published_snapshot_id": None,
        "ingestion_fix_rule_id": None,
        "ingestion_fix_rule_version": None,
        "row_count": int(stats["row_count_trip_fact"]),
        "time_range": {"min_event_time": t0, "max_event_time": t1},
        "built_at": now,
        "ingestion_delay_summary": ids,
        "output_relative_uri": out_uri,
    }


def build_trip_run_map_manifest(
    *,
    source_snapshot_id: str,
    trip_start_gaming_day: str,
    l0_fingerprint_path: Path | None,
    output_parquet: Path,
    manifest_uri_anchor: Path,
    stats: dict[str, Any],
    source_partitions: list[str],
    ingestion_delay_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Manifest dict for ``trip_run_map``."""
    validate_source_snapshot_id(source_snapshot_id)
    day = _validate_gaming_day(trip_start_gaming_day, param_name="trip_start_gaming_day")
    parts = list(source_partitions)
    hashes = _align_hashes_to_partitions(_manifest_hashes_for_output(l0_fingerprint_path), len(parts))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    t0, t1 = _manifest_time_range(stats)
    out_uri = manifest_output_relative_uri(output_parquet, manifest_uri_anchor)
    ids = ingestion_delay_summary if ingestion_delay_summary is not None else manifest_ingestion_delay_placeholder()
    return {
        "artifact_kind": "trip_run_map",
        "partition_keys": {"source_snapshot_id": source_snapshot_id.strip(), "trip_start_gaming_day": day},
        "definition_version": TRIP_DEFINITION_VERSION_DEFAULT,
        "feature_version": "na_l1_trip_run_map",
        "transform_version": _TRIP_RUN_MAP_TRANSFORM_VERSION,
        "source_partitions": parts,
        "source_hashes": hashes,
        "source_snapshot_id": source_snapshot_id.strip(),
        "preprocessing_rule_id": "preprocess_bet_v1",
        "preprocessing_rule_version": "v1",
        "published_snapshot_id": None,
        "ingestion_fix_rule_id": None,
        "ingestion_fix_rule_version": None,
        "row_count": int(stats["row_count_trip_run_map"]),
        "time_range": {"min_event_time": t0, "max_event_time": t1},
        "built_at": now,
        "ingestion_delay_summary": ids,
        "output_relative_uri": out_uri,
    }
