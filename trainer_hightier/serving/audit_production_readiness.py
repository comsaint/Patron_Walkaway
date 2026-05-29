"""Production RCA: Feast mid/slow coverage vs deploy smoke and prediction log.

Run on the deploy host (same layout as ``deploy/main.py``):

    python -m trainer_hightier.serving.audit_production_readiness \\
        --bundle-dir /path/to/deploy_bundle \\
        --prediction-log /path/to/gmwds_deploy_predict_log.csv \\
        --output-json ./artifacts/feast/production_audit.json

Loads optional ``.env`` from the bundle root (does not override existing env).
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from trainer_hightier.config import (
    HightierServingConfig,
    apply_hightier_serving_environ_overrides,
    set_hightier_serving_deploy_override,
)
from trainer_hightier.feature_experiment.feature_cadence import _MID_TERM_COMPOSITE_FEAST_DEPS
from trainer_hightier.serving.feast_online_adapter import FeastSdkOnlineAdapter
from trainer_hightier.serving.feast_readiness import (
    evaluate_feast_readiness_gate,
    load_feast_online_readiness,
    resolve_feast_readiness_path,
    run_allowlist_feast_lookup_smoke,
    run_deploy_feast_readiness_check,
)
from trainer_hightier.serving.feature_supply import (
    build_scorer_supplier_plan,
    load_frozen_registry_for_bundle,
)
from trainer_hightier.serving.model_bundle import load_hightier_model_bundle

logger = logging.getLogger(__name__)

_MODEL_MID_COLS = (
    "fe__bets_cnt__w1d",
    "fe__wager_sum__w15m_over_w1d",
    "fe__wager_cv_w7d",
    "fe__payout_odds_z_prior_w30d",
    "fe__interarrival__last_gap_z__w7d",
)


def _load_bundle_rel(bundle_root: Path) -> dict[str, Any]:
    """Read ``deploy_bundle_paths.json`` from the deploy bundle root."""
    p = bundle_root / "deploy_bundle_paths.json"
    if not p.is_file():
        raise FileNotFoundError(f"deploy_bundle_paths.json missing under {bundle_root}")
    raw = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("deploy_bundle_paths.json must be a JSON object")
    return raw


def _serving_config_for_bundle(bundle_root: Path, rel: dict[str, Any]) -> HightierServingConfig:
    """Mirror ``deploy/main.py`` path layout for serving modules."""
    br = bundle_root.resolve()
    ls = rel.get("local_state_dir", "local_state")
    feast_art = rel.get("feast_artifacts_dir", "artifacts/feast")
    feast_repo = rel.get("feast_repo_dir", "feast_repo")
    base = HightierServingConfig()
    return replace(
        base,
        state_db_path=br / ls / "state.db",
        prediction_log_db_path=br / ls / "prediction_log.db",
        feature_state_db_path=br / ls / "feature_state.db",
        snapshot_manifest_dir=br / rel.get("snapshot_manifest_dir", "snapshots"),
        adt_allowed_players_parquet=(
            br / rel.get("adt_allowlist_parquet", "mapping/adt_allowed_players_q0p99.parquet")
        ).resolve(),
        scorer_feast_repo_path=(br / feast_repo).resolve(),
        scorer_feast_readiness_path=(
            br / rel.get("feast_readiness_path", f"{feast_art}/feast_online_readiness.json")
        ).resolve(),
    )


def _load_dotenv(bundle_root: Path) -> None:
    """Load bundle ``.env`` when python-dotenv is installed."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    path = bundle_root / ".env"
    if path.is_file():
        load_dotenv(path, override=False)


def _all_canonical_ids(
    allowlist_parquet: Path,
    mapping_parquet: Path,
    *,
    max_canonicals: int | None,
) -> list[str]:
    """Distinct canonical ids for ADT allowlist players (ordered)."""
    allow_esc = str(allowlist_parquet.resolve()).replace("\\", "/").replace("'", "''")
    cmap_esc = str(mapping_parquet.resolve()).replace("\\", "/").replace("'", "''")
    import duckdb

    limit_sql = f"LIMIT {int(max_canonicals)}" if max_canonicals is not None else ""
    sql = f"""
SELECT DISTINCT TRIM(CAST(c.canonical_id AS VARCHAR)) AS canonical_id
FROM read_parquet('{allow_esc}') AS a
INNER JOIN read_parquet('{cmap_esc}') AS c
  ON TRY_CAST(a.player_id AS BIGINT) = TRY_CAST(c.player_id AS BIGINT)
WHERE TRIM(CAST(c.canonical_id AS VARCHAR)) <> ''
ORDER BY canonical_id
{limit_sql}
""".strip()
    df = duckdb.sql(sql).fetchdf()
    if df.empty:
        return []
    return [str(x).strip() for x in df["canonical_id"].tolist() if str(x).strip()]


def _feast_audit_column_sets(plan_mid: tuple[str, ...], plan_slow: tuple[str, ...], plan_composite: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Mid/slow column sets for Feast lookup (model plan + composite upstream)."""
    mid = list(plan_mid)
    for comp in plan_composite:
        for dep in _MID_TERM_COMPOSITE_FEAST_DEPS.get(comp, ()):
            if dep not in mid:
                mid.append(dep)
    return tuple(dict.fromkeys(mid)), tuple(dict.fromkeys(plan_slow))


def _lookup_feast_batched(
    adapter: FeastSdkOnlineAdapter,
    canonical_ids: list[str],
    *,
    mid_columns: tuple[str, ...],
    slow_columns: tuple[str, ...],
    batch_size: int,
) -> pd.DataFrame:
    """Batch ``lookup_mid_slow`` to limit peak memory and Feast payload size."""
    if not canonical_ids:
        return pd.DataFrame(columns=["canonical_id", *mid_columns, *slow_columns])
    chunks: list[pd.DataFrame] = []
    bs = max(1, int(batch_size))
    for i in range(0, len(canonical_ids), bs):
        batch = canonical_ids[i : i + bs]
        chunks.append(
            adapter.lookup_mid_slow(batch, mid_columns=mid_columns, slow_columns=slow_columns)
        )
    return pd.concat(chunks, ignore_index=True)


def _summarize_feast_lookup(
    canonical_ids: list[str],
    lookup_df: pd.DataFrame,
    *,
    mid_columns: tuple[str, ...],
    slow_columns: tuple[str, ...],
    entity_missing_fail_fraction: float,
) -> dict[str, Any]:
    """Entity-missing and per-column null rates (same semantics as deploy smoke)."""
    wanted = tuple(dict.fromkeys([*mid_columns, *slow_columns]))
    n = len(canonical_ids)
    if n == 0:
        raise ValueError("[audit] zero canonical ids in allowlist sample")
    n_entity_missing = 0
    cell_null_counts: dict[str, int] = {c: 0 for c in wanted}
    if lookup_df.empty or "canonical_id" not in lookup_df.columns:
        n_entity_missing = n
        for col in wanted:
            cell_null_counts[col] = n
    else:
        lk = lookup_df.drop_duplicates(subset=["canonical_id"], keep="last")
        present = set(lk["canonical_id"].astype(str).str.strip().tolist())
        for cid in canonical_ids:
            if cid not in present:
                n_entity_missing += 1
        for col in wanted:
            if col not in lk.columns:
                cell_null_counts[col] = n
            else:
                cell_null_counts[col] = int(lk[col].isna().sum())
    rate = float(n_entity_missing) / float(n)
    null_rates = {c: round(cell_null_counts[c] / float(n), 4) for c in wanted}
    model_mid_null = {c: null_rates.get(c, 1.0) for c in _MODEL_MID_COLS if c in wanted or c in mid_columns}
    return {
        "n_canonical": n,
        "n_entity_missing": n_entity_missing,
        "entity_missing_rate": round(rate, 4),
        "entity_missing_fail_fraction": float(entity_missing_fail_fraction),
        "entity_missing_ok": rate <= float(entity_missing_fail_fraction),
        "cell_null_counts": cell_null_counts,
        "cell_null_rates": null_rates,
        "model_mid_null_rates": model_mid_null,
    }


def _feast_lookup_error_payload(exc: BaseException) -> dict[str, Any]:
    """Structured failure when Feast online store is not materialized."""
    return {
        "ok": False,
        "error": str(exc),
        "hint": "在 bundle 上執行 startup feast refresh（feast apply + materialize）後重跑本腳本",
    }


def _audit_readiness_and_smoke(
    cfg: HightierServingConfig,
    *,
    allowlist: Path,
    mapping: Path,
    mid_cols: tuple[str, ...],
    slow_cols: tuple[str, ...],
) -> dict[str, Any]:
    """Readiness JSON, deploy gate, and small-sample smoke (production default)."""
    path = resolve_feast_readiness_path(cfg)
    readiness = load_feast_online_readiness(path)
    gate = evaluate_feast_readiness_gate(
        readiness,
        require_mid=True,
        require_slow=True,
        readiness_path=path,
        close_hour=int(cfg.gaming_day_close_hour),
        mid_hard_cap_days=int(cfg.mid_term_stale_hard_cap_days),
        slow_hard_cap_days=int(cfg.slow_stale_hard_cap_days),
        slow_grace_days=int(cfg.slow_monthly_grace_days),
    )
    smoke: dict[str, Any] | None = None
    deploy_check_ok: bool | None = None
    if cfg.scorer_feast_repo_path is None:
        raise ValueError("[audit] scorer_feast_repo_path not set on serving config")
    feast_repo = Path(cfg.scorer_feast_repo_path)
    try:
        smoke = run_allowlist_feast_lookup_smoke(
            feast_repo=feast_repo,
            allowlist_parquet=allowlist,
            canonical_mapping_parquet=mapping,
            mid_columns=mid_cols,
            slow_columns=slow_cols,
            sample_size=int(cfg.scorer_feast_deploy_lookup_smoke_sample_size),
            entity_missing_fail_fraction=float(cfg.scorer_feast_entity_missing_fail_fraction),
        )
        deploy_check_ok = run_deploy_feast_readiness_check(
            require_mid=True,
            require_slow=True,
            allowlist_parquet=allowlist,
            canonical_mapping_parquet=mapping,
            mid_columns=mid_cols,
            slow_columns=slow_cols,
            run_lookup_smoke=True,
        ).ok
    except Exception as exc:
        logger.warning("[audit] deploy smoke skipped: %s", exc)
        smoke = _feast_lookup_error_payload(exc)
    return {
        "readiness_path": str(path),
        "readiness_exists": path.is_file(),
        "readiness_doc": readiness.to_dict() if readiness is not None else None,
        "freshness_gate_ok": gate.ok,
        "freshness_gate_reason": gate.hard_failure_reason,
        "deploy_smoke_sample_size": int(cfg.scorer_feast_deploy_lookup_smoke_sample_size),
        "deploy_smoke": smoke,
        "deploy_check_ok": deploy_check_ok,
    }


def _audit_prediction_log(log_path: Path, *, threshold: float) -> dict[str, Any]:
    """Score / missing-family stats from exported production CSV."""
    usecols = [
        "score",
        "is_alert",
        "threshold",
        "model_features_missing",
        "missing_family_json",
        "scoring_status",
        "mid_term_freshness_status",
        "slow_freshness_status",
        "snapshot_scoring_degraded",
    ]
    df = pd.read_csv(log_path, usecols=lambda c: c in usecols)
    n = len(df)
    if n == 0:
        return {"n_rows": 0, "error": "empty prediction log"}
    scores = pd.to_numeric(df["score"], errors="coerce")
    thr = float(df["threshold"].iloc[0]) if "threshold" in df.columns else threshold
    miss = pd.to_numeric(df.get("model_features_missing"), errors="coerce").fillna(0)
    alert = pd.to_numeric(df.get("is_alert"), errors="coerce").fillna(0)
    pct = lambda s, q: float(np.nanpercentile(s.dropna(), q)) if s.notna().any() else None
    family_ctr: Counter[str] = Counter()
    for raw in df.get("missing_family_json", pd.Series(dtype=object)).dropna():
        try:
            obj = json.loads(str(raw))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            for k, v in obj.items():
                if int(v or 0) > 0:
                    family_ctr[str(k)] += 1
    return {
        "n_rows": n,
        "threshold_in_log": thr,
        "alert_rate": round(float(alert.mean()), 6),
        "n_alerts": int(alert.sum()),
        "model_features_missing_rate": round(float((miss > 0).mean()), 4),
        "score_p50": pct(scores, 50),
        "score_p90": pct(scores, 90),
        "score_p99": pct(scores, 99),
        "score_max": float(scores.max()) if scores.notna().any() else None,
        "n_below_threshold": int((scores < thr).sum()),
        "missing_family_row_counts": dict(family_ctr.most_common(20)),
        "scoring_status_counts": df["scoring_status"].value_counts().to_dict()
        if "scoring_status" in df.columns
        else {},
        "mid_freshness_status_counts": df["mid_term_freshness_status"].value_counts().to_dict()
        if "mid_term_freshness_status" in df.columns
        else {},
    }


def _build_verdict(
    *,
    readiness: dict[str, Any],
    full_lookup: dict[str, Any],
    log_audit: dict[str, Any] | None,
    threshold: float,
) -> dict[str, Any]:
    """Ranked hypotheses for low alert rate / high missing features."""
    findings: list[str] = []
    severity = "ok"
    if not readiness.get("readiness_exists"):
        findings.append("feast_online_readiness.json 不存在 — startup refresh 可能未跑完")
        severity = "critical"
    elif not readiness.get("freshness_gate_ok"):
        findings.append(f"readiness gate 失敗: {readiness.get('freshness_gate_reason')}")
        severity = "critical"
    if full_lookup.get("error"):
        findings.append(f"Feast 全量 lookup 失敗: {full_lookup.get('error')}")
        severity = "critical"
    smoke = readiness.get("deploy_smoke") or {}
    full_rate = float(full_lookup.get("entity_missing_rate", 0.0))
    smoke_rate = float(smoke.get("entity_missing_rate", 0.0)) if smoke.get("ok") is not False else 0.0
    if full_rate > float(full_lookup.get("entity_missing_fail_fraction", 0.10)):
        findings.append(
            f"全量 allowlist entity_missing_rate={full_rate:.2%} 超過 scorer 容忍 "
            f"{full_lookup.get('entity_missing_fail_fraction')}"
        )
        severity = "critical"
    elif full_rate > smoke_rate + 0.05:
        findings.append(
            f"全量覆蓋率 ({full_rate:.2%}) 明顯差於 deploy smoke 抽樣 ({smoke_rate:.2%}, n={smoke.get('sample_size')})"
        )
        if severity == "ok":
            severity = "warn"
    for col, rate in sorted(
        (full_lookup.get("model_mid_null_rates") or {}).items(),
        key=lambda x: -x[1],
    ):
        if rate >= 0.15:
            findings.append(f"Feast mid 欄位 {col!r} null rate={rate:.1%}（allowlist 全量）")
            if severity == "ok":
                severity = "warn"
    if log_audit:
        p99 = log_audit.get("score_p99")
        if p99 is not None and p99 < threshold * 0.7:
            findings.append(
                f"production score p99={p99:.4f} 遠低於 threshold={threshold:.4f} — 主因為分數左移，非 threshold 錯誤"
            )
            if severity == "ok":
                severity = "warn"
        fam = log_audit.get("missing_family_row_counts") or {}
        top = next(iter(fam), None)
        if top and fam.get(top, 0) > log_audit.get("n_rows", 1) * 0.1:
            findings.append(f"log missing_family 最常見: {top} ({fam[top]} rows)")
    if not findings:
        findings.append("未偵測到 readiness / Feast 覆蓋的明顯異常；若 alert 仍低，優先查 short-term pool（6h）與 fe_derived 覆蓋")
    return {"severity": severity, "findings": findings}


def run_audit(argv: list[str] | None = None) -> int:
    """CLI entry: write JSON report and print summary."""
    pr = argparse.ArgumentParser(description="Production Feast + prediction-log RCA")
    pr.add_argument("--bundle-dir", type=Path, required=True, help="deploy bundle root")
    pr.add_argument("--prediction-log", type=Path, default=None, help="exported prediction_log CSV")
    pr.add_argument("--output-json", type=Path, default=None, help="write machine-readable report")
    pr.add_argument("--batch-size", type=int, default=500, help="Feast lookup batch size")
    pr.add_argument(
        "--max-canonicals",
        type=int,
        default=None,
        help="cap allowlist audit size (debug); default = full allowlist",
    )
    args = pr.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    bundle_root = Path(args.bundle_dir).resolve()
    rel = _load_bundle_rel(bundle_root)
    _load_dotenv(bundle_root)
    cfg = apply_hightier_serving_environ_overrides(_serving_config_for_bundle(bundle_root, rel))
    set_hightier_serving_deploy_override(cfg)
    model_dir = bundle_root / rel.get("model_bundle_dir", "models")
    bundle = load_hightier_model_bundle(bundle_dir=model_dir)
    snap = load_frozen_registry_for_bundle(model_dir)
    plan = build_scorer_supplier_plan(snap, bundle.feature_columns)
    mid_cols, slow_cols = _feast_audit_column_sets(
        plan.feast_mid_cols, plan.feast_slow_cols, plan.mid_composite_cols
    )
    allowlist = cfg.adt_allowed_players_parquet
    mapping = bundle_root / rel.get("canonical_mapping_parquet", "mapping/canonical_player_mapping.parquet")
    if allowlist is None or not Path(allowlist).is_file():
        raise FileNotFoundError(f"adt allowlist parquet missing: {allowlist}")
    if not mapping.is_file():
        raise FileNotFoundError(f"canonical mapping missing: {mapping}")
    feast_repo = cfg.scorer_feast_repo_path
    if feast_repo is None or not Path(feast_repo).is_dir():
        raise FileNotFoundError(f"feast_repo missing under bundle: {feast_repo}")
    report: dict[str, Any] = {
        "bundle_dir": str(bundle_root),
        "model_version": bundle.model_version,
        "threshold": bundle.threshold,
        "model_feature_count": len(bundle.feature_columns),
        "supplier_plan": {
            "feast_mid_cols": list(plan.feast_mid_cols),
            "mid_composite_cols": list(plan.mid_composite_cols),
            "feast_slow_cols": list(plan.feast_slow_cols),
            "short_term_cols": list(plan.short_term_cols),
        },
        "config": {
            "hot_feature_pool_lookback_hours": int(cfg.hot_feature_pool_lookback_hours),
            "scorer_feast_deploy_lookup_smoke_sample_size": int(
                cfg.scorer_feast_deploy_lookup_smoke_sample_size
            ),
            "scorer_feast_entity_missing_fail_fraction": float(
                cfg.scorer_feast_entity_missing_fail_fraction
            ),
            "production_fe_coverage_hours": int(cfg.production_fe_coverage_hours),
        },
    }
    report["readiness"] = _audit_readiness_and_smoke(
        cfg, allowlist=Path(allowlist), mapping=mapping, mid_cols=mid_cols, slow_cols=slow_cols
    )
    cids = _all_canonical_ids(Path(allowlist), mapping, max_canonicals=args.max_canonicals)
    logger.info("[audit] allowlist canonical_ids n=%d", len(cids))
    try:
        adapter = FeastSdkOnlineAdapter(feast_repo=Path(feast_repo))
        lookup_df = _lookup_feast_batched(
            adapter,
            cids,
            mid_columns=mid_cols,
            slow_columns=slow_cols,
            batch_size=int(args.batch_size),
        )
        report["allowlist_feast_full_lookup"] = _summarize_feast_lookup(
            cids,
            lookup_df,
            mid_columns=mid_cols,
            slow_columns=slow_cols,
            entity_missing_fail_fraction=float(cfg.scorer_feast_entity_missing_fail_fraction),
        )
    except Exception as exc:
        logger.warning("[audit] full allowlist lookup skipped: %s", exc)
        report["allowlist_feast_full_lookup"] = _feast_lookup_error_payload(exc)
    if args.prediction_log is not None:
        report["prediction_log"] = _audit_prediction_log(
            Path(args.prediction_log), threshold=float(bundle.threshold)
        )
    report["verdict"] = _build_verdict(
        readiness=report["readiness"],
        full_lookup=report["allowlist_feast_full_lookup"],
        log_audit=report.get("prediction_log"),
        threshold=float(bundle.threshold),
    )
    text = json.dumps(report, indent=2, default=str)
    print(text)
    if args.output_json is not None:
        out = Path(args.output_json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        logger.info("[audit] wrote %s", out)
    return 1 if report["verdict"]["severity"] == "critical" else 0


if __name__ == "__main__":
    raise SystemExit(run_audit())
