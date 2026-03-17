# Validator–Trainer Parity 修正計畫

**目標**：(1) Validator 不寫死常數，改與 trainer 共用 config；(2) Validator 改為僅用 bet-based 邏輯，與 trainer 的 label 定義一致。

**依據**：trainer 的 label 來自 `trainer/labels.py` 的 `compute_labels()`（僅 bet stream，使用 `WALKAWAY_GAP_MIN`、`ALERT_HORIZON_MIN`）；validator 目前寫死 15/30/45 且含 session 路徑，存在常數與定義兩類 parity 風險。  
**決策**：DEC-030（見 .cursor/plans/DECISION_LOG.md）。

---

## 一、常數改為共用 config（Step 1）

### 1.1 現狀

- **trainer**：`trainer/labels.py` 與 `trainer/core/config.py` 使用 `WALKAWAY_GAP_MIN = 30`、`ALERT_HORIZON_MIN = 15`、`LABEL_LOOKAHEAD_MIN = WALKAWAY_GAP_MIN + ALERT_HORIZON_MIN`（= 45）。
- **validator**：`trainer/serving/validator.py` 內多處寫死 `timedelta(minutes=15)`、`30`、`45`，未從 config 讀取。

### 1.2 修改範圍

| 位置 | 現狀 | 改為 |
|------|------|------|
| `find_gap_within_window()` | `horizon_end = alert_ts + timedelta(minutes=45)` | `alert_ts + timedelta(minutes=config.LABEL_LOOKAHEAD_MIN)` |
| 同上 | `gap_minutes >= 30` | `gap_minutes >= config.WALKAWAY_GAP_MIN` |
| 同上 | `(current_start - alert_ts).total_seconds() / 60.0 <= 15` | `<= config.ALERT_HORIZON_MIN` |
| `validate_alert_row()` | `wait_minutes = 45 + max(0, freshness_buffer_min)` | `config.LABEL_LOOKAHEAD_MIN + max(0, ...)` |
| 同上 | `(bet_ts - last_bet_before) > timedelta(minutes=15)`（gap_started_before_alert） | `timedelta(minutes=config.ALERT_HORIZON_MIN)` |
| 同上 | `horizon_end = bet_ts + timedelta(minutes=45)` | `bet_ts + timedelta(minutes=config.LABEL_LOOKAHEAD_MIN)` |
| 同上 | `late_threshold = bet_ts + timedelta(minutes=15)` | `bet_ts + timedelta(minutes=config.ALERT_HORIZON_MIN)` |
| 同上 | `extended_end = bet_ts + timedelta(minutes=45 + extended_wait)` | `bet_ts + timedelta(minutes=config.LABEL_LOOKAHEAD_MIN + extended_wait)` |

**Import**：`trainer/serving/validator.py` 頂部已有 `import config`（即 `trainer.config` → `trainer.core.config`），只需在函式內使用 `config.WALKAWAY_GAP_MIN`、`config.ALERT_HORIZON_MIN`、`config.LABEL_LOOKAHEAD_MIN`。

**Docstring**：`find_gap_within_window` 的 docstring 改為「Gap must start within ALERT_HORIZON_MIN of alert and last >= WALKAWAY_GAP_MIN」（或註明數值來自 config）。

### 1.3 驗收

- 單元測試：patch `config.WALKAWAY_GAP_MIN` / `ALERT_HORIZON_MIN` 為不同值，確認 `find_gap_within_window` 與 `validate_alert_row` 的結果隨之改變。
- 回歸：現有 validator 相關測試在未 patch config 下仍全過（預設 15/30/45 不變）。

---

## 二、改為僅 bet-based 邏輯（Step 2）

### 2.1 與 trainer 的對齊定義

- **Trainer**（`labels.py`）：label=1 ⟺ 在 **bet stream**（canonical_id, payout_complete_dtm 排序）上，存在「gap_start」（下一筆 bet 與當前 bet 間隔 ≥ WALKAWAY_GAP_MIN，或 terminal 且可判定）且該 gap 的**開始時間**落在 [bet_ts, bet_ts + ALERT_HORIZON_MIN]。不使用 session。
- **Validator（目標）**：MATCH ⟺ 在 **bet stream** 上，存在 gap ≥ WALKAWAY_GAP_MIN、gap 開始在 [bet_ts, bet_ts + ALERT_HORIZON_MIN] 內；且觀察窗（LABEL_LOOKAHEAD_MIN + extended_wait）內，**沒有任何 bet** 落在 (bet_ts + ALERT_HORIZON_MIN, bet_ts + LABEL_LOOKAHEAD_MIN]（即「late arrival」僅以 bet 為準）。不使用 session 做 MATCH/MISS 判決。

### 2.2 要移除／改動的 session 邏輯

| 區塊（約略行號） | 現狀 | 作法 |
|------------------|------|------|
| 679–734（session 路徑） | 用 `session_cache`、`matched_session`，依 session end / next_start 與 15/30 下 PENDING 或 MISS | **整段移除**。不再依 session 做任何 early return 或 verdict；一律進入下方「Fallback to bet-gap check」。 |
| 759–763（late arrival） | `any_late_bet_in_window or any_late_session_in_window` | 改為僅 **any_late_bet_in_window**（只檢查 bet_list）。 |
| 792–794（no gap 時 MISS） | `any_late_bet_within_horizon or any_late_session_within_horizon` | 改為僅 **any_late_bet_within_horizon**。 |
| 815–819（finalize 時 late） | `any_late_bet_in_extended or any_late_session_in_extended` | 改為僅 **any_late_bet_in_extended**。 |

### 2.3 保留的 API 與行為

- **`validate_alert_row(..., session_cache=...)`**：保留參數與呼叫端不變。在 docstring 註明：`session_cache` 不再用於 verdict（bet-based only），僅保留 API 相容。
- **`fetch_sessions_by_canonical_id`**：可保留呼叫（供日後其他用途或日誌），但 verdict 不依賴 session。

### 2.4 驗收

- 同一 `bet_cache`、`session_cache={}` 與 `session_cache={...}` 時 verdict 一致且僅依 bet 決定。
- 現有 validator 相關測試通過或改為 bet-only 預期。
- 可選：加「與 labels.compute_labels 對齊」的測試（同一 bet stream → label=1 ⟺ MATCH）。

---

## 三、實作順序與風險

| 步驟 | 內容 | 風險 |
|------|------|------|
| 1 | 常數改 config（Step 1） | 低；僅替換數值來源，預設值不變。 |
| 2 | 移除 session 路徑、late arrival 僅看 bet（Step 2） | 中；部分歷史 alert 可能從「session 路徑 MATCH」變為「bet 路徑 MISS」或反之，需在 release note 說明並視需要做短期監控。 |
| 3 | 測試與文件 | 低。 |

建議先做 **Step 1**，跑完測試與一次手動驗證後再做 **Step 2**。

---

## 四、檔案清單

| 檔案 | 變更 |
|------|------|
| `trainer/serving/validator.py` | 常數改 config；移除 session 判定、late arrival 僅用 bet。 |
| `trainer/core/config.py` | 無需改（已有 WALKAWAY_GAP_MIN, ALERT_HORIZON_MIN, LABEL_LOOKAHEAD_MIN）。 |
| `tests/...` | 依現有 validator 測試補 patch config / bet-only 案例；必要時調整依賴 session 的預期。 |
