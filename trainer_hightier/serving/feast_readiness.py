"""Feast online readiness metadata for scorer v2 deploy / refresh gates (P5)."""

from __future__ import annotations

import argparse
import json
import logging
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from trainer_hightier.config import (
    FEAST_ONLINE_READINESS_SCHEMA_VERSION,
    HK_TZ,
    MID_TERM_SNAPSHOT_SCOPE_PRODUCTION,
    MID_TERM_SNAPSHOT_SCOPE_TRAINING,
    TRAINER_HIGHTIER_PACKAGE_DIR,
    default_hightier_serving_config,
)
from trainer_hightier.serving.feast_production_constants import (
    PRODUCTION_LONG_TERM_FEATURE_COLUMNS,
    PRODUCTION_MID_TERM_FEATURE_COLUMNS,
    feast_entity_rows,
)
from trainer_hightier.serving.feast_online_adapter import (
    FEAST_CANONICAL_JOIN_KEY,
    default_feast_repo_path,
    read_feast_parquet_max_event_timestamp,
    resolve_online_feature_refs,
    resolve_production_mid_feast_parquet,
)
from trainer_hightier.serving.snapshot_freshness import (
    LayerFreshnessResult,
    evaluate_mid_term_freshness,
    evaluate_slow_freshness,
)

logger = logging.getLogger(__name__)

FEAST_READINESS_SCOPE_PRODUCTION: str = "production"
FEAST_READINESS_SCOPE_ALLOWLIST: str = "adt_allowlist"
_ACCEPTED_SLOW_SCOPES: frozenset[str] = frozenset(
    {FEAST_READINESS_SCOPE_PRODUCTION, FEAST_READINESS_SCOPE_ALLOWLIST}
)


@dataclass(frozen=True)
class FeastLayerReadiness:
    """Per-layer Feast online metadata written by refresh / spike jobs."""

    layer: str
    source_scope: str
    anchor_gaming_day_event_max: date | None
    generated_at: datetime
    row_count: int
    distinct_canonical_count: int | None
    cell_null_counts: dict[str, int]
    lookup_sample_size: int | None
    lookup_entity_present_rate: float | None
    feature_columns: tuple[str, ...]
    feast_feature_view: str | None
    materialize_source: str
    expected_anchor_gaming_day_event: date | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable payload for ``feast_online_readiness.json``."""
        out: dict[str, Any] = {
            "layer": self.layer,
            "source_scope": self.source_scope,
            "anchor_gaming_day_event_max": (
                self.anchor_gaming_day_event_max.isoformat() if self.anchor_gaming_day_event_max else None
            ),
            "expected_anchor_gaming_day_event": (
                self.expected_anchor_gaming_day_event.isoformat()
                if self.expected_anchor_gaming_day_event
                else None
            ),
            "generated_at": self.generated_at.isoformat(),
            "row_count": self.row_count,
            "distinct_canonical_count": self.distinct_canonical_count,
            "cell_null_counts": dict(self.cell_null_counts),
            "lookup_sample_size": self.lookup_sample_size,
            "lookup_entity_present_rate": self.lookup_entity_present_rate,
            "feature_columns": list(self.feature_columns),
            "feast_feature_view": self.feast_feature_view,
            "materialize_source": self.materialize_source,
        }
        if self.layer == "mid_term":
            age = compute_mid_snapshot_age_days(
                self.anchor_gaming_day_event_max,
                self.expected_anchor_gaming_day_event,
            )
            if age is not None:
                out["snapshot_age_days"] = age
            top = compute_mid_null_top_features(
                self.cell_null_counts,
                sample_size=self.lookup_sample_size,
            )
            if top:
                out["mid_null_top_features"] = top
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> FeastLayerReadiness:
        """Parse one layer block from readiness JSON."""
        anchor_raw = raw.get("anchor_gaming_day_event_max")
        anchor = pd.Timestamp(str(anchor_raw)).date() if anchor_raw else None
        exp_raw = raw.get("expected_anchor_gaming_day_event")
        expected_anchor = pd.Timestamp(str(exp_raw)).date() if exp_raw else None
        gen_raw = raw.get("generated_at")
        if not gen_raw:
            raise ValueError(f"[feast_readiness] layer {raw.get('layer')!r} missing generated_at")
        gen = pd.Timestamp(str(gen_raw)).to_pydatetime()
        if gen.tzinfo is None:
            gen = gen.replace(tzinfo=timezone.utc)
        cols = raw.get("feature_columns") or []
        return cls(
            layer=str(raw.get("layer", "")),
            source_scope=str(raw.get("source_scope", "")),
            anchor_gaming_day_event_max=anchor,
            generated_at=gen,
            row_count=int(raw.get("row_count") or 0),
            distinct_canonical_count=(
                int(raw["distinct_canonical_count"])
                if raw.get("distinct_canonical_count") is not None
                else None
            ),
            cell_null_counts={str(k): int(v) for k, v in (raw.get("cell_null_counts") or {}).items()},
            lookup_sample_size=(
                int(raw["lookup_sample_size"]) if raw.get("lookup_sample_size") is not None else None
            ),
            lookup_entity_present_rate=(
                float(raw["lookup_entity_present_rate"])
                if raw.get("lookup_entity_present_rate") is not None
                else None
            ),
            feature_columns=tuple(str(c) for c in cols),
            feast_feature_view=(
                str(raw["feast_feature_view"]) if raw.get("feast_feature_view") else None
            ),
            materialize_source=str(raw.get("materialize_source") or "unknown"),
            expected_anchor_gaming_day_event=expected_anchor,
        )


@dataclass(frozen=True)
class FeastOnlineReadiness:
    """Combined mid + slow Feast online readiness snapshot for scorer gates."""

    schema_version: int
    generated_at: datetime
    feast_repo: str
    mid_term: FeastLayerReadiness | None
    slow_patron: FeastLayerReadiness | None

    def to_dict(self) -> dict[str, Any]:
        """Full readiness document."""
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at.isoformat(),
            "feast_repo": self.feast_repo,
            "mid_term": self.mid_term.to_dict() if self.mid_term else None,
            "slow_patron": self.slow_patron.to_dict() if self.slow_patron else None,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> FeastOnlineReadiness:
        """Load readiness document from JSON dict."""
        gen_raw = raw.get("generated_at")
        if not gen_raw:
            raise ValueError("[feast_readiness] document missing generated_at")
        gen = pd.Timestamp(str(gen_raw)).to_pydatetime()
        if gen.tzinfo is None:
            gen = gen.replace(tzinfo=timezone.utc)
        mid_raw = raw.get("mid_term")
        slow_raw = raw.get("slow_patron")
        return cls(
            schema_version=int(raw.get("schema_version") or 1),
            generated_at=gen,
            feast_repo=str(raw.get("feast_repo") or ""),
            mid_term=FeastLayerReadiness.from_dict(mid_raw) if isinstance(mid_raw, dict) else None,
            slow_patron=FeastLayerReadiness.from_dict(slow_raw) if isinstance(slow_raw, dict) else None,
        )


@dataclass(frozen=True)
class FeastReadinessGateResult:
    """Outcome of deploy-time Feast readiness evaluation."""

    ok: bool
    mid_fresh: LayerFreshnessResult | None
    slow_fresh: LayerFreshnessResult | None
    hard_failure_reason: str | None
    readiness_path: Path
    deploy_lookup_smoke: dict[str, Any] | None = None

    def to_log_dict(self) -> dict[str, Any]:
        """Compact dict for scorer startup logs."""
        out: dict[str, Any] = {
            "ok": self.ok,
            "readiness_path": str(self.readiness_path),
            "hard_failure_reason": self.hard_failure_reason,
        }
        if self.mid_fresh is not None:
            out["mid_freshness"] = self.mid_fresh.status
            out["mid_anchor_max"] = (
                self.mid_fresh.anchor_max.isoformat() if self.mid_fresh.anchor_max else None
            )
            if self.mid_fresh.staleness_days is not None:
                out["mid_snapshot_age_days"] = int(self.mid_fresh.staleness_days)
        if self.slow_fresh is not None:
            out["slow_freshness"] = self.slow_fresh.status
            out["slow_anchor_max"] = (
                self.slow_fresh.anchor_max.isoformat() if self.slow_fresh.anchor_max else None
            )
        if self.deploy_lookup_smoke:
            out["deploy_lookup_smoke"] = self.deploy_lookup_smoke
        return out


def default_feast_readiness_path() -> Path:
    """Default combined readiness JSON under ``trainer_hightier/artifacts/feast``."""
    return TRAINER_HIGHTIER_PACKAGE_DIR / "artifacts" / "feast" / "feast_online_readiness.json"


def resolve_feast_readiness_path(cfg: Any | None = None) -> Path:
    """Resolve readiness path from serving config override or default."""
    serving = cfg or default_hightier_serving_config()
    override = getattr(serving, "scorer_feast_readiness_path", None)
    if override is not None and str(override).strip():
        return Path(override).resolve()
    return default_feast_readiness_path().resolve()


def _parse_anchor(value: Any) -> date | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return pd.Timestamp(text).date()


def compute_mid_snapshot_age_days(
    anchor_max: date | None,
    expected_anchor: date | None = None,
    *,
    close_hour: int = 3,
) -> int | None:
    """Gaming-day staleness of mid anchor vs expected serving anchor (P1 observability)."""
    from trainer_hightier.serving.snapshot_freshness import (
        _staleness_days,
        expected_mid_term_anchor,
        serving_gaming_day_event,
    )

    if anchor_max is None:
        return None
    expected = expected_anchor or expected_mid_term_anchor(serving_gaming_day_event())
    return _staleness_days(anchor_max, expected)


def compute_mid_null_top_features(
    cell_null_counts: dict[str, int],
    *,
    sample_size: int | None = None,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """Rank mid ``fe__*`` columns by smoke-sample null count (P1 observability)."""
    if not cell_null_counts:
        return []
    denom = int(sample_size) if sample_size is not None and int(sample_size) > 0 else None
    ranked: list[tuple[str, int, float | None]] = []
    for col, raw_null in cell_null_counts.items():
        if not str(col).startswith("fe__"):
            continue
        n_null = int(raw_null)
        if n_null <= 0:
            continue
        null_rate = float(n_null) / float(denom) if denom is not None else None
        ranked.append((str(col), n_null, null_rate))
    ranked.sort(key=lambda item: (-item[1], item[0]))
    out: list[dict[str, Any]] = []
    for feature_id, null_count, null_rate in ranked[: max(1, int(top_k))]:
        row: dict[str, Any] = {"feature_id": feature_id, "null_count": null_count}
        if null_rate is not None:
            row["null_rate"] = round(null_rate, 4)
        out.append(row)
    return out


def compute_batch_mid_null_top_features(
    frame: pd.DataFrame,
    mid_columns: tuple[str, ...],
    *,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """Rank mid columns by null count across one scored batch."""
    if frame is None or frame.empty or not mid_columns:
        return []
    counts: dict[str, int] = {}
    n_rows = len(frame)
    for col in mid_columns:
        if col not in frame.columns:
            counts[str(col)] = n_rows
        else:
            counts[str(col)] = int(frame[col].isna().sum())
    return compute_mid_null_top_features(counts, sample_size=n_rows, top_k=top_k)


def _hk_now() -> datetime:
    return datetime.now(ZoneInfo(HK_TZ))


def _lookup_entity_present_rate(report: dict[str, Any]) -> float | None:
    n = int(report.get("lookup_batch_size") or 0)
    if n <= 0:
        return None
    ok = int(report.get("lookup_ok_rows") or 0)
    return round(float(ok) / float(n), 4)


def layer_readiness_from_mid_spike_report(report: dict[str, Any]) -> FeastLayerReadiness:
    """Build mid-term layer metadata from a mid-term spike report dict."""
    scope = str(report.get("snapshot_scope") or FEAST_READINESS_SCOPE_PRODUCTION).strip()
    return FeastLayerReadiness(
        layer="mid_term",
        source_scope=scope,
        anchor_gaming_day_event_max=_parse_anchor(report.get("mid_term_anchor_gaming_day_event_max")),
        generated_at=_hk_now(),
        row_count=int(report.get("feast_spike_rows") or report.get("row_count") or 0),
        distinct_canonical_count=int(report["distinct_canonical_count"])
        if report.get("distinct_canonical_count") is not None
        else None,
        cell_null_counts={
            str(k): int(v) for k, v in (report.get("lookup_missing_by_feature") or {}).items()
        },
        lookup_sample_size=int(report["lookup_batch_size"])
        if report.get("lookup_batch_size") is not None
        else None,
        lookup_entity_present_rate=_lookup_entity_present_rate(report),
        feature_columns=tuple(str(c) for c in (report.get("feature_columns") or PRODUCTION_MID_TERM_FEATURE_COLUMNS)),
        feast_feature_view=str(report.get("feast_feature_view") or "mid_term_daily_spike_features"),
        materialize_source="feast_mid_term_spike",
    )


def layer_readiness_from_production_mid_meta(meta: dict[str, Any]) -> FeastLayerReadiness:
    """Build mid-term readiness from production refresh / materialize meta."""
    distinct = meta.get("distinct_canonical_count")
    if distinct is None and meta.get("feast_spike_rows") is not None:
        distinct = int(meta["feast_spike_rows"])
    elif distinct is None and meta.get("distinct_bet_count") is not None:
        distinct = int(meta["distinct_bet_count"])
    return FeastLayerReadiness(
        layer="mid_term",
        source_scope=str(meta.get("snapshot_scope") or FEAST_READINESS_SCOPE_PRODUCTION).strip(),
        anchor_gaming_day_event_max=_parse_anchor(meta.get("mid_term_anchor_gaming_day_event_max")),
        generated_at=_hk_now(),
        row_count=int(meta.get("feast_spike_rows") or meta.get("row_count") or 0),
        distinct_canonical_count=int(distinct) if distinct is not None else None,
        cell_null_counts={},
        lookup_sample_size=None,
        lookup_entity_present_rate=None,
        feature_columns=PRODUCTION_MID_TERM_FEATURE_COLUMNS,
        feast_feature_view="mid_term_daily_spike_features",
        materialize_source=str(meta.get("materialize_source") or "production_materialize"),
        expected_anchor_gaming_day_event=_parse_anchor(meta.get("mid_term_expected_anchor_gaming_day_event")),
    )


def layer_readiness_from_production_slow_meta(meta: dict[str, Any]) -> FeastLayerReadiness:
    """Build slow-patron readiness from production slow canonical ASOF materialize meta."""
    return FeastLayerReadiness(
        layer="slow_patron",
        source_scope=FEAST_READINESS_SCOPE_PRODUCTION,
        anchor_gaming_day_event_max=_parse_anchor(meta.get("slow_anchor_gaming_day_event_max")),
        generated_at=_hk_now(),
        row_count=int(meta.get("row_count") or 0),
        distinct_canonical_count=int(meta["distinct_bet_count"])
        if meta.get("distinct_bet_count") is not None
        else None,
        cell_null_counts={},
        lookup_sample_size=None,
        lookup_entity_present_rate=None,
        feature_columns=PRODUCTION_LONG_TERM_FEATURE_COLUMNS,
        feast_feature_view="long_term_slow_spike_features",
        materialize_source="production_materialize",
    )


def publish_feast_layer_readiness(
    layer: FeastLayerReadiness,
    *,
    path: Path | None = None,
    feast_repo: Path | None = None,
) -> Path:
    """Merge one production/spike layer into ``feast_online_readiness.json``."""
    out_path = Path(path).resolve() if path is not None else resolve_feast_readiness_path()
    merged = merge_layer_readiness(load_feast_online_readiness(out_path), layer, feast_repo=feast_repo)
    return write_feast_online_readiness(merged, out_path)


def write_minimal_test_feast_readiness(
    path: Path,
    *,
    feast_repo: Path | None = None,
) -> Path:
    """Write fresh production-scoped readiness for unit tests (no spike reports required)."""
    from trainer_hightier.serving.snapshot_freshness import expected_mid_term_anchor, serving_gaming_day_event

    anchor = expected_mid_term_anchor(serving_gaming_day_event())
    mid = FeastLayerReadiness(
        layer="mid_term",
        source_scope=FEAST_READINESS_SCOPE_PRODUCTION,
        anchor_gaming_day_event_max=anchor,
        generated_at=_hk_now(),
        row_count=1,
        distinct_canonical_count=1,
        cell_null_counts={},
        lookup_sample_size=1,
        lookup_entity_present_rate=1.0,
        feature_columns=PRODUCTION_MID_TERM_FEATURE_COLUMNS,
        feast_feature_view="mid_term_daily_spike_features",
        materialize_source="test_fixture",
    )
    slow = FeastLayerReadiness(
        layer="slow_patron",
        source_scope=FEAST_READINESS_SCOPE_PRODUCTION,
        anchor_gaming_day_event_max=anchor,
        generated_at=mid.generated_at,
        row_count=1,
        distinct_canonical_count=1,
        cell_null_counts={},
        lookup_sample_size=1,
        lookup_entity_present_rate=1.0,
        feature_columns=PRODUCTION_LONG_TERM_FEATURE_COLUMNS,
        feast_feature_view="long_term_slow_spike_features",
        materialize_source="test_fixture",
    )
    doc = FeastOnlineReadiness(
        schema_version=FEAST_ONLINE_READINESS_SCHEMA_VERSION,
        generated_at=mid.generated_at,
        feast_repo=str((feast_repo or default_feast_repo_path()).resolve()),
        mid_term=mid,
        slow_patron=slow,
    )
    return write_feast_online_readiness(doc, path)


def layer_readiness_from_long_spike_report(report: dict[str, Any]) -> FeastLayerReadiness:
    """Build slow-patron layer metadata from a long-term spike report dict."""
    scope = str(report.get("scope") or FEAST_READINESS_SCOPE_ALLOWLIST).strip()
    return FeastLayerReadiness(
        layer="slow_patron",
        source_scope=scope,
        anchor_gaming_day_event_max=_parse_anchor(report.get("slow_anchor_gaming_day_event_max")),
        generated_at=_hk_now(),
        row_count=int(report.get("feast_spike_rows") or report.get("slow_snapshot_rows") or 0),
        distinct_canonical_count=None,
        cell_null_counts={
            str(k): int(v) for k, v in (report.get("lookup_missing_by_feature") or {}).items()
        },
        lookup_sample_size=int(report["lookup_batch_size"])
        if report.get("lookup_batch_size") is not None
        else None,
        lookup_entity_present_rate=_lookup_entity_present_rate(report),
        feature_columns=tuple(
            str(c) for c in (report.get("feature_columns") or PRODUCTION_LONG_TERM_FEATURE_COLUMNS)
        ),
        feast_feature_view=str(report.get("feast_feature_view") or "long_term_slow_spike_features"),
        materialize_source="feast_long_term_spike",
    )


def load_feast_online_readiness(path: Path | None = None) -> FeastOnlineReadiness | None:
    """Load combined readiness JSON; return ``None`` when file is absent."""
    p = Path(path).resolve() if path is not None else resolve_feast_readiness_path()
    if not p.is_file():
        return None
    raw = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"[feast_readiness] invalid readiness JSON at {p}")
    return FeastOnlineReadiness.from_dict(raw)


def write_feast_online_readiness(readiness: FeastOnlineReadiness, path: Path | None = None) -> Path:
    """Persist combined readiness JSON via temp+replace."""
    p = Path(path).resolve() if path is not None else resolve_feast_readiness_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(readiness.to_dict(), indent=2, sort_keys=True)
    fd, tmp = tempfile.mkstemp(prefix="feast_readiness_", suffix=".json", dir=str(p.parent))
    try:
        with open(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        Path(tmp).replace(p)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    logger.info("[feast_readiness] wrote %s", p)
    return p


def merge_layer_readiness(
    existing: FeastOnlineReadiness | None,
    layer: FeastLayerReadiness,
    *,
    feast_repo: Path | None = None,
) -> FeastOnlineReadiness:
    """Return readiness with one layer replaced."""
    repo = str((feast_repo or default_feast_repo_path()).resolve())
    mid = layer if layer.layer == "mid_term" else (existing.mid_term if existing else None)
    slow = layer if layer.layer == "slow_patron" else (existing.slow_patron if existing else None)
    gen = max(
        [layer.generated_at, *(x.generated_at for x in (mid, slow) if x is not None)],
        default=layer.generated_at,
    )
    return FeastOnlineReadiness(
        schema_version=FEAST_ONLINE_READINESS_SCHEMA_VERSION,
        generated_at=gen,
        feast_repo=existing.feast_repo if existing and existing.feast_repo else repo,
        mid_term=mid,
        slow_patron=slow,
    )


def update_readiness_layer_from_spike_report(
    report: dict[str, Any],
    *,
    layer: str,
    path: Path | None = None,
    feast_repo: Path | None = None,
) -> Path:
    """Merge one spike report into ``feast_online_readiness.json``."""
    if layer == "mid_term":
        layer_doc = layer_readiness_from_mid_spike_report(report)
    elif layer == "slow_patron":
        layer_doc = layer_readiness_from_long_spike_report(report)
    else:
        raise ValueError(f"unsupported readiness layer={layer!r}")
    out_path = Path(path).resolve() if path is not None else resolve_feast_readiness_path()
    merged = merge_layer_readiness(load_feast_online_readiness(out_path), layer_doc, feast_repo=feast_repo)
    return write_feast_online_readiness(merged, out_path)


def refresh_readiness_from_spike_reports(
    *,
    mid_report_path: Path | None = None,
    long_report_path: Path | None = None,
    out_path: Path | None = None,
    feast_repo: Path | None = None,
) -> FeastOnlineReadiness:
    """Rebuild combined readiness from existing spike report JSON files."""
    from trainer_hightier.feature_experiment.feast_long_term_spike import default_long_term_spike_config
    from trainer_hightier.feature_experiment.feast_mid_term_spike import default_spike_config

    mid_p = Path(
        mid_report_path or default_spike_config().report_path,
    ).resolve()
    long_p = Path(
        long_report_path or default_long_term_spike_config().report_path,
    ).resolve()
    repo = feast_repo or default_feast_repo_path()
    merged: FeastOnlineReadiness | None = None
    if mid_p.is_file():
        mid_report = json.loads(mid_p.read_text(encoding="utf-8"))
        merged = merge_layer_readiness(
            merged,
            layer_readiness_from_mid_spike_report(mid_report),
            feast_repo=repo,
        )
    if long_p.is_file():
        long_report = json.loads(long_p.read_text(encoding="utf-8"))
        merged = merge_layer_readiness(
            merged,
            layer_readiness_from_long_spike_report(long_report),
            feast_repo=repo,
        )
    if merged is None:
        raise FileNotFoundError(
            f"[feast_readiness] no spike reports found at {mid_p} or {long_p}"
        )
    return write_feast_online_readiness(merged, out_path)


def _assert_layer_scope(layer: FeastLayerReadiness) -> None:
    if layer.layer == "mid_term":
        if layer.source_scope == MID_TERM_SNAPSHOT_SCOPE_TRAINING:
            raise RuntimeError(
                "[feast_readiness] mid_term source_scope is training-scoped; "
                "production scorer cannot use training_step4_only artifacts"
            )
        if layer.source_scope != MID_TERM_SNAPSHOT_SCOPE_PRODUCTION:
            raise RuntimeError(
                "[feast_readiness] mid_term source_scope must be "
                f"{MID_TERM_SNAPSHOT_SCOPE_PRODUCTION!r}, got {layer.source_scope!r}"
            )
    if layer.layer == "slow_patron" and layer.source_scope not in _ACCEPTED_SLOW_SCOPES:
        raise RuntimeError(
            "[feast_readiness] slow_patron source_scope must be production or adt_allowlist, "
            f"got {layer.source_scope!r}"
        )


def evaluate_feast_readiness_gate(
    readiness: FeastOnlineReadiness | None,
    *,
    require_mid: bool,
    require_slow: bool,
    readiness_path: Path,
    close_hour: int,
    mid_hard_cap_days: int,
    slow_hard_cap_days: int,
    slow_grace_days: int,
) -> FeastReadinessGateResult:
    """Deploy-time gate: scope, anchors, and freshness vs serving gaming day."""
    if readiness is None:
        missing = []
        if require_mid:
            missing.append("mid_term")
        if require_slow:
            missing.append("slow_patron")
        return FeastReadinessGateResult(
            ok=False,
            mid_fresh=None,
            slow_fresh=None,
            hard_failure_reason=(
                "[feast_readiness] feast_online_readiness.json missing at "
                f"{readiness_path}; run mid/long spike materialize or "
                "`python -m trainer_hightier.serving.feast_readiness --refresh-from-reports`"
            ),
            readiness_path=readiness_path,
        )
    mid_fresh: LayerFreshnessResult | None = None
    slow_fresh: LayerFreshnessResult | None = None
    try:
        if require_mid:
            if readiness.mid_term is None:
                raise RuntimeError("[feast_readiness] mid_term layer missing from readiness document")
            _assert_layer_scope(readiness.mid_term)
            mid_fresh = evaluate_mid_term_freshness(
                anchor_max=readiness.mid_term.anchor_gaming_day_event_max,
                hard_cap_days=mid_hard_cap_days,
                close_hour=close_hour,
                expected_anchor=readiness.mid_term.expected_anchor_gaming_day_event,
            )
            if mid_fresh.status == "hard_cap_breached":
                raise RuntimeError(mid_fresh.message)
        if require_slow:
            if readiness.slow_patron is None:
                raise RuntimeError("[feast_readiness] slow_patron layer missing from readiness document")
            _assert_layer_scope(readiness.slow_patron)
            slow_fresh = evaluate_slow_freshness(
                anchor_max=readiness.slow_patron.anchor_gaming_day_event_max,
                monthly_grace_days=slow_grace_days,
                hard_cap_days=slow_hard_cap_days,
                close_hour=close_hour,
            )
            if slow_fresh.status == "hard_cap_breached":
                raise RuntimeError(slow_fresh.message)
    except RuntimeError as exc:
        return FeastReadinessGateResult(
            ok=False,
            mid_fresh=mid_fresh,
            slow_fresh=slow_fresh,
            hard_failure_reason=str(exc),
            readiness_path=readiness_path,
        )
    return FeastReadinessGateResult(
        ok=True,
        mid_fresh=mid_fresh,
        slow_fresh=slow_fresh,
        hard_failure_reason=None,
        readiness_path=readiness_path,
    )


def _sample_canonical_ids_from_allowlist(
    allowlist_parquet: Path,
    mapping_parquet: Path,
    *,
    sample_size: int,
    mid_feast_parquet: Path | None = None,
) -> list[str]:
    """Return up to ``sample_size`` canonical ids from ADT allowlist + mapping.

    When ``mid_feast_parquet`` is set, only ids present in that materialized mid
    Feast parquet are eligible so smoke validates online rows we actually wrote.
    """
    allow_esc = str(Path(allowlist_parquet).resolve()).replace("\\", "/").replace("'", "''")
    cmap_esc = str(Path(mapping_parquet).resolve()).replace("\\", "/").replace("'", "''")
    import duckdb

    mid_join = ""
    if mid_feast_parquet is not None and Path(mid_feast_parquet).is_file():
        mid_esc = str(Path(mid_feast_parquet).resolve()).replace("\\", "/").replace("'", "''")
        mid_join = f"""
INNER JOIN (
  SELECT DISTINCT TRIM(CAST(canonical_id AS VARCHAR)) AS canonical_id
  FROM read_parquet('{mid_esc}')
  WHERE TRIM(CAST(canonical_id AS VARCHAR)) <> ''
) AS m
  ON TRIM(CAST(c.canonical_id AS VARCHAR)) = m.canonical_id
"""
    sql = f"""
SELECT DISTINCT TRIM(CAST(c.canonical_id AS VARCHAR)) AS canonical_id
FROM read_parquet('{allow_esc}') AS a
INNER JOIN read_parquet('{cmap_esc}') AS c
  ON TRY_CAST(a.player_id AS BIGINT) = TRY_CAST(c.player_id AS BIGINT)
{mid_join}
WHERE TRIM(CAST(c.canonical_id AS VARCHAR)) <> ''
ORDER BY canonical_id
LIMIT {int(max(1, sample_size))}
""".strip()
    df = duckdb.sql(sql).fetchdf()
    if df.empty:
        return []
    return [str(x).strip() for x in df["canonical_id"].tolist() if str(x).strip()]


def _mid_cell_null_rate(
    cell_null_counts: dict[str, int],
    *,
    mid_columns: tuple[str, ...],
    sample_size: int,
) -> float:
    """Average null fraction across ``mid_columns`` in the smoke sample."""
    if sample_size <= 0 or not mid_columns:
        return 0.0
    total = 0
    for col in mid_columns:
        total += int(cell_null_counts.get(col, sample_size))
    return float(total) / float(sample_size * len(mid_columns))


def _normalize_feast_online_lookup_frame(
    df: pd.DataFrame,
    wanted: tuple[str, ...],
) -> pd.DataFrame:
    """Map Feast online response columns to model feature names when prefixed."""
    if df.empty:
        return df
    out = df.copy()
    rename: dict[str, str] = {}
    for col in out.columns:
        if col in wanted or col == FEAST_CANONICAL_JOIN_KEY:
            continue
        if ":" in col:
            rename[col] = col.rsplit(":", 1)[-1]
            continue
        for feat in wanted:
            suffix = f"__{feat}"
            if col == feat or col.endswith(suffix):
                rename[col] = feat
                break
    if rename:
        out = out.rename(columns=rename)
    return out


def evaluate_feast_lookup_smoke_gate(
    smoke: dict[str, Any],
    *,
    mid_columns: tuple[str, ...],
    entity_missing_fail_fraction: float,
    mid_cell_null_fail_fraction: float,
    feast_spike_rows: int | None = None,
    allowlist_canonical_count: int | None = None,
    min_feast_spike_rows: int = 1,
    mid_smoke_columns: tuple[str, ...] | None = None,
) -> tuple[bool, str | None]:
    """Return (ok, hard_failure_reason) for deploy / refresh smoke."""
    entity_rate = float(smoke.get("entity_missing_rate") or 0.0)
    if entity_rate > float(entity_missing_fail_fraction):
        return (
            False,
            "[feast_readiness] allowlist lookup smoke entity missing rate "
            f"{entity_rate} exceeds fail_fraction={entity_missing_fail_fraction}",
        )
    # Slow-only models pass mid_columns=(); do not enforce mid smoke cols in that case.
    if mid_columns:
        extra = mid_smoke_columns if mid_smoke_columns is not None else mid_columns
        null_cols = tuple(dict.fromkeys([*mid_columns, *extra]))
        raw_mid = smoke.get("mid_cell_null_rate")
        mid_rate = float(raw_mid if raw_mid is not None else 1.0)
        if mid_rate > float(mid_cell_null_fail_fraction):
            counts = smoke.get("cell_null_counts") or {}
            smoke_cols = tuple(smoke.get("mid_smoke_columns") or list(null_cols))
            bad = {
                str(col): int(counts[col])
                for col in smoke_cols
                if col in counts and int(counts[col]) > 0
            }
            missing_cols = [
                str(col)
                for col in smoke_cols
                if col not in counts or int(counts.get(col, 0)) >= int(smoke.get("sample_size") or 0)
            ]
            detail_parts = [f"cell_null_counts={bad}"]
            if missing_cols:
                detail_parts.append(f"missing_or_all_null={missing_cols[:8]}")
            if smoke.get("smoke_event_timestamp"):
                detail_parts.append(f"smoke_event_timestamp={smoke.get('smoke_event_timestamp')}")
            detail = "; ".join(detail_parts)
            return (
                False,
                "[feast_readiness] allowlist lookup smoke mid cell null rate "
                f"{mid_rate} exceeds fail_fraction={mid_cell_null_fail_fraction}; {detail}",
            )
        if feast_spike_rows is not None and int(feast_spike_rows) < int(min_feast_spike_rows):
            return (
                False,
                "[feast_readiness] mid Feast materialized zero canonical rows "
                f"(feast_spike_rows={feast_spike_rows}, min={min_feast_spike_rows})",
            )
        sample_size = int(smoke.get("sample_size") or 0)
        if sample_size < 1:
            return (
                False,
                "[feast_readiness] allowlist lookup smoke produced zero sampled canonical ids "
                "(no overlap between allowlist and materialized mid Feast parquet?)",
            )
    return True, None


def mid_feast_coverage_telemetry(
    *,
    feast_spike_rows: int | None,
    allowlist_canonical_count: int | None,
) -> dict[str, Any]:
    """Return informational mid Feast allowlist coverage fields for smoke / readiness reports."""
    if feast_spike_rows is None or allowlist_canonical_count is None:
        return {}
    if int(allowlist_canonical_count) <= 0:
        return {"feast_spike_rows": int(feast_spike_rows), "allowlist_canonical_count": 0}
    coverage = float(feast_spike_rows) / float(allowlist_canonical_count)
    return {
        "feast_spike_rows": int(feast_spike_rows),
        "allowlist_canonical_count": int(allowlist_canonical_count),
        "mid_canonical_coverage_fraction": round(coverage, 4),
    }


def run_allowlist_feast_lookup_smoke(
    *,
    feast_repo: Path,
    allowlist_parquet: Path,
    canonical_mapping_parquet: Path,
    mid_columns: tuple[str, ...],
    slow_columns: tuple[str, ...],
    sample_size: int,
    entity_missing_fail_fraction: float,
    mid_cell_null_fail_fraction: float | None = None,
    mid_smoke_columns: tuple[str, ...] | None = None,
    smoke_event_timestamp: datetime | None = None,
    mid_feast_parquet: Path | None = None,
) -> dict[str, Any]:
    """P5-3: sample allowlist canonical ids against Feast online store."""
    from feast import FeatureStore

    cfg = default_hightier_serving_config()
    if mid_columns:
        smoke_cols = (
            mid_smoke_columns if mid_smoke_columns is not None else cfg.scorer_feast_mid_smoke_columns
        )
        lookup_mid = tuple(dict.fromkeys([*mid_columns, *smoke_cols]))
    else:
        smoke_cols = ()
        lookup_mid = ()
    cids = _sample_canonical_ids_from_allowlist(
        allowlist_parquet,
        canonical_mapping_parquet,
        sample_size=sample_size,
        mid_feast_parquet=mid_feast_parquet,
    )
    if not cids:
        hint = (
            " (no overlap with mid_feast_parquet)"
            if mid_feast_parquet is not None and Path(mid_feast_parquet).is_file()
            else ""
        )
        raise ValueError(
            "[feast_readiness] allowlist sample produced zero canonical_id rows" + hint
        )
    refs = list(resolve_online_feature_refs(lookup_mid, slow_columns))
    store = FeatureStore(repo_path=str(Path(feast_repo).resolve()))
    entity_rows = feast_entity_rows(cids)
    if smoke_event_timestamp is not None and mid_columns:
        entity_rows = {
            **entity_rows,
            "event_timestamp": [smoke_event_timestamp] * len(cids),
        }
    t0 = time.perf_counter()
    out = store.get_online_features(
        features=refs,
        entity_rows=entity_rows,
    ).to_df()
    out = _normalize_feast_online_lookup_frame(out, wanted=tuple(dict.fromkeys([*lookup_mid, *slow_columns])))
    latency_ms = round((time.perf_counter() - t0) * 1000.0, 3)
    wanted = tuple(dict.fromkeys([*lookup_mid, *slow_columns]))
    n_entity_missing = 0
    cell_null_counts: dict[str, int] = {c: 0 for c in wanted}
    if out.empty or FEAST_CANONICAL_JOIN_KEY not in out.columns:
        n_entity_missing = len(cids)
        for col in wanted:
            cell_null_counts[col] = len(cids)
    else:
        lk = out.drop_duplicates(subset=[FEAST_CANONICAL_JOIN_KEY], keep="last")
        present = set(lk[FEAST_CANONICAL_JOIN_KEY].astype(str).str.strip().tolist())
        for cid in cids:
            if cid not in present:
                n_entity_missing += 1
        for col in wanted:
            if col not in lk.columns:
                cell_null_counts[col] = len(cids)
            else:
                cell_null_counts[col] = int(lk[col].isna().sum())
    rate = float(n_entity_missing) / float(len(cids))
    mid_rate = _mid_cell_null_rate(
        cell_null_counts,
        mid_columns=smoke_cols,
        sample_size=len(cids),
    )
    cfg = default_hightier_serving_config()
    mid_fail = float(
        mid_cell_null_fail_fraction
        if mid_cell_null_fail_fraction is not None
        else cfg.scorer_feast_mid_cell_null_fail_fraction
    )
    entity_ok = rate <= float(entity_missing_fail_fraction)
    mid_ok = (not mid_columns) or mid_rate <= mid_fail
    ok = entity_ok and mid_ok
    distinct_in_store: int | None = None
    if not out.empty and FEAST_CANONICAL_JOIN_KEY in out.columns:
        distinct_in_store = int(
            out[FEAST_CANONICAL_JOIN_KEY].astype(str).str.strip().nunique()
        )
    return {
        "ok": ok,
        "sample_size": len(cids),
        "n_entity_missing": n_entity_missing,
        "entity_missing_rate": round(rate, 4),
        "entity_missing_fail_fraction": float(entity_missing_fail_fraction),
        "mid_cell_null_rate": round(mid_rate, 4),
        "mid_cell_null_fail_fraction": mid_fail,
        "mid_smoke_columns": list(smoke_cols),
        "smoke_event_timestamp": (
            smoke_event_timestamp.isoformat() if smoke_event_timestamp is not None else None
        ),
        "lookup_latency_ms": latency_ms,
        "feature_refs": len(refs),
        "cell_null_counts": cell_null_counts,
        "distinct_canonical_in_store": distinct_in_store,
        "mid_feast_parquet": (
            str(Path(mid_feast_parquet).resolve()) if mid_feast_parquet is not None else None
        ),
    }


def assert_readiness_covers_plan_columns(
    readiness: FeastOnlineReadiness,
    *,
    mid_columns: tuple[str, ...],
    slow_columns: tuple[str, ...],
) -> str | None:
    """Return hard-fail reason when readiness metadata omits model-specific Feast columns."""

    if mid_columns and readiness.mid_term is not None:
        ready = set(readiness.mid_term.feature_columns)
        if ready:
            missing = [c for c in mid_columns if c not in ready]
            if missing:
                tip = ", ".join(missing[:8])
                ellipsis = "" if len(missing) <= 8 else ", …"
                return (
                    "[feast_readiness] mid_term readiness feature_columns missing plan cols: "
                    f"[{tip}{ellipsis}]"
                )
    if slow_columns and readiness.slow_patron is not None:
        ready = set(readiness.slow_patron.feature_columns)
        if ready:
            missing = [c for c in slow_columns if c not in ready]
            if missing:
                tip = ", ".join(missing[:8])
                ellipsis = "" if len(missing) <= 8 else ", …"
                return (
                    "[feast_readiness] slow_patron readiness feature_columns missing plan cols: "
                    f"[{tip}{ellipsis}]"
                )
    return None


def run_deploy_feast_readiness_check(
    *,
    require_mid: bool,
    require_slow: bool,
    allowlist_parquet: Path | None = None,
    canonical_mapping_parquet: Path | None = None,
    mid_columns: tuple[str, ...] = (),
    slow_columns: tuple[str, ...] = (),
    run_lookup_smoke: bool = True,
) -> FeastReadinessGateResult:
    """Load readiness metadata and optionally run allowlist online lookup smoke."""
    cfg = default_hightier_serving_config()
    path = resolve_feast_readiness_path(cfg)
    readiness = load_feast_online_readiness(path)
    plan_cov = assert_readiness_covers_plan_columns(
        readiness,
        mid_columns=mid_columns,
        slow_columns=slow_columns,
    )
    if plan_cov is not None:
        return FeastReadinessGateResult(
            ok=False,
            mid_fresh=None,
            slow_fresh=None,
            hard_failure_reason=plan_cov,
            readiness_path=path,
            deploy_lookup_smoke=None,
        )
    gate = evaluate_feast_readiness_gate(
        readiness,
        require_mid=require_mid,
        require_slow=require_slow,
        readiness_path=path,
        close_hour=int(cfg.gaming_day_close_hour),
        mid_hard_cap_days=int(cfg.mid_term_stale_hard_cap_days),
        slow_hard_cap_days=int(cfg.slow_stale_hard_cap_days),
        slow_grace_days=int(cfg.slow_monthly_grace_days),
    )
    smoke: dict[str, Any] | None = None
    if run_lookup_smoke and gate.ok and allowlist_parquet and canonical_mapping_parquet:
        if mid_columns or slow_columns:
            feast_repo = default_feast_repo_path()
            mid_feast_parquet = resolve_production_mid_feast_parquet(feast_repo)
            smoke_event_ts = None
            if mid_columns and mid_feast_parquet is not None:
                smoke_event_ts = read_feast_parquet_max_event_timestamp(mid_feast_parquet)
            smoke = run_allowlist_feast_lookup_smoke(
                feast_repo=feast_repo,
                allowlist_parquet=allowlist_parquet,
                canonical_mapping_parquet=canonical_mapping_parquet,
                mid_columns=mid_columns,
                slow_columns=slow_columns,
                sample_size=int(cfg.scorer_feast_deploy_lookup_smoke_sample_size),
                entity_missing_fail_fraction=float(cfg.scorer_feast_entity_missing_fail_fraction),
                mid_cell_null_fail_fraction=float(cfg.scorer_feast_mid_cell_null_fail_fraction),
                mid_smoke_columns=cfg.scorer_feast_mid_smoke_columns,
                smoke_event_timestamp=smoke_event_ts,
                mid_feast_parquet=mid_feast_parquet,
            )
            smoke_ok, smoke_reason = evaluate_feast_lookup_smoke_gate(
                smoke,
                mid_columns=mid_columns,
                entity_missing_fail_fraction=float(cfg.scorer_feast_entity_missing_fail_fraction),
                mid_cell_null_fail_fraction=float(cfg.scorer_feast_mid_cell_null_fail_fraction),
                feast_spike_rows=None,
                allowlist_canonical_count=None,
                mid_smoke_columns=cfg.scorer_feast_mid_smoke_columns,
            )
            if not smoke_ok:
                return FeastReadinessGateResult(
                    ok=False,
                    mid_fresh=gate.mid_fresh,
                    slow_fresh=gate.slow_fresh,
                    hard_failure_reason=smoke_reason,
                    readiness_path=path,
                    deploy_lookup_smoke=smoke,
                )
    return FeastReadinessGateResult(
        ok=gate.ok,
        mid_fresh=gate.mid_fresh,
        slow_fresh=gate.slow_fresh,
        hard_failure_reason=gate.hard_failure_reason,
        readiness_path=path,
        deploy_lookup_smoke=smoke,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI: refresh readiness from spike reports or run deploy smoke."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    pr = argparse.ArgumentParser(description="Feast online readiness metadata (scorer v2 P5)")
    pr.add_argument(
        "--refresh-from-reports",
        action="store_true",
        help="merge mid_term_spike_report.json + long_term_spike_report.json into feast_online_readiness.json",
    )
    pr.add_argument("--mid-report", type=Path, default=None)
    pr.add_argument("--long-report", type=Path, default=None)
    pr.add_argument("--out", type=Path, default=None)
    pr.add_argument("--deploy-smoke", action="store_true", help="run readiness + allowlist lookup smoke")
    pr.add_argument("--allowlist-parquet", type=Path, default=None)
    pr.add_argument("--canonical-mapping", type=Path, default=None)
    pr.add_argument("--require-mid", action="store_true", default=True)
    pr.add_argument("--require-slow", action="store_true", default=True)
    args = pr.parse_args(argv)
    if args.refresh_from_reports:
        doc = refresh_readiness_from_spike_reports(
            mid_report_path=args.mid_report,
            long_report_path=args.long_report,
            out_path=args.out,
        )
        logger.info("[feast_readiness] refreshed %s", doc.to_dict())
        return 0
    if args.deploy_smoke:
        if args.allowlist_parquet is None or args.canonical_mapping is None:
            pr.error("--deploy-smoke requires --allowlist-parquet and --canonical-mapping")
        gate = run_deploy_feast_readiness_check(
            require_mid=bool(args.require_mid),
            require_slow=bool(args.require_slow),
            allowlist_parquet=Path(args.allowlist_parquet).resolve(),
            canonical_mapping_parquet=Path(args.canonical_mapping).resolve(),
            mid_columns=PRODUCTION_MID_TERM_FEATURE_COLUMNS[:1],
            slow_columns=PRODUCTION_LONG_TERM_FEATURE_COLUMNS,
            run_lookup_smoke=True,
        )
        logger.info("[feast_readiness] deploy_check %s", gate.to_log_dict())
        return 0 if gate.ok else 1
    pr.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
