"""Unit tests for ``manifest_lineage_v1``."""

import json
from pathlib import Path

from layered_data_assets.manifest_lineage_v1 import (
    merge_ingestion_delay_summary,
    merge_source_hashes_into_manifest,
    source_hashes_from_l0_fingerprint,
)


def test_source_hashes_from_fingerprint(tmp_path: Path) -> None:
    fp = tmp_path / "snapshot_fingerprint.json"
    fp.write_text(
        json.dumps(
            {
                "inputs": [
                    {"relative_path": "a.parquet", "sha256": "ab" * 32, "size_bytes": 1},
                    {"relative_path": "b.parquet", "sha256": "cd" * 32, "size_bytes": 2},
                ]
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    h = source_hashes_from_l0_fingerprint(fp)
    assert h == [f"sha256:{'ab' * 32}", f"sha256:{'cd' * 32}"]


def test_merge_source_hashes_truncates_to_partitions(tmp_path: Path) -> None:
    fp = tmp_path / "snapshot_fingerprint.json"
    fp.write_text(
        json.dumps({"inputs": [{"sha256": "11" * 32}, {"sha256": "22" * 32}]}),
        encoding="utf-8",
    )
    m = {
        "source_partitions": ["l1/a", "l1/b"],
        "source_hashes": ["sha256:unknown"],
    }
    out = merge_source_hashes_into_manifest(m, fp)
    assert out["source_hashes"] == [f"sha256:{'11' * 32}", f"sha256:{'22' * 32}"]


def test_merge_ingestion_delay_summary() -> None:
    m = {"artifact_kind": "run_fact", "ingestion_delay_summary": {"ingest_delay_p50_sec": None}}
    s = {"ingest_delay_p50_sec": 1.0, "ingest_delay_p95_sec": None, "ingest_delay_p99_sec": None, "ingest_delay_max_sec": None, "late_row_count": None, "late_row_ratio": None, "affected_run_count": None, "affected_trip_count": None}
    out = merge_ingestion_delay_summary(m, s)
    assert out["ingestion_delay_summary"]["ingest_delay_p50_sec"] == 1.0
