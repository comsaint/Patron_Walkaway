"""trainer/training/data_sources.py
====================================
Data ingress helpers extracted from ``trainer/training/trainer.py`` (Issue #12 PR-12.2).

Scope
-----
This module owns *where data comes from* for the training pipeline.

* ClickHouse session/bet pull (``load_clickhouse_data``) — unchanged.
* Local Parquet session/bet load (``load_local_parquet``): Issue #14 Workstream A
  resolves bet/session paths via ``trainer_local_parquet_bridge.manifest.json``;
  legacy bare ``data/gmwds_t_*.parquet`` discovery without that manifest is removed.
* Local Parquet metadata helpers used to date-range the run window and
  hash chunk-cache invalidation tokens
  (``_parquet_date_range``, ``_detect_local_data_end``,
  ``_parquet_stable_rowgroups_schema_digest``,
  ``_local_parquet_source_data_hash``).

The original ``trainer.training.trainer`` module re-exports these names so
existing call-sites (and external consumers like
``parallel_lda_mvp.trainer_bridge_mvp``) keep working unchanged.

Notes on configuration
----------------------
Resolves ``PLACEHOLDER_PLAYER_ID``, ``SOURCE_DB``, ``TBET``, ``TSESSION``, and
``HISTORY_BUFFER_DAYS`` from ``trainer.config`` (with the same legacy
top-level ``config`` fallback used by ``trainer.py``). ``LOCAL_PARQUET_DIR``
is computed from this file's path so it points at the same ``data/`` folder
``trainer.py`` always used (``<repo>/data``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

try:
    import config as _cfg  # type: ignore[import]
    from db_conn import get_clickhouse_client  # type: ignore[import]
except ModuleNotFoundError:
    import trainer.config as _cfg  # type: ignore[import]
    from trainer.db_conn import get_clickhouse_client  # type: ignore[import]

logger = logging.getLogger(__name__)

PLACEHOLDER_PLAYER_ID = _cfg.PLACEHOLDER_PLAYER_ID
SOURCE_DB = _cfg.SOURCE_DB
TBET = _cfg.TBET
TSESSION = _cfg.TSESSION
HISTORY_BUFFER_DAYS: int = getattr(_cfg, "HISTORY_BUFFER_DAYS", 2)
HK_TZ = ZoneInfo(getattr(_cfg, "HK_TZ", "Asia/Hong_Kong"))

# ``trainer/training/data_sources.py`` -> parents[2] = repository root.
# Identical to ``LOCAL_PARQUET_DIR`` previously defined in trainer.py.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOCAL_PARQUET_DIR = PROJECT_ROOT / "data"
LOCAL_PARQUET_DIR.mkdir(parents=True, exist_ok=True)


def trainer_local_parquet_bridge_manifest_path() -> Path:
    """Path to ``trainer_local_parquet_bridge.manifest.json`` under ``LOCAL_PARQUET_DIR``.

    Resolved from ``LOCAL_PARQUET_DIR`` at call time so tests can monkeypatch
    ``LOCAL_PARQUET_DIR`` and still pick up the manifest in the temp root.
    """
    return LOCAL_PARQUET_DIR / "trainer_local_parquet_bridge.manifest.json"


# Minimal session columns needed for canonical-map + dummy-player detection.
# Defined at module level so tests can validate coverage against
# ``identity._REQUIRED_SESSION_COLS``. Reading only these columns (instead of
# all 80+) avoids OOM on the 74M-row session parquet.
_CANONICAL_MAP_SESSION_COLS: list = [
    "session_id", "player_id", "casino_player_id",
    "lud_dtm", "session_start_dtm", "session_end_dtm",
    "is_manual", "is_deleted", "is_canceled", "num_games_with_wager",
    "turnover",
]

# Run/trip LDA pass-through columns on bet (must match ``parallel_lda_mvp.trainer_bridge_mvp.LDA_RUN_TRIP_BET_COLUMNS``).
# When the bridge manifest sets ``bet_includes_run_trip_lda_columns: true`` (or legacy ``phase_c: true``),
# all of these must exist on the bet Parquet schema (fail-fast).
_MANIFEST_KEY_BET_INCLUDES_RUN_TRIP_LDA = "bet_includes_run_trip_lda_columns"
_LEGACY_MANIFEST_KEY_PHASE_C = "phase_c"

_OPTIONAL_BET_LDA_RUN_TRIP_COLS: tuple[str, ...] = (
    "lda_l1_run_bet_count",
    "lda_trip_run_count",
    "lda_run_ord_in_trip",
    "lda_trip_is_closed",
    "lda_l1_run_duration_min",
)

# Minimal bet columns needed by the full process_chunk pipeline.
# Column pushdown: load_local_parquet reads only these from the ~60-column
# t_bet Parquet, cutting RAM by ~2/3 and avoiding the 17-object-column
# .copy() OOM. Includes DQ/identity, Track Human, Track LLM YAML and legacy
# columns. If a future feature spec references additional source columns,
# add them here.
_REQUIRED_BET_PARQUET_COLS: list = [
    # Keys & timestamps
    "bet_id",
    "session_id",
    "player_id",
    "game_id",
    "table_id",
    "payout_complete_dtm",
    "gaming_day",
    # DQ guard / Track Human state machines
    "wager",
    "status",
    "casino_win",
    # Legacy / Track LLM features
    "payout_odds",
    "base_ha",
    "is_back_bet",
    "position_idx",
]


_BET_SELECT_COLS = """
    bet_id,
    session_id,
    player_id,
    table_id,
    payout_complete_dtm,
    wager,
    casino_win,
    status,
    COALESCE(gaming_day, toDate(payout_complete_dtm)) AS gaming_day,
    is_back_bet,
    base_ha,
    bet_type,
    payout_odds,
    position_idx
""".strip()

_SESSION_SELECT_COLS = """
    session_id,
    player_id,
    CASE WHEN lower(trim(casino_player_id)) IN ('', 'null')
         THEN NULL ELSE trim(casino_player_id) END AS casino_player_id,
    table_id,
    session_start_dtm,
    session_end_dtm,
    COALESCE(lud_dtm, session_end_dtm, session_start_dtm) AS lud_dtm,
    is_manual,
    is_deleted,
    is_canceled,
    COALESCE(turnover, 0) AS turnover,
    COALESCE(num_games_with_wager, 0) AS num_games_with_wager
""".strip()


# ---------------------------------------------------------------------------
# Local bridge manifest (Issue #14 Workstream A)
# ---------------------------------------------------------------------------

def load_trainer_local_parquet_bridge_manifest() -> Dict[str, Any]:
    """Load ``trainer_local_parquet_bridge.manifest.json`` from ``LOCAL_PARQUET_DIR``.

    Local-parquet training ingress requires this file; legacy bare
    ``gmwds_t_*.parquet`` discovery without a manifest is no longer supported.

    Returns
    -------
    dict
        Parsed manifest JSON.

    Raises
    ------
    FileNotFoundError
        If the manifest file is missing.
    json.JSONDecodeError
        If the file is not valid JSON.
    """
    p = trainer_local_parquet_bridge_manifest_path()
    if not p.is_file():
        raise FileNotFoundError(
            f"Workstream A: local Parquet bridge manifest missing: {p}. "
            "Materialize the trainer bridge (parallel_lda_mvp trainer_bridge_mvp / "
            "run_mvp --emit-trainer-local-parquet) so this JSON exists; "
            "training no longer auto-discovers data/gmwds_t_bet.parquet without it."
        )
    return dict(json.loads(p.read_text(encoding="utf-8")))


def _manifest_path_strict_enabled() -> bool:
    """When True (default), absolute paths outside ``PROJECT_ROOT`` fail fast (portable manifest contract)."""
    v = (os.environ.get("TRAINER_MANIFEST_PATH_STRICT") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _resolve_manifest_path_str(
    raw: str,
    *,
    manifest_path: Path,
    prefer_project_root: bool = False,
) -> Path:
    """Resolve a manifest path entry to an absolute Path.

    ``prefer_project_root`` pins the intended anchor for relative paths.
    This avoids ambiguous resolution when manifests contain mixed anchors
    (some paths relative to ``PROJECT_ROOT``, others to ``manifest_path.parent``).
    """
    p = Path(str(raw)).expanduser()
    if not p.is_absolute():
        anchor_primary = PROJECT_ROOT if prefer_project_root else manifest_path.parent
        anchor_secondary = manifest_path.parent if prefer_project_root else PROJECT_ROOT
        primary = (anchor_primary / p).resolve()
        secondary = (anchor_secondary / p).resolve()
        if primary.is_file():
            return primary
        if secondary.is_file():
            return secondary
        return primary
    pr_abs = p.resolve()
    try:
        rel = pr_abs.relative_to(PROJECT_ROOT.resolve())
        return (PROJECT_ROOT / rel).resolve()
    except ValueError:
        if _manifest_path_strict_enabled():
            raise ValueError(
                f"Manifest path {raw!r} resolves to {pr_abs} which is outside "
                f"PROJECT_ROOT={PROJECT_ROOT.resolve()!s}. "
                "Ship manifests with repo-relative paths or paths relative to the manifest "
                "directory only (see doc/pipeline requirements.md Inputs). "
                "Set TRAINER_MANIFEST_PATH_STRICT=0 to allow legacy absolute paths (not for CI/production)."
            ) from None
        logger.warning(
            "Manifest path %r is absolute and outside PROJECT_ROOT — using as-is (portability risk).",
            raw,
        )
        return pr_abs


def resolve_local_parquet_bet_session_paths_from_manifest(
    manifest: Dict[str, Any],
    manifest_path: Optional[Path] = None,
) -> Tuple[Path, Path]:
    """Resolve bet and session Parquet paths from a bridge manifest dict."""
    mp = manifest_path if manifest_path is not None else trainer_local_parquet_bridge_manifest_path()
    raw_bet: Optional[str] = None
    bet_prefer_project_root = False
    # Prefer trainer-ready bridge output (L1-enriched) over L0 provenance in ``t_bet_paths``.
    if manifest.get("gmwds_t_bet"):
        raw_bet = str(manifest["gmwds_t_bet"])
        bet_prefer_project_root = False
    if raw_bet is None:
        t_bet_paths = manifest.get("t_bet_paths")
        if isinstance(t_bet_paths, list) and t_bet_paths:
            raw_bet = str(t_bet_paths[0])
            bet_prefer_project_root = True
    if not raw_bet:
        raise KeyError(
            "manifest must contain 'gmwds_t_bet' or non-empty 't_bet_paths' "
            f"(got keys={sorted(manifest.keys())!r})"
        )
    sess_raw: Optional[Any] = None
    sess_prefer_project_root = False
    if manifest.get("gmwds_t_session"):
        sess_raw = manifest.get("gmwds_t_session")
        sess_prefer_project_root = False
    elif manifest.get("t_session_source"):
        sess_raw = manifest.get("t_session_source")
        sess_prefer_project_root = True
    if not sess_raw:
        raise KeyError(
            "manifest must contain 'gmwds_t_session' or 't_session_source' "
            f"(got keys={sorted(manifest.keys())!r})"
        )
    return (
        _resolve_manifest_path_str(
            raw_bet,
            manifest_path=mp,
            prefer_project_root=bet_prefer_project_root,
        ),
        _resolve_manifest_path_str(
            str(sess_raw),
            manifest_path=mp,
            prefer_project_root=sess_prefer_project_root,
        ),
    )


def manifest_bet_includes_run_trip_lda_columns(manifest: Dict[str, Any]) -> bool:
    """Return True if the manifest requires the five ``lda_*`` run/trip columns on bet Parquet.

    Reads the canonical key ``bet_includes_run_trip_lda_columns`` first, then the legacy
    ``phase_c`` key for manifests emitted by older bridge builds.
    """
    if bool(manifest.get(_MANIFEST_KEY_BET_INCLUDES_RUN_TRIP_LDA)):
        return True
    return bool(manifest.get(_LEGACY_MANIFEST_KEY_PHASE_C, False))


def local_parquet_session_path_for_trainer() -> Path:
    """Return session Parquet path from the bridge manifest (single source of truth)."""
    mp = trainer_local_parquet_bridge_manifest_path()
    m = load_trainer_local_parquet_bridge_manifest()
    _, sess = resolve_local_parquet_bet_session_paths_from_manifest(m, manifest_path=mp)
    return sess


def _validate_bet_parquet_run_trip_lda_schema(bet_path: Path, manifest: Dict[str, Any]) -> None:
    """If manifest requires run/trip LDA columns on bet, require all five on schema."""
    if not manifest_bet_includes_run_trip_lda_columns(manifest):
        return
    import pyarrow.parquet as _pq_bet
    names = set(_pq_bet.read_schema(bet_path).names)
    missing = [c for c in _OPTIONAL_BET_LDA_RUN_TRIP_COLS if c not in names]
    if missing:
        raise ValueError(
            "Workstream A: manifest requires bet_includes_run_trip_lda_columns but bet Parquet "
            f"is missing columns {missing!r} (path={bet_path})"
        )


@dataclass(frozen=True)
class BridgeLocalParquetReadiness:
    """Result of :func:`probe_trainer_local_parquet_bridge_readiness` (WS1, no side effects)."""

    ready: bool
    reasons: Tuple[str, ...]
    manifest_path: Optional[Path] = None


def probe_trainer_local_parquet_bridge_readiness() -> BridgeLocalParquetReadiness:
    """Check whether local Parquet bridge ingress is ready (manifest + paths + run/trip LDA schema).

    Pure probe: does not materialize files or mutate disk. Used by AutoBuild orchestrator
    before calling ``load_local_parquet``.
    """
    reasons: List[str] = []
    p = trainer_local_parquet_bridge_manifest_path()
    if not p.is_file():
        return BridgeLocalParquetReadiness(
            ready=False,
            reasons=(f"manifest_missing:{p}",),
            manifest_path=p,
        )
    try:
        manifest = dict(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        return BridgeLocalParquetReadiness(
            ready=False,
            reasons=(f"manifest_unreadable:{p}:{exc}",),
            manifest_path=p,
        )
    try:
        bet_path, sess_path = resolve_local_parquet_bet_session_paths_from_manifest(manifest, manifest_path=p)
    except KeyError as exc:
        return BridgeLocalParquetReadiness(
            ready=False,
            reasons=(f"manifest_keys:{exc}",),
            manifest_path=p,
        )
    if not sess_path.is_file():
        reasons.append(f"session_parquet_missing:{sess_path}")
    if not bet_path.is_file():
        reasons.append(f"bet_parquet_missing:{bet_path}")
    if reasons:
        return BridgeLocalParquetReadiness(
            ready=False,
            reasons=tuple(reasons),
            manifest_path=p,
        )
    if manifest_bet_includes_run_trip_lda_columns(manifest):
        try:
            _validate_bet_parquet_run_trip_lda_schema(bet_path, manifest)
        except ValueError as exc:
            return BridgeLocalParquetReadiness(
                ready=False,
                reasons=(str(exc),),
                manifest_path=p,
            )
    return BridgeLocalParquetReadiness(ready=True, reasons=(), manifest_path=p)


# ---------------------------------------------------------------------------
# ClickHouse path
# ---------------------------------------------------------------------------

def load_clickhouse_data(
    window_start: datetime,
    extended_end: datetime,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Query ClickHouse for bets in [window_start, extended_end] and matching sessions."""
    logger.info("ClickHouse pull: %s -> %s", window_start, extended_end)
    client = get_clickhouse_client()
    params = {"start": window_start, "end": extended_end}

    # Pull extra history so Track Human state machines (loss_streak, run_boundary)
    # have cross-chunk context.  process_chunk filters training rows to
    # [window_start, window_end) after Track-B features are computed.
    # E4/F1: exclude invalid player_id (PLAN Step 1)
    # E5: t_bet may use FINAL for read-after-write consistency (G1: t_session must NOT)
    bets_query = f"""
        SELECT {_BET_SELECT_COLS}
        FROM {SOURCE_DB}.{TBET} FINAL
        WHERE payout_complete_dtm >= %(start)s - INTERVAL {HISTORY_BUFFER_DAYS} DAY
          AND payout_complete_dtm < %(end)s
          AND wager > 0
          AND payout_complete_dtm IS NOT NULL
          AND player_id IS NOT NULL
          AND player_id != {PLACEHOLDER_PLAYER_ID}
    """

    # No FINAL on t_session (G1). FND-01 CTE dedup for train-serve parity with scorer/validator.
    # Pull sessions overlapping the window with a ±1-day buffer.
    # FND-02: is_manual=1 rows are accounting adjustments, not real play (R38 parity fix)
    # FND-04: exclude sessions with no real activity (SSOT §5)
    session_query = f"""
        WITH deduped AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY session_id
                       ORDER BY lud_dtm DESC NULLS LAST, __etl_insert_Dtm DESC
                   ) AS rn
            FROM {SOURCE_DB}.{TSESSION}
            WHERE session_start_dtm >= %(start)s - INTERVAL 1 DAY
              AND session_start_dtm < %(end)s + INTERVAL 1 DAY
              AND is_deleted = 0
              AND is_canceled = 0
              AND is_manual = 0
        )
        SELECT {_SESSION_SELECT_COLS}
        FROM deduped
        WHERE rn = 1
          AND (COALESCE(turnover, 0) > 0 OR COALESCE(num_games_with_wager, 0) > 0)
    """

    bets = client.query_df(bets_query, parameters=params)
    sessions = client.query_df(session_query, parameters=params)
    logger.info("Loaded %d bets, %d sessions", len(bets), len(sessions))
    return bets, sessions


# ---------------------------------------------------------------------------
# Local Parquet path (dev / offline iteration)
# ---------------------------------------------------------------------------

def load_local_parquet(
    window_start: datetime,
    extended_end: datetime,
    sessions_only: bool = False,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Load bets + sessions from local Parquet files, filtered to the window.

    Issue #14 Workstream A: paths come from ``trainer_local_parquet_bridge.manifest.json``
    under ``LOCAL_PARQUET_DIR`` (see ``trainer_local_parquet_bridge_manifest_path``).
    Legacy discovery of
    fixed ``data/gmwds_t_*.parquet`` filenames without that manifest is not supported.

    Applies the same DQ filters (wager > 0, payout_complete_dtm IS NOT NULL)
    and time window restriction as the ClickHouse path.

    Args:
        sessions_only: If True, skip loading the bet parquet entirely and
            return an empty bets DataFrame.  Use this when only sessions are
            needed (e.g. canonical map build) to avoid OOM on the 400M+ row
            bet file.
    """
    # R402: contract check — module-level _CANONICAL_MAP_SESSION_COLS must include
    # "turnover" so FND-04 DQ logic sees consistent columns in sessions_only mode.
    assert "turnover" in _CANONICAL_MAP_SESSION_COLS, (
        "FND-04 contract violated: _CANONICAL_MAP_SESSION_COLS must include 'turnover'"
    )

    _mf = trainer_local_parquet_bridge_manifest_path()
    manifest = load_trainer_local_parquet_bridge_manifest()
    bets_path, sess_path = resolve_local_parquet_bet_session_paths_from_manifest(
        manifest, manifest_path=_mf
    )

    logger.info(
        "Workstream A ingress: manifest=%s artifact_kind=%s bet_includes_run_trip_lda=%s bet=%s session=%s%s",
        _mf,
        manifest.get("artifact_kind"),
        manifest_bet_includes_run_trip_lda_columns(manifest),
        bets_path,
        sess_path,
        " (sessions only)" if sessions_only else "",
    )

    if not sess_path.exists():
        raise FileNotFoundError(
            f"Session Parquet missing (manifest): {sess_path}. "
            "Fix paths in trainer_local_parquet_bridge.manifest.json or re-run the MVP bridge."
        )
    if not sessions_only:
        if not bets_path.exists():
            raise FileNotFoundError(
                f"Bet Parquet missing (manifest): {bets_path}. "
                "Fix paths in trainer_local_parquet_bridge.manifest.json or re-run the MVP bridge."
            )
        _validate_bet_parquet_run_trip_lda_schema(bets_path, manifest)

    logger.info("Reading local Parquet via manifest: %s%s", bets_path.parent, " (sessions only)" if sessions_only else "")

    def _filter_ts(dt, parquet_path: Path, col: str) -> pd.Timestamp:
        """Return a Timestamp compatible with the Parquet column's tz schema.

        Reads the schema of the target file once (cheap: no data rows) to
        determine whether the column is tz-aware or tz-naive, then returns
        either a UTC-aware or tz-naive Timestamp accordingly.

        Background: R28 originally stripped tz for tz-naive columns, but
        ClickHouse exports can produce tz=UTC columns (timestamp[ms, tz=UTC]),
        which requires a tz-aware filter bound.  Mismatched tz triggers
        ArrowNotImplementedError at pushdown time.
        """
        import pyarrow.parquet as pq
        ts = pd.Timestamp(dt)
        try:
            schema = pq.read_schema(parquet_path)
            field = schema.field(col)
            col_tz = getattr(field.type, "tz", None)
        except Exception:
            col_tz = None
        if col_tz:
            return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        else:
            return ts.tz_localize(None) if ts.tzinfo is None else ts.replace(tzinfo=None)

    if sessions_only:
        bets = pd.DataFrame()
        # When building canonical map, read only the minimal set of session
        # columns to avoid OOM on the 74M-row × 80-column session parquet.
        import pyarrow.parquet as _pq
        _sess_schema_cols = set(_pq.read_schema(sess_path).names)
        _sess_cols = [c for c in _CANONICAL_MAP_SESSION_COLS if c in _sess_schema_cols]
        if "__etl_insert_Dtm" in _sess_schema_cols:
            _sess_cols.append("__etl_insert_Dtm")
    else:
        # Use pyarrow pushdown filters to avoid loading the full table per chunk (R26).
        # Column pushdown: only load _REQUIRED_BET_PARQUET_COLS to cut RAM by ~2/3 vs
        # loading all ~60 t_bet columns (OOM fix — 17 object columns were the final straw).
        bets_lo = window_start - timedelta(days=HISTORY_BUFFER_DAYS)
        import pyarrow.parquet as _pq_bets
        _bet_schema_cols = set(_pq_bets.read_schema(bets_path).names)
        _bet_cols = [c for c in _REQUIRED_BET_PARQUET_COLS if c in _bet_schema_cols]
        for _c in _OPTIONAL_BET_LDA_RUN_TRIP_COLS:
            if _c in _bet_schema_cols and _c not in _bet_cols:
                _bet_cols.append(_c)
        bets = pd.read_parquet(
            bets_path,
            columns=_bet_cols,
            filters=[
                ("payout_complete_dtm", ">=", _filter_ts(bets_lo, bets_path, "payout_complete_dtm")),
                ("payout_complete_dtm", "<",  _filter_ts(extended_end, bets_path, "payout_complete_dtm")),
            ],
        )
        # DQ filters are applied fully in apply_dq; quick guards here (E4/F1 parity with ClickHouse).
        # Use one combined mask to avoid double-copy RAM overhead on large Parquet chunks.
        _mask = pd.Series(True, index=bets.index)
        if "wager" in bets.columns:
            _mask &= bets.get("wager", pd.Series(dtype=float)).fillna(0) > 0
        if "player_id" in bets.columns:
            _mask &= bets["player_id"].notna() & (bets["player_id"] != PLACEHOLDER_PLAYER_ID)
        bets = bets[_mask].copy()
        _sess_cols = None  # read all columns for normal chunk processing

    sessions = pd.read_parquet(
        sess_path,
        filters=[
            ("session_start_dtm", ">=", _filter_ts(window_start - timedelta(days=1), sess_path, "session_start_dtm")),
            ("session_start_dtm", "<",  _filter_ts(extended_end + timedelta(days=1), sess_path, "session_start_dtm")),
        ],
        columns=_sess_cols,
    )

    sessions = sessions[
        (sessions.get("is_deleted", pd.Series(0, index=sessions.index)) == 0)
        & (sessions.get("is_canceled", pd.Series(0, index=sessions.index)) == 0)
    ].copy() if len(sessions) > 0 else sessions

    logger.info("Local Parquet: %d bets, %d sessions", len(bets), len(sessions))
    return bets, sessions


# ---------------------------------------------------------------------------
# Local Parquet metadata helpers
# ---------------------------------------------------------------------------

def _parse_obj_to_date(v: Any) -> Optional[date]:
    """Best-effort parse for Parquet stats values (date/datetime/str)."""
    if v is None:
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        if v.tzinfo is not None:
            return v.astimezone(HK_TZ).date()
        return v.date()
    s = str(v).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            return None


def _parquet_date_range(path: Path, candidate_cols: List[str]) -> Optional[Tuple[date, date]]:
    """Read min/max date from Parquet metadata stats without full table scan."""
    if not path.exists():
        return None
    try:
        import pyarrow.parquet as pq

        pf = pq.ParquetFile(path)
        cols = pf.schema_arrow.names
        for col in candidate_cols:
            if col not in cols:
                continue
            col_idx = cols.index(col)
            mins: List[date] = []
            maxs: List[date] = []
            for i in range(pf.metadata.num_row_groups):
                stats = pf.metadata.row_group(i).column(col_idx).statistics
                if stats is None or not getattr(stats, "has_min_max", False):
                    continue
                dmin = _parse_obj_to_date(stats.min)
                dmax = _parse_obj_to_date(stats.max)
                if dmin is not None:
                    mins.append(dmin)
                if dmax is not None:
                    maxs.append(dmax)
            if mins and maxs:
                return min(mins), max(maxs)
    except Exception as exc:
        logger.warning("Failed to read parquet metadata date range (%s): %s", path, exc)
    return None


def _detect_local_data_end() -> Optional[date]:
    """Detect the latest available date from local bet & session Parquet metadata.

    Uses row-group statistics only (no data scan). Returns the conservative
    (min) of the two max dates so both tables have data up to the returned
    date. Returns None if metadata is unavailable for both.
    """
    try:
        _mp = trainer_local_parquet_bridge_manifest_path()
        _m = load_trainer_local_parquet_bridge_manifest()
        bet_path, sess_path = resolve_local_parquet_bet_session_paths_from_manifest(
            _m, manifest_path=_mp
        )
    except (FileNotFoundError, OSError, KeyError, json.JSONDecodeError) as exc:
        logger.warning("Workstream A: could not resolve manifest for _detect_local_data_end: %s", exc)
        return None

    bet_rng = _parquet_date_range(bet_path, ["payout_complete_dtm", "gaming_day"])
    sess_rng = _parquet_date_range(
        sess_path, ["gaming_day", "session_end_dtm", "lud_dtm", "session_start_dtm"]
    )

    maxes: List[date] = []
    if bet_rng is not None:
        maxes.append(bet_rng[1])
    if sess_rng is not None:
        maxes.append(sess_rng[1])

    if not maxes:
        return None
    return min(maxes)


def _parquet_stable_rowgroups_schema_digest(meta: Any) -> str:
    """Stable digest from Parquet metadata only (no mtime, no file ``created_by``).

    Incorporates footer ``num_rows``, per-row-group ``num_rows`` / ``total_byte_size``,
    and column path + physical/logical types so copies across machines with different
    mtime still match; schema changes bust the digest without scanning row data.
    """
    rgs: List[List[int]] = []
    for i in range(meta.num_row_groups):
        rg = meta.row_group(i)
        rgs.append([int(rg.num_rows), int(rg.total_byte_size)])
    schema = meta.schema
    # PyArrow: never use FileMetaData.num_columns here — some builds implement it
    # via ParquetSchema.num_columns, which was removed (AttributeError). len(schema)
    # is the portable column count across tested pyarrow versions.
    _n_cols = len(schema)
    cols: List[Tuple[str, str, str]] = []
    for i in range(int(_n_cols)):
        col = schema.column(i)
        path = col.path
        if hasattr(path, "as_tuple"):
            path_key = ".".join(str(x) for x in path.as_tuple())
        elif isinstance(path, str):
            path_key = path
        else:
            path_key = str(path)
        cols.append((path_key, str(col.physical_type), str(col.logical_type)))
    cols.sort(key=lambda t: t[0])
    blob = {
        "columns": [[a, b, c] for a, b, c in cols],
        "fp_v": 2,
        "nrows": int(meta.num_rows),
        "row_groups": rgs,
    }
    raw = json.dumps(blob, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _local_parquet_source_data_hash(
    window_start: datetime,
    extended_end: datetime,
) -> str:
    """Task 7 R5 (+ portable fp_v2): fingerprint local bet/session Parquet without row scan.

    Uses file size, footer ``num_rows``, a stable metadata digest (row groups + schema;
    excludes ``st_mtime`` and file ``created_by``), and the same logical filter bounds as
    ``load_local_parquet`` so chunk keys update when exports or window bounds change.

    Workstream A: bet/session paths and optional ``input_fingerprint`` come from the
    bridge manifest so re-materialization busts chunk cache keys.

    **Trade-off**: extreme in-place edits keeping identical Parquet metadata could
    theoretically false-hit; prefer false miss for content changes that alter metadata.
    """
    _mf = trainer_local_parquet_bridge_manifest_path()
    manifest = load_trainer_local_parquet_bridge_manifest()
    bets_path, sess_path = resolve_local_parquet_bet_session_paths_from_manifest(
        manifest, manifest_path=_mf
    )
    bets_lo = window_start - timedelta(days=HISTORY_BUFFER_DAYS)
    sess_lo = window_start - timedelta(days=1)
    sess_hi = extended_end + timedelta(days=1)
    import pyarrow.parquet as pq

    def _file_token(label: str, p: Path) -> str:
        if not p.exists():
            return f"{label}:missing:{p.name}"
        st = p.stat()
        try:
            meta = pq.read_metadata(p)
            nrows = int(meta.num_rows)
            digest = _parquet_stable_rowgroups_schema_digest(meta)
        except Exception as _meta_exc:
            nrows = -1
            digest = "0" * 16
            logger.warning(
                "Task 7 R5: read_metadata failed for %s (%s): %s",
                p, label, _meta_exc,
            )
        return f"{label}|{p.name}|{st.st_size}|{nrows}|{digest}"

    _mf_fp = manifest.get("input_fingerprint")
    _mf_fp_s = str(_mf_fp) if _mf_fp is not None else ""

    payload = json.dumps({
        "artifact_kind": str(manifest.get("artifact_kind", "")),
        "bet_filter_lo": bets_lo.isoformat(),
        "bet_filter_hi": extended_end.isoformat(),
        "bet_file": _file_token("bet", bets_path),
        "manifest_input_fingerprint": _mf_fp_s,
        "manifest_path": str(trainer_local_parquet_bridge_manifest_path().resolve()),
        "sess_filter_lo": sess_lo.isoformat(),
        "sess_filter_hi": sess_hi.isoformat(),
        "sess_file": _file_token("sess", sess_path),
    }, sort_keys=True)
    return hashlib.md5(payload.encode()).hexdigest()[:8]
