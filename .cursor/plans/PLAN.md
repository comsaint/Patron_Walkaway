# Plan index

## Phase 1（已結案）

Phase 1 訓練／特徵／serving 主線已結案。歷史執行細節、回合紀錄與 gap 分析之完整脈絡見 **[archive/PLAN_phase1.md](archive/PLAN_phase1.md)**。本檔下方「特徵整合計畫」僅保留 **測試契約（R147 等）** 所需之最小摘要。

---

**Current execution plan**: [PLAN_phase2_p0_p1.md](PLAN_phase2_p0_p1.md) (Phase 2 P0–P1)。

**STATUS 日誌**（執行與 code review 流水帳）：見 [STATUS.md](STATUS.md)。較舊之長段已分批移至 [archive/STATUS_archive.md](archive/STATUS_archive.md)（最近一次：**2026-03-22**，Phase 2 前結構整理起至 Train–Serve Parity 2026-03-16 等區塊）。

---

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
| **§6 測試** | ✅ **自動化已覆蓋計畫主力**；⏳ **可選補強** | review_risks／integration／單元；含 **`test_review_risks_pipeline_plan_section6_contract.py`**（AST 單次 `log_artifact_safe`、bundle chunk 不變量、RSS／OOM 守衛、Reviewer 風險 MRE）、`test_pipeline_diagnostics_build_and_bundle.py`、`test_review_risks_pipeline_diagnostics_write_review.py`、**`test_t_pipeline_step_durations_review_mre.py`**（全步驟耗時 Review 風險 MRE）等。doc §6 所列「執行期依存在檔數 mock `log_artifact_safe`」「端到端迷你 pipeline／凍結時鐘」仍為**可選**。 |
| **§7 文件** | ✅ 已實作 | README 三語 artifacts、`credential/mlflow.env.example`。 |
| **§8 驗收** | ⏳ **仍待人工** | 實際訓練／建包／MLflow UI／export run（見 plan doc §8 清單）。 |
| **Import／子程序冷啟動** | ✅ **2026-03-22** | `trainer.core`／`trainer.etl` package `__init__` 瘦身、`trainer` 根包 lazy `config`／`db_conn`、`trainer.trainer`／`trainer.etl_player_profile` 的 `--help` 輕量 argparse、`status_server` 延遲載入 pandas；見 [STATUS.md](STATUS.md)「冷啟動／子程序逾時修補」。 |

**Phase 2 status**（2026-03-22，再對照程式修訂）：**T0–T10 已完成**；**T-PipelineStepDurations** **Done**；**T-DEC031 程式步驟 1–6 Done**（見 [STATUS.md](STATUS.md)）。**T-OnlineCalibration／DEC-032（選阈與 backtester oracle）**：**部分完成** — `threshold_selection` 單次 PR、`searchsorted`、per-hour 參數 sanitize、非二元 fallback、`select_threshold_dec026` 別名；`compute_micro_metrics` oracle **僅 rated** 且四 recall 共用一條 PR 曲線（見 STATUS「DEC-032／T-OnlineCalibration」）。**Review 契約測試**：`test_threshold_dec032_review_risks_mre.py`、`test_status_review_20260322_threshold_mre.py`（STATUS Code Review §1–§8 MRE；§9 skipped 至 runtime 閾值）；**全量驗證** ruff／mypy／pytest 通過見 STATUS「**全量驗證回歸**」。**仍屬本主題待辦**：state DB runtime 閾值、校準腳本、`prediction_ground_truth`、scorer 讀覆寫、（可選）test 集 `_compute_test_metrics_from_scores` 接線共用選阈。**Credential／DB 預設路徑**：程式已支援 **`credential/.env`**；**`STATE_DB_PATH`／`PREDICTION_LOG_DB_PATH` 預設皆在 repo `local_state/`**（細節見 [PLAN_phase2_p0_p1.md](PLAN_phase2_p0_p1.md) **Ordered Tasks → Remaining**）。**仍待（精簡）**：(1) **T-DEC031 步驟 7** — 於 [`doc/training_oom_and_runtime_audit.md`](../../doc/training_oom_and_runtime_audit.md) **補 DEC-031／train 指標一句交叉引用**（該 doc 本體已存在）；(2) **T-TrainingMetricsSchema** — baseline 讀取**仍缺**多數鍵之 **`rated` fallback**（或 A1 寫檔 denormalize）；(3) **Credential／DB** — 僅剩 **營運遷移**、**過時註解**、舊部署路徑；(4) 可選 **Scorer lookback** fallback、**§6／§8** 人工與 mock。**✅ round235／242 collect**：已改 `tests.integration.test_api_server`（2026-03-22，見 [STATUS.md](STATUS.md)「round235／242」）；全量 `pytest tests/` **無需** `--ignore` 該檔。

This file exists so README and review tests (R384, R147) that reference `.cursor/plans/PLAN.md` pass. The **Phase 1（已結案）** 一節與下方 **特徵整合計畫** 摘要並存：後者仍服務 round147 契約（該節不含 Step 9+）。

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
