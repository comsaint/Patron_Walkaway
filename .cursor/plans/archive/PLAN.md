# Plan index

## Phase 1（已結案）

Phase 1 訓練／特徵／serving 主線已結案。歷史執行細節、回合紀錄與 gap 分析之完整脈絡見 **[archive/PLAN_phase1.md](archive/PLAN_phase1.md)**。本檔下方「特徵整合計畫」僅保留 **測試契約（R147 等）** 所需之最小摘要。

---

**Current execution plan**: [PATCH_20260324.md](PATCH_20260324.md)（Task 1–7，含 Task 3 / Phase 3 收斂驗證）。

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

**Phase 2 status**（2026-03-22 末次修訂 · 含 Code Review **實裝硬化**）：**T0–T10 已完成**；**T-PipelineStepDurations** **Done**；**T-DEC031 程式步驟 1–6 Done**；**T-DEC031 步驟 7（doc 交叉引用）✅** — 見 [`doc/training_oom_and_runtime_audit.md`](../../doc/training_oom_and_runtime_audit.md) 與 STATUS「Phase 2 剩餘項落地」。**T-TrainingMetricsSchema（讀取端）✅** — `run_r1_r6_analysis._load_training_metrics_baseline` 對 `test_precision_at_recall_*` 等支援 **`rated`／`rated.metrics` fallback**；artifact 另寫 **`threshold_selected_at_recall_floor`**。**Scorer lookback ✅** — `SCORER_LOOKBACK_HOURS` env 非法／≤0 → **8**；超 **`SCORER_LOOKBACK_HOURS_MAX`**（預設 8760）→ **cap**（避免 `timedelta` 溢位）。**T-OnlineCalibration／DEC-032**：**MVP 完成** — state DB **`runtime_rated_threshold`**、**`upsert_runtime_rated_threshold`** 區間驗證（**`ValueError`**）、**`read_effective_runtime_rated_threshold`**（可選 **`RUNTIME_THRESHOLD_MAX_AGE_HOURS`**；TTL 下空白 **`updated_at`** → bundle）、`prediction_log.db` 上 **`prediction_ground_truth`／`calibration_runs`** schema、CLI（**`--init-schema` 拒空路徑**／`--set-runtime-threshold`）；**仍待** CH 標註／自動 PR 校準閉環、（可選）test 集選阈接線、（可選）**`CALIBRATE_ALLOW_WRITE` 閘門**。詳見 [STATUS.md](STATUS.md)「Phase 2 剩餘項落地」與「**Phase 2 Code Review 風險實裝修正**」。**Credential／DB**：預設路徑已對齊 **`local_state/`**；**仍待營運**搬移舊 env／分散路徑。**仍待（精簡）**：Pipeline **§6 可選**、**§8 人工**。**✅ round235／242 collect** — 見 STATUS。

---

## Patch Plan 狀態（2026-03-24）

來源：[`PATCH_20260324.md`](PATCH_20260324.md)

| Item | 狀態 | 備註 |
|------|------|------|
| Task 1 | ✅ Done | Scorer payout-age cap + deploy flush 參數 |
| Task 2 | ✅ Done | `/alerts`、`/validation` 無參數預設 1h |
| Task 3 Phase 0/1/2/4 | ✅ Done | 基線、validator 優化、API SQL 下推完成 |
| Task 3 Phase 3 | ⏳ In progress | 工具/runbook/單測已落地；**仍待** p95 與 alerts 整合比對之**實測輸出** |
| Task 3 Phase 5 | ⏳ In progress | 設計分析稿已具；**仍待**補齊實測欄位（rows/latency/frequency/FINAL_used）與 PATCH 宣告 Done |
| Task 4 | ✅ Done | deploy 降噪 + validator 15m/1h KPI |
| Task 5 | 📝 Planned | 動態 K 取代固定 top-50（見 PATCH Task 5） |
| Task 6 | ✅ Done | 移除 `gap_started_before_alert` early return |
| Task 7 | ⏳ In progress | **R1–R6 MVP** 已實作；**2026-04-07**：R6 **預設開**（`config.chunk_two_stage_cache_enabled`）、local **`data_hash` fp_v2**（無 mtime，含 row group + schema digest）— 見 `PLAN_chunk_cache_portable_hit.md`、DEC-039、STATUS 同日條目；**仍待**：canonical_map／filter／schema／原子寫、合併驗證、DoD 分 Phase 計數（見 STATUS Review） |

### 本輪驗證狀態（實作健康度）

- `ruff check trainer package tests`：✅（**2026-03-25**：`trainer/training/trainer.py` 以 `importlib.import_module` 載入 `trainer.core.config`，消除 E402）
- `mypy trainer/serving/scorer.py trainer/serving/validator.py package/deploy/main.py`（`--follow-imports skip --ignore-missing-imports`）：✅
- `pytest tests -q`：✅（**1514** passed, 64 skipped, 16 subtests passed；**2026-03-25** 全量）

### LightGBM GPU（Phase A，2026-03-25）

| 項目 | 狀態 | 備註 |
|------|------|------|
| 訓練 `device_type` cpu/gpu、CLI `--lgbm-device`、probe／fallback、metrics／MLflow | ✅ 已實作 | 見 `GPU_enable_plan.md`、STATUS「LightGBM GPU Phase A」 |
| Review MRE（R1–R8 現行行為） | ✅ 已落地 | `tests/review_risks/test_lightgbm_gpu_phase_a_review_risks_mre.py` |
| **仍待（可選）** | ⏳ | Review 建議之 **R2 正規化**、**R4 hyperparams 防覆寫**、**R7 n_jobs 上限**、**R1 非 run_pipeline 路徑**、Linux **cuda**、**R8 skip probe** 等 — 未納入本輪實作；若實裝須同步改 MRE 斷言 |
| Phase B（Optuna 平行多 GPU） | 📝 Planned | 維持暫緩（見 GPU 計畫） |

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
