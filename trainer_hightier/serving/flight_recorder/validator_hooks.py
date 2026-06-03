"""Validator cycle hooks: alerts, ClickHouse ground-truth, decision traces."""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from zoneinfo import ZoneInfo

from trainer_hightier.config import HK_TZ
from trainer_hightier.serving.flight_recorder.ch_capture import (
    build_validator_bet_id_query_record,
    build_validator_canonical_query_record,
    save_clickhouse_capture,
)
from trainer_hightier.serving.flight_recorder.config import FlightRecorderConfig
from trainer_hightier.serving.flight_recorder.context import RecorderContext
from trainer_hightier.serving.flight_recorder.failure import cycle_policy_fields, handle_recorder_failure
from trainer_hightier.serving.flight_recorder.manifest import RecordingRoot
from trainer_hightier.serving.flight_recorder.session import get_active_validator_recorder
from trainer_hightier.serving.flight_recorder.parquet_io import write_parquet_safe

logger = logging.getLogger(__name__)

_TRACE_COLUMNS: tuple[str, ...] = (
    "bet_id",
    "result",
    "reason",
    "gap_start",
    "gap_minutes",
    "validated_at",
    "alert_ts",
    "bet_ts",
    "canonical_id",
    "player_id",
    "score",
    "model_version",
)


@dataclass
class ValidatorCycleRecorder:
    """Records one validator cycle under ``cycles/validator/cycle_NNNNNN/``."""

    recording: RecordingRoot
    config: FlightRecorderConfig
    enabled: bool = True
    cycle_dir: Path | None = None
    ch_dir: Path | None = None
    alerts_dir: Path | None = None
    decisions_dir: Path | None = None
    partial: bool = False
    failed_steps: list[str] = field(default_factory=list)
    _decision_rows: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_recorder_context(cls, ctx: RecorderContext) -> ValidatorCycleRecorder:
        """Build recorder bound to an open :class:`RecorderContext`."""
        capture = ctx.config.capture_validator_stages and ctx.config.enabled
        return cls(recording=ctx.recording, config=ctx.config, enabled=capture)

    def _mark_partial(self, step: str) -> None:
        """Record a partial failure for fail-open debug runs."""
        self.partial = True
        self.failed_steps.append(step)

    def _on_failure(
        self,
        step: str,
        exc: BaseException,
        *,
        artifact_path: Path | None = None,
    ) -> None:
        """Apply configured fail-fast or fail-open policy."""
        handle_recorder_failure(
            role="validator",
            step=step,
            exc=exc,
            config=self.config,
            mark_partial=lambda: self._mark_partial(step),
            artifact_path=artifact_path,
        )

    def begin_cycle(self, *, n_alerts: int, n_pending: int) -> None:
        """Allocate validator cycle directories."""
        if not self.enabled:
            return
        try:
            self.cycle_dir = self.recording.next_validator_cycle_dir()
            sub = {
                "clickhouse": self.cycle_dir / "clickhouse",
                "alerts": self.cycle_dir / "alerts",
                "decisions": self.cycle_dir / "decisions",
            }
            for path in sub.values():
                path.mkdir(parents=True, exist_ok=True)
            self.ch_dir, self.alerts_dir, self.decisions_dir = (
                sub["clickhouse"],
                sub["alerts"],
                sub["decisions"],
            )
            manifest = {
                **cycle_policy_fields(self.config),
                "role": "validator",
                "started_at_utc": datetime.now(timezone.utc).isoformat(),
                "hostname": socket.gethostname(),
                "python": sys.executable,
                "pid": os.getpid(),
                "model_version": self.recording.model_version,
                "n_alerts": n_alerts,
                "n_pending": n_pending,
            }
            (self.cycle_dir / "cycle_manifest.json").write_text(
                json.dumps(manifest, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            self._on_failure("begin_cycle", exc, artifact_path=self.cycle_dir)

    def capture_pending_alerts(self, pending: pd.DataFrame) -> None:
        """Write pending alerts consumed this cycle."""
        if not self.enabled or self.alerts_dir is None:
            return
        try:
            out = self.alerts_dir / "pending_alerts.parquet"
            write_parquet_safe(out, pending)
        except Exception as exc:
            self._on_failure("capture_pending_alerts", exc, artifact_path=self.alerts_dir)

    def capture_canonical_fetch(
        self,
        raw_frames: list[pd.DataFrame],
        *,
        player_ids: list[int],
        fetch_start: datetime,
        fetch_end: datetime,
    ) -> None:
        """Store canonical-id ground-truth query + merged Parquet."""
        if not self.enabled or self.ch_dir is None:
            return
        try:
            merged = (
                pd.concat(raw_frames, ignore_index=True)
                if raw_frames
                else pd.DataFrame()
            )
            meta = build_validator_canonical_query_record(
                player_ids=player_ids,
                start=fetch_start,
                end=fetch_end,
            )
            save_clickhouse_capture(
                self.ch_dir,
                "fetch_bets_by_canonical_id",
                merged,
                meta,
            )
            if self.config.capture_ch_diagnostic_requery and self.cycle_dir is not None:
                from trainer_hightier.serving.flight_recorder.window_registry import register_window

                rel_t0 = (
                    self.ch_dir / "fetch_bets_by_canonical_id.final.parquet"
                ).relative_to(self.recording.root).as_posix()
                register_window(
                    self.recording.root,
                    source=f"{self.cycle_dir.relative_to(self.recording.root).as_posix()}/clickhouse",
                    fetch=str(meta.get("fetch", "fetch_bets_by_canonical_id")),
                    query_meta=meta,
                    t0_final_parquet=rel_t0,
                )
        except Exception as exc:
            self._on_failure("capture_canonical_fetch", exc, artifact_path=self.ch_dir)

    def capture_bet_id_lookup(
        self,
        raw_frames: list[pd.DataFrame],
        *,
        bet_ids: list[int],
    ) -> None:
        """Store no-bet ``bet_id`` lookup query + raw rows."""
        if not self.enabled or self.ch_dir is None:
            return
        try:
            merged = (
                pd.concat(raw_frames, ignore_index=True)
                if raw_frames
                else pd.DataFrame()
            )
            meta = build_validator_bet_id_query_record(bet_ids=bet_ids)
            save_clickhouse_capture(
                self.ch_dir,
                "validator_no_bet_bet_id_lookup",
                merged,
                meta,
            )
            if self.config.capture_ch_diagnostic_requery and self.cycle_dir is not None:
                from trainer_hightier.serving.flight_recorder.window_registry import register_window

                rel_t0 = (
                    self.ch_dir / "validator_no_bet_bet_id_lookup.final.parquet"
                ).relative_to(self.recording.root).as_posix()
                register_window(
                    self.recording.root,
                    source=f"{self.cycle_dir.relative_to(self.recording.root).as_posix()}/clickhouse",
                    fetch=str(meta.get("fetch", "fetch_bet_payout_times_by_bet_ids")),
                    query_meta=meta,
                    t0_final_parquet=rel_t0,
                )
        except Exception as exc:
            self._on_failure("capture_bet_id_lookup", exc, artifact_path=self.ch_dir)

    def record_decision(self, res: dict[str, Any]) -> None:
        """Append one alert decision (including PENDING) to in-memory trace."""
        if not self.enabled:
            return
        row = {col: res.get(col) for col in _TRACE_COLUMNS}
        if res.get("result") is None and res.get("reason") is None:
            row["reason"] = "skipped_too_recent"
        self._decision_rows.append(row)

    def finish_cycle(self, *, verified_count: int) -> None:
        """Flush decision trace and finalize cycle manifest."""
        if not self.enabled or self.cycle_dir is None:
            return
        try:
            if self.decisions_dir is not None:
                trace = pd.DataFrame(self._decision_rows)
                trace_path = self.decisions_dir / "decision_trace.parquet"
                write_parquet_safe(trace_path, trace)
            summary = {
                "verified_this_cycle": verified_count,
                "n_decisions_recorded": len(self._decision_rows),
            }
            if self.decisions_dir is not None:
                (self.decisions_dir / "cycle_summary.json").write_text(
                    json.dumps(summary, indent=2),
                    encoding="utf-8",
                )
            manifest_path = self.cycle_dir / "cycle_manifest.json"
            existing: dict[str, Any] = {}
            if manifest_path.is_file():
                existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            existing.update(
                {
                    "ended_at_utc": datetime.now(timezone.utc).isoformat(),
                    "ended_at_hk": datetime.now(ZoneInfo(HK_TZ)).isoformat(),
                    "recorder_partial": self.partial,
                    "recorder_failed_steps": list(self.failed_steps),
                    **summary,
                }
            )
            manifest_path.write_text(json.dumps(existing, indent=2, default=str), encoding="utf-8")
            if self.partial:
                self.recording.partial = True
        except Exception as exc:
            self._on_failure("finish_cycle", exc, artifact_path=self.cycle_dir)


def on_validate_begin(*, n_alerts: int, n_pending: int, pending: pd.DataFrame) -> None:
    """Hook when validator begins processing pending alerts."""
    rec = get_active_validator_recorder()
    if rec is None:
        return
    rec.begin_cycle(n_alerts=n_alerts, n_pending=n_pending)
    rec.capture_pending_alerts(pending)


def on_canonical_fetch(
    raw_frames: list[pd.DataFrame],
    *,
    player_ids: list[int],
    fetch_start: datetime,
    fetch_end: datetime,
) -> None:
    """Hook after ``fetch_bets_by_canonical_id`` completes."""
    rec = get_active_validator_recorder()
    if rec is None:
        return
    rec.capture_canonical_fetch(
        raw_frames,
        player_ids=player_ids,
        fetch_start=fetch_start,
        fetch_end=fetch_end,
    )


def on_bet_id_lookup(raw_frames: list[pd.DataFrame], *, bet_ids: list[int]) -> None:
    """Hook after no-bet ``bet_id`` lookup."""
    rec = get_active_validator_recorder()
    if rec is None:
        return
    rec.capture_bet_id_lookup(raw_frames, bet_ids=bet_ids)


def on_alert_decision(res: dict[str, Any]) -> None:
    """Hook after each ``validate_alert_row`` (or equivalent) verdict."""
    rec = get_active_validator_recorder()
    if rec is None:
        return
    rec.record_decision(res)


def on_validate_end(*, verified_count: int) -> None:
    """Hook at end of ``_validate_alerts_once`` (including early exits)."""
    rec = get_active_validator_recorder()
    if rec is None:
        return
    rec.finish_cycle(verified_count=verified_count)
