"""Wave 5 shadow-capable player-game scorer (Phase A baseline parity).

Scores completed ready-queue player-games with the native PG model using
representative-bet features plus txn at ``player_game_ready_ts``. Writes to
``pg_shadow_scores`` only; does not change production alerts.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb
import numpy as np
import pandas as pd

from trainer_hightier.config import (
    DuckDbRuntimeConfig,
    default_hightier_serving_config,
    txn_lite_feature_columns,
)
from trainer_hightier.feature_experiment.materialize_txn_lite import (
    _build_player_game_txn_copy_sql,
    default_cleaned_casino_txn_root,
    resolve_cleaned_casino_txn_read_sql,
)
from trainer_hightier.player_game_grain import BET_ID_COLUMN, GAME_ID_COLUMN, PLAYER_ID_COLUMN
from trainer_hightier.serving.feature_builder import assert_features_ready, prepare_lgbm_feature_matrix
from trainer_hightier.serving.feature_supply import build_scorer_supplier_plan
from trainer_hightier.serving.model_bundle import HightierModelBundle, load_hightier_model_bundle
from trainer_hightier.serving.player_game_ready_queue import (
    PlayerGameRefetchFn,
    fetch_player_game_bets_clickhouse,
    refetch_player_game_from_frame,
)
from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas

logger = logging.getLogger(__name__)

_SHADOW_TABLE = "pg_shadow_scores"
_COMPLETED_TABLE = "pg_completed_player_games"


@dataclass(frozen=True)
class PlayerGameShadowScoreSummary:
    """Counts from one shadow scoring pass."""

    n_candidates: int
    n_scored: int
    n_skipped: int


def init_player_game_shadow_tables(conn: sqlite3.Connection) -> None:
    """Create shadow score table (idempotent)."""

    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_SHADOW_TABLE} (
            player_id INTEGER NOT NULL,
            game_id INTEGER NOT NULL,
            player_game_ready_ts TEXT NOT NULL,
            scored_at TEXT NOT NULL,
            representative_bet_id INTEGER,
            player_game_score REAL NOT NULL,
            threshold REAL NOT NULL,
            shadow_alert INTEGER NOT NULL,
            model_version TEXT,
            bet_count INTEGER,
            PRIMARY KEY (player_id, game_id)
        )
        """,
    )


def _list_completed_pending_shadow(conn: sqlite3.Connection) -> pd.DataFrame:
    """Return completed dry-run rows not yet shadow-scored."""

    rows = conn.execute(
        f"""
        SELECT
            c.player_id,
            c.game_id,
            c.player_game_ready_ts,
            c.representative_bet_id,
            c.bet_count
        FROM {_COMPLETED_TABLE} AS c
        LEFT JOIN {_SHADOW_TABLE} AS s
          ON c.player_id = s.player_id AND c.game_id = s.game_id
        WHERE s.player_id IS NULL
        ORDER BY c.dry_run_completed_at ASC
        """,
    ).fetchall()
    if not rows:
        return pd.DataFrame(
            columns=[
                "player_id",
                "game_id",
                "player_game_ready_ts",
                "representative_bet_id",
                "bet_count",
            ],
        )
    return pd.DataFrame(
        rows,
        columns=[
            "player_id",
            "game_id",
            "player_game_ready_ts",
            "representative_bet_id",
            "bet_count",
        ],
    )


def extract_representative_bet_rows(
    pending: pd.DataFrame,
    *,
    fetch_fn: PlayerGameRefetchFn,
    refetch_cache: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Build one representative bet row per pending completed player-game."""

    if pending.empty:
        return pd.DataFrame()
    rep_rows: list[pd.Series] = []
    for row in pending.itertuples(index=False):
        pid = int(row.player_id)
        gid = int(row.game_id)
        rep_id = row.representative_bet_id
        if refetch_cache is not None and not refetch_cache.empty:
            bets = refetch_player_game_from_frame(refetch_cache, pid, gid)
        else:
            bets = fetch_fn(pid, gid)
        if bets.empty:
            continue
        if rep_id is not None and BET_ID_COLUMN in bets.columns:
            bid = pd.to_numeric(bets[BET_ID_COLUMN], errors="coerce")
            hit = bets.loc[bid == int(rep_id)]
            if not hit.empty:
                rep_rows.append(hit.iloc[0])
                continue
        rep_rows.append(bets.iloc[-1])
    if not rep_rows:
        return pd.DataFrame()
    out = pd.DataFrame(rep_rows).reset_index(drop=True)
    out[PLAYER_ID_COLUMN] = pd.to_numeric(out[PLAYER_ID_COLUMN], errors="coerce")
    out[GAME_ID_COLUMN] = pd.to_numeric(out[GAME_ID_COLUMN], errors="coerce")
    return out


def compute_txn_pg_features_for_ready_rows(
    ready_rows: pd.DataFrame,
    *,
    cleaned_casino_txn_root: Path | None = None,
    duckdb_runtime: DuckDbRuntimeConfig | None = None,
) -> pd.DataFrame:
    """Compute ``txn__*`` at ``player_game_ready_ts`` for player-game rows."""

    cols = txn_lite_feature_columns()
    if ready_rows.empty:
        return pd.DataFrame(columns=[PLAYER_ID_COLUMN, GAME_ID_COLUMN, *cols])
    work = ready_rows[[PLAYER_ID_COLUMN, GAME_ID_COLUMN, "player_game_ready_ts"]].copy()
    work["player_game_ready_ts"] = pd.to_datetime(
        work["player_game_ready_ts"],
        errors="coerce",
        utc=True,
    )
    if work["player_game_ready_ts"].isna().any():
        raise ValueError(
            "compute_txn_pg_features_for_ready_rows has null player_game_ready_ts; "
            f"got {work['player_game_ready_ts'].isna().sum()} nulls",
        )
    runtime = duckdb_runtime or DuckDbRuntimeConfig()
    cleaned_root = Path(cleaned_casino_txn_root or default_cleaned_casino_txn_root()).resolve()
    cleaned_read, _, _ = resolve_cleaned_casino_txn_read_sql(cleaned_root, exclude_partial=True)
    txn_sql = _build_player_game_txn_copy_sql(
        train_source="pg_ready_rows",
        cleaned_read=cleaned_read,
        extra_window_hours=(),
    )
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, runtime)
        con.register("pg_ready_rows", work)
        out = con.execute(txn_sql).fetchdf()
    finally:
        con.close()
    for col in cols:
        if col not in out.columns:
            out[col] = 0.0
        out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0.0)
    return out


def _merge_txn_pg_features(staged: pd.DataFrame, txn_pg: pd.DataFrame) -> pd.DataFrame:
    """Replace ``txn__*`` columns on staged rows with PG-cutoff txn features."""

    if staged.empty:
        return staged
    out = staged.copy()
    txn_cols = [c for c in txn_lite_feature_columns() if c in txn_pg.columns]
    if not txn_cols:
        return out
    keys = [PLAYER_ID_COLUMN, GAME_ID_COLUMN]
    merged = out.merge(txn_pg[keys + txn_cols], on=keys, how="left", suffixes=("", "_pg"))
    for col in txn_cols:
        pg_col = f"{col}_pg"
        if pg_col in merged.columns:
            merged[col] = pd.to_numeric(merged[pg_col], errors="coerce").fillna(
                pd.to_numeric(merged.get(col), errors="coerce"),
            ).fillna(0.0)
            merged = merged.drop(columns=[pg_col])
    return merged


def _write_shadow_scores(
    conn: sqlite3.Connection,
    *,
    pending: pd.DataFrame,
    scores: np.ndarray,
    threshold: float,
    model_version: str,
    scored_at_iso: str,
) -> int:
    """Persist shadow scores for completed player-games."""

    n = 0
    for i, row in enumerate(pending.itertuples(index=False)):
        score = float(scores[i])
        conn.execute(
            f"""
            INSERT INTO {_SHADOW_TABLE} (
                player_id, game_id, player_game_ready_ts, scored_at,
                representative_bet_id, player_game_score, threshold,
                shadow_alert, model_version, bet_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(player_id, game_id) DO NOTHING
            """,
            (
                int(row.player_id),
                int(row.game_id),
                str(row.player_game_ready_ts),
                scored_at_iso,
                int(row.representative_bet_id) if row.representative_bet_id is not None else None,
                score,
                float(threshold),
                int(score >= float(threshold)),
                str(model_version),
                int(row.bet_count) if row.bet_count is not None else None,
            ),
        )
        n += 1
    return n


def run_player_game_shadow_scoring(
    conn: sqlite3.Connection,
    *,
    batch: Any,
    pg_bundle: HightierModelBundle,
    mapping_parquet: Path | None,
    feast_adapter: Any,
    manifest: Any | None,
    fetch_fn: PlayerGameRefetchFn | None = None,
    refetch_cache: pd.DataFrame | None = None,
) -> PlayerGameShadowScoreSummary:
    """Score newly completed player-games and write shadow rows only."""

    from trainer_hightier.serving import scorer as scorer_mod
    from trainer_hightier.serving.feature_builder import attach_mid_term_composite_columns
    from trainer_hightier.serving.feature_supply import (
        assert_scorer_supplier_plan_or_raise,
        build_scorer_supplier_plan,
        load_frozen_registry_for_bundle,
    )
    from trainer_hightier.serving.mid_term_bounded_asof import apply_mid_term_bounded_asof

    init_player_game_shadow_tables(conn)
    pending = _list_completed_pending_shadow(conn)
    if pending.empty:
        return PlayerGameShadowScoreSummary(n_candidates=0, n_scored=0, n_skipped=0)

    refetch = fetch_fn or fetch_player_game_bets_clickhouse
    rep_bets = extract_representative_bet_rows(
        pending,
        fetch_fn=refetch,
        refetch_cache=refetch_cache,
    )
    if rep_bets.empty:
        return PlayerGameShadowScoreSummary(
            n_candidates=int(len(pending)),
            n_scored=0,
            n_skipped=int(len(pending)),
        )

    rep_batch = scorer_mod._ScoringBatch(
        bets=rep_bets,
        cursor=batch.cursor,
        pool=batch.pool,
        pool_window_start=batch.pool_window_start,
        pool_window_end=batch.pool_window_end,
    )
    registry_snap = load_frozen_registry_for_bundle(Path(pg_bundle.bundle_dir))
    supplier_plan = build_scorer_supplier_plan(registry_snap, pg_bundle.feature_columns)
    assert_scorer_supplier_plan_or_raise(supplier_plan)
    staged = scorer_mod._build_staged_features(
        rep_batch,
        mapping_parquet=mapping_parquet,
        supplier_plan=supplier_plan,
    )
    cfg = default_hightier_serving_config()
    fail_frac = float(cfg.scorer_feast_entity_missing_fail_fraction)
    staged, skipped, _diag = scorer_mod._attach_feast_mid_slow(
        staged,
        feast_adapter,
        mid_columns=supplier_plan.feast_mid_cols,
        slow_columns=supplier_plan.feast_slow_cols,
        fail_fraction=fail_frac,
    )
    if skipped.shape[0]:
        logger.warning(
            "[pg_shadow] skipped %d representative bets with missing feast entities",
            len(skipped),
        )
    staged = apply_mid_term_bounded_asof(
        staged,
        mid_primitive_columns=supplier_plan.feast_mid_cols,
        n_days=int(cfg.production_mid_asof_backfill_days),
    )
    staged = attach_mid_term_composite_columns(staged, supplier_plan.mid_composite_cols)
    txn_pg = compute_txn_pg_features_for_ready_rows(pending)
    staged = _merge_txn_pg_features(staged, txn_pg)
    meta_cols = [
        PLAYER_ID_COLUMN,
        GAME_ID_COLUMN,
        "player_game_ready_ts",
        "representative_bet_id",
        "bet_count",
    ]
    staged_meta = staged.merge(
        pending[meta_cols],
        on=[PLAYER_ID_COLUMN, GAME_ID_COLUMN],
        how="inner",
    )
    if staged_meta.empty:
        return PlayerGameShadowScoreSummary(
            n_candidates=int(len(pending)),
            n_scored=0,
            n_skipped=int(len(pending)),
        )
    assert_features_ready(staged_meta, pg_bundle.feature_columns)
    x_f = prepare_lgbm_feature_matrix(
        staged_meta,
        feature_columns=pg_bundle.feature_columns,
        categorical_columns=pg_bundle.categorical_columns,
        category_categories=dict(pg_bundle.category_categories),
    )
    scores = np.asarray(pg_bundle.model.predict_proba(x_f)[:, 1], dtype=np.float64)
    scored_at = datetime.now(ZoneInfo(cfg.hk_tz)).isoformat()
    n_scored = _write_shadow_scores(
        conn,
        pending=staged_meta,
        scores=scores,
        threshold=float(pg_bundle.threshold),
        model_version=str(pg_bundle.model_version),
        scored_at_iso=scored_at,
    )
    logger.info(
        "[pg_shadow] scored=%d candidates=%d threshold=%.4f",
        n_scored,
        len(pending),
        float(pg_bundle.threshold),
    )
    return PlayerGameShadowScoreSummary(
        n_candidates=int(len(pending)),
        n_scored=int(n_scored),
        n_skipped=int(len(pending) - n_scored),
    )


@dataclass(frozen=True)
class PlayerGameShadowComparisonSummary:
    """Legacy top3_mean alerts vs native PG shadow scores on the same player-games."""

    n_legacy_player_games: int
    n_shadow_scored: int
    n_overlap: int
    n_legacy_alert: int
    n_shadow_alert: int
    n_both_alert: int
    n_legacy_only_alert: int
    n_shadow_only_alert: int
    score_delta_mean: float | None
    score_delta_p95_abs: float | None
    ready_lag_sec_p50: float | None
    ready_lag_sec_p95: float | None
    pending_age_sec_p95: float | None
    shadow_alert_volume_ratio: float | None


def summarize_player_game_shadow_comparison(
    conn: sqlite3.Connection,
    *,
    since_iso: str | None = None,
) -> PlayerGameShadowComparisonSummary:
    """Compare production legacy alerts with PG shadow scores for W6 gate review."""

    init_player_game_shadow_tables(conn)
    since_clause = ""
    params: list[str] = []
    if since_iso is not None:
        since_clause = "AND s.scored_at >= ?"
        params.append(str(since_iso))

    shadow_rows = conn.execute(
        f"""
        SELECT
            s.player_id,
            s.game_id,
            s.player_game_score AS shadow_score,
            s.shadow_alert,
            s.scored_at
        FROM {_SHADOW_TABLE} AS s
        WHERE 1=1 {since_clause}
        """,
        params,
    ).fetchall()
    legacy_rows = conn.execute(
        """
        SELECT
            CAST(player_id AS INTEGER) AS player_id,
            CAST(game_id AS INTEGER) AS game_id,
            score AS legacy_score,
            scored_at
        FROM alerts
        WHERE game_id IS NOT NULL AND player_id IS NOT NULL
        """,
    ).fetchall()
    if not shadow_rows and not legacy_rows:
        return PlayerGameShadowComparisonSummary(
            n_legacy_player_games=0,
            n_shadow_scored=0,
            n_overlap=0,
            n_legacy_alert=0,
            n_shadow_alert=0,
            n_both_alert=0,
            n_legacy_only_alert=0,
            n_shadow_only_alert=0,
            score_delta_mean=None,
            score_delta_p95_abs=None,
            ready_lag_sec_p50=None,
            ready_lag_sec_p95=None,
            pending_age_sec_p95=None,
            shadow_alert_volume_ratio=None,
        )

    shadow_df = pd.DataFrame(
        shadow_rows,
        columns=["player_id", "game_id", "shadow_score", "shadow_alert", "scored_at"],
    )
    legacy_df = pd.DataFrame(
        legacy_rows,
        columns=["player_id", "game_id", "legacy_score", "legacy_scored_at"],
    )
    merged = shadow_df.merge(
        legacy_df,
        on=["player_id", "game_id"],
        how="outer",
        indicator=True,
    )
    overlap = merged.loc[merged["_merge"] == "both"].copy()
    n_overlap = int(len(overlap))
    n_legacy_alert = int(len(legacy_df))
    n_shadow_alert = int((shadow_df["shadow_alert"] == 1).sum()) if not shadow_df.empty else 0

    both_alert = 0
    legacy_only_alert = 0
    shadow_only_alert = 0
    if not overlap.empty:
        legacy_thr = overlap["legacy_score"].notna()
        shadow_thr = overlap["shadow_alert"] == 1
        both_alert = int((legacy_thr & shadow_thr).sum())
        legacy_only_alert = int((legacy_thr & ~shadow_thr).sum())
        shadow_only_alert = int((~legacy_thr & shadow_thr).sum())

    score_delta_mean: float | None = None
    score_delta_p95_abs: float | None = None
    if not overlap.empty:
        deltas = (
            pd.to_numeric(overlap["shadow_score"], errors="coerce")
            - pd.to_numeric(overlap["legacy_score"], errors="coerce")
        ).dropna()
        if not deltas.empty:
            score_delta_mean = float(deltas.mean())
            score_delta_p95_abs = float(deltas.abs().quantile(0.95))

    lag_rows = conn.execute(
        f"""
        SELECT pending_age_sec, ready_lag_sec
        FROM {_COMPLETED_TABLE}
        WHERE ready_lag_sec IS NOT NULL
        """,
    ).fetchall()
    ready_lag_p50: float | None = None
    ready_lag_p95: float | None = None
    pending_age_p95: float | None = None
    if lag_rows:
        lag_df = pd.DataFrame(lag_rows, columns=["pending_age_sec", "ready_lag_sec"])
        ready_lag = pd.to_numeric(lag_df["ready_lag_sec"], errors="coerce").dropna()
        pending_age = pd.to_numeric(lag_df["pending_age_sec"], errors="coerce").dropna()
        if not ready_lag.empty:
            ready_lag_p50 = float(ready_lag.quantile(0.5))
            ready_lag_p95 = float(ready_lag.quantile(0.95))
        if not pending_age.empty:
            pending_age_p95 = float(pending_age.quantile(0.95))

    volume_ratio: float | None = None
    if n_legacy_alert > 0:
        volume_ratio = float(n_shadow_alert) / float(n_legacy_alert)

    return PlayerGameShadowComparisonSummary(
        n_legacy_player_games=int(len(legacy_df)),
        n_shadow_scored=int(len(shadow_df)),
        n_overlap=n_overlap,
        n_legacy_alert=n_legacy_alert,
        n_shadow_alert=n_shadow_alert,
        n_both_alert=both_alert,
        n_legacy_only_alert=legacy_only_alert,
        n_shadow_only_alert=shadow_only_alert,
        score_delta_mean=score_delta_mean,
        score_delta_p95_abs=score_delta_p95_abs,
        ready_lag_sec_p50=ready_lag_p50,
        ready_lag_sec_p95=ready_lag_p95,
        pending_age_sec_p95=pending_age_p95,
        shadow_alert_volume_ratio=volume_ratio,
    )


def evaluate_player_game_shadow_gate(
    summary: PlayerGameShadowComparisonSummary,
    *,
    max_ready_lag_sec_p95: float = 120.0,
    max_pending_age_sec_p95: float = 120.0,
    min_alert_volume_ratio: float = 0.5,
    max_alert_volume_ratio: float = 2.0,
    max_score_delta_p95_abs: float = 0.15,
    min_overlap: int = 10,
) -> dict[str, Any]:
    """Return W6 shadow gate pass/fail with explicit reasons."""

    checks: dict[str, bool] = {}
    checks["min_overlap_ok"] = summary.n_overlap >= int(min_overlap)
    if summary.ready_lag_sec_p95 is not None:
        checks["ready_lag_p95_ok"] = summary.ready_lag_sec_p95 <= max_ready_lag_sec_p95
    if summary.pending_age_sec_p95 is not None:
        checks["pending_age_p95_ok"] = summary.pending_age_sec_p95 <= max_pending_age_sec_p95
    if summary.shadow_alert_volume_ratio is not None and summary.n_overlap >= int(min_overlap):
        ratio = summary.shadow_alert_volume_ratio
        checks["alert_volume_ratio_ok"] = min_alert_volume_ratio <= ratio <= max_alert_volume_ratio
    if summary.score_delta_p95_abs is not None and summary.n_overlap >= int(min_overlap):
        checks["score_delta_p95_ok"] = summary.score_delta_p95_abs <= max_score_delta_p95_abs
    proceed = bool(checks) and all(checks.values())
    failed = [name for name, ok in checks.items() if not ok]
    reason = (
        "shadow gate checks passed"
        if proceed
        else f"failed checks: {', '.join(failed)}"
    )
    return {
        "proceed_to_production_switch": proceed,
        "reason": reason,
        "checks": checks,
        "thresholds": {
            "max_ready_lag_sec_p95": max_ready_lag_sec_p95,
            "max_pending_age_sec_p95": max_pending_age_sec_p95,
            "min_alert_volume_ratio": min_alert_volume_ratio,
            "max_alert_volume_ratio": max_alert_volume_ratio,
            "max_score_delta_p95_abs": max_score_delta_p95_abs,
            "min_overlap": int(min_overlap),
        },
        "summary": summary.__dict__,
    }


def build_player_game_shadow_gate_report(
    conn: sqlite3.Connection,
    *,
    since_iso: str | None = None,
    cfg: Any | None = None,
) -> dict[str, Any]:
    """Build W6 shadow gate JSON payload from state DB tables."""

    from trainer_hightier.serving.player_game_ready_queue import summarize_dry_run_metrics

    serving = cfg or default_hightier_serving_config()
    comparison = summarize_player_game_shadow_comparison(conn, since_iso=since_iso)
    gate = evaluate_player_game_shadow_gate(
        comparison,
        max_ready_lag_sec_p95=float(serving.player_game_shadow_gate_max_ready_lag_sec_p95),
        max_pending_age_sec_p95=float(serving.player_game_shadow_gate_max_pending_age_sec_p95),
        min_alert_volume_ratio=float(serving.player_game_shadow_gate_min_alert_volume_ratio),
        max_alert_volume_ratio=float(serving.player_game_shadow_gate_max_alert_volume_ratio),
        max_score_delta_p95_abs=float(serving.player_game_shadow_gate_max_score_delta_p95_abs),
        min_overlap=int(serving.player_game_shadow_gate_min_overlap),
    )
    return {
        "report_kind": "player_game_shadow_gate_w6",
        "since_iso": since_iso,
        "dry_run_metrics": summarize_dry_run_metrics(conn),
        "comparison": comparison.__dict__,
        "gate": gate,
    }


def write_player_game_shadow_gate_report(
    conn: sqlite3.Connection,
    output_json: Path,
    *,
    since_iso: str | None = None,
    cfg: Any | None = None,
) -> dict[str, Any]:
    """Write W6 shadow gate report to disk and return the payload."""

    payload = build_player_game_shadow_gate_report(conn, since_iso=since_iso, cfg=cfg)
    path = Path(output_json).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    logger.info("[pg_shadow_gate] wrote report → %s proceed=%s", path, payload["gate"]["proceed_to_production_switch"])
    return payload


def load_player_game_shadow_bundle(cfg: Any | None = None) -> HightierModelBundle | None:
    """Load configured PG shadow bundle or return None when disabled/missing."""

    serving = cfg or default_hightier_serving_config()
    bundle_dir = getattr(serving, "player_game_shadow_model_bundle_dir", None)
    if bundle_dir is None:
        return None
    path = Path(bundle_dir).resolve()
    if not (path / "model.pkl").is_file():
        logger.warning("[pg_shadow] bundle missing at %s", path)
        return None
    bundle = load_hightier_model_bundle(bundle_dir=path)
    if str(bundle.score_aggregation) != "native":
        logger.warning(
            "[pg_shadow] expected score_aggregation='native'; got %r",
            bundle.score_aggregation,
        )
    return bundle


def main() -> int:
    """CLI: export W6 shadow gate report from state DB."""

    parser = argparse.ArgumentParser(description="Player-game shadow gate report (Wave 6)")
    parser.add_argument(
        "--state-db",
        type=Path,
        default=None,
        help="SQLite state.db path (default: config state_db_path)",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        required=True,
        help="Write gate report JSON here",
    )
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="Only include shadow scores with scored_at >= this ISO timestamp",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    cfg = default_hightier_serving_config()
    db_path = Path(args.state_db).resolve() if args.state_db else Path(cfg.state_db_path).resolve()
    if not db_path.is_file():
        raise SystemExit(f"state db not found: {db_path}")
    from trainer_hightier.serving.state_db import apply_sqlite_serving_pragmas, init_state_db

    init_state_db(db_path)
    conn = sqlite3.connect(db_path)
    apply_sqlite_serving_pragmas(conn)
    try:
        payload = write_player_game_shadow_gate_report(
            conn,
            args.output_json,
            since_iso=args.since,
            cfg=cfg,
        )
    finally:
        conn.close()
    return 0 if payload["gate"]["proceed_to_production_switch"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
