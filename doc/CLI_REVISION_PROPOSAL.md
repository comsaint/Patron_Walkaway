# 命令列參數修訂建議（CLI Revision Proposal）

本文件根據目前程式碼狀態，對各模組的 command-line 參數提出修訂建議，並標註與 README / 說明文字不一致之處。

---

## 1. trainer.trainer（訓練流程）

### 1.1 目前參數與程式對應

| 參數 | 程式使用處 | 建議 |
|------|------------|------|
| `--start` / `--end` | `parse_window(args)` | 保留 |
| `--days` | `parse_window`、`_detect_local_data_end` 調整視窗 | 保留 |
| `--use-local-parquet` | `run_pipeline` → `use_local` | 保留 |
| `--force-recompute` | `run_pipeline` → `force` → `process_chunk(..., force_recompute=force)` | 保留 |
| `--skip-optuna` | `run_pipeline` → `skip_optuna`；`--fast-mode` 會覆寫為 True | 保留 |
| `--recent-chunks` | `run_pipeline` 修剪 `chunks` | 保留 |
| `--fast-mode` | `run_pipeline` → `fast_mode`，並隱含 `skip_optuna`、`no_afg` | 保留 |
| `--no-afg` | `run_pipeline` → `no_afg`；跳過 **Track LLM**（DuckDB + feature spec） | 保留，**說明文字需統一** |
| `--fast-mode-no-preload` | `run_pipeline` → `no_preload`，傳入 profile backfill | 保留 |
| `--sample-rated` | `run_pipeline` → `sample_rated_n` | 保留 |
| `--no-month-end-snapshots` | `dest="month_end_snapshots"`，但 run_pipeline **未使用**此屬性 | **建議：標記為 deprecated 或移除** |

### 1.2 術語與文件一致性

- **程式實際行為**：`--no-afg` 跳過的是 **Track LLM**（DuckDB + Feature Spec YAML），不是 Featuretools DFS。
- **README / 部分 help** 仍寫「Track A (Featuretools DFS)」「Track B」：
  - 程式內已統一為 **Track Human**、**Track LLM**、**Track B (legacy)**；**Track A** 在程式裡已不再使用。
- **建議**：
  - 在 **trainer.py** 的 `--no-afg` help 中維持現有正確描述（Track LLM），無需改為 Track A。
  - 在 **README** 的「Trainer 指令參數」表中，將 `--no-afg` 說明改為：「跳過 **Track LLM**（DuckDB + feature spec），僅用 Track Human + profile + legacy」；並可註明「與舊文件中的 Track A 無關，現架構為 Track LLM / Track Human / Track B」。

### 1.3 廢棄參數

- **`--no-month-end-snapshots`**：  
  - `run_pipeline` 完全沒有讀取 `args.month_end_snapshots`，月結 snapshot 行為已固定。
  - **建議**：  
  - **選項 A**：從 `ArgumentParser` 移除該參數，避免誤解。  
  - **選項 B**：保留參數但標為 `deprecated`，並在 help 寫明「已無效，保留僅供向後相容」。

---

## 2. trainer.backtester（回測）

### 2.1 目前參數

| 參數 | 說明 | 建議 |
|------|------|------|
| `--start` / `--end` | 回測視窗 | 保留 |
| `--use-local-parquet` | 從 `data/*.parquet` 讀取 | 保留 |
| `--skip-optuna` | 不跑 Optuna 閾值搜尋 | 保留 |
| `--n-trials` | Optuna 試驗次數，預設 `config.OPTUNA_N_TRIALS` | 保留 |

### 2.2 建議新增

- **`--model-dir`**（可選）：  
  - 目前 `load_dual_artifacts()` 僅從固定 `MODEL_DIR`（`trainer/models`）讀取。  
  - **建議**：與 scorer 一致，新增 `--model-dir`，讓回測可指定不同目錄的 artifact（例如 A/B 模型比較、歷史版本）。

---

## 3. trainer.scorer（即時評分）

### 3.1 目前參數

| 參數 | 說明 | 建議 |
|------|------|------|
| `--interval` | 輪詢間隔（秒） | 保留 |
| `--lookback-hours` | 每輪拉取歷史小時數 | 保留 |
| `--once` | 單次執行後結束 | 保留 |
| `--model-dir` | 覆寫 model 目錄 | 保留 |
| `--log-level` | 日誌等級 | **需修正** |

### 3.2 建議修正

- **`--log-level`**：  
  - 目前 `choices=["DEBUG", "INFO", "WARNING"]`，缺少 `"ERROR"`。  
  - **建議**：改為 `choices=["DEBUG", "INFO", "WARNING", "ERROR"]`，與 etl_player_profile 等模組一致。

---

## 4. trainer.validator（告警驗證）

| 參數 | 說明 | 建議 |
|------|------|------|
| `--interval` | 輪詢間隔（秒） | 保留 |
| `--once` | 單次驗證後結束 | 保留 |
| `--force-finalize` | 強制將 PENDING 結案 | 保留 |

目前無需變更；若未來要支援多環境，可考慮加 `--db` 指定 SQLite 路徑（與 empty_state_db 類似）。

---

## 5. trainer.etl_player_profile（Profile ETL）

| 參數 | 說明 | 建議 |
|------|------|------|
| `--snapshot-date` | 單日 snapshot | 保留 |
| `--start-date` / `--end-date` | 回填區間 | 保留 |
| `--local-parquet` | 本地 Parquet 模式 | 保留（與 trainer 的 `--use-local-parquet` 命名不同，可於 README 註明） |
| `--log-level` | 日誌等級 | 保留 |

無需變更；僅在 README 註明 ETL 使用 `--local-parquet` 而 trainer/backtester 使用 `--use-local-parquet` 即可。

---

## 6. 其他腳本（簡要）

- **empty_state_db.py**：`--db`、`--yes` — 符合用途，無需變更。
- **auto_build_player_profile.py**：`--start-date`、`--end-date` 等 — 依腳本用途，無需變更。
- **view_alerts.py**、**analyze_session_history*.py**：各司其職，無需變更。

---

## 7. README 修訂摘要

1. **Trainer 指令參數表**（繁/簡體）：  
   - 將 `--no-afg` 說明改為「跳過 **Track LLM**（DuckDB + feature spec）」，並可註明與 Track Human / Track B 的關係。  
   - 若保留 `--no-month-end-snapshots`：在表內註明「已廢棄、無效」；若從程式移除，則從 README 刪除該列。

2. **使用方式**：  
   - 範例指令已正確，可維持；若有加 `--model-dir` 的 backtester 範例，可在實作 `--model-dir` 後補上。

3. **Backtester / Scorer**：  
   - Backtester 若新增 `--model-dir`，需在 README 補一行說明。  
   - Scorer 的 `--log-level` 若加上 ERROR，無需特別寫在 README（一般使用者較少改）。

---

## 8. 實作優先順序建議

| 優先 | 項目 | 影響 |
|------|------|------|
| 1 | 修正 scorer `--log-level` 加入 `ERROR` | 小改動，避免傳入 ERROR 時報錯 |
| 2 | README 中 `--no-afg` 改為「Track LLM」術語 | 文件與程式一致 |
| 3 | 處理 `--no-month-end-snapshots`（deprecated 或移除） | 減少混淆 |
| 4 | Backtester 新增 `--model-dir` | 功能擴充，與 scorer 對齊 |

若你希望，我可以依上述建議直接改動對應的 `.py` 與 README 段落。
