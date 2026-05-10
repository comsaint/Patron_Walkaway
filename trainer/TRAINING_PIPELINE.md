# 訓練 Pipeline 步驟（I/O 速覽）

主入口：`python -m trainer.trainer …`（`trainer/trainer.py` → `trainer.training.trainer.run_pipeline` → `trainer.training.pipeline_run_core.run_pipeline_core`）。

步驟編號與 log 前綴 `Step N/11` 一致：**共 11 段（0–10）**，另含可選 **7b**。

## 路徑約定（預設）

| 用途 | 預設位置（相對 repo 根目錄） |
|------|------------------------------|
| Chunk Parquet | `trainer/.data/chunks/`（`CHUNK_DIR`） |
| Canonical mapping | `data/canonical_mapping.parquet`、`data/canonical_mapping.cutoff.json`（`LOCAL_PARQUET_DIR` 下） |
| Feature spec | `trainer/feature_spec/feature_candidates.yaml`（`FEATURE_SPEC_PATH`） |
| 模型產物 | `out/models/<model_version>/`（`DEFAULT_MODEL_DIR`，見 `trainer/core/_config_env_paths.py`） |
| Latest manifest | `out/models/_latest_model_manifest.json` |
| L2 auto-bundle（local 預設） | `data/l2_training_bundle/`（可用 `--l2-auto-bundle-dir` 覆寫） |

## 主流程表格

| 步驟 | 做什麼 | 為何 | 主要輸入 | 主要輸出 |
|------|--------|------|----------|----------|
| **0/11** | 資料源 preflight（local bridge 就緒或 ClickHouse 可連） | 在重計算前先驗證資料可讀，避免跑到一半才因連線或 manifest 失敗浪費時間與資源 | CLI、`--use-local-parquet`、連線／manifest 設定 | 通過則繼續；失敗則中止 |
| **1/11** | 解析訓練時間窗並建立 chunk 清單（單一 window） | 把訓練範圍固定成可迭代的 workload 邊界，後續 materialize、快取與重試才有一致單元 | `--start`/`--end` 或 `--days`；local 且未指定起訖時可對齊資料末端 | `chunks`（後續索引） |
| **2/11** | 依 window 劃分，**僅**為 identity cutoff 算出 `train_end` | B1／防 identity leakage：canonical cutoff 必須對齊「訓練窗內允許的最後邊界」，語意上與列級 split 的 `train_end` 分開 | `chunks` | `train_end`（給 canonical cutoff 用） |
| **3/11** | 建或載入 canonical identity mapping、dummy player 清單 | 將多 `player_id` 對齊同一真人（`canonical_id`）、排除 dummy；沒有此表後續 PIT 與聚合特徵無法一致 | session／連線、`train_end`、`--rebuild-canonical-mapping`、既有 `canonical_mapping.*` | `canonical_map`；通常寫入 `data/canonical_mapping.parquet` + `data/canonical_mapping.cutoff.json` |
| **4/11** | 若 spec 啟用 `player_run_asset`：確保 L1 run 資產就緒 | 該類特徵依賴 L1 形態的 run 事實表；先就緒可「失敗快」，避免 Step 6 中途才缺檔 | `feature_spec`、local 分層資料 | 就緒確認；缺資料則錯誤中止 |
| **5/11** | 多數路徑：**不**預載 player profile（改由 chunk 內 materialize） | 降低峰值 RAM、避免整表預載；與現行 layered／chunk 內 materialize 設計一致 | Step 4 的 gate | 通常無獨立產物 |
| **6/11** | 逐 chunk：DQ、標籤、特徵 materialization、identity／PIT、chunk cache／impact orchestrator | 把 raw 轉成可訓練的中間矩陣；chunk 快取與 orchestrator 可省重算、支援局部失效重跑 | `chunks`、`canonical_map`、feature spec、`--force-recompute`、負樣本／OOM 相關設定 | 每 chunk 的 materialized Parquet（`trainer/.data/chunks/`） |
| **7/11** | 讀取 chunk、排序、**列級** train／valid／test split（DuckDB 優先，必要時 pandas） | 依時間排序後做 holdout，使 valid／test 可作 leakage-aware 評估；DuckDB 路徑可減少全量進 RAM | chunk Parquet 路徑、`TRAIN_SPLIT_FRAC`／`VALID_SPLIT_FRAC` | train／valid／test（記憶體 DataFrame 或 on-disk Parquet，依設定） |
| **7b/11**（可選） | 僅對 **train** 做負樣本下採樣 | 負樣本極多時壓低訓練端記憶體與時間；valid／test 維持完整分布以利無偏指標 | train split、effective neg sample 比例 | 更新後的 train（檔或 DataFrame） |
| **8/11** | 物化 **L2 training bundle**；對 train 做 bounded **feature screening** | Bundle 固定可重現的 split 快照供訓練與稽核；screening 在有限樣本上刪弱特徵，降維與過擬合風險 | 上一步 splits、feature spec | L2 bundle 目錄（manifest 與 split 匯出）；`active_feature_cols` |
| **9/11** | 訓練 rated GBM（Optuna、bakeoff、高額客分段等依設定） | 在選定特徵上擬合主模型（與可選 challenger），產出可評分權重與驗證指標 | L2 splits、`active_feature_cols`、`--skip-optuna`、`--no-gbm-bakeoff` 等 | 模型訓練結果、`combined_metrics` |
| **10/11** | 寫入模型產物、metadata、pipeline diagnostics、latest manifest、MLflow | 固化產物供 serving／回溯；manifest 讓下游能穩定解析「最新一版」而不猜路徑 | 上一步產物、window／split lineage | `out/models/<model_version>/` 下整包（如 `model.pkl`、`training_metrics*.json`、`model_metadata.json` 等）+ `_latest_model_manifest.json` |

## 捷徑（會少跑前面步驟）

| 情況 | 為何 | 實際從哪裡開始 | 略過 |
|------|------|----------------|------|
| `--l2-training-bundle DIR` | 已有他處或先前 run 物化好的 split，可跳過 ETL／chunk 以加速或做固定對照實驗 | 約 **8→10**（bundle 內已含 split） | 通常 **1–7** |
| `--use-local-parquet` 且 **L2 auto-cache hit**（Step 3 後早退） | 窗、spec、fingerprint 等鍵未變時重用物化結果，省 Step 4–7 的 I/O 與 CPU | **8→10** 從快取 bundle | **4–7** 等 chunk 路徑 |
| `--no-l2-auto-bundle` | 需要完整 in-process 路徑除錯，或要明確不依賴 auto-bundle／早退語意時使用 | 完整跑完 Step 7 後再物化 bundle 並走 8–10 | 不套用上述 auto-cache 早退 |

實作參考：`trainer/training/pipeline_run_core.py`、`trainer/training/pipeline_l2_bundle.py`。
