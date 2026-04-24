"""評估 runner：smoke 與後續 Tier 產物寫入 ``results/<run_id>/``。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Mapping, cast

import numpy as np
import pandas as pd

from ..baseline_config import load_run_config_yaml
from ..data_contract import (
    apply_time_window,
    load_baseline_frame,
    temporal_train_valid_test_split,
    validate_label_and_censor_columns,
)
from ..feature_views import select_feature_subset
from ..models.logistic_baseline import fit_logistic_baseline, predict_proba_positive
from ..models.sgd_baseline import fit_sgd_baseline, predict_proba_positive as predict_sgd_proba_positive
from ..rules.adt_rules import (
    AdtVariant,
    R3_ADT_VARIANTS,
    adt_rule_scores,
    r3_formula_specs,
    r3_model_type_for_metrics,
    r3_run_state_block,
)
from ..rules.loss_rules import LossProxy, loss_rule_scores
from ..rules.pace_rules import pace_rule_scores
from ..rules.single_feature_rank import single_feature_scores
from .metrics import build_eval_metrics_row, build_smoke_metrics_row
from .reference_model import (
    build_reference_lightgbm_snapshot,
    markdown_lightgbm_peer_table,
)


def default_results_dir(run_id: str, repo_root: Path | None = None) -> Path:
    """回傳 ``baseline_models/results/<run_id>/`` 絕對路徑。

    Args:
        run_id: 本次實驗目錄名（EXECUTION_PLAN §0.4）。
        repo_root: 倉庫根目錄；若為 ``None`` 則以本檔所在位置推算 ``baseline_models/``。

    Returns:
        結果目錄路徑（不一定已建立）。
    """
    if repo_root is None:
        baseline_pkg = Path(__file__).resolve().parents[2]
        return baseline_pkg / "results" / run_id
    return repo_root / "baseline_models" / "results" / run_id


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_json(path: Path, obj: Mapping[str, Any] | list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _log_phase(msg: str) -> None:
    """印出進度列至 stdout（供 ``python -m baseline_models`` 觀察）。"""
    print(f"[baseline_models] {msg}", flush=True)


def run_smoke(config_path: str, run_id: str) -> dict[str, Any]:
    """最小 smoke：載入／切分、於 **test** 上算 DEC-026 指標、寫三件套。

    Args:
        config_path: YAML 設定路徑。
        run_id: 結果子目錄名。

    Returns:
        摘要 dict（含 ``results_dir``）。

    Raises:
        各種契約錯誤：見 ``data_contract``／設定載入。
    """
    t0 = time.perf_counter()
    cfg_path = Path(config_path).resolve()
    _log_phase(f"開始 run_id={run_id!r}；設定檔={cfg_path}")
    cfg = load_run_config_yaml(config_path)
    _log_phase(
        f"已載入設定：experiment_id={cfg.experiment_id!r}；"
        f"data_source={cfg.data_source_kind!r}"
        + (
            f"；path={cfg.data_source_path!r}"
            if cfg.data_source_path
            else ""
        )
    )
    raw_frame = load_baseline_frame(cfg)
    _log_phase(f"已載入資料表：列數={len(raw_frame)}")
    tw = cfg.data_window
    frame = apply_time_window(raw_frame, cfg.column_event_time, tw)
    _tc = cfg.column_event_time
    if _tc in frame.columns and not frame.empty:
        _ts = pd.to_datetime(frame[_tc], errors="coerce")
        _log_phase(
            f"時間窗過濾後：列數={len(frame)}；{_tc} min={_ts.min()} max={_ts.max()}"
        )
    else:
        _log_phase(f"時間窗過濾後：列數={len(frame)}")
    clean = validate_label_and_censor_columns(
        frame, cfg.column_label, cfg.column_censored
    )
    _log_phase(
        f"排除 censored 後：列數={len(clean)}；"
        f"切分 train_frac={cfg.split_train_frac} valid_frac={cfg.split_valid_frac}"
    )
    train_df, _valid_df, test_df, split_spec = temporal_train_valid_test_split(
        clean,
        cfg.column_event_time,
        cfg.split_train_frac,
        cfg.split_valid_frac,
    )
    _log_phase(
        "時序切分完成："
        f"train={len(train_df)} valid={len(_valid_df)} test={len(test_df)}；"
        f"train_end={split_spec.train_end_exclusive!s} "
        f"valid_end={split_spec.valid_end_exclusive!s}"
    )
    score_col = cfg.column_score or "smoke_score"
    if score_col not in test_df.columns:
        raise KeyError(f"test 分數欄不存在: {score_col!r}；columns={list(test_df.columns)!r}")
    y_true = np.asarray(test_df[cfg.column_label].values, dtype=float)
    y_score = np.asarray(pd.to_numeric(test_df[score_col], errors="raise").values, dtype=float)
    exp_id = cfg.experiment_id or f"smoke_{run_id}"
    elapsed = time.perf_counter() - t0
    rows: list[dict[str, Any]] = []
    row = build_smoke_metrics_row(
        experiment_id=exp_id,
        split_protocol=cfg.split_protocol_name,
        feature_set_version=cfg.feature_set_version,
        label_contract_version=cfg.label_contract_version,
        data_window=tw,
        y_true=y_true,
        y_score=y_score,
        min_alert_count=cfg.threshold_min_alert_count,
        recall_floor=cfg.threshold_recall_floor,
        min_alerts_per_hour=cfg.threshold_min_alerts_per_hour,
        window_hours=cfg.threshold_window_hours,
        runtime_sec=elapsed,
        peak_memory_est_mb=0.0,
    )
    rows.append(row)
    _log_phase("已計算 smoke 分數列之 test 指標（1 列 metrics）")
    if cfg.tier0_r1_enabled and not cfg.tier0_r1_signals:
        raise ValueError("tier0.r1.enabled 為 true 但 tier0.r1.signals 為空；請至少指定一個 pace 訊號欄。")
    if cfg.tier0_r1_enabled:
        _log_phase(f"Tier-0 R1 pace：評估 {len(cfg.tier0_r1_signals)} 個訊號 …")
        for sig in cfg.tier0_r1_signals:
            y_r1 = np.asarray(pace_rule_scores(test_df, sig).values, dtype=float)
            r1_row = build_eval_metrics_row(
                experiment_id=exp_id,
                baseline_family="rule",
                model_type=f"R1_pace:{sig}",
                proxy_type=None,
                split_protocol=cfg.split_protocol_name,
                feature_set_version=cfg.feature_set_version,
                label_contract_version=cfg.label_contract_version,
                data_window=tw,
                y_true=y_true,
                y_score=y_r1,
                min_alert_count=cfg.threshold_min_alert_count,
                recall_floor=cfg.threshold_recall_floor,
                min_alerts_per_hour=cfg.threshold_min_alerts_per_hour,
                window_hours=cfg.threshold_window_hours,
                runtime_sec=0.0,
                peak_memory_est_mb=0.0,
                decision="iterate",
                notes=(
                    f"Tier-0 R1 pace：訊號欄 {sig!r}，排序方向=數值越大 walkaway 風險越高（SSOT §4.1 R1）。"
                ),
            )
            rows.append(r1_row)
        _log_phase("Tier-0 R1 完成")
    if cfg.tier0_r2_enabled:
        _log_phase("Tier-0 R2 loss：評估 net / wager …")
        for proxy in ("net", "wager"):
            y_r2 = np.asarray(
                loss_rule_scores(
                    test_df,
                    cast(LossProxy, proxy),
                    net_column=cfg.tier0_r2_net_column,
                    wager_column=cfg.tier0_r2_wager_column,
                ).values,
                dtype=float,
            )
            r2_row = build_eval_metrics_row(
                experiment_id=exp_id,
                baseline_family="rule",
                model_type=f"R2_loss:{proxy}",
                proxy_type=proxy,
                split_protocol=cfg.split_protocol_name,
                feature_set_version=cfg.feature_set_version,
                label_contract_version=cfg.label_contract_version,
                data_window=tw,
                y_true=y_true,
                y_score=y_r2,
                min_alert_count=cfg.threshold_min_alert_count,
                recall_floor=cfg.threshold_recall_floor,
                min_alerts_per_hour=cfg.threshold_min_alerts_per_hour,
                window_hours=cfg.threshold_window_hours,
                runtime_sec=0.0,
                peak_memory_est_mb=0.0,
                decision="iterate",
                notes=(
                    f"Tier-0 R2 proxy={proxy!r}；"
                    + (
                        f"欄={cfg.tier0_r2_net_column!r}；評分=-(欄位)；"
                        "玩家視角負值＝虧（SSOT §4.1 R2）。"
                        if proxy == "net"
                        else f"欄={cfg.tier0_r2_wager_column!r}；評分=欄位值（累積下注 proxy）。"
                    )
                ),
            )
            rows.append(r2_row)
        _log_phase("Tier-0 R2 完成")
    if cfg.tier0_r3_enabled and not cfg.tier0_r3_variants:
        raise ValueError(
            "tier0.r3.enabled 為 true 但 tier0.r3.variants 為空；請至少指定 adt30／adt180／theo_per_session 之一。"
        )
    if cfg.tier0_r3_enabled:
        _n_r3 = len(cfg.tier0_r3_variants) * len(cfg.tier0_r3_tau_grid)
        _log_phase(
            f"Tier-0 R3：{len(cfg.tier0_r3_variants)} 個 variant × "
            f"{len(cfg.tier0_r3_tau_grid)} 個 tau → {_n_r3} 組指標 …"
        )
        specs = r3_formula_specs()
        for v in cfg.tier0_r3_variants:
            if v not in R3_ADT_VARIANTS:
                raise ValueError(
                    f"不支援的 R3 variant: {v!r}；允許: {sorted(R3_ADT_VARIANTS)!r}"
                )
            for tau in cfg.tier0_r3_tau_grid:
                y_r3 = np.asarray(
                    adt_rule_scores(
                        test_df,
                        cast(AdtVariant, v),
                        current_session_theo_column=cfg.tier0_r3_current_session_theo_column,
                        tau=float(tau),
                    ).values,
                    dtype=float,
                )
                r3_row = build_eval_metrics_row(
                    experiment_id=exp_id,
                    baseline_family="rule",
                    model_type=r3_model_type_for_metrics(v, float(tau)),
                    proxy_type=v,
                    split_protocol=cfg.split_protocol_name,
                    feature_set_version=cfg.feature_set_version,
                    label_contract_version=cfg.label_contract_version,
                    data_window=tw,
                    y_true=y_true,
                    y_score=y_r3,
                    min_alert_count=cfg.threshold_min_alert_count,
                    recall_floor=cfg.threshold_recall_floor,
                    min_alerts_per_hour=cfg.threshold_min_alerts_per_hour,
                    window_hours=cfg.threshold_window_hours,
                    runtime_sec=0.0,
                    peak_memory_est_mb=0.0,
                    decision="iterate",
                    notes=(
                        f"Tier-0 R3 variant={v!r}，tau={tau!r}；{specs.get('score', '')} "
                        f"估算式摘要：{specs.get(v, '')}"
                    ),
                )
                rows.append(r3_row)
        _log_phase("Tier-0 R3 完成")
    if cfg.tier1_m1_enabled and not cfg.tier1_m1_feature_columns:
        raise ValueError(
            "tier1.m1.enabled 為 true 但 tier1.m1.feature_columns 為空；請至少指定一個數值特徵欄。"
        )
    if cfg.tier1_m1_enabled:
        cols = cfg.tier1_m1_feature_columns
        _log_phase(f"Tier-1 M1 LogisticRegression：擬合 train（特徵數={len(cols)}）…")
        train_x = select_feature_subset(train_df, cols)
        test_x = select_feature_subset(test_df, cols)
        train_y = train_df[cfg.column_label]
        t_m1 = time.perf_counter()
        lr = fit_logistic_baseline(
            train_x,
            train_y,
            max_iter=cfg.tier1_m1_max_iter,
        )
        y_m1 = predict_proba_positive(lr, test_x)
        m1_elapsed = time.perf_counter() - t_m1
        _log_phase(f"Tier-1 M1 完成（擬合+推論約 {m1_elapsed:.2f}s）")
        m1_row = build_eval_metrics_row(
            experiment_id=exp_id,
            baseline_family="linear",
            model_type="M1_LogisticRegression",
            proxy_type=None,
            split_protocol=cfg.split_protocol_name,
            feature_set_version=cfg.feature_set_version,
            label_contract_version=cfg.label_contract_version,
            data_window=tw,
            y_true=y_true,
            y_score=y_m1,
            min_alert_count=cfg.threshold_min_alert_count,
            recall_floor=cfg.threshold_recall_floor,
            min_alerts_per_hour=cfg.threshold_min_alerts_per_hour,
            window_hours=cfg.threshold_window_hours,
            runtime_sec=float(m1_elapsed),
            peak_memory_est_mb=0.0,
            decision="iterate",
            notes=(
                "Tier-1 M1：LogisticRegression（solver=saga, class_weight=balanced, penalty=l2）；"
                f"特徵欄 {cols!r}；僅於 train 切片擬合，於 test 評估（SSOT §4.2）。"
            ),
        )
        rows.append(m1_row)
    if cfg.tier1_m2_enabled and not cfg.tier1_m2_feature_columns:
        raise ValueError(
            "tier1.m2.enabled 為 true 但 tier1.m2.feature_columns 為空；請至少指定一個數值特徵欄。"
        )
    if cfg.tier1_m2_enabled:
        cols_m2 = cfg.tier1_m2_feature_columns
        _log_phase(f"Tier-1 M2 SGDClassifier：擬合 train（特徵數={len(cols_m2)}）…")
        train_x_m2 = select_feature_subset(train_df, cols_m2)
        test_x_m2 = select_feature_subset(test_df, cols_m2)
        train_y_m2 = train_df[cfg.column_label]
        t_m2 = time.perf_counter()
        sgd = fit_sgd_baseline(
            train_x_m2,
            train_y_m2,
            max_iter=cfg.tier1_m2_max_iter,
        )
        y_m2 = predict_sgd_proba_positive(sgd, test_x_m2)
        m2_elapsed = time.perf_counter() - t_m2
        _log_phase(f"Tier-1 M2 完成（擬合+推論約 {m2_elapsed:.2f}s）")
        m2_row = build_eval_metrics_row(
            experiment_id=exp_id,
            baseline_family="linear",
            model_type="M2_SGDClassifier",
            proxy_type=None,
            split_protocol=cfg.split_protocol_name,
            feature_set_version=cfg.feature_set_version,
            label_contract_version=cfg.label_contract_version,
            data_window=tw,
            y_true=y_true,
            y_score=y_m2,
            min_alert_count=cfg.threshold_min_alert_count,
            recall_floor=cfg.threshold_recall_floor,
            min_alerts_per_hour=cfg.threshold_min_alerts_per_hour,
            window_hours=cfg.threshold_window_hours,
            runtime_sec=float(m2_elapsed),
            peak_memory_est_mb=0.0,
            decision="iterate",
            notes=(
                "Tier-1 M2：SGDClassifier（loss=log_loss, class_weight=balanced, penalty=l2）；"
                f"特徵欄 {cols_m2!r}；僅於 train 切片擬合，於 test 評估（SSOT §4.2）。"
            ),
        )
        rows.append(m2_row)
    if cfg.tier1_s1_enabled and not cfg.tier1_s1_rankings:
        raise ValueError(
            "tier1.s1.enabled 為 true 但 tier1.s1.rankings 為空；請至少指定一筆 "
            "{ column, high_is_risk, proxy_type? }。"
        )
    if cfg.tier1_s1_enabled:
        _log_phase(f"Tier-1 S1 單特徵排名：{len(cfg.tier1_s1_rankings)} 組 …")
        for col_s1, high_is_risk, proxy_s1 in cfg.tier1_s1_rankings:
            y_s1 = np.asarray(
                single_feature_scores(test_df, col_s1, high_is_risk).values,
                dtype=float,
            )
            dir_note = "數值越大風險越高" if high_is_risk else "取負後：原數值越低（如 net 越負＝虧越多）風險越高"
            s1_row = build_eval_metrics_row(
                experiment_id=exp_id,
                baseline_family="rule",
                model_type=f"S1_rank:{col_s1}",
                proxy_type=proxy_s1,
                split_protocol=cfg.split_protocol_name,
                feature_set_version=cfg.feature_set_version,
                label_contract_version=cfg.label_contract_version,
                data_window=tw,
                y_true=y_true,
                y_score=y_s1,
                min_alert_count=cfg.threshold_min_alert_count,
                recall_floor=cfg.threshold_recall_floor,
                min_alerts_per_hour=cfg.threshold_min_alerts_per_hour,
                window_hours=cfg.threshold_window_hours,
                runtime_sec=0.0,
                peak_memory_est_mb=0.0,
                decision="iterate",
                notes=(
                    "Tier-1 S1：單特徵直接排序（無訓練；SSOT §4.2）；"
                    f"欄={col_s1!r}；{dir_note}。"
                    + (
                        f" proxy_type={proxy_s1!r} 對齊 R2 語意。"
                        if proxy_s1
                        else " proxy_type 未填（非 net／wager／ADT 列舉之純數值欄）。"
                    )
                ),
            )
            rows.append(s1_row)
        _log_phase("Tier-1 S1 完成")
    ref_block = build_reference_lightgbm_snapshot(
        enabled=cfg.reference_model_enabled,
        config_path=cfg.config_path,
        training_metrics_path=cfg.reference_model_training_metrics_path,
        metrics_section=cfg.reference_model_metrics_section,
    )
    if cfg.reference_model_enabled:
        _log_phase(
            "同窗 reference_lightgbm："
            f"status={ref_block.get('status')!r}"
            + (
                f"；path={ref_block.get('path')!r}"
                if ref_block.get("path")
                else ""
            )
        )
    out_dir = default_results_dir(run_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    _log_phase(
        f"寫入結果目錄：{out_dir}（baseline_metrics.json、run_state.json、baseline_summary.md）"
    )
    _write_json(out_dir / "baseline_metrics.json", {"metrics": rows})
    notes_contract = (cfg.raw.get("notes_contract") or {}) if isinstance(cfg.raw, Mapping) else {}
    run_state: dict[str, Any] = {
        "experiment_id": exp_id,
        "run_id": run_id,
        "data_window": tw,
        "split_protocol": cfg.split_protocol_name,
        "feature_set_version": cfg.feature_set_version,
        "label_contract_version": cfg.label_contract_version,
        "temporal_split_ends": {
            "train_end_exclusive": str(split_spec.train_end_exclusive),
            "valid_end_exclusive": str(split_spec.valid_end_exclusive),
            "test_end_exclusive": str(split_spec.test_end_exclusive),
        },
        "notes": (
            f"net_sign_convention={notes_contract.get('net_sign_convention', 'unset')}; "
            "Phase A smoke：禁 shuffle 之時間切片；詳見 baseline_models/EXECUTION_PLAN.md。"
        ),
        "tier0_r1": {
            "enabled": cfg.tier0_r1_enabled,
            "signals_evaluated": list(cfg.tier0_r1_signals),
        },
        "tier0_r2": {
            "enabled": cfg.tier0_r2_enabled,
            "net_column": cfg.tier0_r2_net_column,
            "wager_column": cfg.tier0_r2_wager_column,
            "proxies_evaluated": (["net", "wager"] if cfg.tier0_r2_enabled else []),
        },
        "tier0_r3": r3_run_state_block(
            enabled=cfg.tier0_r3_enabled,
            variants=list(cfg.tier0_r3_variants),
            current_session_theo_column=cfg.tier0_r3_current_session_theo_column,
            tau_grid=list(cfg.tier0_r3_tau_grid),
        ),
        "tier1_m1": {
            "enabled": cfg.tier1_m1_enabled,
            "feature_columns": list(cfg.tier1_m1_feature_columns),
            "max_iter": cfg.tier1_m1_max_iter,
        },
        "tier1_m2": {
            "enabled": cfg.tier1_m2_enabled,
            "feature_columns": list(cfg.tier1_m2_feature_columns),
            "max_iter": cfg.tier1_m2_max_iter,
        },
        "tier1_s1": {
            "enabled": cfg.tier1_s1_enabled,
            "rankings": [
                {
                    "column": c,
                    "high_is_risk": hir,
                    "proxy_type": px,
                }
                for c, hir, px in cfg.tier1_s1_rankings
            ],
        },
        "reference_lightgbm": ref_block,
    }
    _write_json(out_dir / "run_state.json", run_state)
    r1_lines = ""
    if cfg.tier0_r1_enabled and cfg.tier0_r1_signals:
        r1_lines = (
            "\n## Tier-0 R1（pace）\n\n"
            + "\n".join(
                f"- 訊號 `{s}`：`model_type` = `R1_pace:{s}`（越高風險越高）"
                for s in cfg.tier0_r1_signals
            )
            + "\n"
        )
    r2_lines = ""
    if cfg.tier0_r2_enabled:
        r2_lines = (
            "\n## Tier-0 R2（loss）\n\n"
            f"- **net**：欄 `{cfg.tier0_r2_net_column}`，`proxy_type=net`，評分＝負欄位值（虧損越大分數越高）。\n"
            f"- **wager**：欄 `{cfg.tier0_r2_wager_column}`，`proxy_type=wager`（與 net **分開** metrics 列）。\n"
        )
    r3_lines = ""
    if cfg.tier0_r3_enabled and cfg.tier0_r3_variants:
        r3_lines = (
            "\n## Tier-0 R3（ADT／theo）\n\n"
            + "\n".join(
                (
                    f"- **`{vv}`**、`tau={tau}`：`proxy_type={vv}`，"
                    f"`model_type={r3_model_type_for_metrics(vv, float(tau))}`"
                    "（本場 theo／(ADT_est·tau)；詳 `run_state.tier0_r3`）。"
                )
                for vv in cfg.tier0_r3_variants
                for tau in cfg.tier0_r3_tau_grid
            )
            + "\n"
        )
    m1_lines = ""
    if cfg.tier1_m1_enabled and cfg.tier1_m1_feature_columns:
        m1_lines = (
            "\n## Tier-1 M1（LogisticRegression）\n\n"
            f"- `model_type=M1_LogisticRegression`，`baseline_family=linear`；特徵欄：`{cfg.tier1_m1_feature_columns}`。\n"
        )
    m2_lines = ""
    if cfg.tier1_m2_enabled and cfg.tier1_m2_feature_columns:
        m2_lines = (
            "\n## Tier-1 M2（SGDClassifier）\n\n"
            f"- `model_type=M2_SGDClassifier`，`baseline_family=linear`；特徵欄：`{cfg.tier1_m2_feature_columns}`。\n"
        )
    s1_lines = ""
    if cfg.tier1_s1_enabled and cfg.tier1_s1_rankings:
        s1_lines = "\n## Tier-1 S1（單特徵排名，無訓練）\n\n" + "\n".join(
            (
                f"- 欄 `{c}`：`model_type=S1_rank:{c}`，`high_is_risk={hir}`，"
                f"`proxy_type`={'`' + str(px) + '`' if px is not None else '`null`'}"
            )
            for c, hir, px in cfg.tier1_s1_rankings
        ) + "\n"
    s1_pending = "- **S1**：尚未產出。\n" if not (cfg.tier1_s1_enabled and cfg.tier1_s1_rankings) else ""
    lgbm_md = markdown_lightgbm_peer_table(rows, ref_block)
    if not lgbm_md and cfg.reference_model_enabled:
        lgbm_md = (
            "\n## LightGBM 同窗對照\n\n"
            f"- **狀態**：`{ref_block.get('status')}`"
            f" — {ref_block.get('reason', '')}\n"
        )
    elif not lgbm_md:
        lgbm_md = (
            "- **LightGBM 對照**：未啟用；於 YAML 設定 `reference_model` "
            "可載入同窗 `training_metrics.json`／`training_metrics.v2.json` bundle（SSOT §8）。\n"
        )
    summary = (
        "# Baseline run summary\n\n"
        f"- **run_id**: `{run_id}`\n"
        f"- **experiment_id**: `{exp_id}`\n"
        f"- **split**: {cfg.split_protocol_name}（train/valid/test 依時間排序切片，無 shuffle）\n"
        f"- **test rows**: {len(test_df)}\n"
        f"- **metrics rows**: {len(rows)}\n"
        f"{r1_lines}"
        f"{r2_lines}"
        f"{r3_lines}"
        f"{m1_lines}"
        f"{m2_lines}"
        f"{s1_lines}"
        f"{lgbm_md}"
        f"{s1_pending}"
    )
    _write_text(out_dir / "baseline_summary.md", summary)
    _elapsed = time.perf_counter() - t0
    _log_phase(
        f"完成：metrics 列數={len(rows)}；總耗時約 {_elapsed:.2f}s；run_id={run_id!r}"
    )
    return {"results_dir": str(out_dir), "metrics_path": str(out_dir / "baseline_metrics.json")}


def run_baseline_pipeline(config_path: str, run_id: str) -> dict[str, Any]:
    """執行目前倉庫內已接線之完整 baseline 評估。

    與 :func:`run_smoke` 等價（Tier-0 R1／R2／R3、Tier-1 M1／M2／S1、可選同窗、三件套）。
    Tier-2（淺樹／NB）與 ``baseline_predictions.parquet`` 尚未接線。

    Args:
        config_path: 設定檔路徑。
        run_id: 結果子目錄名。

    Returns:
        執行摘要字典。
    """
    return run_smoke(config_path, run_id)
