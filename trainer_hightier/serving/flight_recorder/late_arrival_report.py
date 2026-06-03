"""Aggregate ClickHouse time-machine diff reports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def collect_ch_diff_reports(recording_root: Path) -> dict[str, Any]:
    """Scan ``ch_time_machine/window_*/diffs/*.json`` and summarize."""
    tm_root = recording_root / "ch_time_machine"
    windows: list[dict[str, Any]] = []
    if not tm_root.is_dir():
        return {"n_windows": 0, "windows": windows}
    for window_dir in sorted(tm_root.glob("window_*")):
        diffs_dir = window_dir / "diffs"
        if not diffs_dir.is_dir():
            continue
        entry: dict[str, Any] = {"window_id": window_dir.name, "diffs": {}}
        for diff_file in sorted(diffs_dir.glob("*.json")):
            payload = json.loads(diff_file.read_text(encoding="utf-8"))
            entry["diffs"][diff_file.name] = {
                "added_keys_count": payload.get("added_keys_count"),
                "removed_keys_count": payload.get("removed_keys_count"),
                "changed_keys_count": payload.get("changed_keys_count"),
            }
        windows.append(entry)
    return {"n_windows": len(windows), "windows": windows}


def write_late_arrival_reports(output_dir: Path, recording_root: Path) -> dict[str, Path]:
    """Write ``clickhouse_late_arrival_report.json`` and ``final_vs_non_final_report.json``."""
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = collect_ch_diff_reports(recording_root)
    late_path = output_dir / "clickhouse_late_arrival_report.json"
    late_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    final_vs: dict[str, Any] = {}
    for win in summary.get("windows", []):
        ff = (win.get("diffs") or {}).get("final_vs_non_final.json")
        if ff:
            final_vs[win["window_id"]] = ff
    fv_path = output_dir / "final_vs_non_final_report.json"
    fv_path.write_text(json.dumps(final_vs, indent=2), encoding="utf-8")
    return {"clickhouse_late_arrival_report": late_path, "final_vs_non_final_report": fv_path}
