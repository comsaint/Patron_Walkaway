"""Production compact source mirror validation for snapshot refresh jobs."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pyarrow.parquet as pq

from trainer_hightier.config import (
    DuckDbRuntimeConfig,
    PRODUCTION_BET_MIRROR_DIRNAME,
    PRODUCTION_SESSION_MIRROR_FILENAME,
    default_hightier_serving_config,
)
from trainer_hightier.utils.bet_l0_preprocess import (
    cleaned_bet_dataset_has_any_parquet,
    first_parquet_under_for_schema,
    resolved_cleaned_bet_read_parquet_sql,
)
from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas

logger = logging.getLogger(__name__)

PRODUCTION_BET_MIRROR_REQUIRED_COLUMNS: tuple[str, ...] = (
    "player_id",
    "gaming_day_event",
    "wager",
    "payout_complete_dtm",
    "bet_id",
)
PRODUCTION_SESSION_MIRROR_REQUIRED_COLUMNS: tuple[str, ...] = (
    "player_id",
    "gaming_day_event",
    "theo_win",
)


@dataclass(frozen=True)
class MirrorValidationResult:
    """Outcome of production source mirror coverage validation."""

    layer: str
    ok: bool
    message: str
    min_gaming_day: date | None = None
    max_gaming_day: date | None = None
    row_count: int = 0


def resolve_production_bet_mirror_dir() -> Path:
    """Return configured cleaned bet mirror root."""

    cfg = default_hightier_serving_config()
    if cfg.production_cleaned_bet_mirror_dir is not None:
        return Path(cfg.production_cleaned_bet_mirror_dir).resolve()
    return (
        Path(cfg.snapshot_manifest_dir).resolve().parent
        / "source_mirror"
        / PRODUCTION_BET_MIRROR_DIRNAME
    )


def resolve_production_session_mirror_path() -> Path:
    """Return configured cleaned session mirror parquet path."""

    cfg = default_hightier_serving_config()
    if cfg.production_cleaned_session_mirror_parquet is not None:
        return Path(cfg.production_cleaned_session_mirror_parquet).resolve()
    return (
        Path(cfg.snapshot_manifest_dir).resolve().parent
        / "source_mirror"
        / PRODUCTION_SESSION_MIRROR_FILENAME
    )


def _missing_columns(path: Path, required: tuple[str, ...]) -> list[str]:
    cols = set(pq.read_schema(path).names)
    return [c for c in required if c not in cols]


def _bet_mirror_gaming_day_bounds(root: Path) -> tuple[date | None, date | None, int]:
    if not cleaned_bet_dataset_has_any_parquet(root):
        return None, None, 0
    bet_from = resolved_cleaned_bet_read_parquet_sql(root)
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, DuckDbRuntimeConfig())
        row = con.execute(
            f"""
SELECT
  MIN(CAST(gaming_day_event AS DATE)) AS min_gday,
  MAX(CAST(gaming_day_event AS DATE)) AS max_gday,
  COUNT(*) AS nrows
FROM {bet_from}
WHERE gaming_day_event IS NOT NULL
""".strip()
        ).fetchone()
    finally:
        con.close()
    if row is None:
        return None, None, 0
    min_g, max_g, nrows = row[0], row[1], row[2]
    min_day = None if min_g is None else date.fromisoformat(str(min_g)[:10])
    max_day = None if max_g is None else date.fromisoformat(str(max_g)[:10])
    return min_day, max_day, int(nrows or 0)


def validate_production_bet_mirror(
    *,
    required_lookback_days: int | None = None,
    mirror_dir: Path | None = None,
) -> MirrorValidationResult:
    """Validate cleaned bet mirror schema and gaming-day coverage."""

    cfg = default_hightier_serving_config()
    root = Path(mirror_dir or resolve_production_bet_mirror_dir()).resolve()
    retain = int(required_lookback_days or cfg.production_bet_mirror_retention_days)
    if not root.exists():
        return MirrorValidationResult(
            layer="bet_mirror",
            ok=False,
            message=f"cleaned bet mirror missing: {root}",
        )
    if not cleaned_bet_dataset_has_any_parquet(root):
        return MirrorValidationResult(
            layer="bet_mirror",
            ok=False,
            message=f"cleaned bet mirror has no parquet under {root}",
        )
    sample = first_parquet_under_for_schema(root)
    miss = _missing_columns(sample, PRODUCTION_BET_MIRROR_REQUIRED_COLUMNS)
    if miss:
        return MirrorValidationResult(
            layer="bet_mirror",
            ok=False,
            message=f"cleaned bet mirror missing columns {miss}",
        )
    min_day, max_day, nrows = _bet_mirror_gaming_day_bounds(root)
    if min_day is None or max_day is None or nrows <= 0:
        return MirrorValidationResult(
            layer="bet_mirror",
            ok=False,
            message="cleaned bet mirror has no usable gaming_day_event rows",
        )
    today = date.today()
    need_from = today - timedelta(days=retain)
    if max_day < today - timedelta(days=1):
        return MirrorValidationResult(
            layer="bet_mirror",
            ok=False,
            message=f"cleaned bet mirror max gaming_day_event={max_day} too stale for refresh",
            min_gaming_day=min_day,
            max_gaming_day=max_day,
            row_count=nrows,
        )
    if min_day > need_from:
        return MirrorValidationResult(
            layer="bet_mirror",
            ok=False,
            message=(
                f"cleaned bet mirror coverage starts {min_day}; need lookback from {need_from}"
            ),
            min_gaming_day=min_day,
            max_gaming_day=max_day,
            row_count=nrows,
        )
    return MirrorValidationResult(
        layer="bet_mirror",
        ok=True,
        message="cleaned bet mirror valid",
        min_gaming_day=min_day,
        max_gaming_day=max_day,
        row_count=nrows,
    )


def validate_production_session_mirror(
    *,
    required_lookback_days: int | None = None,
    mirror_path: Path | None = None,
) -> MirrorValidationResult:
    """Validate cleaned session mirror schema and gaming-day coverage."""

    cfg = default_hightier_serving_config()
    path = Path(mirror_path or resolve_production_session_mirror_path()).resolve()
    retain = int(required_lookback_days or cfg.production_session_mirror_retention_days)
    if not path.is_file():
        return MirrorValidationResult(
            layer="session_mirror",
            ok=False,
            message=f"cleaned session mirror missing: {path}",
        )
    miss = _missing_columns(path, PRODUCTION_SESSION_MIRROR_REQUIRED_COLUMNS)
    if miss:
        return MirrorValidationResult(
            layer="session_mirror",
            ok=False,
            message=f"cleaned session mirror missing columns {miss}",
        )
    pf = pq.ParquetFile(path)
    nrows = int(pf.metadata.num_rows) if pf.metadata is not None else 0
    if nrows <= 0:
        return MirrorValidationResult(
            layer="session_mirror",
            ok=False,
            message="cleaned session mirror is empty",
        )
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, DuckDbRuntimeConfig())
        esc = str(path).replace("\\", "/").replace("'", "''")
        row = con.execute(
            f"""
SELECT
  MIN(CAST(gaming_day_event AS DATE)) AS min_gday,
  MAX(CAST(gaming_day_event AS DATE)) AS max_gday
FROM read_parquet('{esc}')
WHERE gaming_day_event IS NOT NULL
""".strip()
        ).fetchone()
    finally:
        con.close()
    if row is None or row[0] is None or row[1] is None:
        return MirrorValidationResult(
            layer="session_mirror",
            ok=False,
            message="cleaned session mirror has no usable gaming_day_event rows",
            row_count=nrows,
        )
    min_day = date.fromisoformat(str(row[0])[:10])
    max_day = date.fromisoformat(str(row[1])[:10])
    need_from = date.today() - timedelta(days=retain)
    if min_day > need_from:
        return MirrorValidationResult(
            layer="session_mirror",
            ok=False,
            message=(
                f"cleaned session mirror coverage starts {min_day}; need lookback from {need_from}"
            ),
            min_gaming_day=min_day,
            max_gaming_day=max_day,
            row_count=nrows,
        )
    return MirrorValidationResult(
        layer="session_mirror",
        ok=True,
        message="cleaned session mirror valid",
        min_gaming_day=min_day,
        max_gaming_day=max_day,
        row_count=nrows,
    )


def ensure_production_mirrors_ready(
    *,
    for_mid_term: bool,
    for_slow: bool,
    cleaned_bet: Path | None = None,
    cleaned_session: Path | None = None,
) -> dict[str, MirrorValidationResult]:
    """Validate required production mirrors before refresh; raise on failure."""

    out: dict[str, MirrorValidationResult] = {}
    if for_mid_term:
        bet = validate_production_bet_mirror(mirror_dir=cleaned_bet)
        out["bet_mirror"] = bet
        if not bet.ok:
            raise ValueError(f"[source_mirror] {bet.message}")
    if for_slow:
        sess = validate_production_session_mirror(mirror_path=cleaned_session)
        out["session_mirror"] = sess
        if not sess.ok:
            raise ValueError(f"[source_mirror] {sess.message}")
    return out
