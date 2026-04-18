"""baseline_models Phase A smoke 與契約單元測試。"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
import numpy as np
import pandas as pd
import pytest

from baseline_models.src.baseline_config import (
    load_run_config_yaml,
    merge_training_provenance_into_raw,
)
from baseline_models.src.data_contract import (
    build_synthetic_smoke_frame,
    temporal_train_valid_test_split,
    validate_label_and_censor_columns,
)
from baseline_models.src.eval import dec026_imports
from baseline_models.src.eval.runner import default_results_dir
from baseline_models.src.rules.adt_rules import adt_rule_scores, r3_model_type_for_metrics
from baseline_models.src.rules.loss_rules import loss_rule_scores
from baseline_models.src.rules.pace_rules import pace_rule_scores
from baseline_models.src.rules.single_feature_rank import single_feature_scores

_CANONICAL_KEYS = (
    "experiment_id",
    "baseline_family",
    "model_type",
    "proxy_type",
    "data_window",
    "split_protocol",
    "feature_set_version",
    "label_contract_version",
    "precision_at_recall_0.01",
    "threshold_at_recall_0.01",
    "pr_auc",
    "alerts",
    "alerts_rate",
    "runtime_sec",
    "peak_memory_est_mb",
    "decision",
    "notes",
)


def test_reference_precision_prefers_prod_adjusted() -> None:
    """同窗 P@R=1% 應優先採 training_metrics 之 prod_adjusted 鍵。"""
    from baseline_models.src.eval.reference_model import (
        reference_precision_at_recall_0_01_for_peer,
    )

    ref = {
        "test_precision_at_recall_0.01": 0.9,
        "test_precision_at_recall_0.01_prod_adjusted": 0.41,
    }
    assert reference_precision_at_recall_0_01_for_peer(ref) == pytest.approx(0.41)


def test_reference_precision_falls_back_to_raw_when_no_adjusted() -> None:
    """無 prod_adjusted 時退回 raw test_precision_at_recall_0.01。"""
    from baseline_models.src.eval.reference_model import (
        reference_precision_at_recall_0_01_for_peer,
    )

    ref = {"test_precision_at_recall_0.01": 0.7}
    assert reference_precision_at_recall_0_01_for_peer(ref) == pytest.approx(0.7)


def test_dec026_imports_loadable() -> None:
    """DEC-026 匯入鏈須可用（與 trainer 單測環境一致）。"""
    assert callable(dec026_imports.pick_threshold_dec026)
    assert isinstance(dec026_imports.THRESHOLD_FBETA, float)
    assert callable(dec026_imports.pick_threshold_dec026_from_pr_arrays)
    assert callable(dec026_imports.dec026_pr_alert_arrays)


def test_validate_label_and_censor_drops_last_row() -> None:
    """合成表最後一列 censored=True 應被排除。"""
    df = build_synthetic_smoke_frame(20)
    assert bool(df["censored"].iloc[-1]) is True
    out = validate_label_and_censor_columns(df, "label", "censored")
    assert len(out) == len(df) - 1


def test_temporal_split_respects_time_order() -> None:
    """禁 shuffle：train 最大時間 ≤ valid 最小時間。"""
    df = validate_label_and_censor_columns(
        build_synthetic_smoke_frame(40), "label", "censored"
    )
    train_df, valid_df, _test_df, _spec = temporal_train_valid_test_split(
        df, "bet_time", 0.5, 0.5
    )
    assert train_df["bet_time"].max() <= valid_df["bet_time"].min()


def test_temporal_split_invalid_frac_raises() -> None:
    """非法 train_frac 應 fail-fast。"""
    df = validate_label_and_censor_columns(
        build_synthetic_smoke_frame(30), "label", "censored"
    )
    with pytest.raises(ValueError, match="訓練比例過大"):
        temporal_train_valid_test_split(df, "bet_time", 0.99, 0.5)


def test_run_smoke_writes_three_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run_smoke 寫入三件套且 metrics 列含 canonical 鍵。"""
    import baseline_models.src.eval.runner as runner_mod

    run_id = "pytest_baseline_smoke_tmp"
    monkeypatch.setattr(
        runner_mod,
        "default_results_dir",
        lambda rid, repo_root=None: tmp_path / rid,
    )
    yaml_path = Path(__file__).resolve().parents[2] / "baseline_models" / "config" / "baseline_default.yaml"
    summary = runner_mod.run_smoke(str(yaml_path), run_id)
    out = Path(summary["results_dir"])
    assert (out / "baseline_metrics.json").is_file()
    assert (out / "run_state.json").is_file()
    assert (out / "baseline_summary.md").is_file()
    payload = json.loads((out / "baseline_metrics.json").read_text(encoding="utf-8"))
    metrics = payload["metrics"]
    assert len(metrics) >= 13
    row = metrics[0]
    for k in _CANONICAL_KEYS:
        assert k in row
    assert "test_precision_at_recall_0.001" in row
    assert "threshold_at_recall_0.1" in row
    rs = json.loads((out / "run_state.json").read_text(encoding="utf-8"))
    ref = rs.get("reference_lightgbm") or {}
    assert ref.get("enabled") is True
    assert ref.get("status") == "loaded"
    assert row["peak_memory_est_mb"] is not None
    r1 = metrics[1]
    assert r1["model_type"] == "R1_pace:pace_drop_ratio"
    assert r1["baseline_family"] == "rule"
    proxies = {m.get("proxy_type") for m in metrics}
    assert "net" in proxies and "wager" in proxies
    r2_net = next(m for m in metrics if m.get("proxy_type") == "net")
    assert r2_net["model_type"] == "R2_loss:net"
    r3_tau1 = next(
        m
        for m in metrics
        if m.get("model_type") == r3_model_type_for_metrics("adt30", 1.0)
    )
    assert r3_tau1["proxy_type"] == "adt30"
    assert r3_tau1["baseline_family"] == "rule"
    m1 = next(m for m in metrics if m.get("model_type") == "M1_LogisticRegression")
    assert m1["baseline_family"] == "linear"
    assert m1["proxy_type"] is None
    m2 = next(m for m in metrics if m.get("model_type") == "M2_SGDClassifier")
    assert m2["baseline_family"] == "linear"
    assert m2["proxy_type"] is None
    assert 0.0 <= float(m2["runtime_sec"]) < 600.0
    s1_pace = next(m for m in metrics if m.get("model_type") == "S1_rank:pace_drop_ratio")
    assert s1_pace["baseline_family"] == "rule"
    assert s1_pace["proxy_type"] is None
    s1_net = next(m for m in metrics if m.get("model_type") == "S1_rank:loss_proxy_net")
    assert s1_net["baseline_family"] == "rule"
    assert s1_net["proxy_type"] == "net"


def test_merge_baseline_data_alignment_from_metrics_then_provenance(tmp_path: Path) -> None:
    """metrics 的 baseline_data_alignment 先合併，training_provenance.json 後覆蓋。"""
    repo = tmp_path / "repo"
    bundle = repo / "out" / "models" / "x"
    bundle.mkdir(parents=True)
    metrics_path = bundle / "training_metrics.json"
    metrics_path.write_text(
        json.dumps(
            {
                "rated": {},
                "baseline_data_alignment": {
                    "data_window": {"start": "2026-01-01T00:00:00"},
                    "split": {"train_frac": 0.7, "valid_frac": 0.5},
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (bundle / "training_provenance.json").write_text(
        json.dumps(
            {
                "data_window": {"start": "2026-02-01T00:00:00", "end": "2026-03-01T00:00:00"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    cfg_dir = repo / "baseline_models" / "config"
    cfg_dir.mkdir(parents=True)
    yaml_path = cfg_dir / "align.yaml"
    rel_metrics = "out/models/x/training_metrics.json"
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "reference_model": {
                    "apply_training_provenance": True,
                    "training_metrics_path": rel_metrics,
                },
                "split": {"train_frac": 0.5, "valid_frac": 0.25},
                "data_window": {"start": "1999-01-01", "end": "1999-12-31"},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    merge_training_provenance_into_raw(data, yaml_path)
    assert data["split"]["train_frac"] == 0.7
    assert data["split"]["valid_frac"] == 0.5
    assert data["data_window"]["start"] == "2026-02-01T00:00:00"
    assert data["data_window"]["end"] == "2026-03-01T00:00:00"


def test_load_run_config_yaml_roundtrip() -> None:
    """設定載入：欄位名與 synthetic kind。"""
    yaml_path = Path(__file__).resolve().parents[2] / "baseline_models" / "config" / "baseline_default.yaml"
    cfg = load_run_config_yaml(yaml_path)
    assert cfg.data_source_kind == "synthetic_smoke"
    assert cfg.column_label == "label"
    assert cfg.tier0_r1_enabled is True
    assert cfg.tier0_r1_signals == ["pace_drop_ratio"]
    assert cfg.tier0_r2_enabled is True
    assert cfg.tier0_r2_net_column == "loss_proxy_net"
    assert cfg.tier0_r2_wager_column == "loss_proxy_wager"
    assert cfg.tier0_r3_enabled is True
    assert cfg.tier0_r3_variants == ["adt30"]
    assert cfg.tier0_r3_current_session_theo_column == "current_session_theo"
    assert cfg.tier0_r3_tau_grid == [0.8, 1.0, 1.2, 1.5, 2.0]
    assert cfg.reference_model_enabled is True
    assert cfg.reference_model_metrics_section == "rated"
    p = cfg.reference_model_training_metrics_path or ""
    assert p.endswith("training_metrics.json")
    assert cfg.tier1_m1_enabled is True
    assert cfg.tier1_m1_feature_columns == [
        "pace_drop_ratio",
        "loss_proxy_net",
        "theo_win_sum_30d",
    ]
    assert cfg.tier1_m1_max_iter == 10000
    assert cfg.tier1_m2_enabled is True
    assert cfg.tier1_m2_feature_columns == [
        "pace_drop_ratio",
        "loss_proxy_net",
        "theo_win_sum_30d",
    ]
    assert cfg.tier1_m2_max_iter == 5000
    assert cfg.tier1_s1_enabled is True
    assert cfg.tier1_s1_rankings == [
        ("pace_drop_ratio", True, None),
        ("loss_proxy_net", False, "net"),
    ]


def test_single_feature_scores_rejects_nan() -> None:
    """S1：欄位含 NaN 時須 fail-fast。"""
    df = build_synthetic_smoke_frame(12)
    df.loc[0, "pace_drop_ratio"] = float("nan")
    with pytest.raises(ValueError, match="NaN"):
        single_feature_scores(df, "pace_drop_ratio", True)


def test_fit_sgd_baseline_rejects_single_class() -> None:
    """訓練集僅單一標籤時須 fail-fast（與 M1 契約一致）。"""
    from baseline_models.src.models.sgd_baseline import fit_sgd_baseline

    x = pd.DataFrame({"f": [1.0, 2.0, 3.0]})
    y = pd.Series([0, 0, 0])
    with pytest.raises(ValueError, match="兩類別"):
        fit_sgd_baseline(x, y, max_iter=50)


def test_predict_sgd_proba_positive_matches_test_rows() -> None:
    """M2：合成 train 擬合後，test 機率列長與界內。"""
    from baseline_models.src.models.sgd_baseline import fit_sgd_baseline, predict_proba_positive

    df = validate_label_and_censor_columns(
        build_synthetic_smoke_frame(48), "label", "censored"
    )
    train_df, _v, test_df, _spec = temporal_train_valid_test_split(
        df, "bet_time", 0.55, 0.45
    )
    cols = ["pace_drop_ratio", "loss_proxy_net", "theo_win_sum_30d"]
    model = fit_sgd_baseline(
        train_df.loc[:, cols],
        train_df["label"],
        max_iter=2000,
    )
    p = predict_proba_positive(model, test_df.loc[:, cols])
    assert len(p) == len(test_df)
    assert (p >= 0.0).all() and (p <= 1.0).all()


def test_pace_rule_scores_on_synthetic() -> None:
    """R1：支援欄位須可轉為有限分數。"""
    df = validate_label_and_censor_columns(
        build_synthetic_smoke_frame(25), "label", "censored"
    )
    s = pace_rule_scores(df, "pace_drop_ratio")
    assert len(s) == len(df)
    assert float(s.min()) >= 0.0


def test_pace_rule_scores_unknown_signal_raises() -> None:
    """不支援的訊號名應 fail-fast。"""
    df = build_synthetic_smoke_frame(12)
    with pytest.raises(ValueError, match="不支援的 R1 pace"):
        pace_rule_scores(df, "not_a_column")


def test_adt_rule_scores_adt30_positive() -> None:
    """R3 adt30：ratio 應為有限正數（合成表）。"""
    df = validate_label_and_censor_columns(
        build_synthetic_smoke_frame(22), "label", "censored"
    )
    s = adt_rule_scores(df, "adt30")
    assert (s > 0).all()
    assert not s.isna().any()


def test_adt_rule_scores_tau_scales_denominator() -> None:
    """tau 加倍時，在相同 ADT_est 下 ratio 應約為一半。"""
    df = validate_label_and_censor_columns(
        build_synthetic_smoke_frame(18), "label", "censored"
    )
    s1 = adt_rule_scores(df, "adt30", tau=1.0)
    s2 = adt_rule_scores(df, "adt30", tau=2.0)
    np.testing.assert_allclose(s2.values, 0.5 * s1.values, rtol=1e-12, atol=0.0)


def test_adt_rule_scores_rejects_nonpositive_tau() -> None:
    """非法 tau 須 fail-fast。"""
    df = validate_label_and_censor_columns(
        build_synthetic_smoke_frame(14), "label", "censored"
    )
    with pytest.raises(ValueError, match="tau"):
        adt_rule_scores(df, "adt30", tau=0.0)


def test_adt_rule_scores_rejects_bad_variant() -> None:
    """非法 variant 應 fail-fast。"""
    df = build_synthetic_smoke_frame(12)
    with pytest.raises(ValueError, match="R3 variant"):
        adt_rule_scores(df, "adt7d")  # type: ignore[arg-type]


def test_loss_rule_net_negates_for_risk_score() -> None:
    """net：欄位負值＝虧損；評分用 -net 使虧越多分數越高。"""
    df = pd.DataFrame({"loss_proxy_net": [-100.0, 50.0], "loss_proxy_wager": [1.0, 2.0]})
    s = loss_rule_scores(
        df,
        "net",
        net_column="loss_proxy_net",
        wager_column="loss_proxy_wager",
    )
    assert float(s.iloc[0]) == 100.0
    assert float(s.iloc[1]) == -50.0


def test_pace_rule_scores_nan_raises() -> None:
    """訊號欄含 NaN 應 fail-fast。"""
    df = build_synthetic_smoke_frame(15)
    df.loc[0, "pace_drop_ratio"] = float("nan")
    with pytest.raises(ValueError, match="NaN"):
        pace_rule_scores(df, "pace_drop_ratio")


def test_default_results_dir_under_baseline_models() -> None:
    """未傳 repo_root 時結果目錄應落在 baseline_models/results 下。"""
    p = default_results_dir("x")
    assert "baseline_models" in str(p).replace("\\", "/")
    assert str(p).endswith("results/x") or str(p).endswith(r"results\x")
