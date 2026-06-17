"""ClickHouse time-machine: scheduled requery and diff reports."""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from trainer_hightier.serving.flight_recorder.ch_requery import (
    execute_query,
    rebuild_query_record,
    requery_skip_reason,
)
from trainer_hightier.serving.flight_recorder.config import (
    DEFAULT_CONFIG_REL,
    FlightRecorderConfig,
)
from trainer_hightier.serving.flight_recorder.diff import diff_dataframes, write_diff_report
from trainer_hightier.serving.flight_recorder.window_registry import (
    list_windows,
    mark_capture_done,
    pending_capture_labels,
)

logger = logging.getLogger(__name__)


def _bootstrap_bundle_clickhouse(bundle_dir: Path) -> None:
    """Load bundle ``.env`` and serving CH config (same contract as deploy ``main``)."""
    from trainer_hightier.config import (
        apply_hightier_serving_environ_overrides,
        default_hightier_serving_config,
        set_hightier_serving_deploy_override,
    )
    from trainer_hightier.deploy.main import (
        _load_dotenv_if_present,
        _load_rel_paths,
        _serving_config_for_bundle,
    )
    from trainer_hightier.serving.ch_adapter import get_clickhouse_client

    _load_dotenv_if_present(bundle_dir)
    try:
        rel = _load_rel_paths(bundle_dir)
        cfg = _serving_config_for_bundle(bundle_dir, rel)
    except FileNotFoundError:
        cfg = default_hightier_serving_config()
    cfg = apply_hightier_serving_environ_overrides(cfg)
    set_hightier_serving_deploy_override(cfg)
    get_clickhouse_client.cache_clear()  # type: ignore[attr-defined]


_SYSTEM_TABLE_PROBES: tuple[str, ...] = (
    "system.query_log",
    "system.parts",
    "system.mutations",
    "system.part_log",
)


def probe_system_tables(bundle_dir: Path, recording_root: Path) -> Path:
    """Probe read access to ClickHouse system tables."""
    out_dir = recording_root / "permissions"
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "clickhouse_system_table_permissions.json"
    results: list[dict[str, Any]] = []
    try:
        from trainer_hightier.serving.ch_adapter import get_clickhouse_client

        client = get_clickhouse_client()
    except Exception as exc:
        payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "tables": results}
        report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return report_path
    for table in _SYSTEM_TABLE_PROBES:
        entry: dict[str, Any] = {"table": table}
        try:
            client.query(f"SELECT 1 FROM {table} LIMIT 1")
            entry["status"] = "ok"
        except Exception as exc:
            entry["status"] = "error"
            entry["error"] = f"{type(exc).__name__}: {exc}"
        results.append(entry)
    payload = {
        "ok": any(r.get("status") == "ok" for r in results),
        "probed_at_utc": datetime.now(timezone.utc).isoformat(),
        "bundle_dir": str(bundle_dir),
        "tables": results,
    }
    report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return report_path


def _schedule_label_to_minutes(label: str) -> int:
    """Map capture label to schedule minutes."""
    if label == "t0":
        return 0
    if label.startswith("t_plus_") and label.endswith("m"):
        return int(label.removeprefix("t_plus_").removesuffix("m"))
    return 0


def _capture_dir(recording_root: Path, window_id: str, label: str) -> Path:
    """Return capture subdirectory for one schedule point."""
    return recording_root / "ch_time_machine" / window_id / f"capture_{label}"


def _write_capture_error(
    cap_dir: Path,
    *,
    window_id: str,
    label: str,
    fetch: str,
    exc: BaseException,
) -> None:
    """Persist a failed capture without marking the window complete."""
    payload = {
        "window_id": window_id,
        "capture_label": label,
        "fetch": fetch,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "error_type": type(exc).__name__,
        "error": str(exc),
    }
    cap_dir.mkdir(parents=True, exist_ok=True)
    (cap_dir / "capture_error.json").write_text(
        json.dumps(payload, indent=2, default=str),
        encoding="utf-8",
    )
    diffs_dir = cap_dir.parent / "diffs"
    diffs_dir.mkdir(parents=True, exist_ok=True)
    write_diff_report(diffs_dir / f"t0_vs_{label}.json", payload)


def _validate_capture_frame(frame: pd.DataFrame, *, business_key: str, fetch: str) -> None:
    """Reject requery frames that cannot participate in key-based diff."""
    if business_key in frame.columns:
        return
    raise ValueError(
        f"requery returned no {business_key!r} column for fetch={fetch}; "
        f"rows={len(frame)} columns={list(frame.columns)}"
    )


def _base_readiness_summary(
    recording_root: Path,
    config: FlightRecorderConfig,
) -> dict[str, Any]:
    """Return common readiness fields for zero-capture diagnostics."""
    registry_path = recording_root / "ch_time_machine" / "windows.json"
    return {
        "capture_ch_diagnostic_requery": bool(config.capture_ch_diagnostic_requery),
        "recording_root": str(recording_root),
        "registry_exists": registry_path.is_file(),
        "windows": 0,
        "pending_labels": 0,
        "due_labels": 0,
        "next_due_in_seconds": None,
    }


def _readiness_reason(*, pending_count: int, due_count: int) -> str:
    """Classify pending capture state for operator logs."""
    if due_count > 0:
        return "due_captures_available"
    if pending_count == 0:
        return "no_pending_labels"
    return "pending_not_due_yet"


def _capture_readiness_counts(
    recording_root: Path,
    config: FlightRecorderConfig,
    *,
    now_ts: float,
) -> dict[str, Any]:
    """Count windows, pending labels, due labels, and next due delay."""
    next_due_in: float | None = None
    due_count = 0
    pending_count = 0
    windows = list_windows(recording_root)
    for window in windows:
        registered = datetime.fromisoformat(str(window["registered_at_utc"]).replace("Z", "+00:00"))
        for label in pending_capture_labels(window, config.requery_schedule_minutes):
            pending_count += 1
            due_at = registered.timestamp() + _schedule_label_to_minutes(label) * 60
            remaining = due_at - now_ts
            if remaining <= 0:
                due_count += 1
            elif next_due_in is None or remaining < next_due_in:
                next_due_in = remaining
    return {
        "windows": len(windows),
        "pending_labels": pending_count,
        "due_labels": due_count,
        "next_due_in_seconds": None if next_due_in is None else int(max(0, next_due_in)),
        "reason": _readiness_reason(pending_count=pending_count, due_count=due_count),
    }


def summarize_capture_readiness(
    recording_root: Path,
    config: FlightRecorderConfig,
    *,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Summarize why a time-machine pass may have no due captures."""
    registry_path = recording_root / "ch_time_machine" / "windows.json"
    summary = _base_readiness_summary(recording_root, config)
    if not config.capture_ch_diagnostic_requery:
        summary["reason"] = "capture_ch_diagnostic_requery_disabled"
        return summary
    if not registry_path.is_file():
        summary["reason"] = "window_registry_missing"
        return summary
    now = time.time() if now_ts is None else float(now_ts)
    summary.update(_capture_readiness_counts(recording_root, config, now_ts=now))
    return summary


def run_window_capture(
    recording_root: Path,
    window: dict[str, Any],
    label: str,
    *,
    include_non_final: bool,
) -> None:
    """Execute one scheduled capture and write diffs vs t0."""
    window_id = str(window["window_id"])
    fetch = str(window.get("fetch", ""))
    query_meta = rebuild_query_record(fetch, dict(window.get("query_meta") or {}))
    cap_dir = _capture_dir(recording_root, window_id, label)
    cap_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "window_id": window_id,
        "capture_label": label,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "fetch": fetch,
        "query_meta": query_meta,
    }
    skip_reason = requery_skip_reason(query_meta)
    if skip_reason is not None:
        manifest["requery_skipped"] = skip_reason
    (cap_dir / "query_manifest.json").write_text(
        json.dumps(manifest, indent=2, default=str),
        encoding="utf-8",
    )
    if skip_reason is not None:
        diffs_dir = cap_dir.parent / "diffs"
        diffs_dir.mkdir(parents=True, exist_ok=True)
        write_diff_report(
            diffs_dir / f"t0_vs_{label}.json",
            {
                "skipped": True,
                "skip_reason": skip_reason,
                "capture_label": label,
                "fetch": fetch,
            },
        )
        mark_capture_done(recording_root, window_id, label)
        logger.info(
            "[ch_time_machine] skipped requery window=%s label=%s reason=%s",
            window_id,
            label,
            skip_reason,
        )
        return
    business_key = str(query_meta.get("business_key") or "bet_id")
    try:
        final_df = execute_query(query_meta, use_final=True)
        _validate_capture_frame(final_df, business_key=business_key, fetch=fetch)
        final_df.to_parquet(cap_dir / "t_bet.final.parquet", index=False)
        if include_non_final:
            non_final_df = execute_query(query_meta, use_final=False)
            _validate_capture_frame(non_final_df, business_key=business_key, fetch=fetch)
            non_final_df.to_parquet(cap_dir / "t_bet.non_final.parquet", index=False)
            diff_ff = diff_dataframes(final_df, non_final_df, business_key=business_key)
            write_diff_report(cap_dir.parent / "diffs" / "final_vs_non_final.json", diff_ff)
    except Exception as exc:
        _write_capture_error(cap_dir, window_id=window_id, label=label, fetch=fetch, exc=exc)
        raise
    t0_path = window.get("t0_final_parquet")
    if t0_path:
        t0_file = recording_root / str(t0_path)
        if t0_file.is_file():
            t0_df = pd.read_parquet(t0_file)
            diff_t0 = diff_dataframes(t0_df, final_df, business_key=business_key)
            write_diff_report(
                cap_dir.parent / "diffs" / f"t0_vs_{label}.json",
                diff_t0,
            )
    mark_capture_done(recording_root, window_id, label)


def run_pending_captures(
    recording_root: Path,
    config: FlightRecorderConfig,
) -> int:
    """Process all windows with pending schedule labels; return capture count."""
    if not config.capture_ch_diagnostic_requery:
        return 0
    count = 0
    schedule = config.requery_schedule_minutes
    for window in list_windows(recording_root):
        window_id = str(window["window_id"])
        registered = datetime.fromisoformat(str(window["registered_at_utc"]).replace("Z", "+00:00"))
        for label in pending_capture_labels(window, schedule):
            offset_min = _schedule_label_to_minutes(label)
            due_at = registered.timestamp() + offset_min * 60
            if time.time() < due_at:
                continue
            try:
                run_window_capture(
                    recording_root,
                    window,
                    label,
                    include_non_final=config.include_non_final_diagnostics,
                )
                count += 1
            except Exception as exc:
                logger.warning(
                    "[ch_time_machine] capture failed window=%s label=%s: %s",
                    window_id,
                    label,
                    exc,
                )
    return count


def main(argv: list[str] | None = None) -> int:
    """CLI entry for ClickHouse time-machine daemon."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="ClickHouse flight recorder time machine")
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--recording-root", type=Path, default=None)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--once", action="store_true", help="run one pending pass then exit")
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument("--no-system-probe", action="store_true")
    args = parser.parse_args(argv)
    bundle_dir = args.bundle_dir.resolve()
    _bootstrap_bundle_clickhouse(bundle_dir)
    cfg_path = args.config or (bundle_dir / DEFAULT_CONFIG_REL)
    config = (
        FlightRecorderConfig.from_yaml_path(cfg_path)
        if cfg_path.is_file()
        else FlightRecorderConfig()
    )
    recording_root = (
        args.recording_root.resolve()
        if args.recording_root is not None
        else config.resolve_recording_root(bundle_dir)
    )
    recording_root.mkdir(parents=True, exist_ok=True)
    if not args.no_system_probe and config.include_system_table_probes:
        probe_system_tables(bundle_dir, recording_root)
    if args.once:
        n = run_pending_captures(recording_root, config)
        if n == 0:
            logger.info(
                "[ch_time_machine] once pass completed captures=0 readiness=%s",
                summarize_capture_readiness(recording_root, config),
            )
        else:
            logger.info("[ch_time_machine] once pass completed captures=%d root=%s", n, recording_root)
        return 0
    while True:
        n = run_pending_captures(recording_root, config)
        if n == 0:
            logger.info(
                "[ch_time_machine] cycle captures=0 readiness=%s",
                summarize_capture_readiness(recording_root, config),
            )
        else:
            logger.info("[ch_time_machine] cycle captures=%d", n)
        time.sleep(max(30, int(args.interval_seconds)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
