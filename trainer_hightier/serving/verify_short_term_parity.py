"""Offline checks: lookback sweep vs training parquet; canonical multi-card pool coverage.

When ``--output-json`` is omitted, writes ``<model-dir>/short_term_parity_verification.json``.
Example::

    python -m trainer_hightier.serving.verify_short_term_parity \\
        --model-dir out/models_high_tier_mvp/20260522-124003-245bd1f
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from trainer_hightier.config import HightierServingConfig
from trainer_hightier.core.model_bundle_paths import (
    SHORT_TERM_PARITY_REPORT_FILENAME,
    model_bundle_report_path,
)
from trainer_hightier.feature_experiment.materialize_fe_derived import (
    compute_fe_derived_features_from_pool,
)
from trainer_hightier.serving.feature_builder import attach_canonical_id
from trainer_hightier.serving.offline_serving_backtest import (
    _ScoringBatch,
    _bets_frame_from_test_batch,
    _iter_test_batches,
    build_pool_from_cleaned_parquet,
    resolve_offline_context,
    run_offline_production_pipeline,
    _build_feast_online_adapter,
)
from trainer_hightier.serving.scorer import compute_hot_pool_window_start

_FE_COL = "fe__canonical__wager_sum__today"
_SHORT_FE_COLS = tuple(
    c
    for c in (
        "fe__wager_sum__w15m",
        "fe__bets_cnt__w15m",
        "fe__canonical__bets_cnt__today",
        "fe__canonical__wager_sum__today",
        "fe__canonical__avg_wager__today",
        "fe__interarrival__lag2_sec",
        "fe__odds__payout_odds_step_ratio",
        "fe__interarrival__last_gap_to_recent_mean_ratio__w1h",
    )
)


def _feature_diff_mask(tr: pd.Series, pr: pd.Series) -> np.ndarray:
    """Row-wise inequality mask for train vs prod feature series."""
    tn, pn = tr.isna().to_numpy(), pr.isna().to_numpy()
    out = tn != pn
    both = ~(tn | pn)
    if not both.any():
        return out
    if pd.api.types.is_numeric_dtype(tr):
        out[both] = (
            np.abs(tr[both].astype(float).to_numpy() - pr[both].astype(float).to_numpy())
            > 1e-6
        )
    else:
        out[both] = tr[both].astype(str).to_numpy() != pr[both].astype(str).to_numpy()
    return out


def _pct_changed(train: pd.DataFrame, prod: pd.DataFrame, cols: tuple[str, ...]) -> dict[str, float]:
    """Per-column fraction of rows where train != prod (bet_id-aligned)."""
    out: dict[str, float] = {}
    for c in cols:
        if c not in train.columns or c not in prod.columns:
            continue
        out[c] = float(_feature_diff_mask(train[c], prod[c]).mean())
    return out


def _run_prod_features(
    test: pd.DataFrame,
    *,
    ctx: Any,
    adapter: Any,
    cfg: HightierServingConfig,
    cleaned_root: Path,
    expand_canonical_pool: bool,
) -> pd.DataFrame:
    """Score test rows with production pipeline; optional canonical-expanded pool."""
    mapping = ctx.mapping_parquet
    cols = [c for c in ctx.bundle.feature_columns if c.startswith("fe__") or c.startswith("bet__")]
    parts: list[pd.DataFrame] = []
    cmap = pd.read_parquet(mapping, columns=["player_id", "canonical_id"])
    cmap["player_id"] = pd.to_numeric(cmap["player_id"], errors="coerce")
    cmap = cmap.dropna(subset=["player_id"])
    pid_by_cid = cmap.groupby("canonical_id")["player_id"].apply(
        lambda s: sorted({int(x) for x in s.tolist()}),
    )

    for batch_df in _iter_test_batches(test, batch_size=8000, max_rows=None):
        bets = _bets_frame_from_test_batch(batch_df)
        pool = build_pool_from_cleaned_parquet(
            bets, cleaned_root=cleaned_root, cfg=cfg, mapping_parquet=mapping,
        )
        if expand_canonical_pool:
            pool = _expand_pool_canonical_aliases(
                bets,
                pool,
                pid_by_cid=pid_by_cid,
                cleaned_root=cleaned_root,
                cfg=cfg,
                mapping_parquet=mapping,
            )
        sb = _ScoringBatch(
            bets=bets.reset_index(drop=True),
            cursor=pd.to_datetime(bets["__etl_insert_Dtm"], errors="coerce"),
            pool=pool,
        )
        res = run_offline_production_pipeline(sb, ctx, adapter, strict_smoke=False)
        parts.append(
            pd.DataFrame(
                {
                    "bet_id": batch_df["bet_id"].values,
                    **{c: res.staged[c].values for c in cols},
                },
            ),
        )
    return pd.concat(parts, ignore_index=True)


def _expand_pool_canonical_aliases(
    bets: pd.DataFrame,
    pool: pd.DataFrame,
    *,
    pid_by_cid: pd.Series,
    cleaned_root: Path,
    cfg: HightierServingConfig,
    mapping_parquet: Path,
) -> pd.DataFrame:
    """Reload pool using all ``player_id`` aliases for canonicals present in *bets*."""
    import duckdb as _ddb

    staged = attach_canonical_id(bets, mapping_parquet=mapping_parquet)
    if "canonical_id" not in staged.columns:
        raise ValueError("bets missing canonical_id after mapping attach")
    extra: set[int] = set()
    for cid in staged["canonical_id"].dropna().astype(str).unique():
        if not str(cid).strip():
            continue
        for pid in pid_by_cid.get(cid, []):
            extra.add(int(pid))
    base_pids = {int(x) for x in bets["player_id"].dropna().unique()}
    all_pids = sorted(extra | base_pids)
    if set(all_pids) == set(pool["player_id"].dropna().astype(int).unique()):
        return pool
    root = Path(cleaned_root).resolve()
    glob_path = str((root / "**" / "*.parquet").as_posix())
    pool_start = compute_hot_pool_window_start(bets, cfg=cfg)
    pool_end = pd.to_datetime(bets["payout_complete_dtm"], errors="coerce").max().to_pydatetime()
    conn = _ddb.connect()
    try:
        conn.execute(
            "CREATE TEMP TABLE allow_pids AS SELECT * FROM (SELECT UNNEST(?) AS player_id)",
            [all_pids],
        )
        q = f"""
            SELECT
                b.bet_id, b.player_id, b.payout_complete_dtm,
                CAST(b.gaming_day AS TIMESTAMP) AS gaming_day,
                b.session_id, b.table_id, b.wager, b.casino_win, b.payout_odds,
                b.is_back_bet, b.bet_type, b.type_of_bet
            FROM read_parquet('{glob_path}', hive_partitioning=true) AS b
            INNER JOIN allow_pids AS p ON b.player_id = p.player_id
            WHERE b.payout_complete_dtm >= ?
              AND b.payout_complete_dtm <= ?
        """
        wide = conn.execute(q, [pool_start, pool_end]).fetchdf()
    finally:
        conn.close()
    if wide.empty:
        return pool
    wide["__etl_insert_Dtm"] = wide["payout_complete_dtm"]
    from trainer_hightier.serving.scorer import (
        _postprocess_incremental_bets_timestamps,
    )
    from trainer_hightier.serving.feature_builder import (
        attach_synthetic_etl_and_prediction_visible,
    )

    _postprocess_incremental_bets_timestamps(wide)
    return attach_synthetic_etl_and_prediction_visible(wide)


def _lookback_sweep(
    test: pd.DataFrame,
    *,
    ctx: Any,
    adapter: Any,
    cleaned_root: Path,
    lookbacks: tuple[int, ...],
) -> dict[str, Any]:
    """Compare train parquet vs prod short-term cols across lookback hours."""
    train = test[["bet_id", *_SHORT_FE_COLS]].copy()
    rows: list[dict[str, Any]] = []
    for lb in lookbacks:
        cfg = replace(ctx.cfg, hot_feature_pool_lookback_hours=int(lb))
        prod = _run_prod_features(
            test,
            ctx=ctx,
            adapter=adapter,
            cfg=cfg,
            cleaned_root=cleaned_root,
            expand_canonical_pool=False,
        )
        merged = train.merge(prod, on="bet_id", suffixes=("_tr", "_pr"))
        tr_frame = merged[[f"{c}_tr" for c in _SHORT_FE_COLS]].rename(
            columns={f"{c}_tr": c for c in _SHORT_FE_COLS},
        )
        pr_frame = merged[[f"{c}_pr" for c in _SHORT_FE_COLS]].rename(
            columns={f"{c}_pr": c for c in _SHORT_FE_COLS},
        )
        pcts = _pct_changed(tr_frame, pr_frame, _SHORT_FE_COLS)
        mean_pct = float(np.mean(list(pcts.values()))) if pcts else 0.0
        rows.append(
            {
                "lookback_hours": lb,
                "n_rows": len(merged),
                "mean_pct_fe_changed": mean_pct,
                "per_feature_pct_changed": pcts,
                f"{_FE_COL}_pct_changed": pcts.get(_FE_COL, 0.0),
            },
        )
    base = rows[0]["per_feature_pct_changed"]
    for i in range(1, len(rows)):
        delta = {
            k: round(rows[i]["per_feature_pct_changed"].get(k, 0) - base.get(k, 0), 6)
            for k in _SHORT_FE_COLS
        }
        rows[i]["delta_vs_first_lookback"] = delta
    return {"lookbacks": list(lookbacks), "runs": rows}


def _fe_today_from_pool_batches(
    test: pd.DataFrame,
    *,
    cfg: HightierServingConfig,
    cleaned_root: Path,
    mapping_parquet: Path,
    expand_canonical_pool: bool,
) -> pd.DataFrame:
    """Compute ``fe__canonical__wager_sum__today`` per batch (single PIT pass, no scorer)."""
    cmap = pd.read_parquet(mapping_parquet, columns=["player_id", "canonical_id"])
    cmap["player_id"] = pd.to_numeric(cmap["player_id"], errors="coerce")
    pid_by_cid = cmap.groupby("canonical_id")["player_id"].apply(
        lambda s: sorted({int(x) for x in s.tolist()}),
    )
    chunks: list[pd.DataFrame] = []
    for batch_df in _iter_test_batches(test, batch_size=8000, max_rows=None):
        bets = _bets_frame_from_test_batch(batch_df)
        pool = build_pool_from_cleaned_parquet(
            bets, cleaned_root=cleaned_root, cfg=cfg, mapping_parquet=mapping_parquet,
        )
        if expand_canonical_pool:
            pool = _expand_pool_canonical_aliases(
                bets,
                pool,
                pid_by_cid=pid_by_cid,
                cleaned_root=cleaned_root,
                cfg=cfg,
                mapping_parquet=mapping_parquet,
            )
        pool = attach_canonical_id(pool, mapping_parquet=mapping_parquet)
        feats = compute_fe_derived_features_from_pool(pool, bets["bet_id"])
        joined = batch_df[["bet_id"]].merge(
            feats[["bet_id", _FE_COL]],
            on="bet_id",
            how="left",
        )
        chunks.append(joined)
    return pd.concat(chunks, ignore_index=True)


def _canonical_alias_study(
    test: pd.DataFrame,
    *,
    ctx: Any,
    cleaned_root: Path,
    mapping_parquet: Path,
) -> dict[str, Any]:
    """Test whether fe__today drift correlates with multi-card canonicals and pool expansion."""
    cfg = ctx.cfg
    need = ["bet_id", "player_id", "canonical_id", "gaming_day", "payout_complete_dtm", _FE_COL]
    miss = [c for c in need if c not in test.columns]
    if miss:
        raise ValueError(f"test parquet missing columns for alias study: {miss}")
    train = test[need].copy()
    prod_std = _fe_today_from_pool_batches(
        test,
        cfg=cfg,
        cleaned_root=cleaned_root,
        mapping_parquet=mapping_parquet,
        expand_canonical_pool=False,
    )
    prod_exp = _fe_today_from_pool_batches(
        test,
        cfg=cfg,
        cleaned_root=cleaned_root,
        mapping_parquet=mapping_parquet,
        expand_canonical_pool=True,
    )
    m = train.merge(
        prod_std[["bet_id", _FE_COL]].rename(columns={_FE_COL: "prod_std"}),
        on="bet_id",
    ).merge(
        prod_exp[["bet_id", _FE_COL]].rename(columns={_FE_COL: "prod_exp"}),
        on="bet_id",
    )
    diff_mask = _feature_diff_mask(m[_FE_COL], m["prod_std"])
    m["fe_diff_std"] = diff_mask
    m["fe_diff_exp"] = _feature_diff_mask(m[_FE_COL], m["prod_exp"])

    cmap = pd.read_parquet(mapping_parquet, columns=["player_id", "canonical_id"])
    cmap["player_id"] = pd.to_numeric(cmap["player_id"], errors="coerce")
    cmap["canonical_id"] = cmap["canonical_id"].astype(str).str.strip()
    n_cards = cmap.groupby("canonical_id")["player_id"].nunique()
    m["n_player_cards"] = (
        m["canonical_id"].astype(str).str.strip().map(n_cards).fillna(1).astype(int)
    )
    m["is_multi_card"] = m["n_player_cards"] > 1

    n_diff = int(diff_mask.sum())
    if n_diff == 0:
        return {
            "n_rows": len(m),
            "n_fe_today_diff": 0,
            "message": "no fe__canonical__wager_sum__today diffs on this slice",
        }

    diff = m.loc[diff_mask]
    multi_rate = float(diff["is_multi_card"].mean())
    all_multi_rate = float(m["is_multi_card"].mean())
    std_vs_exp_identical = float(
        (m["prod_std"].astype(float) - m["prod_exp"].astype(float)).abs().le(1e-6).mean(),
    )
    still_after = m.loc[diff_mask, "fe_diff_exp"].to_numpy()
    recovered = int((~still_after).sum())

    sample_n = min(200, len(diff))
    alias_rows: list[dict[str, Any]] = []
    if sample_n > 0:
        samp = diff.sample(n=sample_n, random_state=42)
        alias_rows = _sample_alias_wager_gap(samp, cleaned_root=cleaned_root, mapping_parquet=mapping_parquet)

    pool_dup = _pool_duplicate_pcd_stats(test, ctx=ctx, cleaned_root=cleaned_root)
    return {
        "n_rows": len(m),
        "pool_duplicate_canonical_pcd": pool_dup,
        "note": (
            "Duplicate (canonical_id, pcd) with distinct bet_id requires ORDER BY pcd, bet_id "
            "for deterministic LAG/ROWS windows."
        ),
        "n_fe_today_diff_std_pool": n_diff,
        "pct_diff_that_are_multi_card": multi_rate,
        "pct_all_rows_multi_card": all_multi_rate,
        "enrichment_multi_card_vs_population": multi_rate / max(all_multi_rate, 1e-9),
        "n_diff_fixed_by_canonical_pool_expand": recovered,
        "n_diff_still_after_expand": n_diff - recovered,
        "pct_diff_fixed_by_expand": float(recovered / max(n_diff, 1)),
        "pct_diff_std_pool": float(n_diff / max(len(m), 1)),
        "pct_std_vs_exp_identical": std_vs_exp_identical,
        "alias_wager_sample": alias_rows,
    }


def _pool_duplicate_pcd_stats(
    test: pd.DataFrame,
    *,
    ctx: Any,
    cleaned_root: Path,
) -> dict[str, Any]:
    """Count duplicate (canonical_id, payout_complete_dtm) rows in a representative pool."""
    batch = next(iter(_iter_test_batches(test, batch_size=8000, max_rows=8000)))
    bets = _bets_frame_from_test_batch(batch)
    pool = build_pool_from_cleaned_parquet(
        bets,
        cleaned_root=cleaned_root,
        cfg=ctx.cfg,
        mapping_parquet=ctx.mapping_parquet,
    )
    pool = attach_canonical_id(pool, mapping_parquet=ctx.mapping_parquet)
    pool["pcd"] = pd.to_datetime(pool["payout_complete_dtm"], errors="coerce")
    dup = pool.groupby(["canonical_id", "pcd"]).size()
    return {
        "pool_rows": int(len(pool)),
        "duplicate_canonical_pcd_groups": int((dup > 1).sum()),
        "max_rows_per_canonical_pcd": int(dup.max()) if len(dup) else 0,
    }


def _sample_alias_wager_gap(
    samp: pd.DataFrame,
    *,
    cleaned_root: Path,
    mapping_parquet: Path,
) -> list[dict[str, Any]]:
    """For sample diffs, compare training fe vs wager sums (batch pid vs all alias pids)."""
    root = Path(cleaned_root).resolve()
    glob_path = str((root / "**" / "*.parquet").as_posix())
    map_esc = str(Path(mapping_parquet).resolve()).replace("\\", "/").replace("'", "''")
    glob_esc = glob_path.replace("'", "''")
    out: list[dict[str, Any]] = []
    con = duckdb.connect(database=":memory:")
    try:
        for _, row in samp.iterrows():
            bid = float(row["bet_id"])
            cid = str(row["canonical_id"]).strip()
            gday = pd.Timestamp(row["gaming_day"]).date()
            pcd = pd.Timestamp(row.get("payout_complete_dtm", row["gaming_day"]))
            q = f"""
WITH cmap AS (
  SELECT TRY_CAST(player_id AS BIGINT) AS player_id,
         TRIM(CAST(canonical_id AS VARCHAR)) AS canonical_id
  FROM read_parquet('{map_esc}')
  WHERE TRIM(CAST(canonical_id AS VARCHAR)) = '{cid.replace("'", "''")}'
),
day_bets AS (
  SELECT TRY_CAST(b.player_id AS BIGINT) AS player_id,
         TRY_CAST(b.wager AS DOUBLE) AS wager,
         CAST(b.payout_complete_dtm AS TIMESTAMPTZ) AS pcd
  FROM read_parquet('{glob_esc}', hive_partitioning=true) AS b
  INNER JOIN cmap AS c ON b.player_id = c.player_id
  WHERE CAST(b.gaming_day AS DATE) = DATE '{gday}'
    AND TRY_CAST(b.wager AS DOUBLE) > 0
)
SELECT
  (SELECT COALESCE(SUM(wager), 0) FROM day_bets WHERE pcd < ?) AS wager_all_alias_prior,
  (SELECT COALESCE(SUM(wager), 0) FROM day_bets
     WHERE pcd < ? AND player_id = ?) AS wager_batch_pid_prior
"""
            batch_pid = int(row["player_id"])
            vals = con.execute(q, [pcd, pcd, batch_pid]).fetchone()
            out.append(
                {
                    "bet_id": bid,
                    "train_fe": float(row[_FE_COL]) if pd.notna(row[_FE_COL]) else None,
                    "prod_std": float(row["prod_std"]) if pd.notna(row["prod_std"]) else None,
                    "n_player_cards": int(row["n_player_cards"]),
                    "wager_prior_all_alias": float(vals[0]) if vals else None,
                    "wager_prior_batch_pid_only": float(vals[1]) if vals else None,
                    "train_matches_all_alias": abs((vals[0] or 0) - (row[_FE_COL] or 0)) < 1e-3
                    if vals and pd.notna(row[_FE_COL])
                    else None,
                    "prod_matches_batch_only": abs((vals[1] or 0) - (row["prod_std"] or 0)) < 1e-3
                    if vals and pd.notna(row["prod_std"])
                    else None,
                },
            )
    finally:
        con.close()
    return out


def main() -> None:
    """CLI entry: lookback sweep + canonical alias study on test split."""
    parser = argparse.ArgumentParser(description="Short-term PIT parity verification")
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--test-parquet", type=Path, default=None)
    parser.add_argument("--cleaned-bet", type=Path, default=None)
    parser.add_argument("--feast-repo", type=Path, default=Path("trainer_hightier/feast_repo"))
    parser.add_argument("--max-rows", type=int, default=80_000)
    parser.add_argument("--lookbacks", type=str, default="6,8,12")
    parser.add_argument(
        "--output-json",
        type=Path,
        default=None,
        help=(
            "write JSON report path (default: <model-dir>/"
            f"{SHORT_TERM_PARITY_REPORT_FILENAME})"
        ),
    )
    args = parser.parse_args()

    model_dir = Path(args.model_dir).resolve()
    test_pq = (
        Path(args.test_parquet).resolve()
        if args.test_parquet
        else Path("trainer_hightier/artifacts/training_data/splits/test.parquet").resolve()
    )
    cleaned = (
        Path(args.cleaned_bet).resolve()
        if args.cleaned_bet
        else Path("trainer_hightier/artifacts/cleaned/cleaned__gmwds_t_bet").resolve()
    )
    test = pd.read_parquet(test_pq)
    if args.max_rows and len(test) > args.max_rows:
        test = test.sort_values("bet_id").head(int(args.max_rows)).reset_index(drop=True)

    ctx = resolve_offline_context(
        bundle_dir=None,
        model_dir=model_dir,
        mapping_parquet=model_dir / "deploy_inputs/canonical_player_mapping.parquet",
        allowlist_parquet=model_dir / "deploy_inputs/adt_allowed_players_q0p99.parquet",
        feast_repo=Path(args.feast_repo).resolve(),
        slow_patron_parquet=None,
        use_feast_online=True,
    )
    adapter = _build_feast_online_adapter(ctx)
    lookbacks = tuple(int(x.strip()) for x in str(args.lookbacks).split(",") if x.strip())

    report: dict[str, Any] = {
        "model_dir": str(model_dir),
        "test_parquet": str(test_pq),
        "n_rows_analyzed": len(test),
        "lookback_sweep": _lookback_sweep(
            test, ctx=ctx, adapter=adapter, cleaned_root=cleaned, lookbacks=lookbacks
        ),
        "canonical_alias_study": _canonical_alias_study(
            test,
            ctx=ctx,
            cleaned_root=cleaned,
            mapping_parquet=ctx.mapping_parquet,
        ),
    }
    out = (
        Path(args.output_json).resolve()
        if args.output_json is not None
        else model_bundle_report_path(model_dir, SHORT_TERM_PARITY_REPORT_FILENAME)
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"wrote {out}")
    print("lookback mean_pct:", [r["mean_pct_fe_changed"] for r in report["lookback_sweep"]["runs"]])
    ca = report["canonical_alias_study"]
    print(
        "canonical diff multi_card rate",
        ca.get("pct_diff_that_are_multi_card"),
        "fixed_by_expand",
        ca.get("pct_diff_fixed_by_expand"),
    )


if __name__ == "__main__":
    main()
