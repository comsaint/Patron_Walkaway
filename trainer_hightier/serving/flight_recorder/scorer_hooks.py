"""Scorer cycle hooks: stage snapshots, ClickHouse capture, cycle audits."""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
from zoneinfo import ZoneInfo

from trainer_hightier.config import HK_TZ, default_hightier_serving_config
from trainer_hightier.serving.feast_online_adapter import RowMissingAudit
from trainer_hightier.serving.feature_supply import ScorerSupplierPlan
from trainer_hightier.serving.flight_recorder.ch_capture import (
    build_incremental_query_record,
    build_pool_query_record,
    save_clickhouse_capture,
)
from trainer_hightier.serving.flight_recorder.config import FlightRecorderConfig
from trainer_hightier.serving.flight_recorder.context import RecorderContext
from trainer_hightier.serving.flight_recorder.manifest import RecordingRoot
from trainer_hightier.serving.flight_recorder.provenance import build_feature_missing_provenance
from trainer_hightier.serving.flight_recorder.parquet_io import write_parquet_safe
from trainer_hightier.serving.flight_recorder.session import get_active_scorer_recorder

logger = logging.getLogger(__name__)


@dataclass
class _ScoringBatchView:
    """Minimal batch view for hooks (avoids importing private scorer types)."""

    bets: pd.DataFrame
    pool: pd.DataFrame
    cursor: pd.Series


@dataclass
class ScorerCycleRecorder:
    """Records one scorer cycle under ``cycles/scorer/cycle_NNNNNN/``."""

    recording: RecordingRoot
    config: FlightRecorderConfig
    enabled: bool = True
    cycle_dir: Path | None = None
    ch_dir: Path | None = None
    stages_dir: Path | None = None
    audits_dir: Path | None = None
    partial: bool = False
    failed_steps: list[str] = field(default_factory=list)
    _pool_window: tuple[datetime, datetime] | None = None

    @classmethod
    def from_recorder_context(cls, ctx: RecorderContext) -> ScorerCycleRecorder:
        """Build recorder bound to an open :class:`RecorderContext`."""
        capture = ctx.config.capture_scorer_stages and ctx.config.enabled
        return cls(
            recording=ctx.recording,
            config=ctx.config,
            enabled=capture,
        )

    def _fail_open(self, step: str, exc: BaseException) -> None:
        """Record a partial failure without raising to the scorer loop."""
        self.partial = True
        self.failed_steps.append(step)
        logger.warning("[flight_recorder] scorer hook %s failed: %s", step, exc)

    def begin_cycle(
        self,
        *,
        high_adt_only: bool,
        allowlist_size: int,
        last_etl: Any,
        lookback_hours: float,
        limit_rows: int,
    ) -> None:
        """Allocate cycle directories and write ``cycle_manifest.json`` skeleton."""
        if not self.enabled:
            return
        try:
            self.cycle_dir = self.recording.next_scorer_cycle_dir()
            sub = {
                "clickhouse": self.cycle_dir / "clickhouse",
                "stages": self.cycle_dir / "stages",
                "audits": self.cycle_dir / "audits",
            }
            for path in sub.values():
                path.mkdir(parents=True, exist_ok=True)
            self.ch_dir, self.stages_dir, self.audits_dir = (
                sub["clickhouse"],
                sub["stages"],
                sub["audits"],
            )
            manifest = {
                "role": "scorer",
                "started_at_utc": datetime.now(timezone.utc).isoformat(),
                "hostname": socket.gethostname(),
                "python": sys.executable,
                "pid": os.getpid(),
                "model_version": self.recording.model_version,
                "high_adt_only": high_adt_only,
                "allowlist_size": allowlist_size,
                "lookback_hours": lookback_hours,
                "limit_rows": limit_rows,
                "last_etl": None if last_etl is None else str(last_etl),
            }
            path = self.cycle_dir / "cycle_manifest.json"
            path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        except Exception as exc:
            self._fail_open("begin_cycle", exc)

    def capture_batch(
        self,
        batch: _ScoringBatchView,
        *,
        last_etl: Any,
        lookback_hours: float,
        limit_rows: int,
        allowlist_ids: frozenset[int],
        high_adt_only: bool,
        pool_window_start: datetime,
        pool_window_end: datetime,
    ) -> None:
        """Capture incremental bets, pool, and stage-00/04 snapshots."""
        if not self.enabled or self.ch_dir is None:
            return
        allowlist_arg: Optional[frozenset[int]] = allowlist_ids if high_adt_only else None
        try:
            inc_meta = build_incremental_query_record(
                last_etl=last_etl,
                lookback_hours=lookback_hours,
                limit_rows=limit_rows,
                allowlist_player_ids=allowlist_arg,
            )
            save_clickhouse_capture(
                self.ch_dir,
                "incremental_t_bet",
                batch.bets,
                inc_meta,
            )
            if self.config.capture_ch_diagnostic_requery and self.cycle_dir is not None:
                from trainer_hightier.serving.flight_recorder.window_registry import register_window

                rel_t0 = (
                    self.ch_dir / "incremental_t_bet.final.parquet"
                ).relative_to(self.recording.root).as_posix()
                register_window(
                    self.recording.root,
                    source=f"{self.cycle_dir.relative_to(self.recording.root).as_posix()}/clickhouse",
                    fetch=str(inc_meta.get("fetch", "fetch_bets_incremental")),
                    query_meta=inc_meta,
                    t0_final_parquet=rel_t0,
                )
            pool_meta = build_pool_query_record(
                player_ids=sorted({int(x) for x in batch.bets["player_id"].dropna().unique()}),
                window_start=pool_window_start,
                window_end=pool_window_end,
            )
            save_clickhouse_capture(self.ch_dir, "short_term_pool_t_bet", batch.pool, pool_meta)
            if self.config.capture_ch_diagnostic_requery and self.cycle_dir is not None:
                from trainer_hightier.serving.flight_recorder.window_registry import register_window

                rel_pool = (
                    self.ch_dir / "short_term_pool_t_bet.final.parquet"
                ).relative_to(self.recording.root).as_posix()
                register_window(
                    self.recording.root,
                    source=f"{self.cycle_dir.relative_to(self.recording.root).as_posix()}/clickhouse",
                    fetch=str(pool_meta.get("fetch", "fetch_bet_pool_window")),
                    query_meta=pool_meta,
                    t0_final_parquet=rel_pool,
                )
            self._pool_window = (pool_window_start, pool_window_end)
            self.record_stage("stage_00_raw_clickhouse_bets", batch.bets)
            self.record_stage("stage_04_short_term_pool", batch.pool)
        except Exception as exc:
            self._fail_open("capture_batch", exc)

    def record_stage(self, filename: str, frame: pd.DataFrame) -> None:
        """Write one stage Parquet under ``stages/``."""
        if not self.enabled or self.stages_dir is None:
            return
        try:
            out = self.stages_dir / f"{filename}.parquet"
            write_parquet_safe(out, frame if frame is not None else pd.DataFrame())
        except Exception as exc:
            self._fail_open(filename, exc)

    def finish_cycle(
        self,
        *,
        n_batch_rows: int,
        n_alerts: int,
        prob: np.ndarray | None,
        staged: pd.DataFrame | None,
        features: pd.DataFrame | None,
        feature_columns: tuple[str, ...] | None,
        supplier_plan: ScorerSupplierPlan | None,
        row_audits: list[RowMissingAudit] | None,
        cycle_readiness: dict[str, Any] | None,
    ) -> None:
        """Write audits (row counts, provenance, score distribution) and update manifest."""
        if not self.enabled or self.audits_dir is None or self.cycle_dir is None:
            return
        try:
            if prob is not None and staged is not None and len(staged) == len(prob):
                scores_df = staged.copy()
                scores_df["score"] = np.asarray(prob, dtype=np.float64)
                self.record_stage("stage_09_scores", scores_df)
            row_counts = {
                "n_batch_rows": n_batch_rows,
                "n_alerts": n_alerts,
                "n_scored_rows": int(len(staged)) if staged is not None else 0,
            }
            (self.audits_dir / "row_counts.json").write_text(
                json.dumps(row_counts, indent=2),
                encoding="utf-8",
            )
            if prob is not None and len(prob) > 0:
                scores = pd.Series(prob, dtype=np.float64)
                dist = {
                    "count": int(scores.count()),
                    "mean": float(scores.mean()),
                    "min": float(scores.min()),
                    "max": float(scores.max()),
                    "p50": float(scores.quantile(0.5)),
                    "p90": float(scores.quantile(0.9)),
                    "p99": float(scores.quantile(0.99)),
                }
                (self.audits_dir / "score_distribution.json").write_text(
                    json.dumps(dist, indent=2),
                    encoding="utf-8",
                )
            if (
                staged is not None
                and features is not None
                and feature_columns
                and supplier_plan is not None
            ):
                prov = build_feature_missing_provenance(
                    staged,
                    features,
                    feature_columns=feature_columns,
                    supplier_plan=supplier_plan,
                    row_audits=row_audits,
                )
                prov_path = self.audits_dir / "feature_missing_provenance.parquet"
                write_parquet_safe(prov_path, prov)
            if cycle_readiness:
                (self.audits_dir / "feature_supplier_diagnostics.json").write_text(
                    json.dumps(cycle_readiness, indent=2, default=str),
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
                }
            )
            manifest_path.write_text(json.dumps(existing, indent=2, default=str), encoding="utf-8")
            if self.partial:
                self.recording.partial = True
        except Exception as exc:
            self._fail_open("finish_cycle", exc)


def on_score_once_empty(*, model_version: str) -> None:
    """Hook when ``score_once`` returns early with no batch."""
    rec = get_active_scorer_recorder()
    if rec is None:
        return
    rec.finish_cycle(n_batch_rows=0, n_alerts=0, prob=None, staged=None, features=None,
                     feature_columns=None, supplier_plan=None, row_audits=None, cycle_readiness=None)


def on_score_once_begin(
    *,
    high_adt_only: bool,
    allowlist_ids: frozenset[int],
    last_etl: Any,
) -> None:
    """Hook at start of ``score_once`` before batch fetch."""
    rec = get_active_scorer_recorder()
    if rec is None:
        return
    cfg = default_hightier_serving_config()
    rec.begin_cycle(
        high_adt_only=high_adt_only,
        allowlist_size=len(allowlist_ids),
        last_etl=last_etl,
        lookback_hours=float(cfg.scorer_dynamic_lookback_cap_hours),
        limit_rows=int(cfg.hightier_scorer_max_bets_per_cycle),
    )


def on_batch_ready(
    batch: Any,
    *,
    last_etl: Any,
    high_adt_only: bool,
    allowlist_ids: frozenset[int],
    pool_window_start: datetime,
    pool_window_end: datetime,
) -> None:
    """Hook after ``_fetch_scoring_batch`` returns a non-empty batch."""
    rec = get_active_scorer_recorder()
    if rec is None:
        return
    cfg = default_hightier_serving_config()
    view = _ScoringBatchView(bets=batch.bets, pool=batch.pool, cursor=batch.cursor)
    rec.capture_batch(
        view,
        last_etl=last_etl,
        lookback_hours=float(cfg.scorer_dynamic_lookback_cap_hours),
        limit_rows=int(cfg.hightier_scorer_max_bets_per_cycle),
        allowlist_ids=allowlist_ids,
        high_adt_only=high_adt_only,
        pool_window_start=pool_window_start,
        pool_window_end=pool_window_end,
    )


def on_stage(frame: pd.DataFrame, stage_name: str) -> None:
    """Hook after a scoring stage DataFrame is materialized."""
    rec = get_active_scorer_recorder()
    if rec is None:
        return
    rec.record_stage(stage_name, frame)


def on_score_once_end(
    *,
    n_batch_rows: int,
    n_alerts: int,
    prob: np.ndarray | None,
    staged: pd.DataFrame | None,
    features: pd.DataFrame | None,
    feature_columns: tuple[str, ...],
    supplier_plan: ScorerSupplierPlan,
    row_audits: list[RowMissingAudit] | None,
    cycle_readiness: dict[str, Any] | None,
) -> None:
    """Hook at end of ``score_once`` (all exit paths should call this)."""
    rec = get_active_scorer_recorder()
    if rec is None:
        return
    if staged is not None and prob is not None and len(staged) == len(prob):
        scores_df = staged.copy()
        scores_df["score"] = np.asarray(prob, dtype=np.float64)
        rec.record_stage("stage_09_scores", scores_df)
    rec.finish_cycle(
        n_batch_rows=n_batch_rows,
        n_alerts=n_alerts,
        prob=prob,
        staged=staged,
        features=features,
        feature_columns=feature_columns,
        supplier_plan=supplier_plan,
        row_audits=row_audits,
        cycle_readiness=cycle_readiness,
    )
