# Track Human Lookback 向量化與 Step 6 進度條 — 規格計畫

> 本文件為 `.cursor/plans/PLAN.md` 中「Track Human Lookback 向量化與 Step 6 進度條（計畫）」之展開規格，含問題摘要、語意不變、Phase 1/2、檔案清單與成功標準。

---

## 1. 目標

- **主要**：解決 `SCORER_LOOKBACK_HOURS=8` 時 Step 6（Process chunks）凍結 7h+ 的問題。
- **次要**：在 Step 6 顯示 chunk 進度與 ETA，避免長時間無輸出被誤判為凍結。

---

## 2. 問題摘要


| 項目                    | 說明                                                                                                                                                                                                   |
| --------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **現象**                | Step 6 在約 25M 列、且 trainer 使用與 scorer 一致的 lookback（如 8h）時，Process chunks 階段無進度輸出、耗時 7 小時以上，畫面像凍結。                                                                                                     |
| **根因**                | `trainer/features.py` 的 `compute_loss_streak` 與 `compute_run_boundary` 在 `lookback_hours` 有設定時，以 **per-row Python 迴圈** 實作：對每筆 bet 切出 `(t - lookback_hours, t]` 視窗再計算。                                |
| **複雜度**               | O(N×B)（N = 列數，B = 視窗內平均 bet 數），常數大；25M 列時不可接受。                                                                                                                                                       |
| **現有實作位置**            | `features.py`：`compute_loss_streak` 約 L314–337（lookback 分支）、`compute_run_boundary` 約 L438–470（lookback 分支）；皆為 `groupby("canonical_id")` 後對每個 `(idx, t)` 做 `grp.loc[(times > lo) & (times <= t)]` 再算。 |
| **為何不能直接拿掉 lookback** | Serving（scorer）需 8h lookback 以限制歷史載入；train–serve parity 要求 trainer 與 scorer 對「8h 視窗內」的語意一致。實作方式應改為單 pass 向量化或 numba two-pointer，而非移除 lookback。                                                       |


---

## 3. 語意不變（契約）

任何改動必須保持以下不變，以供測試與 parity 驗證：

### 3.1 視窗定義

- 對每個 row 的時間 `t_i`（`payout_complete_dtm`），僅使用 `**(t_i - lookback_hours, t_i]`** 內的 bet 參與計算。
- 排序：`(canonical_id, payout_complete_dtm, bet_id)`，stable sort（G3）。

### 3.2 `compute_loss_streak` 輸出

- **型別**：`pd.Series[int]`（int32），index 與輸入 `bets_df` 一致（或依 cutoff 子集）。
- **語意**：F4 — LOSE→+1、WIN→reset、PUSH→依 `LOSS_STREAK_PUSH_RESETS` 決定是否 reset；streak 為「處理完該 bet 後」的連續 LOSE 數。

### 3.3 `compute_run_boundary` 輸出

- **型別**：DataFrame，原欄位 + `run_id`（int32）、`minutes_since_run_start`（float64）、`bets_in_run_so_far`（int32）、`wager_sum_in_run_so_far`（float64）。
- **語意**：B2 — 同一 canonical_id 內，與前一筆 gap ≥ `RUN_BREAK_MIN` 分鐘即新 run；run_id 0-based，minutes_since_run_start ≥ 0，bets_in_run_so_far 1-based。

---

## 4. Phase 1 — 解封（立即可做）


| 要點       | 內容                                                                                                                                                                 |
| -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **作法**   | Trainer 呼叫 `add_track_human_features` 時傳 `**lookback_hours=None`**（或透過 config 如 `TRAINER_USE_LOOKBACK=False`），使 Step 6 走現有 **無 lookback** 的向量化路徑。                  |
| **效果**   | Step 6 可在合理時間內完成；無需改動 `features.py` 的 lookback 迴圈。                                                                                                                 |
| **代價**   | Scorer 仍使用 `SCORER_LOOKBACK_HOURS`；train 與 serve 對「8h 視窗」的完全一致延至 Phase 2。                                                                                          |
| **建議改動** | `trainer/config.py`：新增 `TRAINER_USE_LOOKBACK`（預設 `False`）；`trainer.py` 呼叫 `add_track_human_features` 時依該 config 傳 `lookback_hours=SCORER_LOOKBACK_HOURS` 或 `None`。 |


---

## 5. Phase 2 — Lookback 向量化（正確解）


| 要點     | 內容                                                                                                                                                                  |
| ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **作法** | 以 **numba（或 Cython）** 實作 **two-pointer + 狀態機**：按 `canonical_id` 分組，每個 group **單 pass**，對每個 row 的 8h 視窗算出 streak / run_boundary，替換目前 lookback 分支的 per-row Python 迴圈。 |
| **輸出** | 欄位與型別與現有 API 完全一致（見 §3），以利既有測試與 parity 驗證。                                                                                                                          |
| **可選** | 若 numba 為可選依賴：無 numba 時 fallback 現有慢路徑，並在資料量大時 log 警告。                                                                                                              |


### 5.1 實作要點（建議）

- `**compute_loss_streak`**：單 pass 內維護視窗 [lo, t]、reset 事件（WIN / PUSH 若 reset）、當前 streak；每 row 只更新 lo 指針與狀態，避免 per-row 再切 subframe。
- `**compute_run_boundary**`：單 pass 內維護視窗、gap ≥ RUN_BREAK_MIN 的 run 邊界、minutes_since_run_start、bets_in_run_so_far、wager_sum_in_run_so_far。
- 仍以 pandas 進出介面為準；內部可將 `canonical_id` 分組後的陣列傳入 numba JIT 函數。

---

## 6. Step 6 進度條（tqdm）


| 項目       | 內容                                                                                                                                                                                       |
| -------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **目的**   | Process chunks 時顯示進度與 ETA，避免長時間無輸出被誤判為凍結。                                                                                                                                                |
| **作法**   | 使用 `**tqdm`**：`total=len(chunks)`、`desc="Step 6 chunks"`、`unit="chunk"`。在 Step 6 開始（`t0` 與 `chunk_paths=[]` 之後）建立 progress bar；每次將 chunk 結果 append 到 `chunk_paths` 時呼叫 `pbar.update(1)`。 |
| **涵蓋分支** | OOM probe、chunks[1:]、path1 為 None 時整份 chunks、以及非 AUTO 的 `enumerate(chunks)` 所有分支；以 **try/finally** 確保 `pbar.close()`。                                                                    |
| **依賴**   | 將 `tqdm` 加入專案依賴（requirements.txt 或 pyproject.toml）。`trainer/trainer.py` 內以 try/import 引入 tqdm；若未安裝則 fallback 為 no-op（例如 `def tqdm(iterable, **kwargs): return iterable`），避免無 tqdm 環境報錯。  |
| **檔案**   | `trainer/trainer.py`（Step 6 迴圈前建立 bar、各分支 append 後 update(1)、finally close）；依賴檔。                                                                                                         |


---

## 7. 檔案清單


| 階段         | 檔案                                    | 改動摘要                                                                 |
| ---------- | ------------------------------------- | -------------------------------------------------------------------- |
| Phase 1    | `trainer/config.py`                   | 新增 `TRAINER_USE_LOOKBACK`（可選）。                                       |
| Phase 1    | `trainer/trainer.py`                  | 呼叫 `add_track_human_features` 時依 config 傳 `lookback_hours` 或 `None`。 |
| Phase 2    | `trainer/features.py`                 | lookback 分支改為 numba two-pointer 單 pass（或 Cython）；可選 fallback 慢路徑。    |
| Step 6 進度條 | `trainer/trainer.py`                  | Step 6 建立 tqdm、各分支 update(1)、finally close。                          |
| Step 6 進度條 | `requirements.txt` 或 `pyproject.toml` | 加入 `tqdm`。                                                           |


---

## 8. 成功標準

- **Phase 1**：設 `TRAINER_USE_LOOKBACK=False`（或 `lookback_hours=None`）時，Step 6 在 25M 級資料下於合理時間內完成（與無 lookback 時相當）；Scorer 仍使用 `SCORER_LOOKBACK_HOURS`。
- **Step 6 進度條**：Step 6 執行時終端顯示 chunk 進度與 ETA；無 tqdm 時不報錯（fallback no-op）。
- **Phase 2**：在 `lookback_hours=8` 下，`compute_loss_streak` / `compute_run_boundary` 輸出與現有 per-row 實作一致（可寫單元測試比對）；Step 6 耗時顯著下降（目標：與 Phase 1 同量級，無 7h+ 凍結）。
- **語意**：§3 視窗與輸出契約全程保持不變。

