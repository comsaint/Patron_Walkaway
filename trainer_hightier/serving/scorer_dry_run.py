"""Bounded scorer dry-run report (P6-3) — no live ClickHouse required for unit tests."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from trainer_hightier.config import HK_TZ, TRAINER_HIGHTIER_PACKAGE_DIR

logger = logging.getLogger(__name__)


def capture_process_rss_mb() -> float | None:
    """Best-effort resident set size in MiB (stdlib ``resource`` on Unix; Windows uses ru_maxrss quirk)."""
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        rss = float(usage.ru_maxrss)
        # Linux reports KiB; macOS reports bytes.
        import sys

        if sys.platform == "darwin":
            return round(rss / (1024.0 * 1024.0), 3)
        return round(rss / 1024.0, 3)
    except Exception:
        return None


@dataclass(frozen=True)
class ScorerDryRunReport:
    """One ``--once`` cycle metrics for laptop / production box acceptance checks."""

    generated_at: datetime
    model_version: str
    n_requested: int
    n_scored: int
    n_alerts: int
    n_skipped_entity_missing: int
    cycle_readiness: dict[str, Any]
    process_rss_mb: float | None
    elapsed_seconds: float | None = None
    feast_readiness_path: str | None = None
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dry-run document."""
        return {
            "generated_at": self.generated_at.isoformat(),
            "model_version": self.model_version,
            "n_requested": self.n_requested,
            "n_scored": self.n_scored,
            "n_alerts": self.n_alerts,
            "n_skipped_entity_missing": self.n_skipped_entity_missing,
            "cycle_readiness": dict(self.cycle_readiness),
            "process_rss_mb": self.process_rss_mb,
            "elapsed_seconds": self.elapsed_seconds,
            "feast_readiness_path": self.feast_readiness_path,
            "notes": self.notes,
        }

    def acceptance_summary(self) -> dict[str, Any]:
        """Heuristic pass / marginal flags for Release Gate review."""
        cr = self.cycle_readiness
        entity_rate = float(cr.get("entity_missing_rate") or 0.0)
        n_req = int(cr.get("n_requested") or self.n_requested)
        n_scored = int(cr.get("n_scored") or self.n_scored)
        row_ok = n_req == 0 or n_scored == n_req
        entity_ok = entity_rate <= float(cr.get("entity_missing_fail_fraction") or 0.10)
        latency_ms = float(cr.get("lookup_latency_ms") or 0.0)
        latency_ok = latency_ms <= 5000.0 or n_req == 0
        verdict = "pass"
        if not row_ok or not entity_ok:
            verdict = "fail"
        elif latency_ms > 500.0:
            verdict = "marginal"
        return {
            "verdict": verdict,
            "row_count_aligned": row_ok,
            "entity_missing_ok": entity_ok,
            "lookup_latency_ok": latency_ok,
            "entity_missing_rate": entity_rate,
            "lookup_latency_ms": latency_ms,
        }


def default_scorer_dry_run_report_path() -> Path:
    """Default report path under ``trainer_hightier/artifacts/feast``."""
    return TRAINER_HIGHTIER_PACKAGE_DIR / "artifacts" / "feast" / "scorer_dry_run_report.json"


def write_scorer_dry_run_report(report: ScorerDryRunReport, path: Path | None = None) -> Path:
    """Persist dry-run report JSON."""
    out = Path(path).resolve() if path is not None else default_scorer_dry_run_report_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = report.to_dict()
    payload["acceptance"] = report.acceptance_summary()
    out.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    logger.info(
        "[scorer_dry_run] wrote %s verdict=%s",
        out,
        payload["acceptance"].get("verdict"),
    )
    return out


def build_dry_run_report_from_cycle(
    *,
    model_version: str,
    cycle_readiness: dict[str, Any],
    n_alerts: int,
    elapsed_seconds: float | None = None,
    feast_readiness_path: Path | None = None,
    notes: str | None = None,
) -> ScorerDryRunReport:
    """Build report from scorer cycle_readiness log payload."""
    return ScorerDryRunReport(
        generated_at=datetime.now(ZoneInfo(HK_TZ)),
        model_version=str(model_version),
        n_requested=int(cycle_readiness.get("n_requested") or 0),
        n_scored=int(cycle_readiness.get("n_scored") or 0),
        n_alerts=int(n_alerts),
        n_skipped_entity_missing=int(cycle_readiness.get("n_skipped_entity_missing") or 0),
        cycle_readiness=dict(cycle_readiness),
        process_rss_mb=capture_process_rss_mb(),
        elapsed_seconds=elapsed_seconds,
        feast_readiness_path=str(feast_readiness_path) if feast_readiness_path else None,
        notes=notes,
    )
