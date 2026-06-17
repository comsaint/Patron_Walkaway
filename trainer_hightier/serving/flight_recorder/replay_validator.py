"""Offline validator replay from captured cycle artifacts."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from trainer_hightier.serving.validator import validate_alert_row


def _build_bet_cache(
    ch_frame: pd.DataFrame,
    pending: pd.DataFrame,
) -> dict[str, list[datetime]]:
    """Rebuild ``bet_cache`` keyed by canonical_id from captured CH rows."""
    pid_to_cid: dict[int, str] = {}
    for _, row in pending.iterrows():
        pid_raw = row.get("player_id")
        if pid_raw is None or pd.isna(pid_raw):
            continue
        cid_raw = row.get("canonical_id")
        cid = (
            str(int(pid_raw))
            if (cid_raw is None or pd.isna(cid_raw) or str(cid_raw).strip() == "")
            else str(cid_raw)
        )
        pid_to_cid[int(pid_raw)] = cid
    cache: dict[str, list[datetime]] = {}
    if ch_frame.empty:
        return cache
    work = ch_frame.copy()
    work["_payout"] = pd.to_datetime(work["payout_complete_dtm"], errors="coerce")
    for _, row in work.iterrows():
        pid = row.get("player_id")
        payout = row.get("_payout")
        if pid is None or pd.isna(pid) or pd.isna(payout):
            continue
        cid = pid_to_cid.get(int(pid), str(int(pid)))
        cache.setdefault(cid, []).append(payout.to_pydatetime())
    for cid in cache:
        cache[cid] = sorted(cache[cid])
    return cache


def replay_one_validator_cycle(cycle_dir: Path) -> dict[str, Any]:
    """Replay validator decisions for one cycle."""
    trace_path = cycle_dir / "decisions" / "decision_trace.parquet"
    pending_path = cycle_dir / "alerts" / "pending_alerts.parquet"
    ch_path = cycle_dir / "clickhouse" / "fetch_bets_by_canonical_id.final.parquet"
    if not trace_path.is_file():
        return {"cycle": cycle_dir.name, "status": "skipped", "detail": "no decision_trace"}
    trace = pd.read_parquet(trace_path)
    pending = pd.read_parquet(pending_path) if pending_path.is_file() else pd.DataFrame()
    ch_frame = pd.read_parquet(ch_path) if ch_path.is_file() else pd.DataFrame()
    bet_cache = _build_bet_cache(ch_frame, pending)
    session_cache: dict[str, list[dict[str, Any]]] = {}
    mismatches: list[dict[str, Any]] = []
    compared = 0
    matched = 0
    for _, prod in trace.iterrows():
        if prod.get("result") is None and prod.get("reason") in (None, "", "skipped_too_recent"):
            continue
        bet_id = prod.get("bet_id")
        if bet_id is None or (isinstance(bet_id, float) and pd.isna(bet_id)):
            continue
        alert_rows = pending[pending["bet_id"].astype(str) == str(bet_id)]
        if alert_rows.empty:
            continue
        replay = validate_alert_row(
            alert_rows.iloc[0],
            bet_cache,
            session_cache,
            force_finalize=False,
        )
        compared += 1
        prod_result = prod.get("result")
        replay_result = replay.get("result")
        prod_reason = str(prod.get("reason") or "")
        replay_reason = str(replay.get("reason") or "")
        ok = prod_result == replay_result and prod_reason == replay_reason
        if ok:
            matched += 1
        else:
            mismatches.append(
                {
                    "bet_id": str(bet_id),
                    "prod_result": prod_result,
                    "replay_result": replay_result,
                    "prod_reason": prod_reason,
                    "replay_reason": replay_reason,
                }
            )
    return {
        "cycle": cycle_dir.name,
        "status": "ok" if compared else "skipped",
        "n_compared": compared,
        "n_match": matched,
        "match_rate": float(matched / compared) if compared else None,
        "mismatches_sample": mismatches[:20],
    }


def run_validator_replay(recording_root: Path) -> dict[str, Any]:
    """Replay all validator cycles."""
    val_dir = recording_root / "cycles" / "validator"
    per_cycle: list[dict[str, Any]] = []
    if val_dir.is_dir():
        for cycle_dir in sorted(val_dir.glob("cycle_*")):
            per_cycle.append(replay_one_validator_cycle(cycle_dir))
    ok = [c for c in per_cycle if c.get("n_compared")]
    total = sum(int(c.get("n_compared", 0)) for c in ok)
    match = sum(int(c.get("n_match", 0)) for c in ok)
    return {
        "n_cycles": len(per_cycle),
        "n_cycles_compared": len(ok),
        "total_compared": total,
        "total_match": match,
        "overall_match_rate": float(match / total) if total else None,
        "per_cycle": per_cycle,
    }


def write_validator_replay_report(output_dir: Path, report: dict[str, Any]) -> Path:
    """Write ``validator_replay_diff_report.json``."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "validator_replay_diff_report.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return path
