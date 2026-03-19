# Phase 2 P0-P1 Work Plan

> 依據：
> [doc/phase2_p0_p1_implementation_plan.md](doc/phase2_p0_p1_implementation_plan.md)
> [ssot/phase2_p0_p1_ssot.md](ssot/phase2_p0_p1_ssot.md)
>
> 本文件為 **execution-level** 工作計畫，直接對應實作順序、檔案級修改、測試、相依、rollback 與 DoD。

---

## Guardrails

- **不修改 `build/lib/**`**。如未來需要打包，應由正式 build 流程重新產出。
- **Scorer hot path 不做任何網路 I/O**，也不在記憶體中累積 5-15 分鐘資料。
- **Scorer 僅 append 到 SQLite**；export 由獨立 process 執行。
- **Prediction log 不做 per-row export update**；使用 **watermark**（例如 `last_exported_prediction_id`）追蹤匯出進度。
- **SQLite 沿用 WAL mode**，避免 scorer 寫入與 export 讀取互相阻塞。
- **MLflow artifact 由 client 直傳 GCS**，不讓 e2-micro 代理大檔。
- **Evidently 僅 manual / ad-hoc**，且明確保留 OOM 風險警告，不預先鎖死抽樣策略。

---

## External Prerequisites

以下不是 repo 內程式修改，但沒有它們，實作與驗證會卡住：

1. GCP MLflow Tracking Server 可連線。
2. GCS bucket 與 service account 權限可用。
3. 匯出程式的執行位置已決定：
   - 與 scorer 同機器跑 cron / Task Scheduler，或
   - 另一台可讀 SQLite 且可連 GCP 的機器。
4. Prediction log 儲存位置已決策：
   - 決策：**拆分獨立的 SQLite 檔案（例如 `prediction_log.db`）**。
   - 理由：Scorer 寫入預測日誌頻率極高，獨立檔案可與 `state.db` 的 API 查詢與 Validator 讀寫在 I/O 層級上實體隔離，徹底避免高頻寫入與大量 export 讀取干擾主系統。
5. 確認 export 預設格式：
   - 預設建議：**Parquet + snappy**
   - 理由：repo 已有 `pyarrow`，snappy CPU 成本較低，對 laptop 較穩；若日後頻寬壓力更大，再評估 gzip/zstd。

---

## High-Level Execution Order

```mermaid
flowchart LR
  prereq[InfraPrereq] --> p01[P0.1SharedMlflow]
  prereq --> p11[P1.1ScorerSqlite]
  p01 --> p02[P0.1TrainerWrite]
  p02 --> p03[P0Docs]
  p02 --> t12[T12 Failed run log]
  p01 --> t11[T11 Local MLflow env]
  p11 --> p12[P1.1ExportScript]
  p12 --> p13[P1.1Retention]
  p12 --> p14[P1.2P1.3Runbook]
  p12 --> p15[P1.4Evidently]
  p15 --> p16[P1.5Skew]
  p12 --> p17[P1.6DriftTemplate]
```

---

## Ordered Tasks

**Current status**（更新於 2026-03-19）：**T0**–**T11** 已完成。**T12 Step 1**（單一 run 涵蓋整次 pipeline、失敗時寫入 tag status=FAILED／error 並 re-raise）已實作；**T12 success diagnostics（T12.2 Step 2）** 已實作；**T12 failure diagnostics params（T12 optional follow-on）** 已實作；並完成 Code Review §1（`has_active_run()` 例外 warning）。本輪額外 production 修補已落地：`log_metrics_safe` 過濾 NaN/inf 與 failure params 長字串截斷，導致對應 contract 測試由 xfail -> xpass。tests/typecheck/lint 相關驗證通過，見 STATUS.md。

**Remaining items**（依執行順序）：
- 其餘 Phase 2 P0–P1 無強制待辦；若要進一步降低風險，可再針對 Code Review §2–§5 的「效能/語義」項（例如 OOM pre-check I/O 成本與 RSS peak 真實最大值語義）做後續優化。

---

### T0. Pre-flight decisions and dependency audit — ✅ Done

- **Depends on**: none
- **Goal**: 凍結最少必要決策，避免後續返工。
- **Files**
  - [requirements.txt](requirements.txt): 確認 training / local script 依賴是否補 `mlflow`
  - [package/deploy/requirements.txt](package/deploy/requirements.txt): 若 export script 在 deploy 環境跑，補 `mlflow`
  - [deploy_dist/requirements.txt](deploy_dist/requirements.txt): 若 deploy_dist 也需要 export script，補 `mlflow`
  - [pyproject.toml](pyproject.toml): 若專案以此作為主依賴來源，也同步更新
- **Implementation notes**
  - `mlflow` 目前 repo **尚未存在**，這是新增依賴。
  - `evidently` 目前 repo **尚未存在**，但它只用於手動 DQ/drift 腳本，不必進 deploy runtime requirements，除非你決定在 deploy 機器上手動跑。
  - 明確排除 `build/lib/**`。
- **Test steps**
  1. 確認依賴檔是否一致，不出現 root 有 `mlflow`、deploy 沒有的半套狀態。
  2. 確認 `pyarrow` 已存在，可支撐 Parquet export。
- **Rollback**
  - 依賴變更尚未進 code，可直接回退 requirements / pyproject 修改。
- **Definition of done**
  - `mlflow` 的安裝邊界已定義清楚。
  - `evidently` 是否只放 root/local script 已明確。
  - 不會有人去改 `build/lib/**`。

### T1. Shared MLflow utility and provenance schema — ✅ Done

- **Depends on**: T0
- **Goal**: 避免 trainer 與 export script 各自手寫 MLflow 邏輯。
- **Files**
  - New: `trainer/core/mlflow_utils.py`
  - New: `doc/phase2_provenance_schema.md`
  - [trainer/core/config.py](trainer/core/config.py)
- **Implementation notes**
  - 在 `trainer/core/mlflow_utils.py` 提供：
    - 讀取 `MLFLOW_TRACKING_URI`
    - safe no-op / warning only 行為
    - 建立或寫入 run/tag/artifact 的 helper
  - provenance schema 至少包含：
    - `model_version`
    - `git_commit`
    - `training_window_start`
    - `training_window_end`
    - `artifact_dir`
    - `feature_spec_path` / feature schema version
    - `training_metrics_path`
  - 不要求 trainer 在 URI 不可達時 fail。
- **Test steps**
  1. 為 utility 補 unit test：URI 未設時僅 warning，不 raise。
  2. mock MLflow client，驗證 tags / params payload 內容。
- **Rollback**
  - trainer / export script 尚未接上前，可單獨回退 utility。
- **Definition of done**
  - repo 內 MLflow 共用邏輯只有一份。
  - provenance key naming 已文檔化。

### T2. P0.1 trainer provenance write — ✅ Done

- **Depends on**: T1
- **Goal**: 在訓練 artifact 完成後，把 provenance 寫到 GCP MLflow。
- **Files**
  - [trainer/training/trainer.py](trainer/training/trainer.py)
  - New: `tests/review_risks/test_review_risks_phase2_mlflow_trainer.py`
  - New: `tests/integration/test_phase2_trainer_mlflow.py`
- **Implementation notes**
  - 接點就在 `save_artifact_bundle(...)` 後、stale artifact cleanup 前後皆可，但要保證：
    - `model_version` 已生成
    - artifact bundle 已落地
  - 建議新增 helper 呼叫，例如 `_log_training_provenance_to_mlflow(...)`
  - 失敗策略：
    - 無 URI / 無法連線 / GCP 失敗 -> `logger.warning(...)`，訓練仍成功
  - 不做本地 fallback MLflow。
- **Test steps**
  1. 跑既有 [tests/integration/test_trainer.py](tests/integration/test_trainer.py) 確認沒回歸。
  2. 新增 mock MLflow integration test，驗證 trainer 在成功與失敗路徑都不 crash。
  3. 手動：設 `MLFLOW_TRACKING_URI` 到測試 server，執行 `run_pipeline`，確認 MLflow run 可查到 `model_version` 與 artifact path。
- **Rollback**
  - 移除 trainer 中的 helper 呼叫即可。
  - 或 unset `MLFLOW_TRACKING_URI` 暫停功能。
- **Definition of done**
  - 給定 `model_version`，能在 MLflow 找到 provenance。
  - URI 不可達時，訓練仍完成。

### T3. P0.2 rollback and provenance query docs — ✅ Done

- **Depends on**: T2
- **Goal**: 將 P0.2「整目錄 rollback」與查詢方式文件化。
- **Files**
  - New: `doc/phase2_provenance_query_runbook.md`
  - New: `doc/phase2_model_rollback_runbook.md`
- **Implementation notes**
  - 明確寫：
    - rollback 只能替換整個 artifact directory / package
    - 禁止只換 `model.pkl`
    - 如何用 `model_version` 查 MLflow provenance
- **Test steps**
  1. 文件 review：用文件步驟實際查一次既有 / 測試 run。
  2. 文件 review：讓另一位維護者照 runbook 模擬 rollback 步驟。
- **Rollback**
  - 純文件，可直接回退。
- **Definition of done**
  - rollback 與 provenance query 均有可操作 runbook。

### T4. P1.1 scorer prediction log schema and write path — ✅ Done

- **Depends on**: T0
- **Goal**: scorer 每次 scoring 後，將必要欄位 append 到 SQLite，不做網路 I/O。
- **Files**
  - [trainer/serving/scorer.py](trainer/serving/scorer.py)
  - [trainer/core/config.py](trainer/core/config.py)
  - New: `tests/review_risks/test_review_risks_phase2_prediction_log_schema.py`
  - New: `tests/integration/test_phase2_prediction_log_sqlite.py`
- **Implementation notes**
  - **不要**在 hot path 寫 full feature vector；只寫最小必要欄位，避免 SQLite 爆量：
    - `prediction_id INTEGER PRIMARY KEY AUTOINCREMENT`
    - `scored_at`
    - `bet_id`
    - `session_id`
    - `player_id`
    - `canonical_id`
    - `casino_player_id`
    - `table_id`
    - `model_version`
    - `score`
    - `margin`
    - `is_alert`
    - `is_rated_obs`
  - 新增 `PREDICTION_LOG_DB_PATH` (預設例如 `local_state/prediction_log.db`) 並獨立開啟連線與 WAL mode。
  - 在 `_score_df(...)` 之後、alert filter 之前或之後插入 prediction log 都可，但要清楚定義：
    - 若目標是「每筆推論」就應在 alert filter **之前**，保存全部 scored rows。
  - 以 batch insert 寫入，不逐 row execute。
- **Test steps**
  1. 跑既有 [tests/integration/test_scorer.py](tests/integration/test_scorer.py)。
  2. 新增 integration test：temp SQLite + mocked artifacts，驗證 scorer 會建立 `prediction_log` 並寫入 rows。
  3. 手動：run scorer once，直接查 SQLite row count 增加。
- **Rollback**
  - 以 config / env 關閉 prediction log。
  - 不必先 drop table；可保留空功能。
- **Definition of done**
  - scorer 可在不連 GCP 的情況下持續寫入 prediction log。
  - 寫入不影響 alerts 主流程。

### T5. P1.1 export watermark, export runner, and MLflow artifact upload

- **Depends on**: T1, T4
- **Goal**: 用獨立 process 匯出 SQLite prediction log 到 MLflow artifact。
- **Files**
  - New: `trainer/scripts/export_predictions_to_mlflow.py`
  - [trainer/serving/scorer.py](trainer/serving/scorer.py) 或同 DB schema 初始處：新增 export watermark / audit metadata
  - [trainer/core/config.py](trainer/core/config.py)
  - New: `tests/integration/test_phase2_prediction_export.py`
- **Implementation notes**
  - **關鍵修正**：不用 `exported_at` per-row update。
  - 方案：
    - 在 `meta` table 加 `prediction_export_last_id`
    - export script 每次讀：
      - `prediction_id > last_exported_id`
      - `scored_at <= now - safety_lag`
      - `ORDER BY prediction_id`
      - `LIMIT batch_rows`
    - 成功上傳後，只更新一次 watermark
  - 可選增加 `prediction_export_runs` audit table，記錄每次 export：
    - start / end time
    - min/max prediction_id
    - row_count
    - artifact path
    - success / error
  - 預設輸出格式：**Parquet + snappy**
  - 路徑建議：依 `model_version/date/hour` 分層，便於查詢與清理。
  - 失敗策略：
    - 上傳失敗 -> 不移動 watermark，不刪資料
    - 下次可重試
- **Test steps**
  1. temp SQLite 建假資料，跑 export script，驗證：
     - 只匯出 watermark 後資料
     - 成功後 watermark 前進
  2. mock MLflow/GCS 失敗，驗證 watermark 不前進、資料保留。
  3. 手動：本機 cron / once 模式跑一輪，確認 artifact 到 MLflow。
- **Rollback**
  - 停掉 export 排程。
  - scorer 仍可照常運作。
- **Definition of done**
  - export 為獨立 process。
  - 失敗不丟資料。
  - 不存在 per-row export update 的高寫入設計。

### T6. P1.1 retention and cleanup

- **Depends on**: T5
- **Goal**: 避免 prediction log 無限成長。
- **Files**
  - [trainer/scripts/export_predictions_to_mlflow.py](trainer/scripts/export_predictions_to_mlflow.py)
  - [trainer/core/config.py](trainer/core/config.py)
  - New: `tests/integration/test_phase2_prediction_retention.py`
- **Implementation notes**
  - retention cleanup 不放在 scorer hot path。
  - export 成功後由 export script 做 **bounded cleanup**：
    - 只刪 `prediction_id <= watermark`
    - 且 `scored_at < retention_cutoff`
    - 分批 delete，避免長 transaction
  - 預設 retention 天數寫進 config。
- **Test steps**
  1. 模擬舊資料 + 新資料，驗證只清理已成功匯出且超過 retention 的 rows。
  2. 驗證未匯出資料不會被清掉。
- **Rollback**
  - 關閉 cleanup，保留資料。
- **Definition of done**
  - DB size 有上界控制。
  - cleanup 不碰未匯出資料。

### T7. P1.2 / P1.3 alert conditions, runbook, message format — ✅ Done

- **Depends on**: T4, T5
- **Goal**: 先把人要怎麼看、怎麼處理寫清楚，即使不做 Slack/email。
- **Files**
  - New: `doc/phase2_alert_runbook.md`
  - New: `doc/phase2_alert_message_format.md`
- **Implementation notes**
  - 至少覆蓋：
    - scorer / export / validator / Evidently 常見異常
    - 誰看、看哪個 DB / artifact / report
    - human-oriented message 應包含哪些欄位
- **Test steps**
  1. 文件 walkthrough：模擬 3 個情境
     - export 失敗
     - validator precision 掉落
     - drift report 異常
- **Rollback**
  - 純文件，可直接回退。
- **Definition of done**
  - 有人能依文件完成 triage。

### T8. P1.4 local Evidently report tooling — ✅ Done

- **Depends on**: T0, T5
- **Goal**: 提供可手動執行的 DQ / drift 報告產生工具，而不只是文件。
- **Files**
  - New: `trainer/scripts/generate_evidently_report.py`
  - New: `doc/phase2_evidently_usage.md`
  - [requirements.txt](requirements.txt) 或 [pyproject.toml](pyproject.toml): 新增 `evidently`
- **Implementation notes**
  - 腳本只做 **manual / ad-hoc**。
  - 輸入建議：
    - reference profile / training snapshot
    - current data file path（由人工挑選或前置匯整）
  - 明確寫出：
    - 報告輸出到本地 `out/` 或 `doc/` 下某固定目錄
    - 可選 sync 到 GCS
    - **OOM 風險警告保留**
  - 不在本任務決定 downsampling / aggregation 實作策略。
- **Test steps**
  1. 小樣本手動跑腳本，確認能產 HTML / JSON 報告。
  2. 驗證無 Evidently 時錯誤訊息清楚。
- **Rollback**
  - 不跑此腳本即可；不影響 scorer / trainer。
- **Definition of done**
  - 至少可手動產生一份本地 Evidently 報告。
  - OOM 風險與操作方式已文件化。

### T9. P1.5 training-serving skew check tooling — ✅ Done

- **Depends on**: T4, T8
- **Goal**: 讓 skew 驗證是可執行流程，不只停留在概念。
- **Files**
  - New: `trainer/scripts/check_training_serving_skew.py`
  - New: `doc/phase2_skew_check_runbook.md`
- **Implementation notes**
  - 先做 **one-shot / manual** 工具即可。
  - 核心輸入：
    - 一批 serving-side ids / timestamps
    - 對應 training-side feature derivation 結果
  - 輸出：
    - 不一致欄位列表
    - 摘要表
    - 可附 CSV / markdown
- **Test steps**
  1. 用小型合成資料驗證一致 / 不一致兩條路徑。
  2. 手動產一份 skew check 輸出。
- **Rollback**
  - 純 script + doc，可獨立回退。
- **Definition of done**
  - 至少能完成一次可重現的 skew 檢查。

### T10. P1.6 drift investigation template and first example report — ✅ Done

- **Depends on**: T5, T7, T8, T9
- **Goal**: 讓 drift 調查有固定產出格式，且能落到 `doc/`。
- **Files**
  - New: `doc/drift_investigation_template.md`
  - New: `doc/phase2_drift_investigation_example.md`
- **Implementation notes**
  - 模板應包含：
    - trigger
    - timeframe
    - model_version
    - evidence used
    - hypotheses
    - checks performed
    - conclusion
    - recommended action
  - example 可用 mock / historical / dry-run 資料，不必等真實事故。
- **Test steps**
  1. 依模板實際填一份 example。
  2. 確認 runbook 中有指向此模板。
- **Rollback**
  - 純文件，可直接回退。
- **Definition of done**
  - repo 內有正式模板與至少一份 example。

### T11. Local MLflow config from project-local file (optional) — ✅ Done

- **Depends on**: T1
- **Goal**: 本機 train / export 預設即帶 MLflow 設定，且**不**將 MLflow 相關變數寫入專案主 `.env`；所有 trials 預設寫入 MLflow。
- **Files**
  - [trainer/core/mlflow_utils.py](trainer/core/mlflow_utils.py)
  - [.gitignore](.gitignore)
  - 使用者建立（不 commit）：`local_state/mlflow.env`
  - 可選：`local_state/mlflow.env.example` 或 doc 說明格式
- **Implementation notes**
  - 在 `mlflow_utils.py` 模組頂層（任一程式 `import` 此模組時）：
    - 由 `Path(__file__).resolve()` 推得 repo 根目錄（`trainer/core/` → 上兩層為 repo root）。
    - 若 `repo_root / "local_state" / "mlflow.env"` 存在，則呼叫 `load_dotenv(該路徑, override=False)`，將該檔內變數注入 `os.environ`。
    - `override=False`：若 process 已設 `MLFLOW_TRACKING_URI` 或 `GOOGLE_APPLICATION_CREDENTIALS`（例如 shell 或系統環境變數），不覆寫。
  - 依賴：專案已有 `python-dotenv`（config 使用），不需新增。
  - `.gitignore`：新增 `local_state/mlflow.env`（或整個 `local_state/`），避免金鑰與 URI 被 commit。
  - `local_state/mlflow.env` 建議內容（兩行，無引號）：
    - `MLFLOW_TRACKING_URI=https://...`
    - `GOOGLE_APPLICATION_CREDENTIALS=<path-to-service-account-key.json>`
  - 主 `.env` 完全不包含上述變數；僅此專用檔負責 MLflow 本機設定。
- **Test steps**
  1. 單元測試：mock 或 temp 建立 `local_state/mlflow.env`，import `mlflow_utils` 後驗證 `os.environ` 含預期鍵（或 `get_tracking_uri()` 回傳該 URI）；無檔時 import 不報錯、不覆寫既有 env。
  2. 可選：integration 測試 — 有檔且 URI 可達時，`is_mlflow_available()` 為 True。
- **Rollback**
  - 移除 `mlflow_utils.py` 頂層的 `load_dotenv(...)` 邏輯即可；不影響既有 `MLFLOW_TRACKING_URI` 由環境變數讀取的行為。
- **Definition of done**
  - 建立 `local_state/mlflow.env` 並設好兩行後，從此專案執行 train / export 無需手動 `export` 環境變數，trials 預設寫入 MLflow。
  - 主 `.env` 仍不包含 MLflow 設定。
- **Code Review follow-up（2026-03-18）**：§1（import 時 try/except，避免 load_dotenv/path 異常導致模組載入失敗）、§2（MLFLOW_ENV_FILE 空字串／空白視為未設）已實作；tests/typecheck/lint 全過，見 STATUS.md「本輪實作：T11 Code Review §1§2 修補」。

### T12. Log failed training runs to MLflow (optional follow-on) — Step 1 ✅ Done

- **Depends on**: T2, T11
- **Step 1 done（2026-03-18）**：單一 run（`train-{start}-{end}-{timestamp}`）、with + try/except、失敗時 `log_tags_safe({"status":"FAILED","error":str(e)[:500]})` 後 raise；`_log_training_provenance_to_mlflow` 有 active run 時只 log 不 start_run。Code Review 風險點已轉成 tests（§1–§4），見 STATUS.md「新增測試：T12 Code Review 風險點」。
- **Goal**: 當訓練 pipeline 在任一步（如 Step 3 canonical mapping OOM）失敗時，也在 MLflow 寫入一筆 run，標記 `status=FAILED`、錯誤訊息（與可選的 failed_step），並寫入 **config、記憶體估計、資料規模** 等，方便在 MLflow UI 區分成功與失敗、排查 OOM／環境問題並改善配置。
- **Files**
  - [trainer/training/trainer.py](trainer/training/trainer.py)
  - [trainer/core/mlflow_utils.py](trainer/core/mlflow_utils.py)
  - 可選：`tests/unit/test_mlflow_utils.py` 或 `tests/integration/test_phase2_trainer_mlflow.py` 補「失敗路徑有 run 且 tag 正確」
- **Implementation notes**
  1. **單一 run 涵蓋整次 pipeline**  
     - 在 `run_pipeline` 內，取得 `effective_start` / `effective_end` 後、Step 1 之前，以 `safe_start_run(run_name=..., tags=...)` 開一個 run。Run 名稱不可依賴 `model_version`（失敗時尚未產生），改為例如 `train-{window_start}-{window_end}-{timestamp}` 或 `train-{timestamp}`。
  2. **整段 pipeline 包在同一個 run 的 context 裡**  
     - 用 `with safe_start_run(run_name=run_name):` 包住從 Step 1 到 Step 10（含 `_log_training_provenance_to_mlflow`）的整段程式，確保成功或失敗離開時 run 都會被結束。
  3. **失敗時寫入狀態與診斷資訊**  
     - 在 `with` 內用 `try/except` 包住 pipeline 本體；在 `except` 中：
       - `log_tags_safe({"status": "FAILED", "error": str(e)[:500]})`，可選 `log_params_safe({"failed_step": current_step})`。
       - **為便於 OOM 與配置改善，失敗時盡量寫入**：`training_window_start` / `training_window_end`、`recent_chunks`、`NEG_SAMPLE_FRAC`（或 `_effective_neg_sample_frac`）、`use_local_parquet`、chunk 數（`len(chunks)`）；若該次 run 已執行 OOM-check，則寫入 est. peak RAM、available、budget（與既有 log 同一組數字）；可選 DuckDB `memory_limit` 等。使 MLflow UI 上可看到「為何失敗、當時 config、記憶體與資料規模」，利於事後調整。
  4. **成功路徑不重複 start_run**  
     - `_log_training_provenance_to_mlflow` 改為：若 `mlflow.active_run()` 已有 run，則**不**再呼叫 `safe_start_run`，僅對當前 run 做 `log_params_safe`（與既有 artifact 寫入）；若沒有 active run（例如單獨呼叫此 helper），則維持現狀（start run + log）。
  5. **風險與注意**  
     - 必須在「所有離開 run_pipeline 的路徑」都結束 run，故一律用 `with safe_start_run(...):` 包住整段，避免漏關。  
     - 不新增依賴；沿用 `log_tags_safe` / `log_params_safe` / `end_run_safe`。MLflow 不可用時 `safe_start_run` 為 no-op，行為與現有一致。
- **Test steps**
  1. 單元或整合：mock pipeline 在 Step 3 拋錯，驗證 MLflow 有對應 run、tag `status=FAILED`、params/tag 含 error（或 failed_step）及 config／資料規模等。
  2. 成功路徑：跑一小段 trainer（如 `--days 1 --use-local-parquet --skip-optuna`），確認 MLflow 仍只有一筆 run、provenance 與 artifact 正確。
  3. 手動：觸發一次 OOM 或人為 exception，在 MLflow UI 確認失敗 run 可見、訊息可讀，且 params 含 window／chunks／NEG_SAMPLE_FRAC／記憶體估計等。
- **Rollback**
  - 移除 `run_pipeline` 頂層的 `with safe_start_run` 與 try/except；還原 `_log_training_provenance_to_mlflow` 為「一律自己 start_run」。即回到「僅成功時寫 MLflow」的行為。
- **Definition of done**
  - 訓練在任一步失敗時，MLflow 會有一筆 run 且可辨識為 FAILED 並含錯誤資訊與 config／記憶體／資料規模（利於 OOM 改善）。
  - 成功完成時仍只有一筆 run，provenance 與 T2 行為一致。
  - 無 MLflow 時行為不變（no-op，不 crash）。

---

## File-Level Edit Summary

### Existing files likely to change

- [trainer/training/trainer.py](trainer/training/trainer.py)
  - 在 `save_artifact_bundle(...)` 後接入 provenance logging。
  - （T12）pipeline 開頭以 `with safe_start_run(...)` 包住整段；失敗時在 except 內 `log_tags_safe` / `log_params_safe`（含 config、chunk 數、OOM-check 估計等）。
- [trainer/serving/scorer.py](trainer/serving/scorer.py)
  - 建立獨立的 `prediction_log.db` 與對應 schema（prediction log + export metadata/audit）
  - 在 `score_once(...)` 中對獨立 DB 進行 batch append
- [trainer/core/config.py](trainer/core/config.py)
  - 新增 Phase 2 相關 env/config
- [trainer/core/mlflow_utils.py](trainer/core/mlflow_utils.py)（T11）
  - 模組頂層：若存在 `repo_root/local_state/mlflow.env` 則 `load_dotenv(..., override=False)`，不寫入主 `.env`
  - （T12）`_log_training_provenance_to_mlflow` 呼叫處：有 active run 時只 log 不 start_run（實作在 trainer 內該 helper 的判斷）
- [.gitignore](.gitignore)（T11）
  - 新增 `local_state/mlflow.env`（或 `local_state/`），避免 MLflow 設定檔被 commit
- [requirements.txt](requirements.txt)
  - 新增 `mlflow`，以及 `evidently`（若 root/local script 需要）
- [package/deploy/requirements.txt](package/deploy/requirements.txt)
  - 若 export script 在 deploy 環境執行，新增 `mlflow`
- [deploy_dist/requirements.txt](deploy_dist/requirements.txt)
  - 同上，視 deploy_dist 是否要跑 export script

### New files likely to be added

- `trainer/core/mlflow_utils.py`
- `trainer/scripts/export_predictions_to_mlflow.py`
- `trainer/scripts/generate_evidently_report.py`
- `trainer/scripts/check_training_serving_skew.py`
- `doc/phase2_provenance_schema.md`
- `doc/phase2_provenance_query_runbook.md`
- `doc/phase2_model_rollback_runbook.md`
- `doc/phase2_alert_runbook.md`
- `doc/phase2_alert_message_format.md`
- `doc/phase2_evidently_usage.md`
- `doc/phase2_skew_check_runbook.md`
- `doc/drift_investigation_template.md`
- `doc/phase2_drift_investigation_example.md`
- new tests under `tests/integration/` and `tests/review_risks/`

---

## Test Plan

### Existing tests to rerun

- [tests/integration/test_trainer.py](tests/integration/test_trainer.py)
- [tests/integration/test_scorer.py](tests/integration/test_scorer.py)
- [tests/integration/test_validator_datetime_naive_hk.py](tests/integration/test_validator_datetime_naive_hk.py)
- relevant `tests/review_risks/**` touching trainer / scorer / validator

### New automated tests to add

- trainer provenance logging:
  - mock MLflow success path
  - missing URI / connection failure path
- scorer prediction log:
  - schema creation
  - append all scored rows, not only alerts
  - WAL-compatible read/write assumptions
- export script:
  - watermark progression
  - failure leaves watermark unchanged
  - retention cleanup only touches exported old rows
- Evidently / skew scripts:
  - small fixture happy-path smoke tests
- T11 local MLflow env:
  - with `local_state/mlflow.env` present, env vars loaded before first `get_tracking_uri()`; without file, no error and no overwrite of existing env
- T12 failed run log:
  - pipeline 於某步失敗時，MLflow 有一筆 run、tag `status=FAILED`、error 及 config／chunk 數／OOM 估計等；成功路徑仍為單一 run

### Manual validation

1. Run trainer with `MLFLOW_TRACKING_URI` unset -> training succeeds, warning only.
2. Run trainer with reachable test tracking URI -> provenance visible in MLflow.
3. (T11) Create `local_state/mlflow.env` with URI and key path -> run trainer/export from repo root without manual `export` -> trials logged to MLflow.
4. (T12) Trigger a pipeline failure (e.g. OOM) -> MLflow UI shows one FAILED run with error and params (config, chunks, memory estimate).
5. Run scorer once -> `prediction_log` row count increases.
6. Run export script with GCP unavailable -> no crash, watermark unchanged, rows remain.
7. Re-run export script with GCP available -> artifact uploaded, watermark advances.
8. Run validator unchanged -> existing behavior preserved.
9. Produce one local Evidently report and one skew-check output.

---

## Rollback Notes

### P0.1 trainer provenance

- Remove trainer helper call, or unset `MLFLOW_TRACKING_URI`.
- No artifact format rollback required if provenance is metadata-only.

### P1.1 scorer prediction log

- Disable via config / env flag.
- Keep SQLite table in place if rollback needs to be low-risk.

### P1.1 export

- Stop cron / scheduled task.
- Leave SQLite data untouched for later retry.

### P1.4-P1.6 scripts/docs

- Independent rollback; no impact on trainer / scorer runtime path.

### T11 local MLflow env

- Remove the `load_dotenv(...)` block at top of `mlflow_utils.py`; MLflow config again only from process env / main `.env` if used.

### T12 failed run log

- Remove the `with safe_start_run` and try/except at top of `run_pipeline`; restore `_log_training_provenance_to_mlflow` to always start its own run. Restores "log to MLflow only on success" behavior.

---

## Phase-Level Definition of Done

### P0 done

- Given a `model_version`, provenance can be found from MLflow (GCP).
- Rollback procedure is documented as whole-artifact only.

### P1.1 done

- scorer appends every scored row to local SQLite without network I/O.
- export runs in a separate process and uploads compressed artifacts to MLflow/GCS.
- export progress uses watermark, not per-row updates.
- GCP outage does not lose prediction rows.
- retention cleanup exists and does not delete unexported rows.

### P1.2-P1.6 done

- alert runbook and alert message format are documented.
- at least one manual Evidently report can be generated locally.
- at least one skew-check run can be performed.
- drift investigation template and one example report exist in `doc/`.

---

## Open Decisions Kept Explicit

這些仍需在實作前或實作中定案，但不阻止本 work plan 開始執行：

1. export batch size、safety lag、retention 天數。
2. export artifact 路徑命名規則。
3. Evidently current data 的前置整理方式。
4. 是否需要 `prediction_export_runs` audit table；本計畫建議加，但若想先簡化，可先只做 `meta` watermark。

