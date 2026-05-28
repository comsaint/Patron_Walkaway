"""Production snapshot validation and per-layer freshness evaluation."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import pandas as pd
import pyarrow.parquet as pq
from zoneinfo import ZoneInfo

from trainer_hightier.config import (
    HK_TZ,
    MID_TERM_GRAIN_CANONICAL_DAILY_ASOF,
    MID_TERM_STALE_HARD_CAP_DAYS,
    SLOW_MONTHLY_GRACE_DAYS,
    SLOW_PATRON_GRAIN_CANONICAL_ASOF,
    SLOW_STALE_HARD_CAP_DAYS,
)
from trainer_hightier.feature_experiment.materialize_mid_term_daily_snapshot import (
    MID_TERM_SNAPSHOT_OUTPUT_COLUMNS,
)

logger = logging.getLogger(__name__)

FreshnessStatus = Literal[
    "fresh",
    "stale_allowed",
    "hard_cap_breached",
    "missing",
    "invalid_grain",
]

MID_TERM_FEATURE_COLUMNS: tuple[str, ...] = tuple(
    c for c in MID_TERM_SNAPSHOT_OUTPUT_COLUMNS if c.startswith("fe__")
)
SLOW_PATRON_FEATURE_PREFIX: str = "patron__"


@dataclass(frozen=True)
class LayerFreshnessResult:
    """Structured freshness outcome for one snapshot layer."""

    layer: str
    status: FreshnessStatus
    staleness_days: int | None
    anchor_max: date | None
    message: str


@dataclass(frozen=True)
class SnapshotValidationResult:
    """Artifact validation outcome for one snapshot layer."""

    layer: str
    ok: bool
    hard_failure: bool
    status: FreshnessStatus
    message: str
    row_count: int = 0


@dataclass(frozen=True)
class ScoringSnapshotGate:
    """Combined gate for one scoring cycle."""

    mid_term: LayerFreshnessResult
    slow: LayerFreshnessResult
    degraded: bool
    allow_scoring: bool
    hard_failure_reason: str | None


def _parse_gaming_day(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    if not text:
        return None
    return pd.Timestamp(text).date()


def _parse_iso_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    ts = pd.Timestamp(text)
    if ts.tzinfo is None:
        return ts.to_pydatetime().replace(tzinfo=timezone.utc)
    return ts.to_pydatetime()


def serving_gaming_day(now: datetime | None = None, *, close_hour: int = 3) -> date:
    """Return current serving gaming day (closes at ``close_hour`` local wall clock)."""
    ts = now or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    local = ts.astimezone()
    day = local.date()
    if local.hour < close_hour:
        day = day - timedelta(days=1)
    return day


def expected_mid_term_anchor(serving_day: date) -> date:
    """Mid-term ASOF anchor for serving ``gaming_day = D`` is ``D - 1``."""
    return serving_day - timedelta(days=1)


def serving_day_for_eval_gaming_day_end(eval_gaming_day_end: date) -> date:
    """Serving gaming day for historical replay through *eval_gaming_day_end* (inclusive).

    Production mid anchor for bets on gaming day ``E`` is ``E - 1``, which matches
    ``expected_mid_term_anchor(serving_day=E)``.
    """
    return eval_gaming_day_end


def mid_feast_event_timestamp_for_anchor(anchor_gaming_day: date) -> datetime:
    """Feast ``event_timestamp`` for mid-term online lookup (end of anchor gaming day, HK).

    Must match ``write_mid_feast_parquet`` in ``feast_online_refresh``.
    """
    zone = ZoneInfo(HK_TZ)
    day_start = datetime.combine(anchor_gaming_day, datetime.min.time(), tzinfo=zone)
    return day_start + timedelta(days=1) - timedelta(seconds=1)


def _staleness_days(anchor_max: date | None, expected_anchor: date) -> int | None:
    if anchor_max is None:
        return None
    return max(0, (expected_anchor - anchor_max).days)


def _classify_staleness(
    *,
    layer: str,
    staleness_days: int | None,
    hard_cap_days: int,
    grace_days: int = 0,
) -> LayerFreshnessResult:
    if staleness_days is None:
        return LayerFreshnessResult(
            layer=layer,
            status="missing",
            staleness_days=None,
            anchor_max=None,
            message=f"{layer}: anchor max unavailable",
        )
    if staleness_days <= 0:
        return LayerFreshnessResult(
            layer=layer,
            status="fresh",
            staleness_days=0,
            anchor_max=None,
            message=f"{layer}: fresh",
        )
    if staleness_days <= grace_days + hard_cap_days:
        if staleness_days <= grace_days:
            return LayerFreshnessResult(
                layer=layer,
                status="fresh",
                staleness_days=staleness_days,
                anchor_max=None,
                message=f"{layer}: within grace ({staleness_days}d)",
            )
        return LayerFreshnessResult(
            layer=layer,
            status="stale_allowed",
            staleness_days=staleness_days,
            anchor_max=None,
            message=f"{layer}: stale allowed ({staleness_days}d <= cap {hard_cap_days}d)",
        )
    return LayerFreshnessResult(
        layer=layer,
        status="hard_cap_breached",
        staleness_days=staleness_days,
        anchor_max=None,
        message=f"{layer}: hard cap breached ({staleness_days}d > {hard_cap_days}d)",
    )


def evaluate_mid_term_freshness(
    *,
    anchor_max: date | None,
    serving_day: date | None = None,
    hard_cap_days: int = MID_TERM_STALE_HARD_CAP_DAYS,
    close_hour: int = 3,
    expected_anchor: date | None = None,
) -> LayerFreshnessResult:
    """Evaluate mid-term freshness against expected ``D - 1`` anchor."""
    day = serving_day or serving_gaming_day(close_hour=close_hour)
    expected = expected_anchor or expected_mid_term_anchor(day)
    stale = _staleness_days(anchor_max, expected)
    base = _classify_staleness(
        layer="mid_term",
        staleness_days=stale,
        hard_cap_days=hard_cap_days,
        grace_days=0,
    )
    return LayerFreshnessResult(
        layer=base.layer,
        status=base.status,
        staleness_days=base.staleness_days,
        anchor_max=anchor_max,
        message=base.message,
    )


def evaluate_slow_freshness(
    *,
    anchor_max: date | None,
    serving_day: date | None = None,
    monthly_grace_days: int = SLOW_MONTHLY_GRACE_DAYS,
    hard_cap_days: int = SLOW_STALE_HARD_CAP_DAYS,
    close_hour: int = 3,
    month_epochs: list[date] | None = None,
) -> LayerFreshnessResult:
    """Evaluate slow monthly freshness under gap/post-gap month-turn contract."""
    del monthly_grace_days, hard_cap_days  # legacy params retained for call-site compatibility
    from trainer_hightier.utils.slow_month_turn import resolve_slow_month_turn_context

    day = serving_day or serving_gaming_day(close_hour=close_hour)
    ctx = resolve_slow_month_turn_context(day, month_epochs=month_epochs)
    if anchor_max is None:
        return LayerFreshnessResult(
            layer="slow",
            status="missing",
            staleness_days=None,
            anchor_max=None,
            message=(
                f"slow: missing anchor for phase={ctx.phase} "
                f"(required={ctx.slow_anchor_required.isoformat()})"
            ),
        )
    if ctx.phase == "post_gap":
        if anchor_max != ctx.slow_anchor_target:
            return LayerFreshnessResult(
                layer="slow",
                status="hard_cap_breached",
                staleness_days=None,
                anchor_max=anchor_max,
                message=(
                    f"slow post-gap requires target anchor {ctx.slow_anchor_target.isoformat()}, "
                    f"got {anchor_max.isoformat()}"
                ),
            )
        return LayerFreshnessResult(
            layer="slow",
            status="fresh",
            staleness_days=0,
            anchor_max=anchor_max,
            message="slow: post-gap target anchor satisfied",
        )
    allowed = {ctx.slow_anchor_effective, ctx.slow_anchor_target}
    if anchor_max not in allowed:
        return LayerFreshnessResult(
            layer="slow",
            status="hard_cap_breached",
            staleness_days=None,
            anchor_max=anchor_max,
            message=(
                f"slow gap-day requires effective/target anchor in {sorted(d.isoformat() for d in allowed)}, "
                f"got {anchor_max.isoformat()}"
            ),
        )
    return LayerFreshnessResult(
        layer="slow",
        status="fresh",
        staleness_days=0,
        anchor_max=anchor_max,
        message=f"slow: gap-day anchor ok (phase={ctx.phase})",
    )


def _read_parquet_columns(path: Path) -> tuple[list[str], int]:
    pf = pq.ParquetFile(path)
    cols = list(pf.schema_arrow.names)
    return cols, int(pf.metadata.num_rows)


def _max_anchor_from_parquet(path: Path, col: str) -> date | None:
    table = pq.read_table(path, columns=[col])
    if table.num_rows == 0:
        return None
    series = table.column(col).to_pandas()
    parsed = pd.to_datetime(series, errors="coerce")
    if parsed.isna().all():
        return None
    return parsed.max().date()


def _columns_all_null(path: Path, columns: tuple[str, ...]) -> list[str]:
    if not columns:
        return []
    table = pq.read_table(path, columns=list(columns))
    if table.num_rows == 0:
        return list(columns)
    frame = table.to_pandas()
    null_cols: list[str] = []
    for col in columns:
        if col not in frame.columns:
            null_cols.append(col)
            continue
        if frame[col].isna().all():
            null_cols.append(col)
    return null_cols


def validate_mid_term_artifact(
    path: Path | None,
    *,
    expected_grain: str = MID_TERM_GRAIN_CANONICAL_DAILY_ASOF,
    manifest_grain: str | None = None,
) -> SnapshotValidationResult:
    """Validate mid-term canonical daily snapshot artifact."""
    if path is None or not Path(path).is_file():
        return SnapshotValidationResult(
            layer="mid_term",
            ok=False,
            hard_failure=True,
            status="missing",
            message="mid_term snapshot parquet missing",
        )
    p = Path(path).resolve()
    grain = manifest_grain or expected_grain
    if grain != expected_grain:
        return SnapshotValidationResult(
            layer="mid_term",
            ok=False,
            hard_failure=True,
            status="invalid_grain",
            message=f"mid_term grain={grain!r} expected {expected_grain!r}",
        )
    cols, n_rows = _read_parquet_columns(p)
    required_keys = ("canonical_id", "anchor_gaming_day")
    missing_keys = [c for c in required_keys if c not in cols]
    if missing_keys:
        return SnapshotValidationResult(
            layer="mid_term",
            ok=False,
            hard_failure=True,
            status="missing",
            message=f"mid_term missing keys: {missing_keys}",
        )
    missing_feats = [c for c in MID_TERM_FEATURE_COLUMNS if c not in cols]
    if missing_feats:
        return SnapshotValidationResult(
            layer="mid_term",
            ok=False,
            hard_failure=True,
            status="missing",
            message=f"mid_term missing feature columns: {missing_feats}",
        )
    if n_rows == 0:
        return SnapshotValidationResult(
            layer="mid_term",
            ok=False,
            hard_failure=True,
            status="missing",
            message="mid_term snapshot is empty",
            row_count=0,
        )
    null_feats = _columns_all_null(p, MID_TERM_FEATURE_COLUMNS)
    if null_feats:
        return SnapshotValidationResult(
            layer="mid_term",
            ok=False,
            hard_failure=True,
            status="missing",
            message=f"mid_term all-null feature columns: {null_feats}",
            row_count=n_rows,
        )
    dup = pq.read_table(p, columns=list(required_keys)).to_pandas()
    if dup.duplicated(subset=list(required_keys)).any():
        return SnapshotValidationResult(
            layer="mid_term",
            ok=False,
            hard_failure=True,
            status="invalid_grain",
            message="mid_term duplicate (canonical_id, anchor_gaming_day)",
            row_count=n_rows,
        )
    return SnapshotValidationResult(
        layer="mid_term",
        ok=True,
        hard_failure=False,
        status="fresh",
        message="mid_term artifact valid",
        row_count=n_rows,
    )


def validate_slow_artifact(
    path: Path | None,
    *,
    expected_grain: str = SLOW_PATRON_GRAIN_CANONICAL_ASOF,
    manifest_grain: str | None = None,
) -> SnapshotValidationResult:
    """Validate slow patron canonical ASOF artifact."""
    if path is None or not Path(path).is_file():
        return SnapshotValidationResult(
            layer="slow",
            ok=False,
            hard_failure=True,
            status="missing",
            message="slow patron parquet missing",
        )
    p = Path(path).resolve()
    grain = manifest_grain or expected_grain
    if grain != expected_grain:
        return SnapshotValidationResult(
            layer="slow",
            ok=False,
            hard_failure=True,
            status="invalid_grain",
            message=f"slow grain={grain!r} expected {expected_grain!r}",
        )
    cols, n_rows = _read_parquet_columns(p)
    if "canonical_id" not in cols:
        return SnapshotValidationResult(
            layer="slow",
            ok=False,
            hard_failure=True,
            status="missing",
            message="slow patron missing canonical_id",
        )
    slow_cols = tuple(c for c in cols if c.startswith(SLOW_PATRON_FEATURE_PREFIX))
    if not slow_cols:
        return SnapshotValidationResult(
            layer="slow",
            ok=False,
            hard_failure=True,
            status="missing",
            message="slow patron missing patron__* columns",
        )
    if n_rows == 0:
        return SnapshotValidationResult(
            layer="slow",
            ok=False,
            hard_failure=True,
            status="missing",
            message="slow patron snapshot is empty",
            row_count=0,
        )
    null_feats = _columns_all_null(p, slow_cols)
    if null_feats:
        return SnapshotValidationResult(
            layer="slow",
            ok=False,
            hard_failure=True,
            status="missing",
            message=f"slow patron all-null columns: {null_feats}",
            row_count=n_rows,
        )
    return SnapshotValidationResult(
        layer="slow",
        ok=True,
        hard_failure=False,
        status="fresh",
        message="slow patron artifact valid",
        row_count=n_rows,
    )


def post_join_feature_smoke(
    frame: pd.DataFrame,
    *,
    mid_term_columns: tuple[str, ...] = MID_TERM_FEATURE_COLUMNS,
    slow_prefix: str = SLOW_PATRON_FEATURE_PREFIX,
) -> list[str]:
    """Return hard-failure messages when joined feature families are all null."""
    failures: list[str] = []
    mid_present = [c for c in mid_term_columns if c in frame.columns]
    if mid_present and frame[mid_present].isna().all().all():
        failures.append("post-join mid-term fe__* all null")
    slow_present = [c for c in frame.columns if str(c).startswith(slow_prefix)]
    if slow_present and frame[slow_present].isna().all().all():
        failures.append("post-join patron__* all null")
    return failures


def build_scoring_snapshot_gate(
    *,
    mid_term: LayerFreshnessResult,
    slow: LayerFreshnessResult,
    mid_validation: SnapshotValidationResult | None = None,
    slow_validation: SnapshotValidationResult | None = None,
    allow_hard_cap_override: bool = False,
) -> ScoringSnapshotGate:
    """Combine layer freshness + validation into scorer gate."""
    hard_reason: str | None = None
    for val in (mid_validation, slow_validation):
        if val is not None and val.hard_failure:
            hard_reason = val.message
            break
    if hard_reason is None:
        for layer in (mid_term, slow):
            if layer.status in ("missing", "invalid_grain"):
                hard_reason = layer.message
                break
            if layer.status == "hard_cap_breached" and not allow_hard_cap_override:
                hard_reason = layer.message
                break
    degraded = (
        mid_term.status == "stale_allowed"
        or (slow.status == "stale_allowed")
        or (mid_term.staleness_days or 0) > 0
    )
    return ScoringSnapshotGate(
        mid_term=mid_term,
        slow=slow,
        degraded=degraded,
        allow_scoring=hard_reason is None,
        hard_failure_reason=hard_reason,
    )


def manifest_anchor_max(raw: dict[str, Any] | None, key: str) -> date | None:
    if not isinstance(raw, dict):
        return None
    return _parse_gaming_day(raw.get(key))


def read_mid_term_anchor_max(path: Path | None, manifest: dict[str, Any] | None) -> date | None:
    from trainer_hightier.config import MANIFEST_KEY_MID_TERM_ANCHOR_MAX

    anchor = manifest_anchor_max(manifest, MANIFEST_KEY_MID_TERM_ANCHOR_MAX)
    if anchor is not None:
        return anchor
    if path is not None and Path(path).is_file():
        return _max_anchor_from_parquet(Path(path), "anchor_gaming_day")
    return None


def read_slow_anchor_max(path: Path | None, manifest: dict[str, Any] | None) -> date | None:
    from trainer_hightier.config import MANIFEST_KEY_SLOW_ANCHOR_MAX

    anchor = manifest_anchor_max(manifest, MANIFEST_KEY_SLOW_ANCHOR_MAX)
    if anchor is not None:
        return anchor
    if path is not None and Path(path).is_file():
        for col in ("anchor_gaming_day", "gaming_day", "asof_gaming_day"):
            try:
                cols, _ = _read_parquet_columns(Path(path))
            except (OSError, ValueError):
                return None
            if col in cols:
                return _max_anchor_from_parquet(Path(path), col)
    return None


def expected_slow_month_end_anchor(serving_day: date | None = None, *, close_hour: int = 3) -> date:
    """Expected slow target anchor (previous calendar month-end) for ``serving_day``."""
    day = serving_day or serving_gaming_day(close_hour=close_hour)
    return date(day.year, day.month, 1) - timedelta(days=1)


@dataclass(frozen=True)
class DeployStartupSnapshotPlan:
    """Startup vs background refresh decisions for deploy supervisor."""

    mid_hard_failure: bool
    slow_hard_failure: bool
    mid_startup_refresh: bool
    slow_startup_refresh: bool
    mid_background_refresh: bool
    slow_background_refresh: bool
    mid_reason: str
    slow_reason: str


def _layer_hard_failure(
    freshness: LayerFreshnessResult,
    validation: SnapshotValidationResult | None,
) -> bool:
    if validation is not None and validation.hard_failure:
        return True
    return freshness.status in ("missing", "hard_cap_breached", "invalid_grain")


def build_deploy_startup_snapshot_plan(cfg: Any | None = None) -> DeployStartupSnapshotPlan:
    """Classify snapshot layers for startup hard repair vs background retry."""

    from trainer_hightier.config import default_hightier_serving_config
    from trainer_hightier.serving.feature_state_store import read_active_manifest

    serving_cfg = cfg or default_hightier_serving_config()
    man = read_active_manifest()
    mid_path = man.mid_term_snapshot_parquet if man is not None else None
    slow_path = man.slow_patron_parquet if man is not None else None
    slow_grain = man.raw.get("slow_patron_grain") if man is not None else None
    mid_val = (
        validate_mid_term_artifact(
            mid_path,
            manifest_grain=(man.raw.get("mid_term_grain") if man is not None else None),
        )
        if man is not None
        else validate_mid_term_artifact(None)
    )
    slow_val = (
        validate_slow_artifact(slow_path, manifest_grain=slow_grain)
        if man is not None
        else validate_slow_artifact(None)
    )
    mid_anchor = read_mid_term_anchor_max(mid_path, man.raw if man is not None else None)
    slow_anchor = read_slow_anchor_max(slow_path, man.raw if man is not None else None)
    mid_fresh = evaluate_mid_term_freshness(
        anchor_max=mid_anchor,
        hard_cap_days=int(serving_cfg.mid_term_stale_hard_cap_days),
        close_hour=int(serving_cfg.gaming_day_close_hour),
    )
    slow_fresh = evaluate_slow_freshness(
        anchor_max=slow_anchor,
        monthly_grace_days=int(serving_cfg.slow_monthly_grace_days),
        hard_cap_days=int(serving_cfg.slow_stale_hard_cap_days),
        close_hour=int(serving_cfg.gaming_day_close_hour),
    )
    mid_hard = _layer_hard_failure(mid_fresh, mid_val)
    slow_hard = _layer_hard_failure(slow_fresh, slow_val)
    now_hk = datetime.now(timezone.utc).astimezone()
    after_refresh_target = int(now_hk.hour) >= int(serving_cfg.mid_term_refresh_target_hour)
    mid_bg = (not mid_hard) and mid_fresh.status == "stale_allowed" and after_refresh_target
    slow_bg = (not slow_hard) and slow_fresh.status in ("stale_allowed", "missing")
    return DeployStartupSnapshotPlan(
        mid_hard_failure=mid_hard,
        slow_hard_failure=slow_hard,
        mid_startup_refresh=mid_hard,
        slow_startup_refresh=slow_hard,
        mid_background_refresh=mid_bg,
        slow_background_refresh=slow_bg,
        mid_reason=mid_val.message if mid_val.hard_failure else mid_fresh.message,
        slow_reason=slow_val.message if slow_val.hard_failure else slow_fresh.message,
    )
