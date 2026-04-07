# 整合計畫：Precision + 訓練速度 (Consolidated Plan)

> 最後更新：2026-04-07  
> 本文件將過去所有獨立計畫（Phase 2, PATCH, Investigation, Chunk Cache, GPU, SQL Analysis 等）的任務項目**打散並重新聚合成單一、以執行順序為導向的路線圖**。

---

## 1. 核心目標 (Core Objectives)

1. **提升 Precision（最高優先）**：找出 test 與 production 之間的落差根因，統一指標口徑，並完成線上校準（Online Calibration）閉環。
2. **加速訓練（次優先）**：透過快取（Chunk Cache）、動態特徵數（Dynamic K）與 GPU，降低反覆迭代的成本，並嚴控記憶體（OOM）風險。

---

## 2. 執行順序 (Implementation Order)

所有剩餘工作請依照此順序推進，確保在做「速度優化」前，先確認「評估尺度的正確性」。

*   **Step 1: 統一量測與排除語意落差 (Precision P0)**
    *   產出同視窗、同定義的 Test vs Production 比較表（固定閾值 vs `precision@recall=1%`）。
    *   完成 Validator 與 `compute_labels()` 的邊界案例（Censored/Terminal）對拍。
    *   確認 `bet_id` 補查機制（Task 9C）已徹底消除長期 PENDING。
*   **Step 2: 線上校準閉環 (Precision P1)**
    *   完成 ClickHouse 全量標註腳本。
    *   完成基於 DEC-026 的自動選阈，並加上安全閘門寫回 State DB。
*   **Step 3: 訓練快取與維度縮減 (Speed P0)**
    *   Chunk Cache 收斂：完成 Local Parquet 穩定指紋（無 `mtime`），並營運化兩階段快取（Prefeatures）。
    *   動態特徵數 (Task 5)：實作基於 Cumulative Gain 的動態 K，取代固定 Top-50。
*   **Step 4: 推論效能與 GPU (Speed P1)**
    *   完成 ClickHouse SQL 效能實測收斂（Task 3 Phase 5，補齊 latency/rows 數據）。
    *   GPU Benchmark：實測 Windows OpenCL 下 CPU vs GPU 的速度，確立使用規範。
*   **Step 5: 維運與穩定性 (Ops P2)**
    *   實作統一的 Logging Level 政策 (Task 8)。
    *   補齊全域 `busy_timeout` 防禦 SQLite 競爭。

---

## 3. 任務狀態追蹤表 (Master Task List)

### A. Precision 與調查 (Measurement & Precision)

包含 R1~R9 調查、Label/Validator 對齊、Online Calibration。

| 任務項目 | 具體內容 | 來源對應 | 狀態 | 下一步行動 |
| :--- | :--- | :--- | :--- | :--- |
| **指標口徑與上界評估** | 確保 Test 報表的 `precision@recall` 與 Validator 看到的指標同定義。建立離線 PR 上界與同窗比較。 | Inv: R1, R2, R6 | ⏳ 進行中 | 產出「固定閾值 vs PR=1%」同口徑、同時間窗之對照報告。 |
| **Label 與 Validator 對齊** | 消除 Validator 的 `gap` 早期 return，確保對 Censored/Terminal 注單的標籤與 `compute_labels()` 邏輯一致。 | PATCH: T6 <br> Inv: R3 | ⏳ 進行中 | 完成 Validator vs Label 的全量對拍稽核。 |
| **資料管線 Parity (Train-Serve)** | 確保 Profile PIT vs Scorer TTL 快取一致；確認 Canonical Cutoff 新鮮度；確認時區轉換無整點偏移。 | Inv: R4, R9 | 📝 計畫中 | 針對特定案例做 Parity 實證比對；確認時區已徹底修復。 |
| **時間與分佈漂移** | 透過離線 Holdout 驗證多時間窗的指標變異，判斷單一 6h Backtest 是否過度樂觀。 | Inv: R5, R7 | 📝 計畫中 | 執行多窗 Backtest 變異分析報告。 |
| **線上校準完整閉環** | Runtime 閾值 MVP 已上線。需從 CH 拉標籤、自動算閾值並寫回 `state.db`，讓 Scorer 自動更新。 | Phase 2 <br> MVP 已上線 | ⏳ 進行中 | 實作 `label_predictions_from_ch.py` 全量標註；實作自動 Upsert 閾值與 `CALIBRATE_ALLOW_WRITE` 閘門。 |
| **Validator 補查機制** | 用 `bet_id` 錨定 TBET 補查，解決 `player_id` 漂移導致的 No-bet data 長期 PENDING 問題。 | PATCH: T9B, T9C | ✅ MVP 完工 | 監控生產環境，確保 PENDING 樣本能順利收斂。 |

---

### B. 訓練速度與成本 (Training Speed & Cost)

包含 Chunk Cache、動態 K、GPU 訓練。

| 任務項目 | 具體內容 | 來源對應 | 狀態 | 下一步行動 |
| :--- | :--- | :--- | :--- | :--- |
| **Chunk Cache: 兩階段快取** | Track Human 之後、Track LLM 之前寫入 Prefeatures，避免改 Spec 時重算 Track Human。 | Cache Plan | ⏳ 進行中 | 確立營運預設行為，文件化 OOM 風險（讀取整檔）與磁碟寫入放大。 |
| **Chunk Cache: 可攜式指紋** | Local Parquet 的 `data_hash` 拔除 `mtime`，改用大小、列數與穩定 Metadata 摘要。 | Cache Plan | ⏳ 進行中 | 實作並驗證跨機器/跨路徑複製能成功 Cache Hit。 |
| **動態特徵數 (Dynamic K)** | 取代固定的 Top-50，依據 Cumulative Gain 門檻自動挑選特徵數（K_min~K_max）。 | PATCH: T5 | 📝 計畫中 | 實作 Phase-A（Gain 目標、Fallback 護欄、Artifact 記錄 K 值）。 |
| **LightGBM GPU 啟用** | 在 Windows 上使用 OpenCL 跑 Optuna trials 與最終訓練。 | GPU Plan | ✅ MVP 完工 | 加入明確 CPU vs GPU Benchmark 數據；強化 `n_jobs` 隔離設定。 |

---

### C. 推論效能與維運 (Inference Perf & Operations)

包含 SQL 優化、API 效能、SQLite 鎖競爭、Logging。

| 任務項目 | 具體內容 | 來源對應 | 狀態 | 下一步行動 |
| :--- | :--- | :--- | :--- | :--- |
| **ClickHouse SQL 設計分析** | 不改索引下，優化 Trainer/Scorer/Validator 的 SQL。 | Task 3 Ph 5 | ⏳ 進行中 | 補齊 Rows, Latency, FINAL_used 等實測數據，確認優化方案。 |
| **Scorer 推論效能** | SQLite 批次處理、增量特徵計算、Numba 加速。 | PATCH: T3 Ph 3 | ⏳ 進行中 | 產出 p95 延遲的「優化前後對照表」。 |
| **Logging 政策統一** | 統一 Trainer, Scorer, Validator, Deploy 的 Log Level 解析順序。 | PATCH: T8 | 📝 計畫中 | 實作 `config.py` 統一解析，取代分散的 `basicConfig`。 |
| **SQLite 啟動鎖競爭** | 利用 `threading.Event` 讓 Validator 延後至 Scorer 首輪完成才啟動。 | PATCH: T10 | ✅ 完工 | 補上全域 `PRAGMA busy_timeout` 作為深度防禦。 |
| **API 預設視窗與下推** | `/alerts` 無參數預設 1h；SQL 下推 SQLite 過濾。 | PATCH: T2, T4 | ✅ 完工 | - |
| **滾動 KPI 上界修正** | Validator 15m/1h Precision 改以週期結束為 `now`，修復首輪 0/0 問題。 | PATCH: T4, T11 | ✅ 完工 | - |

---

## 4. 關鍵風險登錄 (Risk & Guardrails)

1. **量測錯位 (Metric Mismatch)**：千萬不可拿 Validator 的當前閾值 Precision 直接與 Training 的 `precision@recall=1%` 做比較，否則會誤判模型退化。
2. **筆電 OOM 風險 (Laptop OOM)**：兩階段快取的 `prefeatures` 命中時會 `read_parquet` 載入整張表。若遇到大月份 chunk，必須確保單 chunk 串行執行或有關閉開關。
3. **校準寫入風險 (Calibration Write-back)**：自動化寫入 Runtime 閾值時，必須要有 TTL（超時 Fallback 至 Bundle 閾值）及人工覆寫的防呆機制。
4. **GPU 退化風險**：小資料集或特徵篩選階段（Step 8）若開 GPU，可能因 Context Switch 導致速度比 CPU 更慢，務必 Benchmark 確認。

---

## 5. 封存/刪除前檢查清單

所有位於 `.cursor/plans/archive/` 下的舊 singleton plans，其核心的「為什麼要做、要做什麼、做到哪裡」皆已吸收至本文件。後續執行以本表為單一真相。
若需變更項目順序或新增任務，**請直接修改本文件的列表**。