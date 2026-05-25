"""Offline backtest: replay production scorer v2 feature + predict path on historical bets.

Similar in spirit to legacy ``trainer.backtester`` (date window, offline metrics), but runs the
**same supplier stack** as live ``score_once`` (PIT short-term, trial 1h, Feast mid/slow, composite)
instead of training-time feature SQL.

Example (deploy bundle + ClickHouse gaming-day window)::

    python -m trainer_hightier.serving.offline_serving_backtest \\
        --bundle-dir /path/to/deploy_bundle \\
        --gaming-day-start 2026-05-01 --gaming-day-end 2026-05-07 \\
        --max-bets 2000

When ``--output-json`` is omitted, the report defaults to
``<model-bundle>/offline_serving_backtest.json`` (canonical training bundle when available).

Example (local cleaned bet mirror, no ClickHouse)::

    python -m trainer_hightier.serving.offline_serving_backtest \\
        --model-dir out/models_high_tier_mvp/20260522-124003-245bd1f \\
        --local-cleaned-bet ./source_mirror/cleaned_bet \\
        --gaming-day-start 2026-05-01 --gaming-day-end 2026-05-07
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final, Iterator

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score
from zoneinfo import ZoneInfo

from trainer_hightier.config import (
    TRAINER_HIGHTIER_PACKAGE_DIR,
    HightierServingConfig,
    apply_hightier_serving_environ_overrides,
    set_hightier_serving_deploy_override,
)
from trainer_hightier.core.model_bundle_paths import (
    OFFLINE_SERVING_BACKTEST_REPORT_FILENAME,
    model_bundle_report_path,
    resolve_model_bundle_for_reports,
)
from trainer_hightier.serving.adt_allowlist import load_adt_allowlist_ids
from trainer_hightier.serving.audit_production_readiness import (
    _load_bundle_rel,
    _load_dotenv,
    _serving_config_for_bundle,
)
from trainer_hightier.serving.audit_supplier_root_cause import (
    _build_scoring_pool,
    _load_audit_bets,
)
from trainer_hightier.serving.ch_adapter import get_clickhouse_client
from trainer_hightier.serving.feast_online_adapter import (
    FeastLookupDiagnostics,
    FeastSdkOnlineAdapter,
    OnlineFeastAdapter,
    compute_row_missing_audits,
    default_feast_repo_path,
)
from trainer_hightier.serving.feature_builder import (
    assert_features_ready,
    attach_mid_term_composite_columns,
    attach_mid_term_snapshot_asof,
    attach_synthetic_etl_and_prediction_visible,
    prepare_lgbm_feature_matrix,
    join_slow_patron_snapshot,
)
from trainer_hightier.serving.feature_supply import (
    ScorerSupplierPlan,
    build_scorer_supplier_plan,
    load_frozen_registry_for_bundle,
    scorer_supplier_route_counts,
)
from trainer_hightier.serving.feast_readiness import (
    run_deploy_feast_readiness_check,
)
from trainer_hightier.serving.model_bundle import HightierModelBundle, load_hightier_model_bundle
from trainer_hightier.serving.runtime_config import HK_TZ
from trainer_hightier.serving.scorer import (
    _ScoringBatch,
    _TBET_CASINO_PLAYER_ID_SELECT,
    _attach_feast_mid_slow,
    _build_staged_features,
    _incremental_bet_select_list,
    _postprocess_incremental_bets_timestamps,
    assert_bets_gaming_day_contract,
    split_allowlist_player_id_chunks,
)
from trainer_hightier.serving.snapshot_freshness import post_join_feature_smoke

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OfflineBacktestContext:
    """Resolved paths and serving config for one offline run."""

    bundle_root: Path | None
    model_dir: Path
    mapping_parquet: Path
    feast_repo: Path | None
    slow_patron_parquet: Path | None
    mid_term_snapshot_parquet: Path | None
    use_feast_online: bool
    cfg: HightierServingConfig
    bundle: HightierModelBundle
    supplier_plan: ScorerSupplierPlan
    allowlist_ids: frozenset[int]
    registry_by_id: dict[str, Any]


@dataclass(frozen=True)
class OfflineScoringResult:
    """Frames and metrics from one offline production replay."""

    bets: pd.DataFrame
    staged: pd.DataFrame
    skipped_entity_missing: pd.DataFrame
    probabilities: np.ndarray
    feast_diag: FeastLookupDiagnostics
    smoke_failures: tuple[str, ...]
    readiness_gate: dict[str, Any]


def _parse_gaming_day(value: str) -> date:
    """Parse ``YYYY-MM-DD`` gaming day."""
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"expected gaming day YYYY-MM-DD, got {value!r}") from exc


def _resolve_feast_repo_path(
    *,
    bundle_root: Path | None,
    feast_repo: Path | None,
    cfg: HightierServingConfig,
) -> Path:
    """Pick Feast repo for offline production replay (bundle > explicit > package default)."""
    if feast_repo is not None:
        repo = Path(feast_repo).resolve()
    elif cfg.scorer_feast_repo_path is not None:
        repo = Path(cfg.scorer_feast_repo_path).resolve()
    elif bundle_root is not None:
        cand = bundle_root / "feast_repo"
        repo = cand if cand.is_dir() else default_feast_repo_path()
    else:
        repo = default_feast_repo_path()
    if not repo.is_dir() or not (repo / "feature_store.yaml").is_file():
        raise FileNotFoundError(
            f"feast_repo missing or invalid (need feature_store.yaml): {repo}"
        )
    online_db = repo / "data" / "online_store.db"
    if not online_db.is_file():
        raise FileNotFoundError(
            f"Feast online store missing at {online_db}; run feast_online_refresh / feast apply first"
        )
    return repo


def _resolve_training_mid_snapshot_path(
    *,
    model_dir: Path,
    mid_term_snapshot_parquet: Path | None,
) -> Path | None:
    """Resolve Step 3.5 training mid snapshot for historical parity replay."""
    if mid_term_snapshot_parquet is not None:
        p = Path(mid_term_snapshot_parquet).resolve()
        return p if p.is_file() else None
    candidates = (
        TRAINER_HIGHTIER_PACKAGE_DIR
        / "artifacts"
        / "training_data"
        / "_main_trainer_mid_term_daily_snapshot.parquet",
    )
    for cand in candidates:
        if cand.is_file():
            return cand.resolve()
    return None


def resolve_offline_context(
    *,
    bundle_dir: Path | None,
    model_dir: Path | None,
    mapping_parquet: Path | None,
    allowlist_parquet: Path | None,
    feast_repo: Path | None,
    slow_patron_parquet: Path | None,
    mid_term_snapshot_parquet: Path | None = None,
    use_feast_online: bool = True,
    allow_slow_parquet_fallback: bool = False,
    use_training_mid_snapshot_for_parity: bool = True,
) -> OfflineBacktestContext:
    """Resolve bundle, mapping, allowlist, and Feast repo (deploy bundle or explicit paths)."""
    if bundle_dir is not None:
        bundle_root = Path(bundle_dir).resolve()
        rel = _load_bundle_rel(bundle_root)
        _load_dotenv(bundle_root)
        cfg = apply_hightier_serving_environ_overrides(
            _serving_config_for_bundle(bundle_root, rel),
        )
        set_hightier_serving_deploy_override(cfg)
        model_path = bundle_root / rel.get("model_bundle_dir", "models")
        mapping = bundle_root / rel.get(
            "canonical_mapping_parquet",
            "mapping/canonical_player_mapping.parquet",
        )
        allowlist = cfg.adt_allowed_players_parquet
        slow_p = bundle_root / "deploy_inputs" / "slow_patron_180d_monthly.parquet"
        if not slow_p.is_file():
            slow_p = None
    else:
        if model_dir is None:
            raise ValueError("provide --bundle-dir or --model-dir")
        bundle_root = None
        model_path = Path(model_dir).resolve()
        base = HightierServingConfig()
        cfg = apply_hightier_serving_environ_overrides(
            replace(
                base,
                scorer_feast_repo_path=(
                    Path(feast_repo).resolve()
                    if feast_repo is not None
                    else TRAINER_HIGHTIER_PACKAGE_DIR / "feast_repo"
                ),
            ),
        )
        mapping = Path(mapping_parquet).resolve() if mapping_parquet else None
        allowlist = Path(allowlist_parquet).resolve() if allowlist_parquet else None
        slow_p = Path(slow_patron_parquet).resolve() if slow_patron_parquet else None
        if slow_p is None:
            cand = model_path / "deploy_inputs" / "slow_patron_180d_monthly.parquet"
            slow_p = cand if cand.is_file() else None

    if mapping is None or not Path(mapping).is_file():
        raise FileNotFoundError(f"canonical mapping parquet missing: {mapping}")
    if allowlist is None or not Path(allowlist).is_file():
        raise FileNotFoundError(f"adt allowlist parquet missing: {allowlist}")

    bundle = load_hightier_model_bundle(bundle_dir=model_path)
    snap = load_frozen_registry_for_bundle(Path(bundle.bundle_dir))
    plan = build_scorer_supplier_plan(snap, bundle.feature_columns)
    needs_feast_suppliers = bool(plan.feast_mid_cols or plan.feast_slow_cols)
    feast_path: Path | None = None
    if needs_feast_suppliers and use_feast_online:
        feast_path = _resolve_feast_repo_path(
            bundle_root=bundle_root,
            feast_repo=feast_repo,
            cfg=cfg,
        )
        cfg = replace(cfg, scorer_feast_repo_path=feast_path)
    elif needs_feast_suppliers and allow_slow_parquet_fallback:
        if slow_p is None or not Path(slow_p).is_file():
            raise FileNotFoundError(
                "slow_patron_parquet required when --slow-parquet-fallback and not using Feast online"
            )
    elif needs_feast_suppliers:
        raise ValueError(
            "model requires Feast mid/slow suppliers; enable Feast online (default) or pass "
            "--slow-parquet-fallback with deploy_inputs/slow_patron_180d_monthly.parquet"
        )

    allowlist_ids = frozenset(load_adt_allowlist_ids(Path(allowlist)))
    registry_by_id = {r.feature_id: r for r in snap.rows}
    mid_snap: Path | None = None
    if use_training_mid_snapshot_for_parity and (
        plan.feast_mid_cols or plan.mid_composite_cols
    ):
        mid_snap = _resolve_training_mid_snapshot_path(
            model_dir=Path(model_path),
            mid_term_snapshot_parquet=mid_term_snapshot_parquet,
        )
    return OfflineBacktestContext(
        bundle_root=bundle_root,
        model_dir=Path(model_path),
        mapping_parquet=Path(mapping),
        feast_repo=feast_path,
        slow_patron_parquet=Path(slow_p) if slow_p is not None else None,
        mid_term_snapshot_parquet=mid_snap,
        use_feast_online=bool(use_feast_online),
        cfg=cfg,
        bundle=bundle,
        supplier_plan=plan,
        allowlist_ids=allowlist_ids,
        registry_by_id=registry_by_id,
    )


def fetch_bets_gaming_day_window(
    *,
    cfg: HightierServingConfig,
    allowlist_ids: frozenset[int],
    gaming_day_start: date,
    gaming_day_end: date,
    max_bets: int,
) -> pd.DataFrame:
    """Fetch allowlist bets whose ``gaming_day`` falls in ``[start, end]`` (inclusive)."""
    if gaming_day_end < gaming_day_start:
        raise ValueError(
            f"gaming_day_end {gaming_day_end} before gaming_day_start {gaming_day_start}"
        )
    if not allowlist_ids:
        return pd.DataFrame()

    client = get_clickhouse_client()
    placeholder = int(cfg.placeholder_player_id)
    lim = max(1, int(max_bets))
    select_cols = _incremental_bet_select_list(casino_player_id_select=_TBET_CASINO_PLAYER_ID_SELECT)
    params = {
        "gday_start": gaming_day_start.isoformat(),
        "gday_end": gaming_day_end.isoformat(),
        "lim": lim,
    }
    chunk_sz = int(cfg.hightier_scorer_player_id_chunk_size)
    chunks = split_allowlist_player_id_chunks(allowlist_ids, chunk_sz)
    frames: list[pd.DataFrame] = []
    for chunk in chunks:
        in_list = ",".join(str(int(x)) for x in chunk)
        q = f"""
            SELECT
                {select_cols}
            FROM {cfg.source_db}.{cfg.tbet} FINAL
            WHERE toDate(gaming_day) >= toDate(%(gday_start)s)
              AND toDate(gaming_day) <= toDate(%(gday_end)s)
              AND payout_complete_dtm IS NOT NULL
              AND gaming_day IS NOT NULL
              AND wager > 0
              AND player_id IS NOT NULL
              AND player_id != {placeholder}
              AND player_id IN ({in_list})
            ORDER BY payout_complete_dtm ASC, bet_id ASC
            LIMIT %(lim)s
        """
        frames.append(client.query_df(q, parameters=params))
    bets = pd.concat([f for f in frames if not f.empty], ignore_index=True) if frames else pd.DataFrame()
    if bets.empty:
        return bets
    bets = bets.drop_duplicates(subset=["bet_id"], keep="first").head(lim).reset_index(drop=True)
    _postprocess_incremental_bets_timestamps(bets)
    assert_bets_gaming_day_contract(bets, "offline_serving_backtest_gaming_day")
    return bets


def load_bets_from_cleaned_parquet(
    cleaned_root: Path,
    *,
    allowlist_ids: frozenset[int],
    gaming_day_start: date,
    gaming_day_end: date,
    max_bets: int,
) -> pd.DataFrame:
    """Load settled bets from hive-partitioned cleaned bet parquet (no ClickHouse)."""
    import duckdb

    root = Path(cleaned_root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"local cleaned bet root missing: {root}")
    glob_path = str((root / "**" / "*.parquet").as_posix())
    lim = max(1, int(max_bets))
    allow = sorted(int(x) for x in allowlist_ids)
    if not allow:
        return pd.DataFrame()
    conn = duckdb.connect()
    try:
        conn.execute(
            "CREATE TEMP TABLE allowlist AS SELECT * FROM (SELECT UNNEST(?) AS player_id)",
            [allow],
        )
        q = f"""
            SELECT
                b.bet_id,
                b.is_back_bet,
                b.bet_type,
                b.type_of_bet,
                b.payout_complete_dtm,
                CAST(b.gaming_day AS TIMESTAMP) AS gaming_day,
                b.session_id,
                b.player_id,
                b.table_id,
                b.wager,
                b.casino_win,
                b.payout_odds
            FROM read_parquet('{glob_path}', hive_partitioning=true) AS b
            INNER JOIN allowlist AS a ON b.player_id = a.player_id
            WHERE CAST(b.gaming_day AS DATE) >= CAST(? AS DATE)
              AND CAST(b.gaming_day AS DATE) <= CAST(? AS DATE)
              AND b.payout_complete_dtm IS NOT NULL
              AND b.wager > 0
            ORDER BY b.payout_complete_dtm ASC, b.bet_id ASC
            LIMIT ?
        """
        bets = conn.execute(
            q,
            [gaming_day_start.isoformat(), gaming_day_end.isoformat(), lim],
        ).fetchdf()
    finally:
        conn.close()
    if bets.empty:
        return bets
    bets["__etl_insert_Dtm"] = bets["payout_complete_dtm"]
    if "casino_player_id" not in bets.columns:
        bets["casino_player_id"] = None
    if "position_idx" not in bets.columns:
        bets["position_idx"] = None
    if "status" not in bets.columns:
        bets["status"] = None
    _postprocess_incremental_bets_timestamps(bets)
    return bets


def load_offline_bets(
    ctx: OfflineBacktestContext,
    *,
    gaming_day_start: date | None,
    gaming_day_end: date | None,
    local_cleaned_bet: Path | None,
    prediction_log: Path | None,
    lookback_hours: float,
    max_bets: int | None,
) -> pd.DataFrame:
    """Load bet rows for offline replay (CH window, local parquet, or audit log path)."""
    cap = int(max_bets) if max_bets is not None else 5000
    if local_cleaned_bet is not None:
        if gaming_day_start is None or gaming_day_end is None:
            raise ValueError("--local-cleaned-bet requires --gaming-day-start and --gaming-day-end")
        return load_bets_from_cleaned_parquet(
            local_cleaned_bet,
            allowlist_ids=ctx.allowlist_ids,
            gaming_day_start=gaming_day_start,
            gaming_day_end=gaming_day_end,
            max_bets=cap,
        )
    if gaming_day_start is not None and gaming_day_end is not None:
        return fetch_bets_gaming_day_window(
            cfg=ctx.cfg,
            allowlist_ids=ctx.allowlist_ids,
            gaming_day_start=gaming_day_start,
            gaming_day_end=gaming_day_end,
            max_bets=cap,
        )
    return _load_audit_bets(
        cfg=ctx.cfg,
        allowlist_ids=ctx.allowlist_ids,
        prediction_log=prediction_log,
        max_bets=max_bets,
        lookback_hours=lookback_hours,
    )


def build_offline_scoring_batch(
    bets: pd.DataFrame,
    *,
    cfg: HightierServingConfig,
) -> _ScoringBatch:
    """Build hot pool + batch matching live ``_fetch_scoring_batch`` output shape."""
    if bets.empty:
        raise ValueError("offline backtest: bet frame is empty")
    pool = _build_scoring_pool(bets, cfg=cfg)
    cursor = pd.to_datetime(bets["__etl_insert_Dtm"], errors="coerce")
    return _ScoringBatch(bets=bets.reset_index(drop=True), cursor=cursor, pool=pool)


_TEST_BET_COLUMNS: tuple[str, ...] = (
    "bet_id",
    "is_back_bet",
    "bet_type",
    "type_of_bet",
    "payout_complete_dtm",
    "gaming_day",
    "session_id",
    "player_id",
    "table_id",
    "position_idx",
    "wager",
    "casino_win",
    "payout_odds",
    "status",
)


def _metrics_at_threshold(
    y_true: np.ndarray,
    scores: np.ndarray,
    threshold: float,
) -> tuple[float, float, float, int]:
    """Return precision, recall, f1, alert_count for ``scores >= threshold``."""
    y = np.asarray(y_true, dtype=np.int8).reshape(-1)
    s = np.asarray(scores, dtype=np.float64).reshape(-1)
    if not math.isfinite(float(threshold)):
        return 0.0, 0.0, 0.0, 0
    pred = (s >= float(threshold)).astype(np.int8)
    tp = int(np.sum((pred == 1) & (y == 1)))
    fp = int(np.sum((pred == 1) & (y == 0)))
    fn = int(np.sum((pred == 0) & (y == 1)))
    prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    rec = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0.0
    return float(prec), float(rec), float(f1), int(np.sum(pred == 1))


def _split_metrics_block(
    split: str,
    y_true: np.ndarray,
    scores: np.ndarray,
    threshold: float,
    *,
    window_hours: float | None,
) -> dict[str, Any]:
    """Build flat metrics keys aligned with ``05_lgbm_train`` naming."""
    y = np.asarray(y_true, dtype=np.int8).reshape(-1)
    s = np.asarray(scores, dtype=np.float64).reshape(-1)
    n_pos = int(np.sum(y == 1))
    n_neg = int(np.sum(y == 0))
    has_both = n_pos >= 1 and n_neg >= 1 and np.isfinite(s).all()
    ap = float(average_precision_score(y, s)) if has_both else 0.0
    prec, rec, f1, alerts = _metrics_at_threshold(y, s, threshold)
    out: dict[str, Any] = {
        f"{split}_ap": ap,
        f"{split}_precision": prec,
        f"{split}_recall": rec,
        f"{split}_f1": f1,
        f"{split}_samples": int(len(y)),
        f"{split}_positives": n_pos,
        f"{split}_alerts": alerts,
        f"{split}_window_hours": float(window_hours) if window_hours is not None else None,
        f"{split}_alerts_per_hour": None,
        f"{split}_true_labels_per_hour": None,
    }
    if window_hours is not None and math.isfinite(float(window_hours)) and float(window_hours) > 0:
        wh = float(window_hours)
        out[f"{split}_alerts_per_hour"] = float(alerts) / wh
        out[f"{split}_true_labels_per_hour"] = float(n_pos) / wh
    return out


def _window_hours_from_payout(bets: pd.DataFrame) -> float:
    """Hours between min/max ``payout_complete_dtm`` (trainer density parity)."""
    ts = pd.to_datetime(bets["payout_complete_dtm"], errors="coerce").dropna()
    if ts.empty:
        return 0.0
    delta = (ts.max() - ts.min()).total_seconds() / 3600.0
    return float(max(delta, 1e-6))


def _iter_test_batches(
    test_df: pd.DataFrame,
    *,
    batch_size: int,
    max_rows: int | None,
) -> Iterator[pd.DataFrame]:
    """Yield test-split batches in stable ``bet_id`` order."""
    work = test_df.sort_values("bet_id").reset_index(drop=True)
    if max_rows is not None:
        work = work.head(int(max_rows))
    bs = max(1, int(batch_size))
    for start in range(0, len(work), bs):
        yield work.iloc[start : start + bs].copy()


def _bets_frame_from_test_batch(batch: pd.DataFrame) -> pd.DataFrame:
    """Subset of test parquet columns required by scorer feature build."""
    miss = [c for c in _TEST_BET_COLUMNS if c not in batch.columns]
    if miss:
        raise ValueError(f"test split missing bet columns {miss}")
    bets = batch[list(_TEST_BET_COLUMNS)].copy()
    bets["__etl_insert_Dtm"] = pd.to_datetime(bets["payout_complete_dtm"], errors="coerce")
    if "casino_player_id" not in bets.columns:
        bets["casino_player_id"] = None
    return bets


def resolve_hot_pool_player_ids(
    bets: pd.DataFrame,
    mapping_parquet: Path,
) -> list[int]:
    """Expand batch ``player_id`` values to all canonical alias ids (training ``pid`` CTE).

    Mirrors ``materialize_fe_derived_parquet``:

    - ``pid_from_train``: distinct ``player_id`` on the scoring batch
    - ``cid_from_train``: canonical ids for those players
    - ``pid``: batch player ids UNION all alias player ids per canonical
    """
    base = sorted(
        {
            int(x)
            for x in pd.to_numeric(bets["player_id"], errors="coerce").dropna().astype(int).tolist()
        },
    )
    if not base:
        return []
    cmap = pd.read_parquet(
        Path(mapping_parquet).resolve(),
        columns=["player_id", "canonical_id"],
    )
    cmap["player_id"] = pd.to_numeric(cmap["player_id"], errors="coerce")
    cmap["canonical_id"] = cmap["canonical_id"].astype(str).str.strip()
    cmap = cmap.dropna(subset=["player_id"])
    cmap = cmap.loc[cmap["canonical_id"] != ""]
    if cmap.empty:
        return base
    pid_to_cid = cmap.drop_duplicates("player_id").set_index("player_id")["canonical_id"]
    canonical_ids: set[str] = set()
    for pid in base:
        cid = pid_to_cid.get(pid)
        if cid:
            canonical_ids.add(str(cid))
    alias_pids = set(base)
    if canonical_ids:
        alias = cmap.loc[cmap["canonical_id"].isin(canonical_ids), "player_id"].astype(int)
        alias_pids.update(alias.tolist())
    return sorted(alias_pids)


def build_pool_from_cleaned_parquet(
    bets: pd.DataFrame,
    *,
    cleaned_root: Path,
    cfg: HightierServingConfig,
    mapping_parquet: Path,
) -> pd.DataFrame:
    """Bounded hot pool from local cleaned bet hive (no ClickHouse)."""
    import duckdb

    from trainer_hightier.serving.scorer import compute_hot_pool_window_start

    if bets.empty:
        return bets
    root = Path(cleaned_root).resolve()
    glob_path = str((root / "**" / "*.parquet").as_posix())
    pool_start = compute_hot_pool_window_start(bets, cfg=cfg)
    pool_end = pd.to_datetime(bets["payout_complete_dtm"], errors="coerce").max().to_pydatetime()
    pids = resolve_hot_pool_player_ids(bets, mapping_parquet)
    fan_cap = int(cfg.hightier_scorer_pool_player_fanout_cap)
    if len(pids) > fan_cap:
        logger.warning("[offline_backtest] pool fanout %d -> %d", len(pids), fan_cap)
        pids = pids[:fan_cap]
    conn = duckdb.connect()
    try:
        conn.execute(
            "CREATE TEMP TABLE allow_pids AS SELECT * FROM (SELECT UNNEST(?) AS player_id)",
            [pids],
        )
        q = f"""
            SELECT
                b.bet_id,
                b.is_back_bet,
                b.bet_type,
                b.type_of_bet,
                b.payout_complete_dtm,
                CAST(b.gaming_day AS TIMESTAMP) AS gaming_day,
                b.session_id,
                b.player_id,
                b.table_id,
                b.wager,
                b.casino_win,
                b.payout_odds
            FROM read_parquet('{glob_path}', hive_partitioning=true) AS b
            INNER JOIN allow_pids AS p ON b.player_id = p.player_id
            WHERE b.payout_complete_dtm >= ?
              AND b.payout_complete_dtm <= ?
        """
        pool = conn.execute(q, [pool_start, pool_end]).fetchdf()
    finally:
        conn.close()
    if pool.empty:
        raise ValueError(
            "[offline_backtest] cleaned bet pool empty; check --local-cleaned-bet path and dates"
        )
    pool["__etl_insert_Dtm"] = pool["payout_complete_dtm"]
    _postprocess_incremental_bets_timestamps(pool)
    return attach_synthetic_etl_and_prediction_visible(pool)


def evaluate_readiness_gate(ctx: OfflineBacktestContext) -> dict[str, Any]:
    """Run Feast readiness gate (same helper as deploy startup)."""
    plan = ctx.supplier_plan
    gate = run_deploy_feast_readiness_check(
        require_mid=bool(plan.feast_mid_cols or plan.mid_composite_cols),
        require_slow=bool(plan.feast_slow_cols),
        allowlist_parquet=ctx.cfg.adt_allowed_players_parquet,
        canonical_mapping_parquet=ctx.mapping_parquet,
        mid_columns=plan.feast_mid_cols,
        slow_columns=plan.feast_slow_cols,
        run_lookup_smoke=True,
    )
    return gate.to_log_dict()


_SLOW_FEAST_ONLINE_TABLE: Final[str] = "trainer_hightier_walkaway_long_term_slow_spike_features"


def feast_online_slow_table_present(feast_repo: Path) -> bool:
    """True when slow feature view table exists in bundle-local Feast SQLite online store."""
    import sqlite3

    db = Path(feast_repo).resolve() / "data" / "online_store.db"
    if not db.is_file():
        return False
    con = sqlite3.connect(str(db))
    try:
        row = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (_SLOW_FEAST_ONLINE_TABLE,),
        ).fetchone()
        return row is not None
    finally:
        con.close()


def ensure_slow_feast_online_materialized(
    feast_repo: Path,
    *,
    adt_allowlist: Path,
    canonical_mapping: Path,
    local_cleaned_session: Path | None = None,
    training_slow_parquet: Path | None = None,
    force_from_training_parquet: bool = False,
) -> dict[str, Any]:
    """Populate slow Feast online from training parquet or full ``feast_online_refresh``."""
    if force_from_training_parquet and training_slow_parquet is not None:
        from trainer_hightier.serving.feast_online_refresh import sync_training_slow_parquet_to_feast_online

        return {
            "skipped": False,
            **sync_training_slow_parquet_to_feast_online(
                feast_repo,
                slow_parquet=training_slow_parquet,
            ),
        }
    if feast_online_slow_table_present(feast_repo):
        return {"skipped": True, "reason": "slow_online_table_already_present"}
    from trainer_hightier.serving.feast_online_refresh import RefreshOptions, run_feast_online_refresh
    from trainer_hightier.utils.session_l0_preprocess import default_cleaned_session_parquet_path

    sess = Path(
        local_cleaned_session or default_cleaned_session_parquet_path(),
    ).resolve()
    if not sess.is_file():
        raise FileNotFoundError(f"cleaned session parquet required for Feast slow materialize: {sess}")
    logger.info(
        "[offline_backtest] materializing slow Feast online into %s (session=%s)",
        feast_repo,
        sess,
    )
    summary = run_feast_online_refresh(
        RefreshOptions(
            layers=("slow",),
            source="local_cleaned",
            skip_apply=False,
            apply_schema=True,
            bootstrap_mid=False,
            skip_materialize=False,
            smoke_only=False,
            dry_run=False,
            feast_repo=Path(feast_repo).resolve(),
            readiness_path=Path(feast_repo).parent / "artifacts" / "feast" / "feast_online_readiness.json",
            canonical_mapping=Path(canonical_mapping).resolve(),
            adt_allowlist=Path(adt_allowlist).resolve(),
            local_cleaned_bet=None,
            local_cleaned_session=sess,
            max_smoke_entities=50,
            summary_path=Path(feast_repo).parent / "artifacts" / "feast" / "offline_backtest_refresh_summary.json",
        ),
    )
    if summary.get("verdict") != "ok":
        raise RuntimeError(f"Feast slow online materialize failed: {summary}")
    return {"skipped": False, "refresh_summary": summary}


def _build_feast_online_adapter(ctx: OfflineBacktestContext) -> FeastSdkOnlineAdapter:
    """Construct production Feast SDK adapter (same as live ``score_once``)."""
    if ctx.feast_repo is None:
        raise ValueError("feast_repo is required for Feast online production replay")
    return FeastSdkOnlineAdapter(feast_repo=ctx.feast_repo)


def run_offline_production_pipeline(
    batch: _ScoringBatch,
    ctx: OfflineBacktestContext,
    feast_adapter: OnlineFeastAdapter | None,
    *,
    strict_smoke: bool,
    allow_slow_parquet_fallback: bool = False,
) -> OfflineScoringResult:
    """Replay production feature suppliers and model predict on *batch*."""
    plan = ctx.supplier_plan
    staged = _build_staged_features(
        batch,
        mapping_parquet=ctx.mapping_parquet,
        supplier_plan=plan,
    )
    fail_frac = float(ctx.cfg.scorer_feast_entity_missing_fail_fraction)
    needs_feast = bool(plan.feast_mid_cols or plan.feast_slow_cols)
    mid_from_snapshot = (
        ctx.mid_term_snapshot_parquet is not None
        and bool(plan.feast_mid_cols)
    )
    if mid_from_snapshot:
        staged = attach_mid_term_snapshot_asof(
            staged,
            mid_term_snapshot_parquet=ctx.mid_term_snapshot_parquet,
            mid_term_columns=plan.feast_mid_cols,
        )
    if needs_feast and ctx.use_feast_online:
        adapter = feast_adapter or _build_feast_online_adapter(ctx)
        if mid_from_snapshot:
            slow_only = plan.feast_slow_cols
            if slow_only:
                staged, skipped, feast_diag = _attach_feast_mid_slow(
                    staged,
                    adapter,
                    mid_columns=(),
                    slow_columns=slow_only,
                    fail_fraction=fail_frac,
                )
            else:
                skipped = staged.iloc[0:0].copy()
                feast_diag = FeastLookupDiagnostics(
                    lookup_latency_ms=0.0,
                    n_requested=len(staged),
                    n_mid_present=len(staged),
                    n_slow_present=0,
                    n_entity_missing=0,
                )
        else:
            staged, skipped, feast_diag = _attach_feast_mid_slow(
                staged,
                adapter,
                mid_columns=plan.feast_mid_cols,
                slow_columns=plan.feast_slow_cols,
                fail_fraction=fail_frac,
            )
    elif needs_feast and allow_slow_parquet_fallback:
        slow_path = Path(ctx.slow_patron_parquet or "").resolve()
        if not slow_path.is_file():
            raise FileNotFoundError(f"slow parquet fallback missing: {slow_path}")
        staged = join_slow_patron_snapshot(staged, slow_path)
        skipped = staged.iloc[0:0].copy()
        feast_diag = FeastLookupDiagnostics(
            lookup_latency_ms=0.0,
            n_requested=len(staged),
            n_mid_present=0,
            n_slow_present=len(staged),
            n_entity_missing=0,
        )
    elif needs_feast:
        raise ValueError("Feast online required for mid/slow suppliers (use --slow-parquet-fallback to opt out)")
    else:
        skipped = staged.iloc[0:0].copy()
        feast_diag = FeastLookupDiagnostics(
            lookup_latency_ms=0.0,
            n_requested=len(staged),
            n_mid_present=0,
            n_slow_present=0,
            n_entity_missing=0,
        )
    from trainer_hightier.serving.mid_term_bounded_asof import apply_mid_term_bounded_asof

    if not mid_from_snapshot:
        staged = apply_mid_term_bounded_asof(
            staged,
            mid_primitive_columns=plan.feast_mid_cols,
            n_days=int(ctx.cfg.production_mid_asof_backfill_days),
        )
    staged = attach_mid_term_composite_columns(staged, plan.mid_composite_cols)
    mid_cols = tuple(
        dict.fromkeys([*plan.feast_mid_cols, *plan.mid_composite_cols]),
    )
    smoke = tuple(post_join_feature_smoke(staged, mid_term_columns=mid_cols))
    if smoke and strict_smoke:
        raise ValueError("post_join_feature_smoke failed: " + "; ".join(smoke))
    assert_features_ready(staged, ctx.bundle.feature_columns)
    x_frame = prepare_lgbm_feature_matrix(
        staged,
        feature_columns=ctx.bundle.feature_columns,
        categorical_columns=ctx.bundle.categorical_columns,
        category_categories=dict(ctx.bundle.category_categories),
    )
    prob = ctx.bundle.model.predict_proba(x_frame)[:, 1]
    readiness: dict[str, Any] = {}
    if ctx.feast_repo is not None:
        readiness = evaluate_readiness_gate(ctx)
    return OfflineScoringResult(
        bets=batch.bets,
        staged=staged,
        skipped_entity_missing=skipped,
        probabilities=prob,
        feast_diag=feast_diag,
        smoke_failures=smoke,
        readiness_gate=readiness,
    )


def summarize_offline_result(
    result: OfflineScoringResult,
    ctx: OfflineBacktestContext,
) -> dict[str, Any]:
    """Build JSON-serializable summary (feature null rates, alerts, Feast diagnostics)."""
    feat_cols = list(ctx.bundle.feature_columns)
    x = result.staged.reindex(columns=feat_cols)
    null_fracs = {c: float(x[c].isna().mean()) for c in feat_cols}
    thr = float(ctx.bundle.threshold)
    alerts = int((result.probabilities >= thr).sum())
    miss_counts = compute_row_missing_audits(
        x,
        feat_cols,
        feast_mid_cols=ctx.supplier_plan.feast_mid_cols,
        feast_slow_cols=ctx.supplier_plan.feast_slow_cols,
        short_term_cols=ctx.supplier_plan.short_term_cols,
    )
    n_high_miss = sum(1 for a in miss_counts if a.model_features_missing >= 3)
    return {
        "generated_at": datetime.now(ZoneInfo(HK_TZ)).isoformat(),
        "model_version": ctx.bundle.model_version,
        "model_dir": str(ctx.model_dir),
        "n_bets": int(len(result.bets)),
        "n_scored": int(len(result.staged)),
        "n_skipped_entity_missing": int(len(result.skipped_entity_missing)),
        "n_alerts": alerts,
        "threshold": thr,
        "supplier_routes": scorer_supplier_route_counts(ctx.supplier_plan),
        "feature_null_fraction": null_fracs,
        "n_rows_high_feature_missing": n_high_miss,
        "feast_diagnostics": {
            "lookup_latency_ms": result.feast_diag.lookup_latency_ms,
            "n_requested": result.feast_diag.n_requested,
            "n_entity_missing": result.feast_diag.n_entity_missing,
            "cell_null_counts": dict(result.feast_diag.cell_null_counts or {}),
        },
        "post_join_smoke_failures": list(result.smoke_failures),
        "readiness_gate": result.readiness_gate,
        "config": {
            "hot_feature_pool_lookback_hours": int(ctx.cfg.hot_feature_pool_lookback_hours),
        },
    }


def evaluate_training_features_baseline(
    ctx: OfflineBacktestContext,
    test_parquet: Path,
) -> dict[str, Any]:
    """Score official test split using **training parquet features** (should match Step 5)."""
    df = pd.read_parquet(test_parquet)
    y = df["walkaway_label"].astype(np.int8).to_numpy()
    cols = list(ctx.bundle.feature_columns)
    x = prepare_lgbm_feature_matrix(
        df,
        feature_columns=tuple(cols),
        categorical_columns=ctx.bundle.categorical_columns,
        category_categories=dict(ctx.bundle.category_categories),
    )
    scores = ctx.bundle.model.predict_proba(x)[:, 1]
    thr = float(ctx.bundle.threshold)
    wh = _window_hours_from_payout(df)
    metrics = _split_metrics_block("offline_training_features", y, scores, thr, window_hours=wh)
    ref = json.loads((ctx.model_dir / "training_metrics.json").read_text(encoding="utf-8"))
    deltas = {
        "ap_delta": metrics["offline_training_features_ap"] - float(ref.get("test_ap", 0.0)),
        "precision_delta": metrics["offline_training_features_precision"]
        - float(ref.get("test_precision", 0.0)),
        "recall_delta": metrics["offline_training_features_recall"]
        - float(ref.get("test_recall", 0.0)),
        "alerts_delta": metrics["offline_training_features_alerts"] - int(ref.get("test_alerts", 0)),
    }
    return {
        "mode": "training_parquet_features",
        "test_parquet": str(Path(test_parquet).resolve()),
        "reference_training_metrics": {
            "test_ap": ref.get("test_ap"),
            "test_precision": ref.get("test_precision"),
            "test_recall": ref.get("test_recall"),
            "test_alerts": ref.get("test_alerts"),
            "test_samples": ref.get("test_samples"),
        },
        "metrics": metrics,
        "deltas_vs_training_metrics": deltas,
    }


def evaluate_production_pipeline_on_test_split(
    ctx: OfflineBacktestContext,
    test_parquet: Path,
    *,
    cleaned_bet_root: Path,
    batch_size: int = 5000,
    max_rows: int | None = None,
    strict_smoke: bool = False,
    allow_slow_parquet_fallback: bool = False,
) -> dict[str, Any]:
    """Replay production suppliers on official test rows (batched; local cleaned bet pool)."""
    test_df = pd.read_parquet(test_parquet)
    feast_adapter: OnlineFeastAdapter | None = None
    feast_materialize: dict[str, Any] | None = None
    if ctx.use_feast_online and (
        ctx.supplier_plan.feast_mid_cols or ctx.supplier_plan.feast_slow_cols
    ):
        if ctx.supplier_plan.feast_slow_cols and ctx.feast_repo is not None:
            feast_materialize = ensure_slow_feast_online_materialized(
                ctx.feast_repo,
                adt_allowlist=ctx.cfg.adt_allowed_players_parquet or ctx.mapping_parquet,
                canonical_mapping=ctx.mapping_parquet,
            )
        feast_adapter = _build_feast_online_adapter(ctx)

    prob_parts: list[np.ndarray] = []
    label_parts: list[np.ndarray] = []
    n_skipped = 0
    feast_entity_missing = 0
    feast_cell_null: dict[str, int] = {}
    batches_run = 0
    for batch_df in _iter_test_batches(test_df, batch_size=batch_size, max_rows=max_rows):
        bets = _bets_frame_from_test_batch(batch_df)
        pool = build_pool_from_cleaned_parquet(
            bets,
            cleaned_root=cleaned_bet_root,
            cfg=ctx.cfg,
            mapping_parquet=ctx.mapping_parquet,
        )
        scoring_batch = _ScoringBatch(
            bets=bets.reset_index(drop=True),
            cursor=pd.to_datetime(bets["__etl_insert_Dtm"], errors="coerce"),
            pool=pool,
        )
        result = run_offline_production_pipeline(
            scoring_batch,
            ctx,
            feast_adapter,
            strict_smoke=strict_smoke,
            allow_slow_parquet_fallback=allow_slow_parquet_fallback,
        )
        prob_parts.append(result.probabilities)
        label_parts.append(batch_df["walkaway_label"].astype(np.int8).to_numpy())
        n_skipped += len(result.skipped_entity_missing)
        feast_entity_missing += int(result.feast_diag.n_entity_missing)
        for col, n in (result.feast_diag.cell_null_counts or {}).items():
            feast_cell_null[col] = feast_cell_null.get(col, 0) + int(n)
        batches_run += 1
        logger.info(
            "[offline_backtest] test batch %d rows=%d scored=%d",
            batches_run,
            len(bets),
            len(result.staged),
        )

    scores = np.concatenate(prob_parts) if prob_parts else np.array([], dtype=np.float64)
    labels = np.concatenate(label_parts) if label_parts else np.array([], dtype=np.int8)
    thr = float(ctx.bundle.threshold)
    wh = _window_hours_from_payout(test_df.head(len(scores)))
    metrics = _split_metrics_block("offline_production_pipeline", labels, scores, thr, window_hours=wh)
    ref = json.loads((ctx.model_dir / "training_metrics.json").read_text(encoding="utf-8"))
    deltas = {
        "ap_delta": metrics["offline_production_pipeline_ap"] - float(ref.get("test_ap", 0.0)),
        "precision_delta": metrics["offline_production_pipeline_precision"]
        - float(ref.get("test_precision", 0.0)),
        "recall_delta": metrics["offline_production_pipeline_recall"]
        - float(ref.get("test_recall", 0.0)),
        "alerts_delta": metrics["offline_production_pipeline_alerts"] - int(ref.get("test_alerts", 0)),
    }
    return {
        "mode": "production_pipeline",
        "feature_supplier": (
            "feast_online" if ctx.use_feast_online else "slow_parquet_fallback"
        ),
        "feast_repo": str(ctx.feast_repo) if ctx.feast_repo else None,
        "feast_online_materialize": feast_materialize,
        "test_parquet": str(Path(test_parquet).resolve()),
        "cleaned_bet_root": str(Path(cleaned_bet_root).resolve()),
        "batch_size": int(batch_size),
        "batches_run": batches_run,
        "n_skipped_entity_missing": n_skipped,
        "feast_entity_missing_total": feast_entity_missing,
        "feast_cell_null_counts": feast_cell_null,
        "slow_parquet": str(ctx.slow_patron_parquet) if ctx.slow_patron_parquet else None,
        "reference_training_metrics": {
            "test_ap": ref.get("test_ap"),
            "test_precision": ref.get("test_precision"),
            "test_recall": ref.get("test_recall"),
            "test_alerts": ref.get("test_alerts"),
            "test_samples": ref.get("test_samples"),
        },
        "metrics": metrics,
        "deltas_vs_training_metrics": deltas,
    }


def run_test_split_comparison(
    *,
    model_dir: Path,
    test_parquet: Path,
    cleaned_bet_root: Path,
    mapping_parquet: Path | None = None,
    allowlist_parquet: Path | None = None,
    feast_repo: Path | None = None,
    batch_size: int = 5000,
    max_rows: int | None = None,
    use_feast_online: bool = True,
    allow_slow_parquet_fallback: bool = False,
) -> dict[str, Any]:
    """Compare training-feature baseline vs production pipeline on the same test split."""
    mroot = Path(model_dir).resolve()
    map_p = mapping_parquet or (mroot / "deploy_inputs" / "canonical_player_mapping.parquet")
    allow_p = allowlist_parquet or (mroot / "deploy_inputs" / "adt_allowed_players_q0p99.parquet")
    ctx = resolve_offline_context(
        bundle_dir=None,
        model_dir=mroot,
        mapping_parquet=map_p,
        allowlist_parquet=allow_p,
        feast_repo=feast_repo,
        slow_patron_parquet=None,
        use_feast_online=use_feast_online,
        allow_slow_parquet_fallback=allow_slow_parquet_fallback,
    )
    baseline = evaluate_training_features_baseline(ctx, test_parquet)
    production = evaluate_production_pipeline_on_test_split(
        ctx,
        test_parquet,
        cleaned_bet_root=cleaned_bet_root,
        batch_size=batch_size,
        max_rows=max_rows,
        allow_slow_parquet_fallback=allow_slow_parquet_fallback,
    )
    gdays = pd.read_parquet(test_parquet, columns=["gaming_day"])["gaming_day"]
    gmin = pd.Timestamp(gdays.min()).date()
    gmax = pd.Timestamp(gdays.max()).date()
    return {
        "model_dir": str(ctx.model_dir),
        "model_version": ctx.bundle.model_version,
        "test_period": {
            "min_gaming_day": str(gmin),
            "max_gaming_day": str(gmax),
        },
        "training_feature_baseline": baseline,
        "production_pipeline": production,
    }


def run_offline_serving_backtest(
    *,
    bundle_dir: Path | None = None,
    model_dir: Path | None = None,
    mapping_parquet: Path | None = None,
    allowlist_parquet: Path | None = None,
    feast_repo: Path | None = None,
    gaming_day_start: date | None = None,
    gaming_day_end: date | None = None,
    local_cleaned_bet: Path | None = None,
    prediction_log: Path | None = None,
    lookback_hours: float = 6.0,
    max_bets: int | None = None,
    strict_smoke: bool = False,
) -> dict[str, Any]:
    """End-to-end offline production replay; returns summary dict."""
    ctx = resolve_offline_context(
        bundle_dir=bundle_dir,
        model_dir=model_dir,
        mapping_parquet=mapping_parquet,
        allowlist_parquet=allowlist_parquet,
        feast_repo=feast_repo,
        slow_patron_parquet=None,
        use_feast_online=True,
        allow_slow_parquet_fallback=False,
    )
    bets = load_offline_bets(
        ctx,
        gaming_day_start=gaming_day_start,
        gaming_day_end=gaming_day_end,
        local_cleaned_bet=local_cleaned_bet,
        prediction_log=prediction_log,
        lookback_hours=lookback_hours,
        max_bets=max_bets,
    )
    if local_cleaned_bet is not None:
        pool = build_pool_from_cleaned_parquet(
            bets,
            cleaned_root=Path(local_cleaned_bet).resolve(),
            cfg=ctx.cfg,
            mapping_parquet=ctx.mapping_parquet,
        )
        batch = _ScoringBatch(
            bets=bets.reset_index(drop=True),
            cursor=pd.to_datetime(bets["__etl_insert_Dtm"], errors="coerce"),
            pool=pool,
        )
    else:
        batch = build_offline_scoring_batch(bets, cfg=ctx.cfg)
    needs_feast = bool(
        ctx.supplier_plan.feast_mid_cols or ctx.supplier_plan.feast_slow_cols
    )
    adapter = (
        _build_feast_online_adapter(ctx) if ctx.use_feast_online and needs_feast else None
    )
    result = run_offline_production_pipeline(
        batch,
        ctx,
        adapter,
        strict_smoke=strict_smoke,
        allow_slow_parquet_fallback=False,
    )
    return summarize_offline_result(result, ctx)


def resolve_backtest_output_json(
    *,
    output_json: Path | None,
    model_dir: Path | None,
    bundle_dir: Path | None,
) -> Path | None:
    """Resolve CLI output path or default beside the model bundle."""
    if output_json is not None:
        return Path(output_json).resolve()
    try:
        bundle = resolve_model_bundle_for_reports(
            model_dir=model_dir,
            deploy_bundle_dir=bundle_dir,
        )
    except (FileNotFoundError, ValueError):
        return None
    return model_bundle_report_path(bundle, OFFLINE_SERVING_BACKTEST_REPORT_FILENAME)


def write_backtest_report(path: Path, report: dict[str, Any]) -> None:
    """Persist offline backtest JSON under the model bundle."""
    out = Path(path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    logger.info("[offline_backtest] wrote %s", out)


def run_cli(argv: list[str] | None = None) -> int:
    """CLI entry for offline serving backtest."""
    pr = argparse.ArgumentParser(
        description="Offline replay of production scorer v2 (PIT + Feast + predict)",
    )
    pr.add_argument("--bundle-dir", type=Path, default=None, help="deploy bundle root")
    pr.add_argument("--model-dir", type=Path, default=None, help="model bundle without deploy layout")
    pr.add_argument("--mapping-parquet", type=Path, default=None)
    pr.add_argument("--allowlist-parquet", type=Path, default=None)
    pr.add_argument("--feast-repo", type=Path, default=None)
    pr.add_argument("--gaming-day-start", type=str, default=None, help="YYYY-MM-DD")
    pr.add_argument("--gaming-day-end", type=str, default=None, help="YYYY-MM-DD")
    pr.add_argument(
        "--local-cleaned-bet",
        type=Path,
        default=None,
        help="hive-partitioned cleaned bet root (skips ClickHouse)",
    )
    pr.add_argument("--prediction-log", type=Path, default=None, help="CSV with bet_id column")
    pr.add_argument("--lookback-hours", type=float, default=6.0, help="when no gaming-day window")
    pr.add_argument("--max-bets", type=int, default=5000)
    pr.add_argument(
        "--output-json",
        type=Path,
        default=None,
        help=(
            "write JSON report path (default: <model-bundle>/"
            f"{OFFLINE_SERVING_BACKTEST_REPORT_FILENAME})"
        ),
    )
    pr.add_argument(
        "--strict-smoke",
        action="store_true",
        help="fail when post_join_feature_smoke fails (like production)",
    )
    pr.add_argument(
        "--evaluate-test-split",
        type=Path,
        default=None,
        help="official test.parquet: training-feature baseline vs production pipeline",
    )
    pr.add_argument(
        "--local-cleaned-bet-for-test",
        type=Path,
        default=None,
        help="cleaned bet root for production replay (required with --evaluate-test-split)",
    )
    pr.add_argument("--test-batch-size", type=int, default=5000)
    pr.add_argument("--test-max-rows", type=int, default=None)
    pr.add_argument(
        "--slow-parquet-fallback",
        action="store_true",
        help="use deploy slow_patron parquet join instead of Feast online (not recommended)",
    )
    args = pr.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.evaluate_test_split is not None:
        if args.model_dir is None and args.bundle_dir is None:
            pr.error("--evaluate-test-split requires --model-dir or --bundle-dir")
        if args.local_cleaned_bet_for_test is None:
            pr.error("--evaluate-test-split requires --local-cleaned-bet-for-test")
        mdir = Path(args.model_dir) if args.model_dir else Path(args.bundle_dir) / "models"
        report = run_test_split_comparison(
            model_dir=mdir,
            test_parquet=Path(args.evaluate_test_split),
            cleaned_bet_root=Path(args.local_cleaned_bet_for_test),
            mapping_parquet=args.mapping_parquet,
            allowlist_parquet=args.allowlist_parquet,
            feast_repo=args.feast_repo,
            batch_size=int(args.test_batch_size),
            max_rows=args.test_max_rows,
            use_feast_online=not bool(args.slow_parquet_fallback),
            allow_slow_parquet_fallback=bool(args.slow_parquet_fallback),
        )
        text = json.dumps(report, indent=2, default=str)
        print(text)
        out = resolve_backtest_output_json(
            output_json=args.output_json,
            model_dir=mdir,
            bundle_dir=args.bundle_dir,
        )
        if out is not None:
            write_backtest_report(out, report)
        return 0

    g_start = _parse_gaming_day(args.gaming_day_start) if args.gaming_day_start else None
    g_end = _parse_gaming_day(args.gaming_day_end) if args.gaming_day_end else None
    report = run_offline_serving_backtest(
        bundle_dir=args.bundle_dir,
        model_dir=args.model_dir,
        mapping_parquet=args.mapping_parquet,
        allowlist_parquet=args.allowlist_parquet,
        feast_repo=args.feast_repo,
        gaming_day_start=g_start,
        gaming_day_end=g_end,
        local_cleaned_bet=args.local_cleaned_bet,
        prediction_log=args.prediction_log,
        lookback_hours=float(args.lookback_hours),
        max_bets=args.max_bets,
        strict_smoke=bool(args.strict_smoke),
    )
    text = json.dumps(report, indent=2, default=str)
    print(text)
    out = resolve_backtest_output_json(
        output_json=args.output_json,
        model_dir=args.model_dir,
        bundle_dir=args.bundle_dir,
    )
    if out is not None:
        write_backtest_report(out, report)
    critical = int(report.get("n_skipped_entity_missing", 0)) + len(
        report.get("post_join_smoke_failures") or [],
    )
    if report.get("readiness_gate", {}).get("ok") is False:
        critical += 1
    return 1 if critical > 0 else 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
