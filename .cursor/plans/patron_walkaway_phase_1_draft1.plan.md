---
name: Patron Walkaway Phase 1
overview: 依據 SSOT 文件（v3，整合三輪 Spec Compliance Review 共 20 項修正），全面重構 trainer/scorer pipeline，封閉所有 leakage、train-serve parity 破口、Schema 層級錯誤（E1–E8、F1–F4），並建立完整的 Testing & Validation 規格，達成 Phase 1 MVP 上線條件。
todos:
  - id: config-definitions
    content: Step 0：在 config.py 集中定義所有常數（含 v3 新增 TABLE_HC_WINDOW_MIN / PLACEHOLDER_PLAYER_ID / LOSS_STREAK_PUSH_RESETS / HIST_AVG_BET_CAP）
    status: pending
  - id: dq-guardrails
    content: Step 1：P0 資料品質護欄（E1 移除 t_bet.is_manual；E3 payout_complete_dtm IS NOT NULL；E4/F1 player_id != -1；E5 所有查詢加 FINAL；E7 FND-04 COALESCE；F3 is_deleted/is_canceled）
    status: pending
  - id: identity-module
    content: Step 2：新建 identity.py（E6 FND-12 正確聚合 SQL；E4 player_id != -1；D2 M:N 衝突規則；B1 cutoff_dtm）
    status: pending
  - id: labels-module
    content: Step 3：新建 labels.py（C1 防洩漏；延伸拉取至少 X+Y；t_bet FINAL + E3 IS NOT NULL）
    status: pending
  - id: features-module
    content: Step 4：新建 features.py（E2 loss_streak 改用 status='LOSE'；F4 PUSH 語義；F2 hist_avg_bet winsorize；D1 TABLE_HC_WINDOW_MIN；E5 FINAL；TRN-05/08/09）
    status: pending
  - id: trainer-refactor
    content: Step 5：重構 trainer.py（整合新模組；雙模型；class_weight='balanced'；TRN-07 快取驗證）
    status: pending
  - id: backtester-g1
    content: Step 6：更新 backtester.py（G1 閾值搜尋；D4 alert volume = 雙模型合計；gaming day 去重）
    status: pending
  - id: scorer-refactor
    content: Step 7：重構 scorer.py（匯入 features.py；E5 FINAL；E3/E4 基礎過濾；D2 三步身份判定；reason codes）
    status: pending
  - id: validator-update
    content: Step 8：更新 validator.py（canonical_id；45min horizon；gaming day 去重）
    status: pending
  - id: api-contract
    content: Step 9：更新 api_server.py（/score /health /model_info；422 schema 驗證）
    status: pending
  - id: testing-validation
    content: Step 10（v3 新增 E8）：定義 Testing & Validation 規格（leakage 偵測、parity 測試、label sanity、D2 覆蓋率、schema 合規測試）
    status: pending
isProject: false
---

# Patron Walkaway Phase 1 實作計畫（v3）

> v3 在 v2 基礎上整合了 Round 3 Spec Compliance Review 的 12 項新修正（E1–E8、F1–F4），以及 Round 2 遺留的 3 項補完（D1/D3/D4）。最重大變更：E1 移除 `t_bet.is_manual`（欄位不存在）、E2 修正 `loss_streak` 使用 `status='LOSE'`（`payout_value` 100% NULL）、E5 所有 ClickHouse 查詢加 `FINAL`。

---

## 架構決策摘要（SSOT 固定）

- 身份歸戶：**D2**（Canonical ID；`casino_player_id` 優先）
- 右截尾：**C1**（Extended pull；至少 X+Y，建議 1 天）
- Session 特徵策略：**S1**（保守；`table_hc` 改用 `t_bet` 即時計算，禁用 `session_end`）
- 上線閾值策略：**G1**（Precision ≥ Pmin AND 警報量 ≥ Vmin；雙模型合計）
- 模型：Phase 1 = **LightGBM 雙模型**（Rated / Non-rated）

---

## 主要異動檔案

```
trainer/
├── config.py        ← 更新：新增 TABLE_HC_WINDOW_MIN / PLACEHOLDER_PLAYER_ID
│                              / LOSS_STREAK_PUSH_RESETS / HIST_AVG_BET_CAP
├── identity.py      ← 新建：D2 歸戶（FND-12 正確聚合 + player_id != -1）
├── labels.py        ← 新建：C1 防洩漏標籤
├── features.py      ← 新建：共用特徵（loss_streak 用 status; winsorize hist_avg_bet）
├── trainer.py       ← 重構：整合新模組，雙模型訓練
├── scorer.py        ← 重構：匯入 features.py，D2 三步身份判定
├── backtester.py    ← 更新：G1 閾值搜尋，alert volume = 雙模型合計
├── validator.py     ← 更新：canonical_id + 45min horizon
└── api_server.py    ← 更新：Model API Contract
```

---

## Phase 1 實作步驟

### Step 0 — 集中常數定義（`[trainer/config.py](trainer/config.py)`）

**目標**：全系統唯一事實來源；v3 新增 4 個常數。

**業務參數**

- `WALKAWAY_GAP_MIN = 30`（X）
- `ALERT_HORIZON_MIN = 15`（Y）
- `LABEL_LOOKAHEAD_MIN = 45`（= X + Y）

**資料可用性延遲**

- `BET_AVAIL_DELAY_MIN = 1`（`t_bet`；SSOT §4.2）
- `SESSION_AVAIL_DELAY_MIN = 15`（`t_session`；採保守值 +15min，SSOT §4.2 vs §2.1 衝突的解法）

**Run 邊界**

- `RUN_BREAK_MIN = WALKAWAY_GAP_MIN`（= 30 分鐘）

**Gaming Day**

- `GAMING_DAY_START_HOUR = 6`（本地時間 06:00；需業務確認）

**G1 閾值策略**

- `G1_PRECISION_MIN = 0.70`（暫定）
- `G1_ALERT_VOLUME_MIN_PER_HOUR = 5`（暫定）
- `G1_FBETA = 0.5`（β < 1，精準度優先）

**v3 新增常數（D1/E2/F2/F4 修正）**

- `TABLE_HC_WINDOW_MIN = 30`（D1：S1 `table_hc` 回溯窗口分鐘數；從 config 讀取，禁止硬編碼）
- `PLACEHOLDER_PLAYER_ID = -1`（E4/F1：`t_bet.player_id = -1` 為無效佔位符，需排除）
- `LOSS_STREAK_PUSH_RESETS = False`（F4：PUSH 是否重置連敗計數；預設不重置）
- `HIST_AVG_BET_CAP = 500_000`（F2：`hist_avg_bet` winsorization 上限；初始值需 EDA 驗證）

**SQL fragment 常數**（供所有模組複用）

- `CASINO_PLAYER_ID_CLEAN_SQL`：`CASE WHEN lower(trim(casino_player_id)) IN ('', 'null') THEN NULL ELSE trim(casino_player_id) END`

---

### Step 1 — 資料品質護欄（`[trainer/trainer.py](trainer/trainer.py)`, `[trainer/scorer.py](trainer/scorer.py)`, `[trainer/identity.py](trainer/identity.py)`）

**目標**：修正所有 P0 DQ 問題，含 v3 新增的 Schema 層級護欄（E1/E3/E4/E5/E7/F1/F3）。

**必須實作的過濾模式（對齊 SSOT §5）**

- **E5（高，v3 新增 — ClickHouse FINAL）**：所有 `t_bet` 和 `t_session` 查詢**必須加 `FINAL`**，例如 `SELECT ... FROM t_bet FINAL WHERE ...`。`ReplacingMergeTree` 在無 `FINAL` 時不保證去重，直接影響標籤、特徵正確性。
- **FND-01（TRN-01）**：`t_session FINAL` 去重：`ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY lud_dtm DESC NULLS LAST, __etl_insert_Dtm DESC) = 1`
- **FND-02（TRN-02）— E1 修正（高）**：`is_manual = 0` 過濾**僅適用 `t_session`**。`t_bet` 表**無 `is_manual` 欄位**（Schema 確認），v2 計畫錯誤對 `t_bet` 套用此過濾，v3 必須移除。TRN-02 對應關係更新為：「`t_session` 加 `WHERE is_manual = 0`；`t_bet` 無對應過濾。」
- **E3（中，v3 新增 — Nullable 時間）**：所有 `t_bet` 查詢的基礎 WHERE 子句必須包含 `payout_complete_dtm IS NOT NULL`，因該欄位為 Nullable，NULL 值無法用於時間排序或滾動窗口計算。
- **E4/F1（中，v3 新增 — 佔位符 player_id）**：所有 `t_bet` 查詢加入 `player_id IS NOT NULL AND player_id != PLACEHOLDER_PLAYER_ID`（from config = -1）；`session_id IS NULL` 的行可保留（用 `player_id` 兜底），但在 D2 歸戶前需記錄比例。
- **F3（低，v3 新增 — 軟刪除欄位）**：`t_session` 查詢加入 `is_deleted = 0 AND is_canceled = 0`；這兩個欄位存在於 schema 但 v2 計畫未使用。
- **FND-03**：`casino_player_id` 清洗使用 `CASINO_PLAYER_ID_CLEAN_SQL`（from config）
- **FND-04（E7 修正 — NULL-safe）**：條件改為 `COALESCE(turnover, 0) > 0 OR COALESCE(num_games_with_wager, 0) > 0`；原版 NULL-unsafe，當欄位為 NULL 時過濾不生效。
- **FND-06**：全面移除 `bet_reconciled_at`（100% 無效值）
- **FND-08**：移除 `bonus`, `tip_amount`, `increment_wager`, `payout_value`（100% NULL；這也是 TRN-09/E2 修正的根本原因）
- **FND-09**：禁用 `is_known_player`；改為 `casino_player_id IS NOT NULL`
- **FND-13**：禁止使用 `__etl_insert_Dtm`、`__ts_ms` 做時間排序；時間軸以 `payout_complete_dtm`（`t_bet`）或 `COALESCE(session_end_dtm, lud_dtm)`（`t_session`）為準
- **FND-14**（Phase 2 備忘）：`t_game` 去重延至 Phase 2

---

### Step 2 — 身份歸戶模組（`[trainer/identity.py](trainer/identity.py)`，D2）

**目標**：建立 `player_id → canonical_id` 映射，修正 FND-11/TRN-03；v3 補完 E6（FND-12 聚合 SQL）與 E4（`player_id != -1`）。

**Mapping 建置查詢**（必須含 `FINAL` — E5 修正）

```sql
-- 第一步：抽取連結邊
SELECT player_id, casino_player_id
FROM t_session FINAL
WHERE is_manual = 0
  AND is_deleted = 0 AND is_canceled = 0
  AND player_id IS NOT NULL AND player_id != {PLACEHOLDER_PLAYER_ID}
  AND COALESCE(session_end_dtm, lud_dtm) <= :cutoff_dtm
  AND {CASINO_PLAYER_ID_CLEAN_SQL} IS NOT NULL
```

**FND-12 假帳號排除（E6 修正）**：`session_cnt` / `total_games` 是聚合欄位，需先聚合再過濾：

```sql
-- 第二步：識別假帳號（先聚合）
SELECT player_id
FROM t_session FINAL
WHERE is_manual = 0
  AND is_deleted = 0 AND is_canceled = 0
  AND player_id IS NOT NULL AND player_id != {PLACEHOLDER_PLAYER_ID}
GROUP BY player_id
HAVING COUNT(session_id) = 1 AND SUM(num_games) <= 1
```

**M:N 衝突規則**（兩種情境明文固定）：

- 情境 1（斷鏈重發）：同一 `casino_player_id` ↔ 多個 `player_id` → 全部映射至同一 `canonical_id = casino_player_id`
- 情境 2（換卡）：同一 `player_id` ↔ 多個 `casino_player_id` → 取最新（最大 `lud_dtm`）的 `casino_player_id`；衝突清單寫入稽核日誌

**訓練期語義**：`identity.py` 接受 `cutoff_dtm`，只用 `training_window_end` 前的 session 建置 mapping，防止 mapping leakage（B1 修正）。

**推論期語義**：定期更新快取（每小時增量），`built_at` 時間戳與 `model_version` 一起記錄。

**D3（已知風險備忘）**：訓練窗口內的 D2 mapping 存在時序漏洞（早期觀測點理論上不應看到晚期才建立的歸戶關係）。Phase 1 接受此風險，因 `cutoff_dtm` 已從外部截斷，影響範圍有限；Phase 2 可考慮 Point-in-time Correct (PIT-correct) mapping。

---

### Step 3 — 標籤建構模組（`[trainer/labels.py](trainer/labels.py)`，C1）

**目標**：防洩漏 walkaway label，處理右截尾（TRN-06）。

- 時間軸依 `t_bet FINAL` 的 `payout_complete_dtm`（需 IS NOT NULL — E3）
- Gap start：`b_{i+1} - b_i ≥ WALKAWAY_GAP_MIN`
- Label：觀測點 `t = b_j`，若 `[t, t + ALERT_HORIZON_MIN]` 內存在 gap start → `label = 1`
- **C1 延伸拉取**：`window_end` 往後**至少** `LABEL_LOOKAHEAD_MIN`，實務建議 **1 天**（SSOT §7.2）；延伸區間僅用於標籤計算，不納入訓練集
- 嚴禁進入特徵的衍生量：`minutes_to_next_bet`、`next_bet_dtm` 等

---

### Step 4 — 共用特徵模組（`[trainer/features.py](trainer/features.py)`，Train-Serve Parity 核心）

**目標**：Trainer 與 Scorer 共同匯入的唯一特徵計算來源；v3 修正 TRN-09（lossstreak 方法）、F2（winsorization）、F4（PUSH 語義）、D1（TABLEHCWINDOWMIN）。

#### A. 當前投注特徵（直接來自 `t_bet FINAL`）

- `wager`, `payout_odds`, `base_ha`, `bet_type`, `is_back_bet`, `position_idx`

#### B. 當前 Run 內累積特徵

- `cum_bets`, `cum_wager`, `avg_wager_sofar`, `minutes_since_run_start`, `bets_per_minute`
- **Run 邊界**（B2 修正）：`detect_run_boundary(prev_dtm, curr_dtm)` 由 `features.py` 實作，`scorer.py` 直接匯入同一函數
- **lossstreak（TRN-09 / E2 修正 — 高）**：`payout_value` 100% NULL（FND-08），不可用 `payout - wager`。改用 `t_bet.status` 直接判定：
  - `status = 'LOSE'` → 連敗 +1
  - `status = 'WIN'` → 連敗重置為 0
  - `status = 'PUSH'` → 依 `LOSS_STREAK_PUSH_RESETS`（from config）決定是否重置（F4 修正）
  - 其他 status 值 → 視同 PUSH 處理並記錄警告

#### C. 滾動窗口特徵

- 過去 5/15/30 分鐘投注次數；10/30 分鐘投注金額
- **邊界語義**（TRN-08 修正）：統一 `[t - window_min, t)`（不含當前 bet）
- 所有查詢必須用 `t_bet FINAL`（E5 修正）

#### D. 時間上下文

- `time_of_day_sin = sin(2π × hour_of_day / 24)`
- `time_of_day_cos = cos(2π × hour_of_day / 24)`

#### E. 歷史賭客特徵（僅有卡客）

- `hist_session_count`、`hist_avg_bet`、`hist_win_rate`
- **A1 修正**：只使用 `COALESCE(session_end_dtm, lud_dtm) + SESSION_AVAIL_DELAY_MIN <= obs_time` 的 `t_session FINAL` 資料
- **F2 修正（v3 新增）**：`hist_avg_bet` 計算後必須套用 winsorization：`min(raw_hist_avg_bet, HIST_AVG_BET_CAP)`（`t_session.avg_bet` 極端值可達 5 兆，直接影響模型穩定性）

#### S1 — `table_hc` 即時計算

- **D1 修正（v3 補完）**：窗口大小使用 `TABLE_HC_WINDOW_MIN`（from config），不在 features.py 內硬編碼
- **A2 修正**：`payout_complete_dtm <= obs_time - BET_AVAIL_DELAY_MIN`（from config）
- 查詢必須用 `t_bet FINAL`（E5 修正）

#### 特徵組對應

- **Rated**：A + B + C + D + E + tablehc
- **Non-rated**：A + B + C + D + tablehc（D 類無 session 依賴，應包含；B3 修正）

---

### Step 5 — 訓練器重構（`[trainer/trainer.py](trainer/trainer.py)`）

- 從 `config.py` 讀取所有參數
- 依序呼叫 `identity.py`（`cutoff_dtm = training_window_end`）→ `labels.py`（C1）→ `features.py`（E 類嚴格過濾）
- **雙模型分離**（§9.1）：Rated / Non-rated 各自訓練
- **類別不平衡**：LightGBM 使用 `class_weight='balanced'`
- **時間切割**：Train 70% / Valid 15% / Test 15%，嚴格按 `payout_complete_dtm` 排序
- **TRN-07 快取驗證**：快取 key = `(window_start, window_end, data_hash)`；不符時強制重算
- **輸出**：`rated_model.pkl`、`nonrated_model.pkl`、`feature_list.json`、`model_version`（格式：`YYYYMMDD-HHMMSS-{git_short_hash}`）

---

### Step 6 — 閾值選擇與回測（`[trainer/backtester.py](trainer/backtester.py)`，G1）

**G1 閾值搜尋**（TRN-11 修正）：

1. 在 validation set 掃描 0.01–0.99（步長 0.01）
2. 過濾：Precision ≥ `G1_PRECISION_MIN`
3. 過濾：每小時警報量 ≥ `G1_ALERT_VOLUME_MIN_PER_HOUR`
4. **D4 修正（v3 補完）**：警報量計算 = **Rated + Non-rated 兩個模型的警報合計**，不得只計算其中一個模型
5. 滿足兩者約束 → 最大化 `F_{G1_FBETA}`
6. 無可行解 → 記錄並回報，降低 `G1_PRECISION_MIN` 後重試
7. 閾值只在 validation set 選定；test set 僅用於最終報告

**回測規則**：

- Visit 去重依 `GAMING_DAY_START_HOUR`（from config）定義 gaming day 邊界
- 嚴格時序處理，不 look-ahead

**輸出**（valid + test 各一份）：Precision / Recall / PR-AUC / F-beta / 每小時警報量（P5–P95）/ `rated_threshold` / `nonrated_threshold`

---

### Step 7 — Scorer 更新（`[trainer/scorer.py](trainer/scorer.py)`）

- **移除**所有獨立特徵計算，改為匯入 `features.py`（TRN-05/07/08 修正）
- 所有 ClickHouse 查詢加 `FINAL`（E5 修正）
- `t_bet` 查詢基礎過濾：`payout_complete_dtm IS NOT NULL AND player_id IS NOT NULL AND player_id != PLACEHOLDER_PLAYER_ID`（E3/E4 修正）
- E 類特徵：只讀取 `session_avail_dtm <= now() - SESSION_AVAIL_DELAY_MIN` 的快取統計（A1 修正）
- **D2 三步身份判定**（§6.4）：依 `identity.py` 定義，三步兜底至無卡客
- **Reason code 輸出**（§12.1）：SHAP top-k → 4 個固定 reason codes；版本化映射表；穩定性護欄（連續兩個輪詢週期一致才展示）

---

### Step 8 — 線上 Validator 更新（`[trainer/validator.py](trainer/validator.py)`）

- Horizon = `LABEL_LOOKAHEAD_MIN = 45`（from config）
- 分組鍵改用 `canonical_id`（D2）
- Ground truth：`t_bet FINAL` 時間序列，警報後 45 分鐘內出現 ≥ `WALKAWAY_GAP_MIN` 間隔
- Visit 去重：同 Step 6（gaming day 邊界）
- 回寫欄位新增 `model_version`, `canonical_id`, `threshold_used`, `margin`

---

### Step 9 — Model Service API（`[trainer/api_server.py](trainer/api_server.py)`）

- `POST /score`：接收 bet-level 特徵列（上限 10,000）→ 3 秒內回傳；stateless & idempotent
- `GET /health`：`{"status": "ok", "model_version": str}`
- `GET /model_info`：從 `feature_list.json` 動態讀取（非硬編碼）
- 缺欄位或多餘欄位一律 422，不默默補齊（SSOT §9.3）

---

### Step 10 — Testing & Validation 規格（E8 v3 新增）

**必須實作的測試，對應各合規議題**

- **Leakage 偵測測試**
  - 驗證 E 類特徵（`hist_*`）在所有觀測點均無未來資料（`session_avail_dtm <= obs_time`）
  - 驗證 S1 `table_hc` 的最晚資料點 ≤ `obs_time - BET_AVAIL_DELAY_MIN`
  - 驗證標籤建構用的延伸資料未混入訓練 feature matrix
- **Train-Serve Parity 測試**
  - 對同一批觀測點，分別從 `trainer.py`（批次）和 `scorer.py`（增量）計算特徵，斷言特徵值差異 < 容忍閾值（例如浮點誤差 1e-6）
  - 驗證 `run_boundary`、rolling window 邊界語義的輸出一致性
  - 驗證訓練與推論均使用 `FINAL` 關鍵字（E5）：對測試 ClickHouse 實例注入重複行，確認查詢結果去重
- **Label Sanity 測試**
  - `label = 1` 的比例在預期範圍（例如 5%–25%）
  - 確認延伸區間的 bet 無 `label = 1` 進入 feature matrix
- **D2 覆蓋率測試**
  - 驗證 `canonical_id` 空值率 < 容忍閾值（例如 < 1%）
  - 驗證 FND-12 假帳號排除後的影響行數與預期相符
  - 驗證 `player_id = -1` 佔位符已全數排除（E4/F1）
- **Schema 合規測試**
  - 驗證 `t_bet` 查詢不包含 `is_manual` 欄位（E1）
  - 驗證 `payout_value` / `bonus` 等 100% NULL 欄位未出現在 feature 計算中（FND-08）
  - 驗證 `loss_streak` 僅依賴 `status` 欄位（E2）
  - 驗證 `hist_avg_bet` 最大值 ≤ `HIST_AVG_BET_CAP`（F2）

---

## TRN-* Remediation Checklist（SSOT §12 + v3 更新）


| TRN        | 問題描述                        | 計畫落地                | v3 更新                                      |
| ---------- | --------------------------- | ------------------- | ------------------------------------------ |
| **TRN-01** | Session 去重未使用 `lud_dtm`     | Step 1（FND-01）      | 加 `FINAL`（E5）                              |
| **TRN-02** | 未排除 `is_manual = 1`         | Step 1（FND-02）      | **僅 `t_session`**；`t_bet` 無此欄（E1 修正）       |
| **TRN-03** | `player_id` 歸戶斷鏈            | Step 2（identity.py） | E6 FND-12 聚合 SQL；E4 `player_id != -1`      |
| **TRN-05** | `table_hc` 依賴 `session_end` | Step 4（S1）、Step 7   | D1 `TABLE_HC_WINDOW_MIN` from config       |
| **TRN-06** | 右截尾標籤膨脹                     | Step 3（labels.py）   | 無新增                                        |
| **TRN-07** | 快取無窗口一致性                    | Step 5、Step 7       | 無新增                                        |
| **TRN-08** | Rolling window 邊界不一致        | Step 4、Step 7       | 無新增                                        |
| **TRN-09** | `loss_streak` 字串比較永為 0      | Step 4 B 類          | **E2 修正**：改用 `status='LOSE'`；E2/F4 PUSH 語義 |
| **TRN-11** | 閾值過度保守，幾乎無警報                | Step 6（G1）          | **D4 修正**：alert volume = 雙模型合計             |


---

## Spec Compliance Review 完整修正摘要（三輪）


| 編號        | 嚴重性   | 類型            | SSOT/Schema 位置                     | v3 修正位置                                           |
| --------- | ----- | ------------- | ---------------------------------- | ------------------------------------------------- |
| A1        | 高     | Leakage       | §4.2, §8.1                         | Step 4 E 類：`session_avail_dtm <= obs_time`        |
| A2        | 低     | Leakage       | §4.2, S1                           | Step 4 S1：扣除 `BET_AVAIL_DELAY_MIN`                |
| B1        | 中     | Parity        | §6.2, D2                           | Step 2：`cutoff_dtm` 截止 + 快取版本控制                   |
| B2        | 中     | Parity        | §8.2.B                             | Step 0/4：`RUN_BREAK_MIN` + 共用函數                   |
| B3        | 低     | 特徵遺漏          | §9.1                               | Step 4：Non-rated 含 D 類                            |
| C1        | 低     | SSOT 含糊       | §7.2                               | Step 3：明確「至少 X+Y，建議 1 天」                          |
| C2        | 資訊性   | SSOT 不一致      | §2.1 vs §4.2                       | Step 0：`SESSION_AVAIL_DELAY_MIN = 15` + rationale |
| C3        | 中     | 定義缺口          | §2.2, §10.3                        | Step 0：`GAMING_DAY_START_HOUR`；Step 6/8           |
| D1        | 低     | 常數遺漏          | S1                                 | Step 0：`TABLE_HC_WINDOW_MIN`                      |
| D3        | 低     | 已知限制          | D2                                 | Step 2：備忘；Phase 2 再實作 PIT-correct mapping         |
| D4        | 中     | 定義缺口          | §10.2, G1                          | Step 6：alert volume = 雙模型合計                       |
| **E1**    | **高** | **Schema 錯誤** | `t_bet` DDL                        | Step 1：移除 `t_bet.is_manual` 過濾                    |
| **E2**    | **高** | **邏輯錯誤**      | FND-08, `t_bet.status`             | Step 4 B 類：`loss_streak` 改用 `status='LOSE'`       |
| **E3**    | 中     | Nullable 欄位   | `t_bet.payout_complete_dtm`        | Step 1/4：基礎過濾加 `IS NOT NULL`                      |
| **E4/F1** | 中     | 佔位符 ID        | `t_bet.player_id = -1`             | Step 0/1/2：`PLACEHOLDER_PLAYER_ID` 常數 + 過濾        |
| **E5**    | 中     | ClickHouse 引擎 | `ReplacingMergeTree`               | Step 1/4/7：所有查詢加 `FINAL`                          |
| **E6**    | 低     | 聚合 SQL 錯誤     | FND-12                             | Step 2：正確聚合 SQL 範例                                |
| **E7**    | 低     | NULL-unsafe   | FND-04                             | Step 1：`COALESCE(field, 0) > 0`                   |
| **E8**    | 低     | 測試缺失          | 整體                                 | Step 10：Testing & Validation 規格                   |
| **F2**    | 低     | 極端值           | `t_session.avg_bet`                | Step 0/4：`HIST_AVG_BET_CAP` + winsorization       |
| **F3**    | 低     | DQ 護欄缺口       | `t_session.is_deleted/is_canceled` | Step 1：加入基礎過濾                                     |
| **F4**    | 低     | 語義未定          | `t_bet.status = 'PUSH'`            | Step 0/4：`LOSS_STREAK_PUSH_RESETS` 常數             |


---

## 開放問題（需業務確認後才可落地 Step 6）

- `G1_PRECISION_MIN`（暫定 0.70）
- `G1_ALERT_VOLUME_MIN_PER_HOUR`（暫定 5）
- `GAMING_DAY_START_HOUR`（暫定 06:00；需營運部門確認）
- `TABLE_HC_WINDOW_MIN`（暫定 30 分鐘）
- `HIST_AVG_BET_CAP`（暫定 500,000；需 EDA 驗證分位數）
- `LOSS_STREAK_PUSH_RESETS`（暫定 False；需業務規則確認）

---

## 本計畫不涵蓋（Phase 2+ 事項）

- PIT-correct D2 mapping（D3 備忘）
- `t_game` 牌局氛圍特徵（FND-14）
- 序列嵌入 / SSL 預訓練（§8.4）
- `tsfresh` / `Featuretools` 自動特徵探索（§8.5）
- 跨桌造訪建模

