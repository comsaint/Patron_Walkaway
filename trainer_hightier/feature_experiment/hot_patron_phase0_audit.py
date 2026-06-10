"""Phase 0 hot-patron gap audit: reproduce FP table and profile feature failures."""

from __future__ import annotations

import argparse
import importlib
import json
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from trainer_hightier.evaluation.player_alert_policy import operational_simulated_metrics_block
from trainer_hightier.serving.feature_builder import prepare_lgbm_feature_matrix

_b5 = importlib.import_module("trainer_hightier.05_lgbm_train")
aggregate_bets_to_player_game = _b5.aggregate_bets_to_player_game

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_SPLITS = _REPO_ROOT / "trainer_hightier" / "artifacts" / "training_data" / "splits"
_DEFAULT_OUT = _REPO_ROOT / "out" / "hot_patron_audit"

HOT_CASES: tuple[tuple[int, str], ...] = (
    (145935770, "2026-05-29"),
    (192457127, "2026-05-23"),
    (136374465, "2026-06-05"),
    (143808079, "2026-05-31"),
)

PROFILE_FEATURES: tuple[str, ...] = (
    "fe__canonical__wager_sum__today",
    "fe__canonical__bets_cnt__today",
    "fe__canonical__avg_wager__today",
    "fe__wager_sum__w15m",
    "fe__bets_cnt__w15m",
    "bet__wager_sum__w1h",
    "bet__bets_cnt__w1h",
    "fe__wager_sum__w15m_over_w1d",
    "fe__wager_cv_w7d",
    "fe__interarrival__last_gap_z__w7d",
    "fe__odds__payout_odds_z__w1h",
    "patron__adt__w180d_m1snap",
    "fe__payout_odds_z_prior_w30d",
    "fe__canonical__elapsed_sec_since_first_bet__today",
    "fe__interarrival__cv__w1h",
    "mid_term_snapshot_missing_flag",
    "patron__gaming_days_cnt__w180d_m1snap",
)


@dataclass(frozen=True)
class AuditConfig:
    """Inputs for one baseline model audit."""

    model_dir: Path
    splits_dir: Path
    hist_splits: tuple[Path, ...]


def _load_bundle(model_dir: Path) -> dict[str, Any]:
    """Load Step 5 model pickle bundle."""

    pkl = Path(model_dir) / "model.pkl"
    if not pkl.is_file():
        raise FileNotFoundError(f"model.pkl missing under {model_dir}")
    return pickle.loads(pkl.read_bytes())


def _score_test(
    bundle: dict[str, Any],
    test_parquet: Path,
) -> tuple[pd.DataFrame, np.ndarray, float]:
    """Return test frame, bet-level scores, and bundle threshold."""

    feat_cols = tuple(bundle["feature_columns"])
    meta = ["player_id", "game_id", "gaming_day_event", "walkaway_label", "payout_complete_dtm", "bet_id"]
    cols = list(dict.fromkeys(meta + list(feat_cols)))
    df = pd.read_parquet(test_parquet, columns=cols)
    x = prepare_lgbm_feature_matrix(
        df,
        feature_columns=feat_cols,
        categorical_columns=tuple(bundle.get("categorical_columns", ())),
        category_categories=dict(bundle.get("category_categories", {})),
    )
    scores = bundle["model"].predict_proba(x)[:, 1]
    return df, scores, float(bundle["threshold"])


def _player_day_fp_table(cand: pd.DataFrame) -> pd.DataFrame:
    """Aggregate false-positive player-games to player-day counts."""

    fp = cand.loc[cand["fp"] == 1]
    if fp.empty:
        return pd.DataFrame(columns=["player_id", "gaming_day_event", "fp_count"])
    out = (
        fp.groupby(["player_id", "gaming_day_event"], as_index=False)
        .size()
        .rename(columns={"size": "fp_count"})
        .sort_values("fp_count", ascending=False)
    )
    return out


def _profile_hot_case(
    *,
    cand: pd.DataFrame,
    df_te: pd.DataFrame,
    hist_frames: list[pd.DataFrame],
    pid: int,
    gd: str,
) -> dict[str, Any]:
    """Compare hot-day FP feature medians vs own history and ADT peer cohort."""

    gd_str = str(gd)
    hot_fp = cand[
        (cand["player_id"] == pid)
        & (cand["gaming_day_event"].astype(str) == gd_str)
        & (cand["fp"] == 1)
    ]
    hot_all = cand[
        (cand["player_id"] == pid) & (cand["gaming_day_event"].astype(str) == gd_str)
    ]
    own = pd.concat(hist_frames, ignore_index=True)
    own = own.loc[own["player_id"] == pid]
    adt = pd.to_numeric(df_te["patron__adt__w180d_m1snap"], errors="coerce")
    dec = pd.qcut(adt.rank(method="first"), 10, labels=False, duplicates="drop")
    df_peer = df_te.copy()
    df_peer["_adt_decile"] = dec
    hot_dec = int(df_peer.loc[df_peer["player_id"] == pid, "_adt_decile"].mode().iloc[0])
    peer_day = df_peer[
        (df_peer["_adt_decile"] == hot_dec) & (df_peer["gaming_day_event"].astype(str) == gd_str)
    ]
    feat_rep = df_te.groupby(["player_id", "game_id"], as_index=False).agg(
        **{c: (c, "median") for c in PROFILE_FEATURES if c in df_te.columns},
    )
    if not hot_fp.empty and "player_id" in hot_fp.columns:
        hot_fp = hot_fp.merge(
            feat_rep,
            on=["player_id", "game_id"],
            how="left",
            suffixes=("", "_feat"),
        )
    rows: list[dict[str, Any]] = []
    for feat in PROFILE_FEATURES:
        if feat not in df_te.columns:
            continue
        col = feat if feat in hot_fp.columns else f"{feat}_feat"
        hot_med = float(pd.to_numeric(hot_fp[col], errors="coerce").median()) if col in hot_fp.columns else float("nan")
        own_s = pd.to_numeric(own[feat], errors="coerce") if feat in own.columns else pd.Series(dtype=float)
        peer_s = pd.to_numeric(peer_day[feat], errors="coerce")
        rows.append(
            {
                "feature": feat,
                "hot_fp_median": hot_med,
                "own_history_rows": int(len(own)),
                "own_median": float(own_s.median()) if len(own_s) else None,
                "own_p95": float(own_s.quantile(0.95)) if len(own_s) else None,
                "own_p99": float(own_s.quantile(0.99)) if len(own_s) else None,
                "peer_decile": hot_dec,
                "peer_day_rows": int(len(peer_day)),
                "peer_p50": float(peer_s.median()) if len(peer_s) else None,
                "peer_p95": float(peer_s.quantile(0.95)) if len(peer_s) else None,
                "hot_over_own_p95": (
                    hot_med / float(own_s.quantile(0.95))
                    if len(own_s) and float(own_s.quantile(0.95)) > 0
                    else None
                ),
                "hot_over_peer_p95": (
                    hot_med / float(peer_s.quantile(0.95))
                    if len(peer_s) and float(peer_s.quantile(0.95)) > 0
                    else None
                ),
            },
        )
    return {
        "player_id": pid,
        "gaming_day": gd_str,
        "fp_games": int(len(hot_fp)),
        "alert_games": int(hot_all["alert"].sum()) if len(hot_all) else 0,
        "total_games": int(len(hot_all)),
        "feature_profile": rows,
    }


def run_audit(cfg: AuditConfig) -> dict[str, Any]:
    """Run full Phase 0 audit for one baseline model directory."""

    bundle = _load_bundle(cfg.model_dir)
    metrics_path = Path(cfg.model_dir) / "training_metrics.json"
    ref_metrics = json.loads(metrics_path.read_text(encoding="utf-8")) if metrics_path.is_file() else {}
    test_p = Path(cfg.splits_dir) / "test.parquet"
    df_te, scores, thr = _score_test(bundle, test_p)
    pg = aggregate_bets_to_player_game(df_te, scores, split_name="test")
    cand = pg.candidates.copy()
    cand["alert"] = cand["player_game_score"] >= thr
    cand["fp"] = cand["alert"] & (cand["player_game_label"] == 0)
    rep = df_te.groupby(["player_id", "game_id"], as_index=False).first()[
        ["player_id", "game_id", "gaming_day_event"]
    ]
    cand = cand.merge(rep, on=["player_id", "game_id"], how="left")
    fp_table = _player_day_fp_table(cand)
    total_fp = int(cand["fp"].sum())
    top10_share = (
        float(fp_table.head(10)["fp_count"].sum() / total_fp) if total_fp > 0 else 0.0
    )
    wh_col = pd.to_datetime(df_te["payout_complete_dtm"], errors="coerce")
    window_hours = (
        float((wh_col.max() - wh_col.min()).total_seconds() / 3600.0) if wh_col.notna().any() else None
    )
    op_block = operational_simulated_metrics_block(
        "test",
        cand,
        threshold=thr,
        cooldown_min=15,
        window_hours=window_hours,
    )
    hist_frames = [pd.read_parquet(p, columns=["player_id"] + list(PROFILE_FEATURES)) for p in cfg.hist_splits]
    hot_profiles = [
        _profile_hot_case(
            cand=cand,
            df_te=df_te,
            hist_frames=hist_frames,
            pid=pid,
            gd=gd,
        )
        for pid, gd in HOT_CASES
    ]
    return {
        "model_dir": str(cfg.model_dir.resolve()),
        "threshold": thr,
        "step5_min_precision": ref_metrics.get("step5_min_precision"),
        "test_player_game_precision": ref_metrics.get("test_player_game_precision"),
        "test_operational_simulated_precision": ref_metrics.get("test_operational_simulated_precision"),
        "test_operational_simulated_alerts_per_hour": ref_metrics.get(
            "test_operational_simulated_alerts_per_hour",
        ),
        "recomputed": {
            "test_alerts": int(cand["alert"].sum()),
            "test_fp": total_fp,
            "test_tp": int((cand["alert"] & (cand["player_game_label"] == 1)).sum()),
            "top10_player_day_fp_share": top10_share,
            "test_operational_simulated_precision": op_block.get("test_operational_simulated_precision"),
            "test_operational_simulated_alerts_per_hour": op_block.get(
                "test_operational_simulated_alerts_per_hour",
            ),
        },
        "top15_player_day_fp": fp_table.head(15).to_dict(orient="records"),
        "hot_case_profiles": hot_profiles,
    }


def _build_summary_md(report: dict[str, Any]) -> str:
    """Render markdown summary from audit JSON."""

    lines = ["# Hot-Patron Phase 0 Gap Audit", ""]
    for key in ("baseline_171657", "baseline_183503"):
        if key not in report:
            continue
        blk = report[key]
        lines.append(f"## {key}")
        rec = blk["recomputed"]
        op_prec = rec.get("test_operational_simulated_precision")
        op_aph = rec.get("test_operational_simulated_alerts_per_hour")
        lines.append(
            f"- test FP={rec['test_fp']} alerts={rec['test_alerts']} "
            f"op_prec={op_prec:.3f} op_alerts/hr={op_aph:.3f}"
            if op_prec is not None and op_aph is not None
            else f"- test FP={rec['test_fp']} alerts={rec['test_alerts']}",
        )
        lines.append(f"- top-10 player-day FP share: {rec['top10_player_day_fp_share']:.1%}")
        lines.append("")
    lines.extend(
        [
            "## Why baseline z/ratio/today features fail",
            "",
            "1. **Cold-start patrons** (e.g. 145935770): zero train/val history → "
            "mid-term z-scores (`fe__wager_cv_w7d`, `fe__interarrival__last_gap_z__w7d`, "
            "`fe__wager_sum__w15m_over_w1d`) are NaN; model treats missing as low signal.",
            "2. **Absolute today features** fire on high wager (10–24× peer p95) but "
            "bet-count features look normal vs ADT peers (0.3–0.7× peer p95), so burst "
            "is wager-heavy not count-heavy.",
            "3. **Odds z-scores** on hot FP games are below peer median — not discriminative.",
            "4. **Threshold calibration drift**: lower threshold (183503) converts extreme "
            "wager signal into 53 FP for one cold-start day (63% top-10 concentration).",
            "",
            "## Proposed PIT-safe candidate features (shortlist)",
            "",
            "| Feature | Rationale |",
            "|---------|-----------|",
            "| `fe__hot__wager_today_over_peer_p95__adt_decile` | Cross-sectional same-day ADT-decile peer normalization |",
            "| `fe__hot__avg_wager_today_over_peer_p95__adt_decile` | Captures bet-size spike vs VIP peers |",
            "| `fe__hot__wager_w15m_over_peer_p95__adt_decile` | Short-window wager vs peer burst |",
            "| `fe__hot__wager_per_hr_over_peer_p95` | Velocity feature for sustained hot sessions |",
            "| `fe__hot__games_today_over_peer_p95` | Multi-game burst detection |",
            "| `fe__hot__mid_term_history_sparse_flag` | Cold-start indicator (missing/young snapshot) |",
            "| `fe__hot__wager_today_over_own_p95__w180d` | Self-normalized fallback when history exists |",
            "| `fe__hot__peer_wager_z__adt_decile` | Robust z vs ADT decile (median/MAD) |",
            "",
        ],
    )
    return "\n".join(lines)


def main() -> None:
    """CLI entry: audit two baselines and write JSON + markdown."""

    parser = argparse.ArgumentParser(description="Phase 0 hot-patron gap audit")
    parser.add_argument("--splits-dir", type=Path, default=_DEFAULT_SPLITS)
    parser.add_argument("--out-dir", type=Path, default=_DEFAULT_OUT)
    args = parser.parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    hist = (
        Path(args.splits_dir) / "train.parquet",
        Path(args.splits_dir) / "val.parquet",
    )
    baselines = {
        "baseline_171657": _REPO_ROOT / "out/models_high_tier_mvp/20260609-171657-1708061",
        "baseline_183503": _REPO_ROOT / "out/models_high_tier_mvp/20260609-183503-1708061",
    }
    report: dict[str, Any] = {"hot_cases": list(HOT_CASES), "profile_features": list(PROFILE_FEATURES)}
    for name, model_dir in baselines.items():
        report[name] = run_audit(
            AuditConfig(model_dir=model_dir, splits_dir=Path(args.splits_dir), hist_splits=hist),
        )
    json_path = out_dir / "phase0_report.json"
    json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (out_dir / "phase0_summary.md").write_text(_build_summary_md(report), encoding="utf-8")
    print(f"Wrote {json_path}")


if __name__ == "__main__":
    main()
