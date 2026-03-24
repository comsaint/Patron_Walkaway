# GPU 啟用計畫 — 訓練管線

**範圍：**僅訓練（`trainer/`、`run_pipeline` / `python -m trainer.training.trainer`）。Scorer、validator、部署推論不在此列（多為純 CPU 環境）。

**現況盤點（repo）：**管線內**沒有** `cupy`、`cudf`、`torch`，也沒有 CUDA 版 DuckDB。**在 Windows 上只有 LightGBM** 可透過 **`device_type=gpu`**（經 NVIDIA 驅動的 **OpenCL**）用 GPU。LightGBM **`device_type=cuda`** 在**上游文件宣告 Windows 不支援**；此工作站等級環境**勿**以 CUDA 路線規劃。

---

## 1. 目標

| 目標 | 說明 |
|------|------|
| **G1** | 同一套程式在**有／無**可用 GPU 的機器上都能跑，預設 **CPU**。 |
| **G2** | 當使用者要求且環境可用時，**Optuna 各 trial** 與**最終 rated 模型訓練**在 `LGBMClassifier` 與 `lgb.train` 各路徑上**一致**使用 LightGBM GPU（OpenCL）。 |
| **G3** | 失敗情境（無 OpenCL、驅動問題、GPU OOM）要**可預期**處理：寫 log + 降級 CPU，或非零退出並提示改設定。 |
| **G4** | 在 **MLflow** / **`training_metrics.json`**（或同等欄位）記錄實際使用的裝置，利於重現。 |

---

## 2. Phase A — 啟用 LightGBM GPU（優先實作）

### 2.1 設定介面（沿用既有架構，勿另開第三種入口）

專案已有兩層慣例，實作時應**對齊**，避免新增平行設定來源：

1. **`trainer/core/config.py`**：與 `NEG_SAMPLE_FRAC`、`STEP7_USE_DUCKDB` 等相同，以 **`os.getenv("LIGHTGBM_DEVICE_TYPE", "cpu")`**（或團隊選定之變數名）給出**預設**，並限制值為 `cpu` / `gpu`（Windows 避免依賴 `cuda`）。
2. **`trainer/training/trainer.py`** 頂部 **`import config as _cfg`** 區塊：以 **`getattr(_cfg, "LIGHTGBM_DEVICE_TYPE", <core 模組預設>)`** 允許**可選的專案根目錄 `config.py`** 覆寫（與現有 `OPTUNA_*`、`STEP7_*` 模式一致）。

**CLI（可選）：**在既有 **`trainer/training/trainer_argparse.py`**（檔案已存在）新增例如 `--lgbm-device {cpu,gpu}`，解析後**覆寫**上述解析結果（優先序：CLI > `_cfg` > `core/config` + env）。

**預設必須為 `cpu`**，筆電、CI、無 OpenCL 的 Linux 訓練機行為不變。

### 2.2 集中 LightGBM 參數

目前 `trainer/training/trainer.py` 的 `_base_lgb_params()` 固定偏 CPU：

```2937:2945:trainer/training/trainer.py
def _base_lgb_params() -> dict:
    return {
        "objective": "binary",
        "class_weight": "balanced",
        "force_col_wise": True,
        "verbose": -1,
        "n_jobs": -1,
        "random_state": 42,
    }
```

**依 LightGBM 4.6 文件須調整：**

- **`device_type`**（別名 `device`）：由設定注入 `cpu` 或 `gpu`。
- **`force_col_wise`：**文件標為**僅適用 CPU**。`device_type=gpu` 時應**省略**或僅在 CPU 分支設定；勿在 GPU 路徑盲目保留 `True`。
- **`n_jobs`：**GPU 訓練時 `n_jobs=-1` 易與 OpenCL 工作**搶 CPU**；GPU 路徑宜改為**較小固定值或 `1`**，並在訓練機上**實測**。
- **可選 GPU 調參：**例如較小的 **`max_bin`**（文件常建議如 63 以換速度）；僅在與指標穩定性驗證後納入。

實作單一 helper（例如 `_lgb_params_for_pipeline()`），**所有**組參處皆經此合併。

### 2.3 必須串同一套參數的呼叫點

| 位置 | 行為 | 動作 |
|------|------|------|
| `run_optuna_search()` | `LGBMClassifier(**{**_base_lgb_params(), **trial}})` | 每個 trial 併入 pipeline 裝置參數。 |
| `_train_one_model()` | Optuna 後最終 `LGBMClassifier` | 同上。 |
| `train_single_rated_model()` | 多條 `lgb.train(hp_lgb, …)`（LibSVM／CSV／記憶體） | `hp_lgb` 須含 `**_lgb_params_for_pipeline()`（今日已 spread `_base_lgb_params()`）。 |
| `train_dual_model()` | 遺留；`run_pipeline` **不應**呼叫（測試已約束） | 建議仍用同一 helper，避免與主路徑漂移；或明註「遺留僅 CPU」。 |

### 2.4 特徵篩選中的 LightGBM（Step 8）

`trainer/features/features.py` 內 `_lgbm_rank_and_cap()` 以精簡 `params` 呼叫 `lgb.train(..., num_boost_round=100)`，目前無 `device_type`。

- **選項 A（建議預設）：**篩選階段**強制 CPU**，避免小矩陣上 GPU 上下文切換與行為漂移。
- **選項 B：**與主訓練共用 `device_type`；小資料時**可能**比 CPU 慢 — 需**實測**再決定。

程式註解中**明文化**團隊選擇。

### 2.5 穩健性

- **試探或 try/except：**首次以 `gpu` 呼叫 LightGBM 若發生致命錯誤，可 **log 後降級 `cpu`**，或 **非零退出**並提示設回 `cpu`（由團隊定案）。
- **LibSVM／CSV：**`lgb.train` 可用 GPU；**資料集建構**仍在 CPU（檔案 I／O、pandas）。

### 2.6 可觀測性

- 啟動時 log：解析後的 `device_type`、是否曾 fallback。
- 指標／MLflow：`lightgbm_device_type`、`lightgbm_device_fallback`（bool）。

### 2.7 依賴與打包

- **OpenCL 路徑不需要**另建 `requirements-gpu.txt`；同一 **`lightgbm`** pin 即可，GPU 依賴驅動的 OpenCL。
- **可選：**於 README 或 trainer 說明註明：Windows 的 `gpu` = OpenCL，**非** CUDA；若日後在 Linux 使用 CUDA 編譯版 LightGBM，屬**另一套環境**故事。

### 2.8 測試與數值／early stopping 對齊

- **單元：**mock／patch `_lgb_params_for_pipeline()`，斷言 Optuna objective 與 `_train_one_model` 合併後的鍵。
- **整合（可選）：**無 GPU 時 `@pytest.mark.skipif`；CI 預設全程 CPU。
- **迴歸／容差：**GPU 可能改變浮點與直方圖行為；除 AP、threshold 等指標外，尚須納入 **early stopping**：驗證集 metric 的微小差異可能導致 **在不同 round 停止**，進而影響 **`best_iteration`、模型樹數、threshold**。定義 CPU vs GPU 對齊策略時應**一併**寫入容差或接受準則。

---

## 3. Phase B — Optuna 平行 trial（非「開 GPU」必要條件）

目前 `study.optimize()` **序列**執行。多 GPU 需 `n_jobs > 1` 加上 **每 worker 的 `CUDA_VISIBLE_DEVICES`** 等隔離，且主要對應 **Linux + CUDA** 建置，與現行 **Windows + OpenCL（`gpu`）** 不同。若未來將 HPO 遷至 **Linux + 含 CUDA 的 LightGBM** 再評估。

---

## 4. 訓練管線對照（脈絡）

`run_pipeline`／`trainer/training/trainer.py` 模組開頭約 Step 1–10：

1. **Chunks** — 時間窗（`get_monthly_chunks`）。
2. **Chunk 級切割** — 導出 `train_end`（身分防洩漏等）。
3. **Canonical mapping** — DuckDB（`build_canonical_links_and_dummy_from_duckdb`）。
4. **Profile 就緒** — 載入／backfill（`ensure_player_profile_ready`、`load_player_profile`、ETL）。
5. **身分／dummy ID** — pandas／查詢。
6. **逐 chunk ETL** — `process_chunk`：ClickHouse 或 Parquet → DQ → 標籤 → **Track Human** → **Track LLM**（DuckDB + YAML）→ profile PIT join → Parquet cache。
7. **合併／排序／列級切分** — DuckDB 或 pandas（`STEP7_USE_DUCKDB`）、train/valid/test、`sample_weight`。
8. **特徵列表／篩選** — `screen_features`（DuckDB std/corr、可選 MI、LightGBM 排序）。
9. **Optuna** — `run_optuna_search` → 多次 **`LGBMClassifier.fit`**（**TPE 在 CPU**；若啟用 GPU，**僅 fit 內部**用 GPU）。
10. **最終訓練與產物** — `train_single_rated_model` → `lgb.train`／`LGBMClassifier`、指標、`save_artifact_bundle`、MLflow、diagnostics JSON。

**v10：**正式路徑為 **`train_single_rated_model`**（僅 rated 單模型）。`train_dual_model` 為遺留；`run_pipeline` 不得呼叫（review 測試已約束）。

**補充：**`STEP7_USE_DUCKDB` 亦影響 RAM 估算（如 `_oom_check_and_adjust_neg_sample_frac`）。**LightGBM 改 GPU 不影響該邏輯**，無需為 GPU 修改 OOM 相關程式。

---

## 5. 未來若要大改才吃得到 GPU 的區域

以下為**新增依賴或架構**才可能讓「非 LightGBM 旗標」用到 GPU 的區塊盤點。

### 5.1 已在 GPU 能力棧上（延伸即可，勿重複造輪）

| 區域 | 檔案／符號 | 方向 |
|------|------------|------|
| **主模型 + Optuna** | `trainer/training/trainer.py`：`_base_lgb_params`、`run_optuna_search`、`_train_one_model`、`train_single_rated_model` | **Phase A** — 主要投資報酬。 |
| **篩選用 LGBM** | `trainer/features/features.py`：`_lgbm_rank_and_cap` | 與 Phase A 共用策略或刻意 CPU。 |

### 5.2 LightGBM predict（僅指標／評估）

| 區域 | 檔案／符號 | 方向 |
|------|------------|------|
| **批次預測分數** | `trainer/training/trainer.py`：**`_batched_booster_predict_scores`**、`_dataframe_for_lgb_predict`；LibSVM 路徑的 `booster.predict(path)` | 相對於訓練，推論量常**較小**，GPU 效益**通常偏低**。若僅訓練開 GPU，**短期不必改**。未來若要做 **GPU 推論**，需一併追蹤此函式與相關 `predict` 呼叫。 |

### 5.3 DuckDB（現為 CPU）

| 區域 | 檔案／符號 | 方向 |
|------|------------|------|
| **Canonical、Step 7、Track LLM、篩選 std/corr、profile ETL** | 各處 `duckdb.connect(":memory:")` | 標準 DuckDB 為 **CPU**；僅當上游出現**明確支援且你們願意維護**的 GPU 路線再評估。改 **RAPIDS／GPU SQL** 屬重寫邊界，非開關即可。 |

### 5.4 pandas／PyArrow／Parquet

| 區域 | 檔案／符號 | 方向 |
|------|------------|------|
| **Chunk 讀寫、DQ、標籤、profile join** | `process_chunk`、`apply_dq`、`compute_labels`、`join_player_profile` 等 | **cuDF** 等需移植與 **VRAM** 上限；工時高。 |

### 5.5 Numba（現為 CPU JIT）

| 區域 | 檔案／符號 | 方向 |
|------|------------|------|
| **Track Human lookback** | `features.py`：`_streak_lookback_numba`、`_run_boundary_lookback_numba` 等 | 可選 **`numba.cuda`** 改特定迴圈 — **專家級**、測試負擔大。 |

### 5.6 scikit-learn（CPU）

| 區域 | 檔案／符號 | 方向 |
|------|------------|------|
| **互資訊** | `mutual_info_classif`（`mi`／`mi_then_lgbm`） | **cuML** 等需新依賴與資料轉移。 |
| **指標／threshold** | `sklearn.metrics`、`threshold_selection.py` | 相對訓練通常可忽略；不建議為此上 GPU。 |

### 5.7 ClickHouse 客戶端／伺服器

| 區域 | 檔案／符號 | 方向 |
|------|------------|------|
| **訓練拉數** | `load_clickhouse_data`、`identity.py`、ETL | 客戶端多為**網路 + 反序列化**；訓練機 GPU 不自動加速查詢，除非改 **CH 端 SQL／硬體**。 |

### 5.8 Optuna 驅動

| 區域 | 檔案／符號 | 方向 |
|------|------------|------|
| **TPE** | `TPESampler` | 實務上 **CPU**。 |
| **平行 trial** | `study.optimize(..., n_jobs=…)`（現未使用） | 多 GPU 需**程序隔離**與裝置綁定 — **Linux + CUDA** 場景。 |

### 5.9 匯出／LibSVM／binary cache

| 區域 | 檔案／符號 | 方向 |
|------|------------|------|
| **Plan B／B+** | `_export_train_valid_to_csv`、`_export_parquet_to_libsvm`、`save_binary` | **I/O 與解析**為主；GPU 難直接加速，間接效益來自後續**更快訓練**。 |

---

## 6. 建議執行順序（含實作優先細項）

| 順序 | 行動 | 對應章節 |
|------|------|----------|
| 1 | 在 **`trainer/core/config.py`** 以 `os.getenv` 定義 `LIGHTGBM_DEVICE_TYPE` 預設 `cpu`；必要時白名單 `cpu`/`gpu`。 | §2.1 |
| 2 | 在 **`trainer.py`** 既有 `_cfg` 區塊以 `getattr(_cfg, "LIGHTGBM_DEVICE_TYPE", …)` 覆寫；可選：`trainer_argparse.py` 的 `--lgbm-device`。 | §2.1 |
| 3 | 實作 **`_lgb_params_for_pipeline()`**，CPU／GPU 分支處理 `force_col_wise`、`n_jobs`（與可選 `max_bin`）。 | §2.2 |
| 4 | 更新 **`run_optuna_search`、`_train_one_model`、`train_single_rated_model`**（含所有 `lgb.train` 分支）；`train_dual_model` 建議共用 helper。 | §2.3 |
| 5 | 決定 **`_lgbm_rank_and_cap` 策略**（建議選項 A：篩選 CPU-only）。 | §2.4 |
| 6 | logging／MLflow／`training_metrics.json` 寫入 **`lightgbm_device_type`**、**`lightgbm_device_fallback`**。 | §2.6 |
| 7 | 測試與 **CPU vs GPU 容差**（含 **early stopping／`best_iteration`**）。 | §2.8 |
| 8 | 在真實 run 上決定篩選是否改 GPU；其餘 §5.2–5.9 **除非 profiling 證明值得數月移植**，否則暫緩。 | §5、§6 後段 |

---

## 7. 實作前待決議

- **Fallback：**要求 `gpu` 時，靜默降 CPU **vs** 硬失敗？
- **篩選：**LightGBM 篩選固定 CPU **vs** 與主訓練一致？
- **CI：**一律 CPU **vs** 可選 GPU job？
- **數值接受度：**CPU vs GPU 之間 AP、threshold、**best iteration** 可接受差異？

---

*文件版本：已併入設定慣例（`core/config.py` + `_cfg` + argparse）、`_batched_booster_predict_scores` 正名、early stopping／`STEP7` 補註與執行優先序表；全文改為繁體中文。*
