---
name: Patron Walkaway Phase 1
overview: 依據 SSOT 文件（v10 對齊，整合七輪 Spec Compliance Review、SSOT 大幅改寫及 §10 評估口徑 / §12.1 展示邊界釐清、DEC-009/010 閾值策略），全面重構 trainer/scorer pipeline，採雙軌特徵工程架構（Featuretools DFS 軌道 A + 向量化手寫軌道 B），**Bet-level 評估**（Phase 1 簡化，DEC-012；Run-level / Cooldown 延後待業務校準），封閉 leakage、train-serve parity 破口、Schema 層級錯誤與 edge cases（含 E1–E8、F1–F4、G1–G5、H1–H4、I1–I6），並建立完整的 Testing & Validation 規格，達成 Phase 1 MVP 上線條件。
todos:
  - id: config-definitions
    content: Step 0：在 config.py 集中定義所有常數（含 v3 新增 TABLE_HC_WINDOW_MIN / PLACEHOLDER_PLAYER_ID / LOSS_STREAK_PUSH_RESETS / HIST_AVG_BET_CAP；v6 新增 OPTUNA_N_TRIALS）
    status: pending
  - id: dq-guardrails
    content: Step 1：P0 資料品質護欄（E1 移除 t_bet.is_manual；E3 payout_complete_dtm IS NOT NULL；E4/F1 player_id != -1；G1 t_session 禁用 FINAL、用 FND-01 去重；E7 FND-04 COALESCE；F3 is_deleted/is_canceled）
    status: pending
  - id: identity-module
    content: Step 2：新建 identity.py（E6 FND-12 正確聚合 SQL；E4 player_id != -1；D2 M:N 衝突規則；B1 cutoff_dtm）
    status: pending
  - id: labels-module
    content: Step 3：新建 labels.py（C1 防洩漏；延伸拉取至少 X+Y；t_bet FINAL + E3 IS NOT NULL；G3 穩定排序 payout_complete_dtm, bet_id）
    status: pending
  - id: features-module
    content: Step 4：新建 features.py（導入雙軌特徵工程；軌道 A EntitySet 兩階段 DFS；軌道 B 向量化手寫 loss_streak 等；嚴格套用 cutoff_time 防漏）
    status: pending
  - id: trainer-refactor
    content: Step 5：重構 trainer.py（整合新模組；特徵篩選 Feature screening；雙模型；class_weight='balanced'；Optuna 超參調優；TRN-07 快取驗證）
    status: pending
  - id: backtester-threshold
    content: Step 6：更新 backtester.py（F1 閾值搜尋 Optuna TPE，OPTUNA_N_TRIALS；無 G1 約束；雙模型合計 alert volume；Bet-level 評估報告，DEC-012）
    status: pending
  - id: scorer-refactor
    content: Step 7：重構 scorer.py（匯入 features.py；t_bet FINAL；t_session 禁用 FINAL + FND-01 去重；E3/E4/G2 基礎過濾與回補；G3 穩定排序；D2 三步身份判定；reason codes）
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

# Patron Walkaway Phase 1 實作計畫（v10）

> **v10** 依據 DEC-009/010 與 SSOT 閾值策略，在 v9 基礎上：
> - **閾值策略**：移除 G1 約束，改為 **F1 最大化**；backtester 僅搜尋閾值，無 precision/alert volume 門檻。
> - **軌道 B Phase 1**：`table_hc` 延至 Phase 2；本期啟用 `loss_streak`、`run_boundary`。
> - **player_profile_daily**：若建置，採 PIT/as-of join 貼到 bet，不納入 EntitySet relationship。
> - **SESSION_AVAIL_DELAY_MIN**：預設 7 分鐘；FND-12 亦須在建置 player_profile_daily 時套用。

> v9 依據 SSOT 最新修訂（§10.3 評估口徑、§12.1 展示邊界、§14 前端/產品 scope 釐清），在 v8 基礎上：
>
> - **評估報告（SSOT §10）**：Phase 1 採 Bet-level（Micro）為主；Run-level / Macro-by-run 延後待業務校準（DEC-012）。術語統一為 **Run**（bet-derived 連續下注段，gap ≥ RUN_BREAK_MIN 切分；不再使用 Visit，見 DEC-013）。
> - **回測去重口徑澄清（SSOT §10.3）**：「每 run 至多計 1 次 TP」為**離線評估口徑**，不等同線上只發一次 alert。線上通知節流屬產品/前端決策。
> - **Reason code 展示邊界（SSOT §12.1）**：模型服務每次推論必須輸出 `reason_codes`/`score`/`margin`；「連續兩輪一致才展示」等 UX 過濾策略屬前端（`trainer/frontend/`）責任，不在模型層實作。

> v8 依據 SSOT 大幅改寫，對齊 §8.2 雙軌特徵工程架構。最重大變更：
>
> - **雙軌特徵工程**：軌道 A（Featuretools DFS）負責自動展開聚合/窗口/組合特徵空間；軌道 B（向量化 Pandas/Polars 手寫）負責 Featuretools 天然無法或效能極差的狀態機/跨玩家邏輯（`loss_streak`、`run_boundary`、`table_hc`）。兩軌共用同一 `cutoff_time` / 時間窗口框架，產出後 join 成統一 feature matrix。
> - **兩階段 DFS 流程**：第一階段在抽樣集上探索並以 `save_features` 持久化計算圖；第二階段以 `calculate_feature_matrix(saved_feature_defs)` 對全量資料計算，從根本消除 train-serve parity 風險。
> - `**player` 實體新增**：EntitySet 擴展至三實體（`t_bet` → `t_session` → `player`），以 `canonical_id` 為軸心支援跨 session 聚合。
> - **集中式時間窗口定義器**：強制建立 Time Fold Splitter / Window Definer 模組（`time_fold.py`），統一管理所有時間切分邊界（SSOT §4.3）。

> v6 在 v5 基礎上新增整合 **第六輪 Spec Compliance Review（I1–I4, I6）**。最重大變更：
>
> - **I1（中）**：Step 2 FND-12 假帳號排除 SQL 的 `HAVING` 子句使用了不存在的 `num_games` 欄位。修正為 `SUM(COALESCE(num_games_with_wager, 0)) <= 1`（`num_games_with_wager` 為 DDL 實際存在欄位，且為 Nullable）。
> - **I2（中）**：Step 2 的兩段 SQL 範例皆直接 `FROM t_session WHERE ...`，未套用 FND-01 去重 CTE，若照抄會在原始未去重 rows 上建 mapping。修正為先包裹 `WITH deduped AS (ROW_NUMBER ...)` 再查詢。
> - **I3（低）**：TRN-* Checklist 的 TRN-01「v3 更新」欄仍寫「加 FINAL（E5）」，與 v4 G1 決策矛盾。修正為「禁用 FINAL（G1），純依 FND-01 去重」。
> - **I4（低）**：Step 6 閾值搜尋只描述單維掃描，未說明如何同時決定 `rated_threshold` 與 `nonrated_threshold`。首次明確二維搜尋需求（後由 I6 取代）。
> - **I6（低）**：二維網格搜尋（99×99 = 9,801 組合）效率不足。改用 **Optuna TPE**（`TPESampler`，`n_trials=OPTUNA_N_TRIALS=300`），以貝葉斯採樣取代窮舉，大幅降低評估次數並智慧收斂至最優閾值組合。

> v5 在 v4 基礎上新增整合 **第五輪 Spec Compliance Review（H1–H4）**。最重大變更：
>
> - **G1（高）**：`t_session` 禁用 `FINAL`，避免 ReplacingMergeTree（無 version 欄位）在 merge 時非決定性丟列，破壞 FND-01 的業務去重（MAX(lud_dtm)）。
> - **G3（中）**：全系統穩定排序 `ORDER BY payout_complete_dtm ASC, bet_id ASC`，封住同毫秒多注造成的 Train-Serve Parity 破口。
> - **G4（低）**：Visit / 去重以表內 `gaming_day` 欄位為準，不再以 `GAMING_DAY_START_HOUR` 自算作為主流程。
> - **G2（中）**：`t_bet.player_id` 缺失但 `session_id` 存在時，先 join `t_session` 回補 `player_id` 再做 D2。
> - **G5（低）**：`hist_avg_bet` winsorize 必須先做 row-level cap，再做跨 session 聚合。
> - **H1（中）**：標籤建構對 `next_bet` 缺失（終端下注）需明確處理：若資料覆蓋足夠則視為 gap start；不足則視為 censored 排除（避免右截尾 TRN-06 反覆出現）。
> - **H3（低）**：雙模型推論路由規則明文化（避免用錯欄位或用 `is_known_player`）。
> - **H4（低）**：針對 `wager` 空值傳播加入防呆，但以 raw parquet 證據確認本批資料 `wager NULL=0`，因此不把 `wager_is_null` 納入 feature list（避免常數特徵）。

---

## 架構決策摘要（SSOT 固定）

- 身份歸戶：**D2**（Canonical ID；`casino_player_id` 優先）
- 右截尾：**C1**（Extended pull；至少 X+Y，建議 1 天）
- Session 特徵策略：**S1**（保守；**Phase 1 不啟用 `table_hc`**；若啟用則 `table_hc` 改用 `t_bet` 即時計算，禁用 `session_end`）
- 上線閾值策略：**F1 最大化**（DEC-009, DEC-010）；不設 precision/alert volume 下限約束
- 模型：Phase 1 = **LightGBM 雙模型**（Rated / Non-rated）
- 評估口徑：**Bet-level**（SSOT §10；Run-level 延後見 DEC-012）。術語統一為 **Run**（DEC-013）

---

## SSOT 章節對應與相關文件

| SSOT 章節 | 對應 Phase 1 步驟 | 備註 |
|-----------|------------------|------|
| §2 名詞定義 | Step 0 | WALKAWAY_GAP_MIN, ALERT_HORIZON_MIN, Run 定義 |
| §4 資料來源與即時可用性 | Step 0, Step 5, Step 7 | BET_AVAIL_DELAY_MIN, SESSION_AVAIL_DELAY_MIN, time_fold |
| §4.3 Player-level table | Step 4 EntitySet | 完整規格見 `doc/player_profile_daily_spec.md` |
| §5 關鍵資料品質護欄 | Step 1, Step 2 | FND-01～FND-14, F3, FND-04 不要過濾 status |
| §6 玩家身份歸戶 D2 | Step 2 identity.py | FND-12 建置時套用；FND-12 亦用於 player_profile_daily |
| §7 標籤設計 | Step 3 labels.py | C1 延伸拉取，G3 穩定排序 |
| §8.2 雙軌特徵工程 | Step 4 features.py | 軌道 A DFS + 軌道 B loss_streak/run_boundary；table_hc Phase 2 |
| §8.3 Session 特徵 S1 | Step 4, Step 7 | 進行中 session 不可見；table_hc Phase 1 不啟用 |
| §9 建模方法 | Step 5, Step 6 | 雙模型、Optuna、PR-AUC 超參、F1 閾值 |
| §9.4 Model API Contract | Step 9 | 完整合約見 `doc/model_api_protocol.md` |
| §10 評估與閾值 | Step 6 | Bet-level；F1 最大化；DEC-009/010 |
| §12 Remediation / Reason codes | Step 7, Step 10 | TRN-* 對應；reason codes 見 §12.1 |

**相關文件**：`doc/FINDINGS.md`（FND-*）、`doc/player_profile_daily_spec.md`（player-level 欄位規格）、`doc/model_api_protocol.md`（API 合約）、`.cursor/plans/DECISION_LOG.md`（DEC-*）、`schema/GDP_GMWDS_Raw_Schema_Dictionary.md`（欄位字典）

---

## 主要異動檔案

```
trainer/
├── config.py        ← 更新：新增 TABLE_HC_WINDOW_MIN / PLACEHOLDER_PLAYER_ID
│                              / LOSS_STREAK_PUSH_RESETS / HIST_AVG_BET_CAP / OPTUNA_N_TRIALS
├── time_fold.py     ← 新建：集中式時間窗口定義器（SSOT §4.3）
│                              所有 ETL / 特徵計算 / CV 必須呼叫此模組
├── identity.py      ← 新建：D2 歸戶（FND-12 正確聚合 + player_id != -1）
├── labels.py        ← 新建：C1 防洩漏標籤
├── features.py      ← 新建：雙軌特徵工程
│                              軌道 A：Featuretools EntitySet + DFS + save_features
│                              軌道 B Phase 1：loss_streak / run_boundary 向量化手寫；table_hc 延至 Phase 2
├── trainer.py       ← 重構：整合新模組，兩階段 DFS，雙模型訓練
├── scorer.py        ← 重構：saved_feature_defs（軌道 A）+ features.py（軌道 B），D2 三步身份判定
├── backtester.py    ← 更新：F1 閾值搜尋（Optuna TPE），無 G1 約束；alert volume = 雙模型合計
├── validator.py     ← 更新：canonical_id + 45min horizon
└── api_server.py    ← 更新：Model API Contract
```

---

## Phase 1 實作步驟

### Step 0 — 集中常數定義（`[trainer/config.py](trainer/config.py)`）

**目標**：全系統唯一事實來源；v3 新增 4 個常數；v6（I6）新增 `OPTUNA_N_TRIALS`。

**業務參數**

- `WALKAWAY_GAP_MIN = 30`（X）
- `ALERT_HORIZON_MIN = 15`（Y）
- `LABEL_LOOKAHEAD_MIN = 45`（= X + Y）

**資料可用性延遲**

- `BET_AVAIL_DELAY_MIN = 1`（`t_bet`；SSOT §4.2）
- `SESSION_AVAIL_DELAY_MIN = 7`（`t_session`；SSOT §2.1/§4.2；若需更保守可設為 15）

**Run 邊界**

- `RUN_BREAK_MIN = WALKAWAY_GAP_MIN`（= 30 分鐘）

**Gaming Day 與 Run 邊界（v4：G4 修正）**

- **主流程以資料表 `gaming_day` 欄位為準**：回測 dedup 可依實作階段使用 `(canonical_id, gaming_day)` 或 `(canonical_id, run_id)`。術語統一見 DEC-013。
- `GAMING_DAY_START_HOUR = 6` 僅保留為**備援參數**（敏感性分析或缺少 `gaming_day` 欄位的資料源才啟用），Phase 1 不依賴自算 gaming day。

**閾值搜尋（DEC-009, DEC-010：F1 最大化，無 G1 約束）**

- `OPTUNA_N_TRIALS = 300`（I6：Optuna TPE 搜尋次數；300 次在 2D 空間通常足以收斂）
- **廢棄（回退備註）**：若未來需恢復約束式 gate，可還原 `G1_PRECISION_MIN`、`G1_ALERT_VOLUME_MIN_PER_HOUR`、`G1_FBETA`；見 DEC-009/010 回退說明

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

- **G1（高，v4 新增 — `t_session` 禁用 `FINAL`）**：`t_session` 使用 `ReplacingMergeTree` 且未指定 version 欄位。
  - 使用 `FINAL` 時，ClickHouse merge 可能對重複 key 任意保留一列（非決定性），使得後續 FND-01 的 `ROW_NUMBER()` 沒機會選到真正最新的 `lud_dtm`。
  - 因此 `**t_session` 查詢不得使用 `FINAL`**，必須依賴 FND-01 的 SQL window 去重。
- **E5（中，v3 新增 — ClickHouse 去重策略）**：
  - `t_bet`：可使用 `FINAL` 以降低重複列風險（若後續確認 `bet_id` 幾乎無重複，可移除以提升效能）。
  - `t_session`：一律不用 `FINAL`（見 G1）。
- **FND-01（TRN-01）**：`t_session` 去重：`ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY lud_dtm DESC NULLS LAST, __etl_insert_Dtm DESC) = 1`
- **FND-02（TRN-02）— E1 修正（高）**：`is_manual = 0` 過濾**僅適用 `t_session`**。`t_bet` 表**無 `is_manual` 欄位**（Schema 確認），v2 計畫錯誤對 `t_bet` 套用此過濾，v3 必須移除。TRN-02 對應關係更新為：「`t_session` 加 `WHERE is_manual = 0`；`t_bet` 無對應過濾。」
- **E3（中，v3 新增 — Nullable 時間）**：所有 `t_bet` 查詢的基礎 WHERE 子句必須包含 `payout_complete_dtm IS NOT NULL`，因該欄位為 Nullable，NULL 值無法用於時間排序或滾動窗口計算。
- **E4/F1（中，v3 新增 — 佔位符 player_id）**：`player_id = -1` 或 NULL 視為無效。
  - **G2（中，v4 新增）**：若 `player_id` 無效但 `session_id` 有效，需先 join `t_session`（用 FND-01 去重後版本）回補 `player_id`：
    - `effective_player_id = COALESCE(t_bet.player_id, t_session.player_id)`
    - 最終只保留 `effective_player_id IS NOT NULL AND effective_player_id != PLACEHOLDER_PLAYER_ID`
- **F3（低，v3 新增 — 軟刪除欄位）**：`t_session` 查詢加入 `is_deleted = 0 AND is_canceled = 0`；這兩個欄位存在於 schema 但 v2 計畫未使用。
- **FND-03**：`casino_player_id` 清洗使用 `CASINO_PLAYER_ID_CLEAN_SQL`（from config）
- **FND-04（E7 修正 — NULL-safe；對齊 SSOT §5）**：**不要**過濾 `status = 'SUCCESS'`。保留所有非人工且 `COALESCE(turnover, 0) > 0 OR COALESCE(num_games_with_wager, 0) > 0` 的 Session；原版 NULL-unsafe，且盲目過濾 status 會蒸發約 17% 真實流水（見 `doc/FINDINGS.md`）。
- **FND-06**：全面移除 `bet_reconciled_at`（100% 無效值）
- **FND-08**：移除 `bonus`, `tip_amount`, `increment_wager`, `payout_value`（100% NULL；這也是 TRN-09/E2 修正的根本原因）
- **FND-09**：禁用 `is_known_player`；改為 `casino_player_id IS NOT NULL`
- **FND-13**：禁止使用 `__etl_insert_Dtm`、`__ts_ms` 做時間排序；時間軸以 `payout_complete_dtm`（`t_bet`）或 `COALESCE(session_end_dtm, lud_dtm)`（`t_session`）為準
- **FND-14**（Phase 2 備忘）：`t_game` 去重延至 Phase 2

---

### Step 2 — 身份歸戶模組（`[trainer/identity.py](trainer/identity.py)`，D2）

**目標**：建立 `player_id → canonical_id` 映射，修正 FND-11/TRN-03；v3 補完 E6（FND-12 聚合 SQL）與 E4（`player_id != -1`）。

**Mapping 建置查詢**（v4：G1 修正 — `t_session` 不用 `FINAL`；v6：I2 修正 — 先以 FND-01 CTE 去重再查詢）

```sql
-- 第一步：抽取連結邊（先 FND-01 去重）
WITH deduped AS (
  SELECT *,
    ROW_NUMBER() OVER (
      PARTITION BY session_id
      ORDER BY lud_dtm DESC NULLS LAST, __etl_insert_Dtm DESC
    ) AS rn
  FROM t_session
)
SELECT player_id, casino_player_id
FROM deduped
WHERE rn = 1
  AND is_manual = 0
  AND is_deleted = 0 AND is_canceled = 0
  AND player_id IS NOT NULL AND player_id != {PLACEHOLDER_PLAYER_ID}
  AND COALESCE(session_end_dtm, lud_dtm) <= :cutoff_dtm
  AND {CASINO_PLAYER_ID_CLEAN_SQL} IS NOT NULL
```

**FND-12 假帳號排除（E6 修正；v6：I1 修正欄位名稱、I2 修正加入 FND-01 去重 CTE；v10：應用時機）**：此排除須在建置 Canonical mapping（identity.py）與 **player_profile_daily**（若啟用）時一併套用。`session_cnt` / `total_games` 是聚合欄位，需先聚合再過濾；`num_games_with_wager`（有實際下注的局數）為 Schema 內實際欄位，需 COALESCE 因其為 Nullable：

```sql
-- 第二步：識別假帳號（先 FND-01 去重，再聚合）
WITH deduped AS (
  SELECT *,
    ROW_NUMBER() OVER (
      PARTITION BY session_id
      ORDER BY lud_dtm DESC NULLS LAST, __etl_insert_Dtm DESC
    ) AS rn
  FROM t_session
)
SELECT player_id
FROM deduped
WHERE rn = 1
  AND is_manual = 0
  AND is_deleted = 0 AND is_canceled = 0
  AND player_id IS NOT NULL AND player_id != {PLACEHOLDER_PLAYER_ID}
GROUP BY player_id
HAVING COUNT(session_id) = 1 AND SUM(COALESCE(num_games_with_wager, 0)) <= 1
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
- **G3（中，v4 新增 — 穩定排序）**：同毫秒多注（主注/旁注）會共享相同 `payout_complete_dtm`，標籤建構必須使用穩定排序：`ORDER BY payout_complete_dtm ASC, bet_id ASC`（訓練與服務端一致）。
- Gap start：`b_{i+1} - b_i ≥ WALKAWAY_GAP_MIN`
- Label：觀測點 `t = b_j`，若 `[t, t + ALERT_HORIZON_MIN]` 內存在 gap start → `label = 1`
- **C1 延伸拉取**：`window_end` 往後**至少** `LABEL_LOOKAHEAD_MIN`，實務建議 **1 天**（SSOT §7.2）；延伸區間僅用於標籤計算，不納入訓練集
- 嚴禁進入特徵的衍生量：`minutes_to_next_bet`、`next_bet_dtm` 等
- **H1（中，v5 新增 — 終端下注 / next_bet 缺失）**：對同一 `canonical_id` 的 bet 序列，若某筆 `b_i` 在延伸拉取後仍無 `b_{i+1}`（next_bet 缺失），必須明確區分：
  - **可判定（非右截尾）**：若延伸資料覆蓋足夠使得 `b_i + WALKAWAY_GAP_MIN <= window_end_extended`，則可視 `b_i` 為 gap start（表示在可觀測的未來至少 X 分鐘內皆無下注）。
  - **不可判定（右截尾）**：若 `b_i` 靠近 `window_end_extended`，使得未來 X 分鐘覆蓋不足，則將該觀測點標記為 **censored** 並排除訓練/評估（避免 TRN-06 重新發生）。
  - 註：這個規則只影響「next_bet 缺失」的邊界樣本；一般樣本仍由 `b_{i+1} - b_i >= X` 判定 gap start。

---

### Step 4 — 共用特徵模組（`[trainer/features.py](trainer/features.py)`，Train-Serve Parity 核心）

**目標**：導入雙軌特徵工程（Featuretools DFS + 向量化手寫），取代舊有靜態特徵清單；確保 EntitySet、Cutoff Time 與手寫邏輯在 Trainer 與 Scorer 間保持絕對一致（Train-Serve Parity）。

#### A. 構建 EntitySet 與基元 (Primitives)

- **實體定義**（對齊 SSOT §8.2）：
  - `t_bet`（target entity）：以 `bet_id` 為 index，`payout_complete_dtm` 為 time_index。
  - `t_session`（歷史輪廓）：以 `session_id` 為 index，`COALESCE(session_end_dtm, lud_dtm)` 為 time_index；僅納入 `available_time <= cutoff_time` 的已可得 sessions。
  - `player`（跨日/跨桌輪廓，Phase 1）：以 `canonical_id` 為 index；由 `t_session` 依 `canonical_id` 聚合作為跨 session 聚合軸心。
  - **player_profile_daily**（Phase 1 後延伸）：行為輪廓快照（RFM 等），以 **PIT/as-of join**（`snapshot_dtm <= bet_time` 最新快照）將欄位貼到 bet，**不**在 EntitySet 內宣告 relationship；見 SSOT §4.3、§8.2；**完整欄位規格與特徵取捨理由**見 `doc/player_profile_daily_spec.md`。
  - **關係**：
    - `t_bet.session_id` → `t_session.session_id`（many-to-one）。`table_id` 僅為共有欄位，不作為 EntitySet 父子關係鍵。
    - `t_session.canonical_id` → `player.canonical_id`（many-to-one）。
- **特徵基元 (Primitives)**：
  - 轉化基元：`time_since`, `cum_sum`, `cum_mean`, 時間週期（時/分轉換）等。
  - 聚合基元：過去行為的 `count`, `sum`, `mean`, `max`, `min`, `trend`。
  - **滾動窗口**：利用 `window_size` 參數（例如 5m, 15m, 30m 等）自動展開近期行為統計。
  - **H4（低，v5 補充 — 數值防呆）**：在餵入 EntitySet 前，對數值欄位（如 `wager`）做 `fillna(0)` 防呆。

#### B. 軌道 B：手工向量化特徵（Featuretools 無法或不宜處理的邏輯）

以下特徵因涉及**條件重置狀態機**或**跨玩家/跨桌聚合**，Featuretools 天然無法支援或效能極差，**必須**以高效的向量化程式碼（Pandas/Polars）手寫實作，禁止逐列遍歷（Python for-loop / `apply`）：


| 特徵                                         | 為何不適合 Featuretools                                                        | 實作方式                                                                                                                          |
| ------------------------------------------ | ------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `loss_streak`                              | 需要「遇 WIN 重置、遇 PUSH 條件不重置」的序列狀態機，無對應內建 Primitive                           | 向量化 Pandas/Polars；`status='LOSE'`→+1，`status='WIN'`→重置，`PUSH` 依 `LOSS_STREAK_PUSH_RESETS`（F4）；嚴格遵守同一 cutoff_time（TRN-09 / E2） |
| `run_boundary` / `minutes_since_run_start` | 需要「相鄰 bet 間距 ≥ `RUN_BREAK_MIN` → 新 run 開始」的序列依賴切割                         | 同上（B2 修正）                                                                                                                     |
| `table_hc`（S1）                             | 需跨玩家、以 `table_id` 為軸心的滾動聚合；若在 EntitySet 中建 table 實體，每個 cutoff 觸發全桌掃描，效能極差 | 同上；**Phase 1 不啟用**，延至 Phase 2；可實作於 `features.py` 但 scorer/trainer 不呼叫 |


**手寫特徵防漏與 Parity 要求**：

- 必須與軌道 A 共用同一個 `cutoff_time` / 時間窗口框架。
- 計算函數必須抽取至 `features.py`，由 trainer 與 scorer 共同匯入（TRN-05/07/08）。
- **G3（v4）**：同毫秒多注必須以 `payout_complete_dtm ASC, bet_id ASC` 穩定排序後再計算狀態。
- **G2（v4）**：餵入前，若 `t_bet.player_id` 無效但 `session_id` 有效，先以去重後 `t_session` 回補 `effective_player_id`。

#### C. 兩階段 DFS 流程（解決單機資源限制）

訓練環境為**單機**，無法對全量 4.38 億筆一次性做 DFS。採用以下兩階段流程：

- **第一階段 — 探索（在抽樣資料上）**：
  - 對各月度窗口內的多數類（label=0）做**時間分層下採樣（保留 10–20%）**，正例全保留，使每月觀測點從 ~2,300 萬降到幾百萬。
  - 在此抽樣集上跑完整 DFS（建議 `max_depth<=2`，primitives 白名單），產出大量候選特徵。
  - 做 Feature Screening（見下方 §D），選出高潛力候選。
  - 以 `featuretools.save_features(feature_defs)` **將選中特徵的計算圖持久化**。
- **第二階段 — 生產（全量）**：
  - 以 `featuretools.calculate_feature_matrix(saved_feature_defs, entityset)` **直接套用已存的特徵定義**，逐月計算全量資料。
  - **不重新實作**：訓練與推論都使用同一份 saved feature definitions，從根本上消除 train-serve parity 風險。
  - 每月產出 parquet 落盤，合併後送入模型。

#### D. 特徵篩選（Feature Screening）

軌道 A（DFS）產出的候選特徵數量可能很大，**必須**在送入正式訓練前篩選：

1. **第一階段**：依單變量與目標關聯（如 mutual information、變異數門檻）及冗餘剔除（如高相關、VIF）縮減候選集。
2. **第二階段（可選）**：在**訓練集**上以輕量 LightGBM 計算 feature importance 或 SHAP，取 top-K。**嚴格限制在 Train 集，不得使用 valid/test**，以符合防洩漏原則（SSOT §8.2.C）。
3. 最終通過篩選的特徵清單（軌道 A 篩選後 + 軌道 B 本期啟用：`loss_streak`、`run_boundary`；不含 `table_hc`）即為 `feature_list.json`，訓練與推論端僅計算此清單內特徵。

#### E. 防洩漏與 Cutoff Time

- 訓練與推論計算特徵時，必須統一依賴 Featuretools 的 `cutoff_time` 參數，嚴格切斷該觀測點時間之後的所有資料。
- **E 類特徵修正（A1/F2/G5）**：使用 `t_session` 資料作為聚合基礎時，必須確保 `session_avail_dtm <= cutoff_time`，並對 `avg_bet` 等 outlier 進行事前 winsorization（row-level cap）再餵給工具聚合。
- **進行中 Session 不可見**（SSOT §8.3）：因 `t_session` 須滿足 `available_time <= cutoff_time`，當前仍在進行中的 session 對該 session 內當下的 bet 而言其 session-level 特徵**不可見**；此為設計護欄，非缺陷。
- **H4（v5 補充 — 數值防呆）**：在餵入 EntitySet 前，對數值欄位（如 `wager`）做 `fillna(0)` 防呆。

#### 特徵組對應（Rated vs Non-rated）

- **Rated**：軌道 A 開放完整 EntitySet 存取（`t_bet` + `t_session` + `player`），含歷史賭客輪廓與跨日聚合；軌道 B 亦全部可用。
- **Non-rated**：軌道 A 限制 EntitySet 僅探索 `t_bet` 內部路徑（不使用 `t_session` 歷史輪廓或 `player` 實體）；軌道 B 中 `loss_streak`、`run_boundary` 可用，但 `t_session` 相關聚合不可用。

---

### Step 5 — 訓練器重構（`[trainer/trainer.py](trainer/trainer.py)`）

- 從 `config.py` 讀取所有參數
- **集中式時間窗口定義器（SSOT §4.3 — 強制）**：**必須**呼叫 `time_fold.py` 模組取得所有時間窗口邊界（月度 chunk 的 `window_start`/`window_end`、C1 延伸拉取緩衝 `extended_end`、Train/Valid/Test cutoff 點）。禁止在 trainer 內部自行計算邊界。
  - **邊界語義合約**：核心窗口為 `[window_start, window_end)`；延伸拉取區間 `[window_end, extended_end)` **僅供**觀測未來事件以計算標籤，其樣本**絕對不可**納入訓練集。
- **時間窗口化抽取（SSOT §4.3）**：不得一次性載入全時段 4.38 億筆。依 `time_fold.py` 發放的邊界逐窗抽取 `t_bet`/`t_session`，對齊 partition（如 `gaming_day`）；每窗內跑 labels + features（DFS 依窗口分批呼叫），產出可寫磁碟或串接後再訓練。若需取樣，須保持時間順序並記錄取樣率。
  - **訓練/開發允許離線 Parquet 作為資料源**：若已將 ClickHouse 的完整表匯出到本機（例如 `.data/` 目錄中的 Parquet），trainer 可改為「讀取 Parquet（或經 DuckDB 掃描）」取代逐窗查 ClickHouse，以加速迭代；但仍必須套用完全相同的 DQ/去重/available_time 規則（Step 1 / SSOT §4.2 / §5）以及相同的時間窗口邊界語義。Production（線上 scorer/validator）資料來源仍以 ClickHouse 為準。
- 依序呼叫 `identity.py`（`cutoff_dtm = training_window_end`）→ 對**各時間窗口**：`labels.py`（C1）→ `features.py`（兩階段 DFS 軌道 A + 軌道 B 向量化手寫，傳入該窗之 cutoff 時間點）→ 特徵篩選（DFS 探索後執行一次，見 Step 4 §D）。
- **雙模型分離**（SSOT §9.1）：Rated / Non-rated 各自訓練
- **演算法與超參調優**（SSOT §9.2）：使用 LightGBM（`class_weight='balanced'`）。必須使用 **Optuna (TPE Sampler)** 在 validation set 上進行超參搜尋（如 `n_estimators`, `learning_rate`, `max_depth` 等），目標為最佳化 **PR-AUC**。閾值則以 **F1 最大化**（Step 6 backtester）選出。
- **時間切割**：Train 70% / Valid 15% / Test 15%，嚴格按 `payout_complete_dtm` 排序
- **TRN-07 快取驗證**：快取 key = `(window_start, window_end, data_hash)`；不符時強制重算
- **輸出（原子單位，版本化交付）**：`rated_model.pkl`、`nonrated_model.pkl`、`**saved_feature_defs`（軌道 A 計算圖）**、`features.py`（軌道 B）、`feature_list.json`、reason code 映射表、`model_version`（格式：`YYYYMMDD-HHMMSS-{git_short_hash}`）。任何組件版本不匹配時，服務端必須拒絕載入並回報錯誤。

---

### Step 6 — 閾值選擇與回測（`[trainer/backtester.py](trainer/backtester.py)`）

**F1 閾值搜尋**（TRN-11 修正；v6：I4/I6 Optuna TPE；v10：移除 G1 約束，DEC-009/010）：

**雙模型閾值搜尋策略（I6 — Optuna TPE）**：Rated 與 Non-rated 各有獨立閾值（`rated_threshold`、`nonrated_threshold`），以 Optuna `TPESampler` 在二維連續空間高效搜尋，**objective = F1 最大化**（無 precision/alert volume 下限約束）：

```python
import numpy as np
import optuna
from sklearn.metrics import f1_score
from config import OPTUNA_N_TRIALS

def objective(trial):
    rt = trial.suggest_float("rated_threshold", 0.01, 0.99)
    nt = trial.suggest_float("nonrated_threshold", 0.01, 0.99)

    rated_pred   = (rated_scores_val   >= rt).astype(int)
    nonrated_pred = (nonrated_scores_val >= nt).astype(int)

    # 目標：最大化 F1（合併 rated + nonrated 的預測與標籤）
    y_all = np.concatenate([y_rated_val, y_nonrated_val])
    pred_all = np.concatenate([rated_pred, nonrated_pred])
    return f1_score(y_all, pred_all, zero_division=0)

study = optuna.create_study(direction="maximize",
                            sampler=optuna.samplers.TPESampler(seed=42))
study.optimize(objective, n_trials=OPTUNA_N_TRIALS,
               callbacks=[optuna.study.MaxTrialsCallback(OPTUNA_N_TRIALS)])
```

- `OPTUNA_N_TRIALS = 300`（預設；可從 config 調整）
- 閾值只在 validation set 選定；test set 僅用於最終報告
- Optuna study 結果（`study.trials_dataframe()`）與最佳組合一同輸出，供後續分析

**回測規則**：

- **G4（低，v4 新增）**：回測 dedup 可依實作使用 `(canonical_id, gaming_day)` 或 `(canonical_id, run_id)`。`gaming_day` 以表內欄位為準；`GAMING_DAY_START_HOUR` 僅作備援。
- **回測評估口徑（SSOT §10.3）**：回測指標計算時，對同一 run 至多計入 **1 次 True Positive**，以避免高頻玩家因大量觀測點膨脹精準度指標。
  > **重要區分**：此去重僅是**離線評估口徑**，**不**意味著線上推論只對每位玩家每個 run 輸出一個 alert。線上通知節流屬產品/前端設計決策，不在模型規格範圍內。
- 嚴格時序處理，不 look-ahead

**輸出**（valid + test 各一份）：

- **Micro（以觀測點為單位）**：Precision / Recall / PR-AUC / F-beta / 每小時警報量（P5–P95）
- **Macro-by-run（Future TODO）**：以 run 為單位取平均；Phase 1 不採用，延後待業務校準（DEC-012）。
- `rated_threshold` / `nonrated_threshold`

---

### Step 7 — Scorer 更新（`[trainer/scorer.py](trainer/scorer.py)`）

- **移除**所有獨立特徵計算，改為：
  - **軌道 A**：以 `featuretools.calculate_feature_matrix(saved_feature_defs, entityset)` 傳入當前輪詢時間為 `cutoff_time`，使用與訓練期**相同的** saved feature definitions 計算特徵。
  - **軌道 B**：匯入 `features.py` 中的向量化手寫函數（`loss_streak`、`run_boundary`；`table_hc` Phase 1 不啟用）。
  - 確保 train-serve parity（TRN-05/07/08 修正）。
- **G3（中，v4 新增）**：推論端處理 bet 流必須使用穩定排序 `ORDER BY payout_complete_dtm ASC, bet_id ASC`。
- `t_bet` 查詢可使用 `FINAL`（E5）；`t_session` 禁用 `FINAL`（G1），並必須套用 FND-01 去重後再 join。
- `t_bet` 查詢基礎條件（v4）：`payout_complete_dtm IS NOT NULL AND (player_id 有效 OR session_id 有效)`。
  - **G2**：若 `player_id` 無效但 `session_id` 有效，先 join 去重後的 `t_session` 回補 `effective_player_id = COALESCE(t_bet.player_id, t_session.player_id)`，最終只保留有效的 `effective_player_id` 再進入狀態機。
- **歷史特徵（原 E 類）處理**：只允許將 `session_avail_dtm <= now() - SESSION_AVAIL_DELAY_MIN` 的已完成 `t_session` 加入 EntitySet（A1 修正）。
- **D2 三步身份判定**（§6.4）：依 `identity.py` 定義，三步兜底至無卡客
- **H3（低，v5 新增 — 雙模型路由規則）**：推論時必須明確決定此觀測點用哪個模型，禁止使用 `is_known_player`。
  - 定義 `resolved_card_id`：依 D2 三步身份判定流程，若能取得有效 `casino_player_id`（來自「可用時間已到的 t_session」或 mapping cache 命中），則 `resolved_card_id = casino_player_id`；否則為 NULL。
  - `is_rated_obs = (resolved_card_id IS NOT NULL)`。
  - `is_rated_obs = True` → 使用 Rated model（開放存取包含歷史關聯的 EntitySet 特徵）；否則 → 使用 Non-rated model（限制 EntitySet 僅能使用 `t_bet` 內部特徵）。
  - 所有輸出需記錄 `is_rated_obs` 與 `resolved_card_id`（若有）以便稽核。
- **Reason code 輸出**（SSOT §12.1）：SHAP top-k → 4 個固定 reason codes（`BETTING_PACE_DROPPING`、`GAP_INCREASING`、`LOSS_STREAK`、`LOW_ACTIVITY_RECENTLY`）；版本化映射表（隨 `model_version` 管理）。
  - 模型服務（`/score` 端點）**每次推論必須輸出** `reason_codes`、`score`、`margin`、`model_version`、`scored_at`，不在模型層做「連續輪詢一致才輸出」的過濾。
  - **展示穩定性屬前端/產品責任**：是否連續兩輪一致才對 Host 展示、或設通知冷卻期，由 `trainer/frontend/` 及 UX 邏輯決定，不在本模型工程規格範圍內。

---

### Step 8 — 線上 Validator 更新（`[trainer/validator.py](trainer/validator.py)`）

- Horizon = `LABEL_LOOKAHEAD_MIN = 45`（from config）
- 分組鍵改用 `canonical_id`（D2）
- Ground truth：`t_bet FINAL` 時間序列，警報後 45 分鐘內出現 ≥ `WALKAWAY_GAP_MIN` 間隔
- Visit 去重：同 Step 6（gaming day 邊界）
- 回寫欄位新增 `model_version`, `canonical_id`, `threshold_used`, `margin`

---

### Step 9 — Model Service API（`[trainer/api_server.py](trainer/api_server.py)`）

- 對齊 SSOT §9.4；**完整 API 合約與 schema 詳見** `doc/model_api_protocol.md`
- `POST /score`：接收 bet-level 特徵列（上限 10,000）→ 3 秒內回傳；stateless & idempotent
- `GET /health`：`{"status": "ok", "model_version": str}`
- `GET /model_info`：從 `feature_list.json` 動態讀取（非硬編碼）；特徵清單隨雙軌架構動態產生，非永久固定合約
- 缺欄位或多餘欄位一律 422，不默默補齊

---

### Step 10 — Testing & Validation 規格（E8 v3 新增）

**必須實作的測試，對應各合規議題**

- **Leakage 偵測測試**
  - 驗證歷史特徵（原 E 類）在所有觀測點均無未來資料（`session_avail_dtm <= obs_time`）。
  - 驗證 S1 `table_hc` 的最晚資料點 ≤ `obs_time - BET_AVAIL_DELAY_MIN`。
  - **Cutoff Time 驗證**：驗證 Featuretools 產出的 feature matrix 中，所有的聚合運算都沒有使用到大於等於 `cutoff_time` 的資料列。
  - 驗證標籤建構用的延伸資料未混入 EntitySet 供特徵合成使用。
- **Train-Serve Parity 測試**
  - 對同一批觀測點，分別從 `trainer.py`（批次 DFS）和 `scorer.py`（增量 EntitySet 更新）計算特徵，斷言特徵值差異 < 容忍閾值（例如浮點誤差 1e-6）。
  - 驗證 `run_boundary`、rolling window 邊界語義的輸出一致性
  - **G1**：驗證 `t_session` 查詢不使用 `FINAL`，且去重結果與 FND-01（MAX(lud_dtm)）一致
  - **G3**：在包含同毫秒多注的樣本集上，驗證 trainer/scorer 的處理順序與軌道 B 手寫狀態特徵及窗口統計逐筆一致（排序 key：`payout_complete_dtm`, `bet_id`）
  - **G2**：驗證 player_id 回補路徑：當 `t_bet.player_id` 無效但 `session_id` 有效時，`effective_player_id` 能被正確回補且不破壞分組/狀態機
- **Label Sanity 測試**
  - `label = 1` 的比例在預期範圍（例如 5%–25%）
  - 確認延伸區間的 bet 無 `label = 1` 進入 feature matrix
  - **H1**：對 `next_bet` 缺失樣本，驗證「覆蓋足夠 → gap start」與「覆蓋不足 → censored 排除」的分流正確，且不會在 window end 附近膨脹正例（防 TRN-06）
- **D2 覆蓋率測試**
  - 驗證 `canonical_id` 空值率 < 容忍閾值（例如 < 1%）
  - 驗證 FND-12 假帳號排除後的影響行數與預期相符
  - 驗證 `player_id = -1` 佔位符已全數排除（E4/F1）
- **Schema 合規測試**
  - 驗證 `t_bet` 查詢不包含 `is_manual` 欄位（E1）
  - 驗證 `payout_value` / `bonus` 等 100% NULL 欄位未出現在 feature 計算中（FND-08）
  - 驗證 `loss_streak` 僅依賴 `status` 欄位（E2）
  - 驗證 `hist_avg_bet` 最大值 ≤ `HIST_AVG_BET_CAP`（F2）
  - **G5**：驗證 winsorization 是先做 row-level cap（單列 `t_session.avg_bet`）再聚合，而不是聚合後才截斷
  - **H4（raw data 證據）**：對「本機匯出的 `t_bet` Parquet」（例如 `.data/` 內對應檔）讀取 parquet 統計或抽樣，驗證 `wager_null_pct == 0`；並在訓練/推論各自記錄 `wager_null_pct` 作為漂移監控（若 >0 觸發告警與 feature contract 更新）
- **Model Routing 測試**
  - **H3**：驗證 `is_rated_obs` 的路由條件不依賴 `is_known_player`，且 Rated/Non-rated 的 EntitySet 使用正確（Non-rated 限制存取歷史關聯路徑）。
- **雙口徑評估測試（SSOT §10.3）**
  - 驗證評估報告以 Bet-level（Micro）為主；Macro-by-run 延後（DEC-012）。
  - 驗證回測中同一 run 至多計入 1 次 True Positive（離線評估口徑去重）。
  - （Future TODO）驗證 Macro-by-run 的計算邏輯：先對每個 run 算 precision/recall，再取平均。

---

## TRN-* Remediation Checklist（SSOT §12 + v3 更新）


| TRN        | 問題描述                        | 計畫落地                | v3 更新                                                    |
| ---------- | --------------------------- | ------------------- | -------------------------------------------------------- |
| **TRN-01** | Session 去重未使用 `lud_dtm`     | Step 1（FND-01）      | v4 修正：`t_session` 禁用 `FINAL`（G1），純依 FND-01 ROW_NUMBER 去重 |
| **TRN-02** | 未排除 `is_manual = 1`         | Step 1（FND-02）      | **僅 `t_session`**；`t_bet` 無此欄（E1 修正）                     |
| **TRN-03** | `player_id` 歸戶斷鏈            | Step 2（identity.py） | E6 FND-12 聚合 SQL；E4 `player_id != -1`                    |
| **TRN-05** | `table_hc` 依賴 `session_end` | Step 4（S1）、Step 7   | D1 `TABLE_HC_WINDOW_MIN` from config                     |
| **TRN-06** | 右截尾標籤膨脹                     | Step 3（labels.py）   | 無新增                                                      |
| **TRN-07** | 快取無窗口一致性                    | Step 5、Step 7       | 無新增                                                      |
| **TRN-08** | Rolling window 邊界不一致        | Step 4、Step 7       | 無新增                                                      |
| **TRN-09** | `loss_streak` 字串比較永為 0      | Step 4 B 類          | **E2 修正**：改用 `status='LOSE'`；E2/F4 PUSH 語義               |
| **TRN-11** | 閾值過度保守，幾乎無警報                | Step 6（F1 閾值搜尋）   | **D4**：alert volume 雙模型合計；**v10**：移除 G1 約束，改 F1 最大化           |


---

## Spec Compliance Review 完整修正摘要（六輪）


| 編號        | 嚴重性   | 類型                 | SSOT/Schema 位置                                     | v6 修正位置                                                            |
| --------- | ----- | ------------------ | -------------------------------------------------- | ------------------------------------------------------------------ |
| A1        | 高     | Leakage            | §4.2, §8.1                                         | Step 4 歷史特徵：`session_avail_dtm <= cutoff_time`                     |
| A2        | 低     | Leakage            | §4.2, S1                                           | Step 4 S1：扣除 `BET_AVAIL_DELAY_MIN`                                 |
| B1        | 中     | Parity             | §6.2, D2                                           | Step 2：`cutoff_dtm` 截止 + 快取版本控制                                    |
| B2        | 中     | Parity             | §8.2.B                                             | Step 0/4：`RUN_BREAK_MIN` + 軌道 B 向量化手寫                              |
| B3        | 低     | 特徵遺漏               | §9.1                                               | Step 4：Non-rated 亦包含時間週期等轉換特徵                                      |
| C1        | 低     | SSOT 含糊            | §7.2                                               | Step 3：明確「至少 X+Y，建議 1 天」                                           |
| C2        | 資訊性   | SSOT 不一致           | §2.1 vs §4.2                                       | Step 0：`SESSION_AVAIL_DELAY_MIN = 7`（可選 15）；與 §2.1/§4.2 對齊     |
| C3        | 中     | 定義缺口               | §2.2, §10.3                                        | Step 0/6/8：回測 dedup 可用 `gaming_day` 或 `run_id`（G4）；術語統一為 Run（DEC-013） |
| D1        | 低     | 常數遺漏               | S1                                                 | Step 0：`TABLE_HC_WINDOW_MIN`                                       |
| D3        | 低     | 已知限制               | D2                                                 | Step 2：備忘；Phase 2 再實作 PIT-correct mapping                          |
| D4        | 中     | 定義缺口               | §10.2                                              | Step 6：F1 閾值搜尋；alert volume = 雙模型合計（報告用）                        |
| **E1**    | **高** | **Schema 錯誤**      | `t_bet` DDL                                        | Step 1：移除 `t_bet.is_manual` 過濾                                     |
| **E2**    | **高** | **邏輯錯誤**           | FND-08, `t_bet.status`                             | Step 4 B 類：`loss_streak` 改用 `status='LOSE'`                        |
| **E3**    | 中     | Nullable 欄位        | `t_bet.payout_complete_dtm`                        | Step 1/4：基礎過濾加 `IS NOT NULL`                                       |
| **E4/F1** | 中     | 佔位符 ID             | `t_bet.player_id = -1`                             | Step 0/1/2：`PLACEHOLDER_PLAYER_ID` 常數 + 過濾                         |
| **E5**    | 中     | ClickHouse 引擎      | `ReplacingMergeTree`                               | Step 1：區分策略（t_bet 可用 FINAL；t_session 禁用 FINAL，見 G1）                |
| **E6**    | 低     | 聚合 SQL 錯誤          | FND-12                                             | Step 2：正確聚合 SQL 範例                                                 |
| **E7**    | 低     | NULL-unsafe        | FND-04                                             | Step 1：`COALESCE(field, 0) > 0`                                    |
| **E8**    | 低     | 測試缺失               | 整體                                                 | Step 10：Testing & Validation 規格                                    |
| **F2**    | 低     | 極端值                | `t_session.avg_bet`                                | Step 0/4：`HIST_AVG_BET_CAP` + winsorization                        |
| **F3**    | 低     | DQ 護欄缺口            | `t_session.is_deleted/is_canceled`                 | Step 1：加入基礎過濾                                                      |
| **F4**    | 低     | 語義未定               | `t_bet.status = 'PUSH'`                            | Step 0/4：`LOSS_STREAK_PUSH_RESETS` 常數                              |
| **G1**    | **高** | 引擎/去重衝突            | `t_session` ReplacingMergeTree（無 version） + FND-01 | Step 1/2/4/7：`t_session` 禁用 FINAL；以 FND-01 去重                      |
| **G2**    | 中     | Identity fallback  | `t_bet.player_id` Nullable/-1                      | Step 1/4/7：回補 `effective_player_id` 後再做 D2                         |
| **G3**    | 中     | Parity（同毫秒多注）      | `t_bet.payout_complete_dtm` tie                    | Step 3/4/7：穩定排序 `payout_complete_dtm, bet_id`                      |
| **G4**    | 低     | 口徑衝突               | `gaming_day` 定義                                    | Step 0/6/8：Visit 去重以 `gaming_day` 欄位為準                             |
| **G5**    | 低     | 聚合順序               | `t_session.avg_bet` outlier                        | Step 4/10：row-level cap → 聚合                                       |
| **H1**    | 中     | 標籤邊界（終端下注）         | C1 / TRN-06（右截尾）                                   | Step 3/10：next_bet 缺失分流（可判定 vs censored）                           |
| **H2**    | 中     | 身份兜底鏈完整性           | D2 / t_session 可用性（available_time）                 | Step 7：僅在 session 可得時用 session card 兜底；否則不使用                       |
| **H3**    | 低     | 模型路由規則             | 雙模型（§9.1）                                          | Step 7/10：`is_rated_obs` 明文化 + routing 測試                          |
| **H4**    | 低     | 空值傳播（累積特徵）         | `t_bet.wager` Nullable（DDL） / raw parquet 證據       | Step 4/10：fillna 防呆 + wager_null_pct 監控                            |
| **I1**    | 中     | SQL 欄位名稱錯誤         | `t_session.num_games_with_wager`（DDL）              | Step 2：FND-12 SQL 改用 `SUM(COALESCE(num_games_with_wager, 0)) <= 1` |
| **I2**    | 中     | SQL 範例缺 FND-01 CTE | Step 2 identity.py 說明                              | Step 2：兩段 SQL 改為先建 deduped CTE（ROW_NUMBER 去重）再查詢                   |
| **I3**    | 低     | Checklist 文字過時     | TRN-* Remediation Checklist TRN-01                 | Checklist：TRN-01 v3 更新欄改為「禁用 FINAL（G1）」描述                          |
| **I4**    | 低     | 閾值搜尋策略缺失           | Step 6，雙模型（§9.1）                                    | Step 6：二維搜尋（I6 Optuna TPE）；v10 改 F1 最大化                           |
| **I6**    | 低     | 網格搜尋效率不足           | Step 6，雙模型閾值搜尋                                      | Step 0/6：改用 Optuna TPE；`OPTUNA_N_TRIALS`；v10 改 F1 objective             |


---

## 第四輪 Spec Compliance Review（v4）— 論證/理由（供後續 review 引用）

- **G1（t_session 禁用 FINAL）**：
  - **事實依據**：`schema/schema.txt` 顯示 `t_session` 為 `ReplicatedReplacingMergeTree`，且 DDL 未提供 version 欄位。
  - **風險機制**：在無 version 欄位下使用 `FINAL`，merge 階段對同 key 的多列保留哪一列是非決定性的；這可能讓「最新 `lud_dtm` 的正確列」在我們套用 FND-01 `ROW_NUMBER()...ORDER BY lud_dtm DESC` 前就被丟棄。
  - **為何違反 SSOT**：SSOT §5/FND-01 要求以 `MAX(lud_dtm)` 進行業務去重；`FINAL` 會把這個「選最新」的決策權交給引擎 merge 行為，破壞 P0 護欄。
- **G2（player_id 回補）**：
  - **事實依據**：Dictionary 與 DDL 指出 `t_bet.player_id` / `t_bet.session_id` 皆為 Nullable，且 `player_id` 觀察到 `-1` placeholder。
  - **風險機制**：若直接丟棄或兜底成散客，會把同一玩家的 bet 序列切斷，造成 run/rolling 斷裂、假 gap，並影響 D2 歸戶。
  - **修正原則**：只要 `session_id` 存在，就優先用去重後的 `t_session.player_id` 回補，再進行 D2。
- **G3（同毫秒多注穩定排序）**：
  - **事實依據**：同一局可有多筆下注（主注/旁注），可共享相同 `payout_complete_dtm`。
  - **風險機制**：若 trainer/scorer 在同 timestamp 的多筆 bet 上處理順序不同，B 類累積狀態與 C 類窗口統計會出現逐筆偏差，形成 Train-Serve Parity 破口。
  - **修正原則**：全系統固定 tie-break：`ORDER BY payout_complete_dtm ASC, bet_id ASC`。
- **G4（gaming_day 欄位為準）**：
  - **事實依據**：`t_bet`/`t_session` 都含 `gaming_day`，且 ClickHouse 以其做 partition。
  - **風險機制**：自算 gaming day（例如固定 06:00 偏移）可能與上游 ETL/財務口徑不一致，導致回測/validator/監控報表口徑漂移。
  - **修正原則**：Visit 去重以表內 `gaming_day` 欄位為 canonical；自算只作備援/敏感性分析。
- **G5（winsorization 聚合順序）**：
  - **事實依據**：Dictionary 指出 `t_session.avg_bet` 可能出現極端 outlier（至 5e12）。
  - **風險機制**：若先跨 session 聚合再截斷，單一 outlier 仍可在聚合前把統計量拉爆，影響特徵與模型穩定性。
  - **修正原則**：先對每列 `avg_bet` 做 row-level cap（`min(avg_bet, cap)`），再做跨 session 聚合。

---

## 第五輪 Spec Compliance Review（v5）— 論證/理由（供後續 review 引用）

- **H1（終端下注 / next_bet 缺失）**：
  - **風險機制**：若標籤建構直接依賴 `b_{i+1} - b_i`，則最後一筆下注 `b_{i+1}` 缺失會導致差值為 NaN，可能被默認判為非 gap；或被「一律視為 gap」而踩回右截尾（TRN-06）。
  - **修正原則**：只在「延伸拉取後仍覆蓋到 `b_i + X`」的條件下，才能把 next_bet 缺失視為 gap start；否則視為 censored 排除。
- **H2（身份兜底與 available_time）**：
  - **風險機制**：用 `t_session.casino_player_id` 兜底可提高 identity 覆蓋，但若 session 尚未達 available_time 就被使用，會引入未來資訊（leakage），違反 SSOT §2.1/§4.2。
  - **修正原則**：session-card 兜底僅在 `session_avail_time <= obs_time` 時可用；否則只能用 mapping cache 或 fallback 到有效的 `player_id`。
- **H3（雙模型路由明文化）**：
  - **風險機制**：未定義路由會導致工程端用錯欄位（尤其是 `is_known_player`），或讓 non-rated 誤用 E 類特徵造成 parity / 服務錯誤。
  - **修正原則**：以 `resolved_card_id`（session-card 或 mapping 命中）定義 `is_rated_obs`，並記錄於輸出供稽核。
- **H4（wager NULL 與 NA propagation）**：
  - **事實依據（raw data）**：對「本機匯出的 `t_bet` Parquet」（例如 `.data/` 內對應檔；約 4.38e8 rows）使用 Parquet row-group statistics 讀取，`wager null_count = 0`；抽樣前 5 個 row groups 亦 `wager NULL = 0`。
  - **結論**：本批資料不會因 `wager` NULL 造成 cumsum NA 汙染；但仍保留 `fillna(0)` 作防呆，以應對未來資料版本變更。不要把 `wager_is_null` 納入 feature list（避免常數欄），改用 `wager_null_pct` 監控觸發合約演進。

## 第八輪 SSOT 對齊（v8）— 論證/理由（供後續 review 引用）

- **雙軌特徵工程架構（Track A + Track B）**：
  - **事實依據**：Featuretools DFS 能系統性搜索聚合/窗口/組合特徵空間，但對「條件重置狀態機」（`loss_streak`）或「跨玩家滾動聚合」（`table_hc`）等邏輯天然無法或效能極差。Custom Primitive 雖可部分橋接，但複雜狀態機的正確性難以驗證，且推論端難以複用。
  - **風險機制**：若把 `loss_streak`、`run_boundary`、`table_hc` 強塞進 Featuretools Custom Primitives，會導致：(1) 狀態機邏輯分散在 Primitive 定義與外部程式碼間，難以測試；(2) Scorer 端難以高效重現；(3) 違反 SSOT §8.2.B 的明確設計意圖。
  - **修正原則**：採雙軌並行——軌道 A（Featuretools DFS）專注自動化特徵探索，捨棄 Custom Primitive 包裝複雜狀態機；軌道 B（向量化 Pandas/Polars 手寫）專責狀態機與跨玩家特徵。兩軌共用同一 `cutoff_time` 框架，`features.py` 為唯一共用進入點，確保 train-serve parity。
- `**player` 實體與兩階段 DFS**：
  - **事實依據**：缺少 `player` 實體時，Featuretools 無法自動展開跨 session 的歷史聚合，必須手動串接；兩階段 DFS（探索在抽樣上 → 生產在全量上）是解決單機記憶體限制的標準模式（`save_features` / `calculate_feature_matrix`）。
  - **修正原則**：EntitySet 新增 `player` 實體（以 `canonical_id` 為 index），建立 `t_session.canonical_id → player.canonical_id` 關係；採兩階段 DFS，第一階段 10–20% 下採樣探索，`save_features` 後第二階段全量套用，從根本封住 train-serve parity 風險。

---

## 第九輪 SSOT 對齊（v9）— 論證/理由（供後續 review 引用）

- **術語與樣本加權（DEC-013）**：
  - **決策**：統一使用 **Run**（bet-derived 連續下注段；gap ≥ RUN_BREAK_MIN 切分），不再使用 Visit。 Phase 1 不採用 visit-level 樣本加權（`1/N_visit`），僅使用 `class_weight='balanced'`。
- **雙口徑評估報告（SSOT §10.3）**：
  - **修正原則**：Phase 1 以 Bet-level（Micro）報告為主；Macro-by-run 延後（DEC-012）。
- **回測去重口徑澄清（SSOT §10.3）**：
  - **問題根源**：原計畫「套用每次造訪最多 1 個警報的去重邏輯」的用語模糊，可能被誤讀為線上推論也只對每個賭客每次造訪發一個 alert。
  - **修正原則**：明確標註此去重僅為**離線評估口徑**（避免指標膨脹），不代表線上推論需節流。線上通知頻率屬產品/前端決策。
- **Reason code 展示邊界（SSOT §12.1）**：
  - **問題根源**：原計畫 Step 7 寫「穩定性護欄（連續兩個輪詢週期一致才展示）」，把 UX 展示過濾放在模型層，違反 SSOT §12.1 的最新定位。
  - **修正原則**：模型服務每次推論必須完整輸出 `reason_codes`/`score`/`margin`/`model_version`/`scored_at`，不在模型層做展示過濾。「連續兩輪一致才展示」等策略屬 `trainer/frontend/` 的 UX 邏輯。

---

## 第六輪 Spec Compliance Review（v6）— 論證/理由（供後續 review 引用）

- **I1（FND-12 欄位名稱錯誤）**：
  - **事實依據**：`schema/schema.txt` 的 `t_session` DDL 中不存在 `num_games` 欄位；實際存在的是 `num_games_elapsed`（含未下注局）與 `num_games_with_wager`（有實際下注的局數，Nullable）。
  - **風險機制**：若 SQL 執行 `SUM(num_games)`，ClickHouse 會直接報錯，identity.py 初始化失敗，整個訓練流程中斷。
  - **修正原則**：FND-12 的「total_games」語意對應的是「有下注的局數」，故改用 `SUM(COALESCE(num_games_with_wager, 0))`；COALESCE 因 Nullable 必要。
- **I2（FND-01 去重 CTE 缺失）**：
  - **事實依據**：`t_session` 為 `ReplicatedReplacingMergeTree` 且不使用 `FINAL`（G1 決定）；在 merge 尚未完成前，同一 `session_id` 可能存在多列。
  - **風險機制**：若直接 `FROM t_session WHERE ...`，一個 `session_id` 的多版本列會各自產生一條 `(player_id, casino_player_id)` 連結邊。重複邊會讓 M:N 衝突計算膨脹（情境 2），也可能讓假帳號排除的 `COUNT(session_id)` 不準確。
  - **修正原則**：先用 FND-01 `ROW_NUMBER()` CTE 取每個 `session_id` 的最新列，再從 CTE 查詢，確保 mapping 建置與假帳號排除都在去重後的乾淨資料上進行。
- **I3（TRN-01 Checklist 文字矛盾）**：
  - **事實依據**：v4 G1 決定 `t_session` 禁用 `FINAL`；但 Checklist TRN-01 的 v3 更新欄仍寫「加 FINAL（E5）」。
  - **風險機制**：Checklist 是工程師對照實作的文件；矛盾文字會讓人誤以為 `t_session` 要加 `FINAL`，與 G1 決策衝突。
  - **修正原則**：統一更正為 G1 決策的描述，避免文件內部矛盾。
- **I4（雙模型二維閾值搜尋，初版）**：
  - **事實依據**：Phase 1 使用 Rated / Non-rated 雙模型，各自輸出獨立分數；D4 的 **alert volume 口徑**以兩模型合計為準（報告/監控用）。
  - **風險機制**：若兩個閾值分別獨立選定（例如各自掃描 99 個值），可能出現 rated 模型過濾太嚴或太鬆，導致合計警報量與 precision/recall 指標出現不必要的偏移；且最佳組合需要在 2D 空間聯合搜尋才穩定。
  - **修正原則（已由 I6 取代）**：首次確立需在二維空間聯合搜尋；具體方法見 I6。
- **I6（Optuna TPE 取代網格搜尋）**：
  - **事實依據**：二維網格（99×99 = 9,801 次）在每次 trial 都需計算整個 validation set 的 precision / recall 時，計算量隨模型複雜度線性成長；且網格搜尋對連續空間的覆蓋率不如隨機或貝葉斯採樣。
  - **風險機制**：網格搜尋步長固定（0.01），可能在最優解附近解析度不足；且若未來引入更多閾值參數（例如 per-table 閾值），網格複雜度會指數爆炸。
  - **修正原則**：採用 Optuna `TPESampler`，以歷史 trial 結果建立代理模型，智慧地引導後續採樣方向；**v10 以 F1 最大化為 objective，無 gate 約束**。`OPTUNA_N_TRIALS=300` 在 2D 空間通常已足夠收斂，且未來擴充參數時只需增加 `n_trials` 而非重新設計搜尋架構。（若未來恢復約束式 gate，才需要將不可行 trial 回傳 `-inf`。）

---

## 持續運營範疇（SSOT §12.2）

Phase 1 上線後，以下閉環必須升格為產品生命週期的一部分：

- **監控**：資料分佈與特徵漂移、警報量、precision/recall proxy（Bet-level）、reason code 分佈、線上 validator 結果、`wager_null_pct` 等 feature contract 指標。
- **校準**：依業務容量與季節性調整閾值 / gate（仍遵守 SSOT §10.2 的策略框架）。
- **重訓**：在 drift 或策略變更時更新模型與特徵集合，並以 `model_version` + `/model_info` 管理演進（含 `saved_feature_defs` 同步版本化）。

---

## 開放問題

### 業務端協商未決（SSOT §13）

1. **營運校準（未決；供 Phase 2 / 回退 gate 使用）**：具體的 Precision/Recall 權衡底線為何？每班公關可處理多少警報（目標警報量範圍）？
2. **Phase 1 閾值策略（v10 已決）**：採 **F1 最大化**、無 gate 約束（DEC-009/010）；`G1_PRECISION_MIN`、`G1_ALERT_VOLUME_MIN_PER_HOUR` 已廢棄。

### 實作待確認參數（需 EDA 或業務確認後才可落地 Step 6）

- ~~`G1_PRECISION_MIN`~~、~~`G1_ALERT_VOLUME_MIN_PER_HOUR`~~（v10 廢棄；見 DEC-009/010 回退備註）
- `TABLE_HC_WINDOW_MIN`（暫定 30 分鐘）
- `HIST_AVG_BET_CAP`（暫定 500,000；需 EDA 驗證分位數）
- `LOSS_STREAK_PUSH_RESETS`（暫定 False；需業務規則確認）

---

## 本計畫不涵蓋（Phase 2+ 事項）

- **player_profile_daily 建置**：Phase 1 後延伸；若建置需依 `doc/player_profile_daily_spec.md` 與 SSOT §4.3
- PIT-correct D2 mapping（D3 備忘）
- `t_game` 牌局氛圍特徵（FND-14）
- `table_hc` 啟用（軌道 B 可實作但 Phase 1 不呼叫）
- 序列嵌入 / SSL 預訓練（SSOT §8.4 Phase 2–3 策略）
- AutoML / FLAML 集成探索（SSOT §9.2 Phase 2+）
- 跨桌造訪建模

