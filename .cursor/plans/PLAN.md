# Plan index

**Current execution plan**: [PLAN_phase2_p0_p1.md](PLAN_phase2_p0_p1.md) (Phase 2 P0–P1).

**Investigation**：Test vs production 性能落差根因與調查步驟見 [INVESTIGATION_PLAN_TEST_VS_PRODUCTION.md](INVESTIGATION_PLAN_TEST_VS_PRODUCTION.md)。

**Pipeline 診斷與 MLflow artifacts**（2026-03-21，狀態已對齊 doc）：詳見 [`doc/plan_pipeline_diagnostics_and_mlflow_artifacts.md`](../../doc/plan_pipeline_diagnostics_and_mlflow_artifacts.md)。

**統一改進計劃 v2**（效能 + 可觀測性，與 Phase 2 獨立）：詳見 [Unified Improvement Plan.md](Unified%20Improvement%20Plan.md)。

| Task | 狀態 | 備註 |
|------|------|------|
| **T1** Scorer 安全裁切（rated-only 於 LLM／profile 前） | ✅ 已實作 | `trainer/serving/scorer.py` `score_once`；UNRATED_VOLUME_LOG 於裁切前預算 |
| **T2** Backtester → MLflow | ✅ 已實作 | `trainer/training/backtester.py`；`backtest_*` 鍵、`model_default` 區段 |
| **T3** Validator precision 歷史化 | ✅ 已實作 | `validator.py` `get_db_conn` + `validate_once` → `validator_metrics`；`alerts` 與 scorer 對齊遷移 |
| **T4** Prediction log 聚合 | ✅ 已實作 | `scorer._export_prediction_log_summary`、`prediction_log_summary` 表；`PREDICTION_LOG_SUMMARY_WINDOW_MINUTES` |
| **Review MRE** | ✅ 已落地 | `tests/review_risks/test_unified_plan_v2_review_risks.py`（STATUS §統一計劃 v2 Review） |

| 章節 | 狀態 | 備註 |
|------|------|------|
| **§1–§5** | ✅ 已實作 | `pipeline_diagnostics.json`、`BUNDLE_FILES`、`mlflow` system metrics optional、`run_pipeline` `bundle/`、provenance 鍵與 runbook。 |
| **§6 測試** | ✅ **自動化已覆蓋計畫主力**；⏳ **可選補強** | review_risks／integration／單元；含 **`test_review_risks_pipeline_plan_section6_contract.py`**（AST 單次 `log_artifact_safe`、bundle chunk 不變量、RSS／OOM 守衛、Reviewer 風險 MRE）、`test_pipeline_diagnostics_build_and_bundle.py`、`test_review_risks_pipeline_diagnostics_write_review.py` 等。doc §6 所列「執行期依存在檔數 mock `log_artifact_safe`」「端到端迷你 pipeline／凍結時鐘」仍為**可選**。 |
| **§7 文件** | ✅ 已實作 | README 三語 artifacts、`credential/mlflow.env.example`。 |
| **§8 驗收** | ⏳ **仍待人工** | 實際訓練／建包／MLflow UI／export run（見 plan doc §8 清單）。 |

**Phase 2 status**（2026-03-18）：**T0–T10 已完成**（詳見 PLAN_phase2_p0_p1.md § Ordered Tasks 與 Remaining items）。

This file exists so README and review tests (R384, R147) that reference `.cursor/plans/PLAN.md` pass. The 特徵整合計畫 section below is retained for round147 contract (no Step 9+ in that section).

---

## 特徵整合計畫：Feature Spec YAML 單一 SSOT（已實作）

### 目標與原則

1. **YAML = 三軌候選特徵的唯一真相來源**：所有 Track Profile / Track LLM / Track Human 的候選特徵均在 Feature Spec YAML 定義。
2. **Scorer 由 Trainer 產出驅動**：Scorer 計算的特徵清單與計算方式完全由 trainer 產出的 `feature_list.json` + `feature_spec.yaml` 決定。
3. **Serving 不依賴 session**：所有進模型的候選特徵計算**不得**依賴 session 資訊。
4. **Track LLM 單一 partition**：所有 Track LLM 的 window/aggregate 一律 `PARTITION BY canonical_id`。

### Step 1 — YAML 補完

（已實作；詳見 archive/PLAN_phase1.md § 特徵整合計畫。）

### Step 2 — Python helper（features.py）

（已實作。）

### Step 3 — 移除硬編碼，改用 YAML

（已實作。）

### Step 4 — compute_track_llm_features 擴充

（已實作。）

### Step 5 — Screening 改造

（已實作。）

### Step 6 — Scorer 對齊

（已實作。）

### Step 7 — Artifact 產出

（已實作。）

### Step 8 — 測試

（已實作。）

### 實作順序

1. Step 1 → 2 → 4 → 3 → 5 → 7 → 6 → 8。
