## 資料發現與品質備忘錄（Findings Log）

本文件用來記錄專案中「高價值、容易踩雷、且可重現驗證」的資料發現（Data Gotchas & Findings），避免後續分析/建模反覆重踩同一批坑。

### 使用原則
- **單一事實來源（SSOT）**：所有重要發現優先記在這裡；`schema/GDP_GMWDS_Raw_Schema_Dictionary.md` 僅保留短版提示（例如 `[DQ Rule]`）。
- **表格速覽，附錄驗證**：為保持易讀性，所有發現摘要於總表中，驗證腳本統一放至附錄。
- **每條都可重現**：至少附一段 DuckDB SQL（或 Python）能跑出同樣結論/數字。

### 資料來源（本批次）
- `data/gmwds_t_session.parquet`（約 74,359,529 列；ClickHouse `v25.3.6.10034.altinitystable`）
- `data/gmwds_t_bet.parquet`（約 438,005,955 列；ClickHouse `v25.3.6.10034.altinitystable`）

---

## 總表：資料發現與處理決策

| ID | 表格與欄位 | 嚴重度 | 發現 (Finding) | 影響 (Impact) | 建議處理 (Recommendation/Decision) |
|---|---|:---:|---|---|---|
| **FND-01** | `t_session`<br>`session_id` | 🔴 P0 | **存在重複版本**：同一 ID 最多出現 3 筆，包含純 ETL 重複與事後帳務/狀態更正（`lud_dtm` 不同）。 | 若不去重，玩家/桌台的 turnover、win 會被重複計算。 | 任何建模或分析前，必須先做去重：首選 `MAX(lud_dtm)`，再取 `MAX(__etl_insert_Dtm)`。 |
| **FND-02** | `t_session`<br>`is_manual` | 🔴 P0 | **人工帳務調整（非打牌）**：`is_manual=1` 皆為 0 局數、0 Turnover，但包含極端 `player_win`（回佣/補償）。 | 若混入真實 session，會嚴重破壞行為特徵（如 avg bet）。 | **行為建模**必須排除；**價值建模**需保留但分離為 `manual_*` 特徵；並設立防呆排除極端金額（防 typo）。 |
| **FND-03** | `t_session`<br>`casino_player_id` | 🔴 P0 | **存在字串 `'null'`**：缺失值不僅為 NULL (27.4%)，還混雜了字串 `'null'` (0.77%)。 | 導致 ID Mapping 錯誤，並產生嚴重的假性「多對多」映射問題。 | 下游關聯或聚合前，一律將字串 `'null'` 或空字串清洗轉換為真實 `NULL`。 |
| **FND-04** | `t_session`<br>`status` | 🔴 P0 | **大量空字串包含真實注單**：高達 4200 萬筆狀態為空字串，且這其中有 96.6% 都能在 `t_bet` 找到對應注單，佔總體正常流水的 17%。同一 session_id 幾乎不會從空狀態轉為 SUCCESS (交集僅135筆)。 | 若盲目過濾 `status='SUCCESS'`，將會直接蒸發掉約 17% 的真實玩家下注資料。 | **不要過濾 `status='SUCCESS'`**！只要 `is_manual=0` 且能產生真實流水 (`turnover > 0` 或 `num_games_with_wager > 0`)，不論狀態為空字串或 SUCCESS 皆應保留作為特徵。 |
| **FND-05** | `t_session`<br>`num_games...` | 🟡 P1 | **出現負值局數**：`num_games_with_wager` 存在負值（全表僅少數幾筆，皆發生在 manual 紀錄）。 | 做活躍或投注量計算時會出現邏輯錯誤。 | 對 `is_manual=0` 強制約束 `num_games >= 0`；對 `is_manual=1` 則不應依賴此欄位。 |
| **FND-06** | `t_bet`<br>`bet_reconciled_at` | 🔴 P0 | **100% 無效值**：全表僅含 NULL (42%) 或 UNIX 預設值 `1970-01-01` (58%)，有效值為 0。 | 無法用於判斷「對帳是否完成」或計算對帳延遲。 | 下游一律將此欄位視為不可用，全部清洗成 NULL，需尋找其他來源判斷對帳狀態。 |
| **FND-07** | `t_bet`<br>`casino_win` | 🟡 P1 | **極端值符合賠率邏輯**：存在單注破億的虧損，但派彩與 wager 比例（`payout_ratio`）完全符合玩法賠率（上限 100 倍）。 | 視覺檢查易被誤判為資料錯誤或灌水。 | 屬真實下注行為，不需如同 `t_session` 般排除，但可加 `payout_ratio <= 100` 作為防禦性 DQ 監控。 |
| **FND-08** | `t_bet`<br>多個欄位 | 🟡 P1 | **本批次全為 NULL**：`bonus`, `tip_amount`, `increment_wager`, `payout_value` 等欄位全空。 | 誤判業務狀況（以為沒有 tip 或 bonus）。 | 先與來源單位確認是功能未啟用、匯出未帶還是廢棄，在此之前標記為本批不可用。 |
| **FND-09** | `t_session`<br>`is_known_player` | 🔴 P0 | **標籤與 ID 嚴重脫鉤**：`is_known_player=0` 卻有 14 萬筆具備 `casino_player_id`；`is_known_player=1` 也有 1 萬筆 ID 為空。 | 若依賴此標籤過濾會員，將會嚴重漏算活躍會員的流水，或混入無效 ID。 | **絕對不要**依賴此欄位判斷會員身分；一律改為檢查 `casino_player_id` 是否為有效字串。 |
| **FND-10** | `t_session`<br>`rating_status` & `verified_status` | 🟡 P1 | **狀態欄位為平行宇宙**：`rating_status` 100% 專屬於人工調整(`is_manual=1`)；`verified_status` 100% 專屬於實際遊玩(`is_manual=0`)。其餘 6600 萬筆皆為 NULL。 | 將兩者混用或視為連續流程會導致邏輯嚴重錯誤。 | 了解這兩個狀態分屬不同的業務流程（帳務審批 vs 桌台驗證），不要混為一談，多數正常遊玩紀錄其實兩者皆無。 |
| **FND-11** | `t_session`<br>`player_id` & `casino_player_id` | 🔴 P0 | **雙向 M:N 多對多映射**：雖然佔比極低（約 **0.03%**），但有 97 個 `player_id` 對應多張卡（玩家換卡）；同時有 97 張卡對應多個 `player_id`（生物辨識系統斷鏈重發 ID）。 | 單純 JOIN 或聚合會產生嚴重重複計算（Cartesian Explosion）與玩家輪廓破碎。 | **行為歸戶唯一真理**：有卡客唯一依賴 `casino_player_id`（並向上溯源彙整所有 `player_id` 紀錄）；無卡客才退而求其次使用 `player_id`。 |
| **FND-12** | `t_session`<br>`casino_player_id` | 🔴 P0 | **大量一次性/零局數的假帳號 (Dummy IDs)**：高達 **4.0%** 的活躍會員卡號（約 1.3 萬個 8 位純數字 ID）終其一生只有 1 個 session 且打牌局數 ≤ 1 局。這些 ID 廣泛分佈在 700 多張桌台上。 | 佔比高達 4%，直接把這些 ID 當作真實活躍會員會嚴重稀釋玩家指標（客單價、留存率）。 | 這些極可能是 CRM 生成的過客/伴遊 Dummy ID，建模時須透過 `session_cnt > 1 OR total_games > 1` 的特徵來排除這批「單次且無下注」的幽靈人口。注意：這批 Dummy ID 與正常會員卡號的長度/前綴完全重疊，**無法**單純用正規表達式過濾。 |
| **FND-13** | `t_session`, `t_bet`, `t_game`<br>時間欄位綜合評估 | 🔴 P0 | **系統時間受回填污染，且 Session 紀錄為「結束後」入湖**：`__etl_insert_Dtm` 與 `__ts_ms` 遇回填會嚴重失真（`t_game` 甚至觀察到長達 200 天的延遲）。正常情況下，`t_session` 是在牌局結束後才完整入湖；`session_end_dtm` 缺值極低 (0.06%) 且精準反映業務結束時間。 | 若依賴系統時間模擬即時串流 (Streaming) 會導致未來資料外洩 (Data Leakage)；若誤用 `session_start_dtm` 當作資料可見時間，會嚴重低估延遲。 | **串流模擬最佳實踐 (Event Time + Delay)**：完全棄用系統時間。<br>1. **`t_bet` / `t_game`**: `event_time = payout_complete_dtm`，可用延遲設為 **+1 分鐘**。<br>2. **`t_session`**: `event_time = COALESCE(session_end_dtm, lud_dtm)`，可用延遲設為 **+7 分鐘** (保守可設 15 分)。<br>*(註：增量抽取仍以 `MAX(lud_dtm)` 或 `MAX(__ts_ms)` 為 watermark)* |
| **FND-14** | `t_game`<br>`game_id` | 🔴 P0 | **存在重複版本**：全表約 2.11 億列中，有約 3.4 萬個 `game_id` 發生重複（總列數大於 unique IDs）。 | 若不去重，牌局的財務結算與狀態會被重複計算。 | 任何建模或分析前，必須先做去重：依賴 `MAX(__ts_ms)` 或 `MAX(__etl_insert_Dtm)` 取得最新狀態。 |
| **FND-15** | `t_game`<br>財務欄位 | 🟡 P1 | **財務欄位非零且包含極端值**：`total_turnover`, `casino_win` 等並非全為 0。`casino_win` 包含極端值（如單局虧損 1.1 億），與 `t_bet` 的極端派彩現象一致。 | 若誤以為 `t_game` 財務欄位全為 0 而忽略，會遺失局級別的財務特徵。 | 這是真實的業務數據彙總，可作為特徵使用，但需注意極端值對模型的影響。 |
| **FND-16** | `t_session`<br>`session_id`, `casino_player_id`, `player_id` | 🟡 P1 | **同一 `session_id` 的多版本中，`casino_player_id` 可能「晚到補齊」(NULL → 非 NULL)，少數情況連 `player_id` 也會被更正**。同時 `t_bet` 本身不含 `casino_player_id` 欄位。 | 線上推論若只看「當下可得的 t_session」可能把實際有卡客暫時當作無卡；若未做 FND-01 去重與 available_time gate，會導致 D2 身份判定與 rated/non-rated 路由不穩定（且可能引入未來資訊）。 | 線上：有卡判定必須加 available_time gate（見 FND-13），並允許身份隨 `t_session` 更新而「升級」。離線/訓練：D2 mapping 必須先做 FND-01 去重再建表；同時保留 mapping cache（`player_id`→`casino_player_id`）作為兜底。 |
| **FND-17** | `t_bet`（及 `t_session`）<br>`theo_win`, `adjusted_theo_win` | 🟡 P1 | **`theo_win` 與 `adjusted_theo_win` 非同義**：前者為系統依玩法／賠率等計算的「精算向」預期利潤；後者為評級／報表向的「調整後」理論贏。於 repo 內 `trainer/sample data/SmartTableData_tbet_sample.csv`（1000 筆）可重現：`bet_type='PLAYER'` 時 **`adjusted_theo_win = wager * 0.0124` 恆成立**（浮點誤差內），`theo_win` 則略低（約 **wager × 0.01235**）；在常見百家樂閒注定義下 **~1.235% 較貼近文獻精算期望**，**1.24% 多為表定／評級取捨**（實桌規則不同時須重算）。`BANKER` / `SMALL_TIGER` / `BIG_TIGER` / `TIE` 樣本中兩欄相等；部分邊注（如 `LUCKY_SIX`、`*_PAIR`）僅極小差異或四捨五入感。**上游 GDP／Smart Table 未在本 repo 提供完整公式**，其餘注種需以全量 Parquet 再驗或向來源單位確認。 | 與場方 ADT／評級報表對帳時若誤用欄位會系統性偏差；本專案 `trainer/etl/etl_player_profile.py` 彙總 profile 使用 **`SUM(theo_win)`**，與「僅認 `adjusted_theo_win`」的口徑可能不一致。 | 對外報表／評級對齊：優先釐清業務採 **`adjusted_theo_win`** 或 **`theo_win`**；建模特徵若需與 profile 一致，沿用 `theo_win` 鏈路前先確認產品定義。全量驗證見附錄 **[FND-17]**。 |
| **FND-18** | `t_session` vs `t_bet`<br>`theo_win`, `turnover/wager`, `num_bets` | 🔴 P0 | **跨表對帳存在結構性落差（非 rounding）**：針對 `canonical_patron_profile.csv` 與 cleaned `t_bet` 的重疊 patrons，`|theo_diff| >= 100k` 的高差異案例可回溯到三類根因：1) `t_session` 去重後版本被 `is_deleted/is_canceled` gate 排除，但 `t_bet` 仍保留該 `session_id` 注單；2) `t_bet.session_id` 在 raw `t_session` 查無對應（原始來源覆蓋不一致）；3) 即使 `session_id` 對齊，`t_session` 的彙總欄位與 `t_bet` 聚合非嚴格等值（口徑差）。 | 若直接把 profile（session 口徑）與 bet 聚合視為「應完全一致」，會誤判資料品質、產生錯誤校驗告警，並在監控與模型回歸檢查中引入大量假陽性。 | 對帳必須先拆成「matched / session_only / bet_only」三層，再標記 root cause。高差異個案詳單與逐案根因見 `doc/fnd18_profile_vs_bet_root_cause_cases.md`；下游監控建議以容忍帶 + root-cause bucket 方式落地，而非單一 hard-equality。 |
| **FND-19** | `t_casino_txn`<br>`casino_txn_id`, `start_dtm`, `status`, `type`, `player_id` | 🔴 P0 | **交易表是 CDC/事件表，不能直接 row-level 聚合**：2026-05 分區樣本有 **4,607,563 rows**、**4,442,245 distinct `casino_txn_id`**；`__ts_ms` **100% NULL**，無法作 CDC 排序；另有 **30 個 delete marker**。若只以 `updated_dtm` 取最新，delete marker 會被舊版非刪除 row 蓋掉。`bet_id` 100% NULL、`session_id` 約 96% NULL，不能用 bet/session direct join。`complete_dtm` 約 99.89% NULL；`gaming_day` 也不能當事件時間，觀察到 `gaming_day=2026-05-02` 但 `start_dtm=2026-04-23` 的回填/分區錯位。 | 若未先做 delete-aware dedup 與 type-specific filter，BUYIN/CASHOUT/CHANGE 特徵會重複計算、混入刪除/取消交易，或用錯時間造成 PIT leakage。`TD_FILL`、`TD_CREDIT`、`TRANSFER` 在此表幾乎沒有 `player_id`，不適合第一版 player-level txn feature。 | **清洗契約 v0**：先找出任一版本有 `__op='d'` 或 `__deleted='True'` 的 `casino_txn_id` 並整筆排除；剩餘版本以 `__etl_insert_Dtm DESC, updated_dtm DESC` 去重。PIT event time 用 `start_dtm`，`gaming_day` 僅作分區/稽核。有效事件需 `action='SUBMIT'`、`status <> 'CANCELED'`、`player_id IS NOT NULL`、`txn_value IS NOT NULL`；cashflow count/sum 建議再要求 `txn_value > 0`。`CASHOUT` 收 `COMPLETED`；`BUYIN` 收 `COMPLETED` 或 `SUBMITTED` 且 `buyin_status IN ('SUCCESS','PROVISIONAL_SUCCESS')`；`CHANGE` 僅作 activity proxy，不把 `txn_value` 視為 win/loss 方向。 |

---

## Session History Distribution — Patron 歷史分佈與 Player-Level Table 決策依據

**日期**：2026-03-02  
**資料來源**：`data/gmwds_t_session.parquet`（DuckDB 全掃描，約 6.5GB）  
**目的**：評估 rated patrons 的 session 歷史深度，以決定是否值得建立 cached player-level 彙總表。

### 資料清洗與 DQ 設定
- **Canonical ID**：`casino_player_id` 若為有效（non-null、非空、非 `'null'` 字串）則用 `casino_player_id`，否則用 `player_id`。
- **Rated 判定**：`casino_player_id` 有效即為 rated。
- **排除**：`is_manual=1`、`is_deleted=1`、`is_canceled=1`。
- **Session 時間**：`COALESCE(session_end_dtm, lud_dtm, session_start_dtm)` 作為排序與 span 計算基準。

### 全量統計

| 指標 | 數值 |
|------|------|
| 總 Patrons | 9,126,540 |
| 其中 Rated | 332,813 |
| 其中 Non-rated | 8,793,727 |
| 總 Sessions | 71,214,034 |

### Sessions per patron（全體）

| 分位 | 值 |
|------|-----|
| p50 | 1 session |
| p75 | 2 |
| p90 | 6 |
| p95 | 11 |
| p99 | 96 |
| max | 156,119 |

### History span（天）per patron（全體）

| 分位 | 值 |
|------|-----|
| p50 | 0 天 |
| p75 | 0 |
| p90 | 0.1 |
| p95 | 0.6 |
| p99 | 170.8 |
| max | 586.2 |

### Rated patrons 專屬（Player-Level Table 目標族群）

| 指標 | 數值 |
|------|------|
| 人數 | 332,813 |
| Sessions/patron 中位數 | 25 |
| Sessions/patron 平均 | 151.7 |
| History span 中位數 | 6.8 天 |
| History span 平均 | 105.8 天 |

| 門檻 | 佔比 |
|------|------|
| ≥5 sessions | 79.3% |
| ≥10 sessions | 67.7% |
| ≥20 sessions | 54.9% |
| ≥30 天 history | 44.3% |
| ≥90 天 history | 35.7% |
| ≥180 天 history | 26.3% |

### Unrated patrons 專屬（Sanity check）

| 指標 | 數值 |
|------|------|
| 人數 | 8,793,727 |
| Sessions/patron 中位數 | 1 |
| Sessions/patron 平均 | 2.4 |
| History span 中位數 | 0 天 |
| History span 平均 | 0.4 天 |

| 門檻 | 佔比 |
|------|------|
| ≥2 sessions | 32.1% |
| ≥5 sessions | 10.1% |
| ≥10 sessions | 3.5% |
| ≥1 天 history | 2.1% |
| ≥7 天 history | 0.9% |
| ≥30 天 history | 0.3% |
| **僅 1 session** | **67.9%** |
| **span = 0（精確零）** | **68.3%** |
| **0 < span < 1 天** | **29.6%** |

**History span 定義與區間說明**  
`history_span_days = (MAX(sess_time) - MIN(sess_time)) / 86400`（秒差轉天數，可為小數）。以下三類為**互斥且窮舉**：

| 區間 | 佔比 | 說明 |
|------|------|------|
| **span = 0** | 68.3% | 僅 1 個 session（first = last），或極罕見的多 session 同時間戳。與「僅 1 session」67.9% 高度重合。 |
| **0 < span < 1 天** | 29.6% | 多個 session 且時間不同，但首末 session 時間差小於 24 小時（同日多場或跨午夜數小時）。 |
| **span ≥ 1 天** | 2.1% | 首末 session 間隔至少 1 天。 |

> **重要澄清**：「0-day span」意即 **span 精確為 0**，並非「同日」或「小於 1 天」。若將 span = 0 與 0 < span < 1 合併，則 **97.9%** 的 Unrated 歷史跨度小於 1 天。

**Sanity check 結論**：Unrated 歷史極短（中位數 1 session、0 天 span），約 97.9% 歷史跨度 < 1 天，符合無卡 walk-in 預期。Player-level table 僅需針對 rated 設計，unrated 彙總效益低。

### 決策結論
Rated patrons 的 session 歷史深度明顯高於非 rated：多數具備多次 sessions 與多日歷史；unrated 則以單 session／同日為主。建立 **cached player-level 彙總表**可避免每次訓練/chunk 對同一批 patron 從 7,100 萬筆 sessions 反覆彙總，具顯著效益；**僅針對 rated** 設計即可。**決策**：進行 player-level table 設計與實作（見 DEC-011；完整欄位規格見 `doc/player_profile_spec.md`）。

---

## 附錄：可重現驗證 SQL (Evidence)

以下為本批次發現所使用的 DuckDB 驗證腳本，未來取得新資料可直接執行比對。

### [FND-01] `session_id` 重複版本
```sql
-- 1. 查看重複分佈
WITH counts AS (
  SELECT session_id, COUNT(*) AS cnt
  FROM read_parquet('data/gmwds_t_session.parquet')
  GROUP BY 1
)
SELECT
  COUNT(*) AS total_unique_session_ids,
  SUM(CASE WHEN cnt = 1 THEN 1 ELSE 0 END) AS unique_once,
  SUM(CASE WHEN cnt > 1 THEN 1 ELSE 0 END) AS duplicated_session_ids,
  SUM(CASE WHEN cnt > 1 THEN cnt ELSE 0 END) AS rows_affected_by_duplication,
  MAX(cnt) AS max_versions_for_one_session
FROM counts;

-- 2. 建議的標準去重檢視表
SELECT *
FROM (
  SELECT *,
         ROW_NUMBER() OVER (
           PARTITION BY session_id
           ORDER BY lud_dtm DESC NULLS LAST, __etl_insert_Dtm DESC
         ) AS rn
  FROM read_parquet('data/gmwds_t_session.parquet')
)
WHERE rn = 1;
```

### [FND-02] `is_manual` 人工帳務調整
```sql
-- 比較 manual vs auto 的財務特徵
SELECT
  is_manual,
  COUNT(*) AS rows_cnt,
  MIN(turnover) AS min_turnover,
  MAX(turnover) AS max_turnover,
  MIN(num_games_elapsed) AS min_games_elapsed,
  MAX(num_games_elapsed) AS max_games_elapsed,
  MIN(player_win) AS min_player_win,
  MAX(player_win) AS max_player_win
FROM read_parquet('data/gmwds_t_session.parquet')
GROUP BY 1
ORDER BY 1;
```

### [FND-03] `casino_player_id` 缺失字串
```sql
SELECT
  COUNT(*) AS total,
  SUM(CASE WHEN casino_player_id IS NULL THEN 1 ELSE 0 END) AS is_null,
  SUM(CASE WHEN trim(casino_player_id) = '' THEN 1 ELSE 0 END) AS is_empty,
  SUM(CASE WHEN lower(trim(casino_player_id)) = 'null' THEN 1 ELSE 0 END) AS is_string_null
FROM read_parquet('data/gmwds_t_session.parquet');
```

### [FND-04] `status` 大量空字串包含真實注單
```sql
-- 1. 驗證空字串與 SUCCESS 在 turnover 上的貢獻差異
SELECT 
    CASE WHEN status = '' THEN '[Empty String]' ELSE status END as session_status,
    COUNT(*) as total_rows,
    SUM(CASE WHEN turnover > 0 THEN 1 ELSE 0 END) as rows_with_turnover,
    SUM(CASE WHEN num_games_with_wager > 0 THEN 1 ELSE 0 END) as rows_with_games,
    SUM(turnover) as sum_turnover,
    SUM(num_games_with_wager) as sum_games
FROM read_parquet('data/gmwds_t_session.parquet')
WHERE is_manual = 0
GROUP BY status
ORDER BY total_rows DESC;

-- 2. 抽樣驗證空字串 session 是否真有對應的 t_bet 注單
WITH empty_sessions AS (
    SELECT session_id, turnover, num_games_with_wager
    FROM read_parquet('data/gmwds_t_session.parquet')
    WHERE is_manual = 0 AND status = ''
    LIMIT 1000000
)
SELECT 
    COUNT(s.session_id) as total_empty_sessions,
    SUM(CASE WHEN b.session_id IS NOT NULL THEN 1 ELSE 0 END) as sessions_with_bets_in_t_bet,
    SUM(s.num_games_with_wager) as sum_games_in_session_table,
    SUM(b.bet_cnt) as sum_bets_found_in_bet_table
FROM empty_sessions s
LEFT JOIN (
    SELECT session_id, COUNT(*) as bet_cnt 
    FROM read_parquet('data/gmwds_t_bet.parquet') 
    GROUP BY session_id
) b ON s.session_id = b.session_id;

-- 3. 驗證同一個 session_id 是否會從空字串轉為 SUCCESS (交集極少)
WITH empty_status_sessions AS (
    SELECT DISTINCT session_id
    FROM read_parquet('data/gmwds_t_session.parquet')
    WHERE is_manual = 0 AND status = ''
),
success_status_sessions AS (
    SELECT DISTINCT session_id
    FROM read_parquet('data/gmwds_t_session.parquet')
    WHERE is_manual = 0 AND status = 'SUCCESS'
)
SELECT COUNT(*) as total_overlap
FROM empty_status_sessions e
INNER JOIN success_status_sessions s ON e.session_id = s.session_id;
```

### [FND-05] `num_games_with_wager` 負值
```sql
SELECT
  is_manual,
  SUM(CASE WHEN num_games_with_wager < 0 THEN 1 ELSE 0 END) AS negative_cnt
FROM read_parquet('data/gmwds_t_session.parquet')
GROUP BY 1
ORDER BY 1;
```

### [FND-06] `bet_reconciled_at` 100% 無效值
```sql
SELECT
  COUNT(*) AS total_rows,
  SUM(CASE WHEN bet_reconciled_at IS NULL THEN 1 ELSE 0 END) AS null_cnt,
  SUM(CASE WHEN CAST(bet_reconciled_at AS VARCHAR) LIKE '1970-01-01%' THEN 1 ELSE 0 END) AS default_1970_cnt,
  SUM(CASE WHEN bet_reconciled_at IS NOT NULL AND CAST(bet_reconciled_at AS VARCHAR) NOT LIKE '1970-01-01%' THEN 1 ELSE 0 END) AS valid_reconciled_cnt
FROM read_parquet('data/gmwds_t_bet.parquet');
```

### [FND-07] `casino_win` 極端值與賠率邏輯
```sql
WITH base AS (
  SELECT wager, casino_win,
         CASE WHEN wager > 0 THEN ABS(casino_win) / wager ELSE NULL END AS payout_ratio
  FROM read_parquet('data/gmwds_t_bet.parquet')
  WHERE casino_win < 0
)
SELECT
  COUNT(*) AS total_player_wins,
  SUM(CASE WHEN payout_ratio > 50 THEN 1 ELSE 0 END) AS ratio_gt_50,
  SUM(CASE WHEN payout_ratio > 100 THEN 1 ELSE 0 END) AS ratio_gt_100,
  MAX(payout_ratio) AS max_payout_ratio,
  MIN(casino_win) AS max_casino_loss
FROM base;
```

### [FND-09] `is_known_player` 標籤與實際 ID 嚴重脫鉤
```sql
SELECT
  is_known_player,
  SUM(CASE WHEN casino_player_id IS NULL OR lower(trim(casino_player_id)) IN ('', 'null') THEN 1 ELSE 0 END) AS missing_id_cnt,
  COUNT(*) AS total
FROM read_parquet('data/gmwds_t_session.parquet')
GROUP BY 1
ORDER BY 1;
```

### [FND-10] `rating_status` 與 `verified_status` 狀態互斥平行宇宙
```sql
SELECT
  is_manual,
  CASE WHEN verified_status IS NOT NULL THEN 1 ELSE 0 END AS has_verified,
  CASE WHEN rating_status IS NOT NULL THEN 1 ELSE 0 END AS has_rating,
  COUNT(*) AS cnt
FROM read_parquet('data/gmwds_t_session.parquet')
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3;
```

### [FND-11] `player_id` 與 `casino_player_id` 雙向 M:N 多對多映射
```sql
-- 1. 清洗 ID (把 'null' 字串洗成真實 NULL)
CREATE OR REPLACE VIEW clean_session AS
SELECT 
    player_id, 
    CASE 
        WHEN casino_player_id IS NULL THEN NULL 
        WHEN lower(trim(casino_player_id)) IN ('', 'null') THEN NULL 
        ELSE trim(casino_player_id) 
    END AS clean_casino_player_id,
    session_start_dtm,
    is_manual
FROM read_parquet('data/gmwds_t_session.parquet');

-- 2. 驗證 1 player_id -> N casino_player_id (玩家換卡)
WITH mapped AS (
    SELECT player_id, COUNT(DISTINCT clean_casino_player_id) as num_casino_ids
    FROM clean_session
    WHERE player_id IS NOT NULL AND clean_casino_player_id IS NOT NULL
    GROUP BY 1
)
SELECT num_casino_ids, COUNT(*) as num_player_ids
FROM mapped
GROUP BY 1 ORDER BY 1;

-- 3. 驗證 1 casino_player_id -> N player_id (系統斷鏈)
WITH mapped_reverse AS (
    SELECT clean_casino_player_id, COUNT(DISTINCT player_id) as num_player_ids
    FROM clean_session
    WHERE player_id IS NOT NULL AND clean_casino_player_id IS NOT NULL
    GROUP BY 1
)
SELECT num_player_ids, COUNT(*) as num_casino_ids
FROM mapped_reverse
GROUP BY 1 ORDER BY 1;
```

### [FND-12] `casino_player_id` 存在大量一次性/零局數的假帳號
```sql
-- 驗證可疑 8 位數純數字短暫 ID 的存在
WITH clean_session AS (
    SELECT 
        session_id, player_id, 
        CASE WHEN lower(trim(casino_player_id)) IN ('', 'null') THEN NULL ELSE trim(casino_player_id) END AS casino_player_id,
        is_known_player, is_manual, num_games_with_wager
    FROM read_parquet('data/gmwds_t_session.parquet')
),
player_stats AS (
    SELECT 
        casino_player_id,
        COUNT(DISTINCT session_id) as session_cnt,
        SUM(num_games_with_wager) as total_games,
        MAX(is_known_player) as is_known_player_flag
    FROM clean_session
    WHERE casino_player_id IS NOT NULL AND is_manual = 0
    GROUP BY 1
)
SELECT 
    CASE WHEN total_games <= 1 THEN '0_or_1_game' ELSE 'multi_games' END as game_type,
    is_known_player_flag,
    COUNT(*) as id_cnt
FROM player_stats
WHERE session_cnt = 1 AND length(casino_player_id) = 8
GROUP BY 1, 2 ORDER BY 1, 2;

-- 驗證這些 Dummy ID 廣泛分佈在多張桌台 (非特定機台測試)
WITH dummy_ids AS (
    SELECT casino_player_id
    FROM clean_session
    WHERE casino_player_id IS NOT NULL AND is_manual = 0
    GROUP BY 1
    HAVING COUNT(DISTINCT session_id) = 1 AND SUM(num_games_with_wager) <= 1 AND length(casino_player_id) = 8
)
SELECT 
    COUNT(DISTINCT d.casino_player_id) as num_dummy_ids,
    COUNT(DISTINCT s.table_id) as tables_using_dummies,
    COUNT(*) as total_sessions
FROM clean_session s
JOIN dummy_ids d ON s.casino_player_id = d.casino_player_id;
```

### [FND-13] 系統時間污染與即時串流模擬策略 (Event Time & Latency)
```sql
-- 1) t_session 欄位完整性：`lud_dtm` 幾乎全表有值，`session_end_dtm` 缺值率極低 (0.06%)
SELECT
  is_manual,
  COUNT(*) AS rows,
  SUM(CASE WHEN session_end_dtm IS NULL THEN 1 ELSE 0 END) AS null_end_cnt,
  ROUND(100.0 * SUM(CASE WHEN session_end_dtm IS NULL THEN 1 ELSE 0 END) / COUNT(*), 4) AS null_end_pct,
  SUM(CASE WHEN lud_dtm IS NULL THEN 1 ELSE 0 END) AS null_lud_cnt
FROM read_parquet('data/gmwds_t_session.parquet')
GROUP BY 1
ORDER BY 1;

-- 2) t_session duration 合理性：end - start（is_manual=0）
WITH d AS (
  SELECT date_diff('second', session_start_dtm, session_end_dtm) AS dur_sec
  FROM read_parquet('data/gmwds_t_session.parquet')
  WHERE is_manual = 0
    AND session_start_dtm IS NOT NULL
    AND session_end_dtm IS NOT NULL
)
SELECT
  COUNT(*) AS n,
  SUM(CASE WHEN dur_sec < 0 THEN 1 ELSE 0 END) AS neg_cnt,
  approx_quantile(dur_sec, 0.5)  AS p50_sec,
  approx_quantile(dur_sec, 0.9)  AS p90_sec,
  approx_quantile(dur_sec, 0.99) AS p99_sec
FROM d;

-- 3) t_session 與 lud_dtm 的貼近程度：lud - end（is_manual=0）
WITH d AS (
  SELECT date_diff('second', session_end_dtm, lud_dtm) AS end_to_lud_sec
  FROM read_parquet('data/gmwds_t_session.parquet')
  WHERE is_manual = 0
    AND session_end_dtm IS NOT NULL
    AND lud_dtm IS NOT NULL
)
SELECT
  COUNT(*) AS n,
  approx_quantile(end_to_lud_sec, 0.5)  AS p50_sec,
  approx_quantile(end_to_lud_sec, 0.9)  AS p90_sec,
  approx_quantile(end_to_lud_sec, 0.99) AS p99_sec,
  SUM(CASE WHEN end_to_lud_sec < 0 THEN 1 ELSE 0 END) AS neg_cnt
FROM d;

-- 4) t_session end -> 入湖延遲（排除明顯回填；用於估算 available_time 的 delay）
WITH d AS (
  SELECT date_diff('second', session_end_dtm, __etl_insert_Dtm) AS end_to_etl_sec
  FROM read_parquet('data/gmwds_t_session.parquet')
  WHERE is_manual = 0
    AND session_end_dtm IS NOT NULL
    AND __etl_insert_Dtm IS NOT NULL
    AND date_diff('day', session_end_dtm, __etl_insert_Dtm) BETWEEN 0 AND 1
    AND date_diff('second', session_end_dtm, __etl_insert_Dtm) >= 0
)
SELECT
  COUNT(*) AS n,
  approx_quantile(end_to_etl_sec, 0.5)  AS p50_sec,
  approx_quantile(end_to_etl_sec, 0.9)  AS p90_sec,
  approx_quantile(end_to_etl_sec, 0.99) AS p99_sec
FROM d;

-- 5) 評估 t_bet 正常資料之真實延遲 (過濾回填資料，只取 1 天內)
SELECT 
    approx_quantile(date_diff('second', payout_complete_dtm, __etl_insert_Dtm), 0.5) as med_etl_delay_sec
FROM read_parquet('data/gmwds_t_bet.parquet')
WHERE date_diff('day', payout_complete_dtm, __etl_insert_Dtm) BETWEEN 0 AND 1;

-- 6) 評估 t_game 系統時間污染與延遲
SELECT 
  APPROX_QUANTILE(date_diff('second', payout_complete_dtm, __etl_insert_Dtm), 0.5) as p50_delay_sec,
  APPROX_QUANTILE(date_diff('second', payout_complete_dtm, __etl_insert_Dtm), 0.95) as p95_delay_sec,
  APPROX_QUANTILE(date_diff('second', payout_complete_dtm, __etl_insert_Dtm), 0.99) as p99_delay_sec
FROM read_parquet('data/gmwds_t_game.parquet')
WHERE payout_complete_dtm IS NOT NULL AND __etl_insert_Dtm IS NOT NULL;
```

### [FND-14] `t_game` 存在重複版本
```sql
SELECT 
  COUNT(game_id) as total_rows, 
  COUNT(DISTINCT game_id) as unique_game_ids,
  COUNT(game_id) - COUNT(DISTINCT game_id) as duplicated_ids
FROM read_parquet('data/gmwds_t_game.parquet');
```

### [FND-15] `t_game` 財務欄位非零且包含極端值
```sql
SELECT 
  MIN(total_turnover) as min_turnover, MAX(total_turnover) as max_turnover,
  MIN(casino_win) as min_casino_win, MAX(casino_win) as max_casino_win,
  MIN(theo_win) as min_theo_win, MAX(theo_win) as max_theo_win
FROM read_parquet('data/gmwds_t_game.parquet');
```

### [FND-16] 同一 `session_id` 的多版本可能「晚到補齊」`casino_player_id`（以及少數 `player_id` 更正）

> 目的：驗證「玩家中途插卡/事後補登」這類情境，在資料上是否會反映為 `t_session` 的多版本更新（同 session_id 先無卡、後有卡），並確認 `t_bet` 不會被回寫出 `casino_player_id`。

```sql
-- 0) 確認 t_bet 沒有 casino_player_id 欄位（DuckDB 可用 DESCRIBE 檢查）
DESCRIBE SELECT * FROM read_parquet('data/gmwds_t_bet.parquet') LIMIT 1;

-- 1) 建立乾淨視圖（清洗 casino_player_id；保留版本時間欄位）
CREATE OR REPLACE VIEW session_versions AS
SELECT
  session_id,
  player_id,
  CASE
    WHEN casino_player_id IS NULL THEN NULL
    WHEN lower(trim(casino_player_id)) IN ('', 'null') THEN NULL
    ELSE trim(casino_player_id)
  END AS clean_casino_player_id,
  lud_dtm,
  __etl_insert_Dtm,
  session_start_dtm,
  session_end_dtm,
  num_bets,
  is_manual,
  is_deleted,
  is_canceled
FROM read_parquet('data/gmwds_t_session.parquet')
WHERE is_manual = 0
  AND COALESCE(is_deleted, 0) = 0
  AND COALESCE(is_canceled, 0) = 0;

-- 2) 找出「同 session_id 多版本，且同時出現 (NULL card) 與 (non-NULL card)」的 session
WITH agg AS (
  SELECT
    session_id,
    COUNT(*) AS version_cnt,
    MAX(CASE WHEN clean_casino_player_id IS NULL THEN 1 ELSE 0 END) AS has_null_card,
    MAX(CASE WHEN clean_casino_player_id IS NOT NULL THEN 1 ELSE 0 END) AS has_card,
    COUNT(DISTINCT player_id) AS player_id_nunique,
    COUNT(DISTINCT clean_casino_player_id) AS card_id_nunique
  FROM session_versions
  GROUP BY 1
)
SELECT
  COUNT(*) AS sessions_with_late_card_update
FROM agg
WHERE version_cnt > 1 AND has_null_card = 1 AND has_card = 1;

-- 3) 抽樣查看幾個例子（按版本數、以及版本時間排序）
WITH agg AS (
  SELECT
    session_id,
    COUNT(*) AS version_cnt,
    MAX(CASE WHEN clean_casino_player_id IS NULL THEN 1 ELSE 0 END) AS has_null_card,
    MAX(CASE WHEN clean_casino_player_id IS NOT NULL THEN 1 ELSE 0 END) AS has_card
  FROM session_versions
  GROUP BY 1
),
cand AS (
  SELECT session_id
  FROM agg
  WHERE version_cnt > 1 AND has_null_card = 1 AND has_card = 1
  ORDER BY version_cnt DESC, session_id
  LIMIT 20
)
SELECT
  v.session_id,
  v.player_id,
  v.clean_casino_player_id,
  v.session_start_dtm,
  v.session_end_dtm,
  v.lud_dtm,
  v.__etl_insert_Dtm,
  v.num_bets
FROM session_versions v
JOIN cand c USING (session_id)
ORDER BY v.session_id, v.lud_dtm NULLS LAST, v.__etl_insert_Dtm NULLS LAST;

-- 4) 對同一批 cand session_id，檢查其在 t_bet 的 bet rows（只有 player_id，沒有 casino_player_id）
WITH agg AS (
  SELECT
    session_id,
    COUNT(*) AS version_cnt,
    MAX(CASE WHEN clean_casino_player_id IS NULL THEN 1 ELSE 0 END) AS has_null_card,
    MAX(CASE WHEN clean_casino_player_id IS NOT NULL THEN 1 ELSE 0 END) AS has_card
  FROM session_versions
  GROUP BY 1
),
cand AS (
  SELECT session_id
  FROM agg
  WHERE version_cnt > 1 AND has_null_card = 1 AND has_card = 1
  ORDER BY version_cnt DESC, session_id
  LIMIT 20
)
SELECT
  b.session_id,
  COUNT(*) AS bet_rows,
  COUNT(DISTINCT b.player_id) AS bet_player_id_nunique,
  MIN(b.payout_complete_dtm) AS first_bet_time,
  MAX(b.payout_complete_dtm) AS last_bet_time
FROM read_parquet('data/gmwds_t_bet.parquet') b
JOIN cand c ON b.session_id = c.session_id
GROUP BY 1
ORDER BY bet_rows DESC;
```

### [Session History Distribution] Patron 歷史分佈全掃描（DuckDB）
```sql
-- 完整腳本見 trainer/scripts/analyze_session_history_duckdb.py
-- 以下為核心聚合邏輯（可直接於 DuckDB 執行驗證）
WITH base AS (
    SELECT
        session_id,
        player_id,
        COALESCE(
            CASE WHEN casino_player_id IS NOT NULL
                 AND TRIM(CAST(casino_player_id AS VARCHAR)) NOT IN ('', 'null', 'NULL', 'nan', 'None')
            THEN TRIM(CAST(casino_player_id AS VARCHAR))
            ELSE NULL END,
            CAST(player_id AS VARCHAR)
        ) AS canonical_id,
        CASE WHEN casino_player_id IS NOT NULL
             AND TRIM(CAST(casino_player_id AS VARCHAR)) NOT IN ('', 'null', 'NULL')
        THEN 1 ELSE 0 END AS is_rated,
        COALESCE(session_end_dtm, lud_dtm, session_start_dtm) AS sess_time
    FROM read_parquet('data/gmwds_t_session.parquet')
    WHERE COALESCE(is_manual, 0) = 0
      AND COALESCE(is_deleted, 0) = 0
      AND COALESCE(is_canceled, 0) = 0
),
per_patron AS (
    SELECT
        canonical_id,
        MAX(is_rated) AS is_rated,
        COUNT(*) AS session_count,
        MIN(sess_time) AS first_session,
        MAX(sess_time) AS last_session,
        EXTRACT(EPOCH FROM (MAX(sess_time) - MIN(sess_time))) / 86400.0 AS history_span_days
    FROM base
    GROUP BY canonical_id
)
SELECT * FROM per_patron;
```

### [FND-17] `t_bet.theo_win` vs `t_bet.adjusted_theo_win`（及 `t_session` 同欄名）

> **資料來源（可離線重現）**：`trainer/sample data/SmartTableData_tbet_sample.csv`（1000 列，無需 Parquet）。  
> **全量驗證（可選）**：若本機存在 `data/gmwds_t_bet.parquet`，可用附錄 DuckDB 片段依 `bet_type` 抽樣比對。

**結論摘要**

1. **`theo_win`**：系統計算的理論贏（預期利潤），較貼近玩法／引擎精算。
2. **`adjusted_theo_win`**：評級或報表向的「調整後」理論贏；與 `t_session` 的 `adjusted_turnover` 一樣屬 **adjusted** 家族，但 **GDP 官方未在本 repo 寫明逐欄公式**。
3. **樣本實證（CSV）**：`bet_type = 'PLAYER'` 時 `abs(adjusted_theo_win - wager * 0.0124)` 全為 0（浮點誤差內）；`theo_win / wager` 均值約 **0.01235**。`BANKER`、`SMALL_TIGER`、`BIG_TIGER`、`TIE` 子樣本內兩欄完全相同。`LUCKY_SIX`、`PLAYER_PAIR`、`BANKER_PAIR` 有時相等、有時差極小（可正可負）。
4. **閒注與標準百家樂（數學 vs 表定）**：在常見規則下，**約 1.235%（`theo_win/wager` 量級）較貼近文獻精算的 house edge**；**1.24%（`adjusted_theo_win/wager`）多為評級／表定取捨**，與真實期望可差數個基點；實桌規則不同時兩者皆需以該桌規則重算。

**Python（pandas；自 repo 根目錄執行）**

```python
import pandas as pd

path = "trainer/sample data/SmartTableData_tbet_sample.csv"
df = pd.read_csv(path)

pl = df.loc[df["bet_type"] == "PLAYER"]
err_player = (pl["adjusted_theo_win"] - 0.0124 * pl["wager"]).abs().max()
ratio_theo = (pl["theo_win"] / pl["wager"]).mean()
ratio_adj = (pl["adjusted_theo_win"] / pl["wager"]).mean()

print("PLAYER max |adj - 0.0124*wager|:", err_player)
print("PLAYER mean theo_win/wager:", ratio_theo)
print("PLAYER mean adjusted/wager:", ratio_adj)

diff_b = (
    df.loc[df["bet_type"] == "BANKER", "theo_win"]
    - df.loc[df["bet_type"] == "BANKER", "adjusted_theo_win"]
).abs().max()
print("BANKER max |theo_win - adjusted_theo_win|:", diff_b)
```

**DuckDB（全量；需 `data/gmwds_t_bet.parquet`）**

```sql
-- PLAYER：檢查 adjusted 是否等於 1.24% * wager（全表；可能較慢）
SELECT
  COUNT(*) AS n_player,
  SUM(CASE
        WHEN ABS(COALESCE(adjusted_theo_win, 0) - COALESCE(wager, 0) * 0.0124) < 1e-6
        THEN 1 ELSE 0
      END) AS n_match_0124,
  SUM(CASE
        WHEN ABS(COALESCE(theo_win, 0) - COALESCE(adjusted_theo_win, 0)) > 1e-6
        THEN 1 ELSE 0
      END) AS n_theo_ne_adj
FROM read_parquet('data/gmwds_t_bet.parquet')
WHERE bet_type = 'PLAYER';
```

### [FND-18] `canonical_patron_profile` vs cleaned `t_bet` 大差異 root-cause 拆解

> 目的：對 `|profile_total_theo_win - bet_total_theo_win| >= 100k` 的高差異案例，區分是 `t_session` gate、raw source 覆蓋不一致，還是 matched-session 口徑差，避免把所有差異都歸咎為單一問題。

**可重現資料（隨 repo 追蹤）**

| 檔案 | 用途 |
|------|------|
| `doc/fixtures/fnd18/canonical_player_mapping.parquet` | `player_id` ↔ `canonical_id`（42 位高差異個案） |
| `doc/fixtures/fnd18/canonical_patron_profile.csv` | patron 彙總（同上 42 位；欄位為附錄 SQL 所需子集） |
| `doc/fixtures/fnd18/cleaned__gmwds_t_bet.parquet` | 對應 cleaned bet 列（同上 42 位） |

**全量重跑（本機產物，不進 git）**：`trainer_hightier/artifacts/**` 由 `.gitignore` 排除；需先依 [`trainer_hightier/RUNBOOK.md`](../trainer_hightier/RUNBOOK.md) 跑 `python -m trainer_hightier.trainer` 產出 mapping／profile／`cleaned__gmwds_t_bet/`。全量掃描時 bet 請用 `read_parquet('trainer_hightier/artifacts/cleaned/cleaned__gmwds_t_bet/**/*.parquet', hive_partitioning=true)`（Hive 分區目錄，非單一 `.parquet` 檔）。

```sql
-- 1) 先鎖定高差異 canonical_id（可調閾值；路徑指向 repo 內 fixture，clone 後可直接跑）
WITH mp AS (
  SELECT CAST(player_id AS BIGINT) AS player_id,
         CAST(canonical_id AS BIGINT) AS canonical_id
  FROM read_parquet('doc/fixtures/fnd18/canonical_player_mapping.parquet')
),
p AS (
  SELECT CAST(canonical_id AS BIGINT) AS canonical_id,
         CAST(total_theo_win AS DOUBLE) AS p_theo
  FROM read_csv_auto('doc/fixtures/fnd18/canonical_patron_profile.csv', header=true)
),
b AS (
  SELECT mp.canonical_id,
         SUM(COALESCE(CAST(bt.theo_win AS DOUBLE), 0.0)) AS b_theo
  FROM read_parquet('doc/fixtures/fnd18/cleaned__gmwds_t_bet.parquet') bt
  JOIN mp ON CAST(bt.player_id AS BIGINT) = mp.player_id
  GROUP BY 1
)
SELECT p.canonical_id, (p.p_theo - b.b_theo) AS theo_gap
FROM p JOIN b USING (canonical_id)
WHERE ABS(p.p_theo - b.b_theo) >= 100000
ORDER BY ABS(p.p_theo - b.b_theo) DESC;
-- 預期：回傳 42 列（與 doc/fnd18_profile_vs_bet_root_cause_cases.md 個案清單相同）

-- 2) 依 canonical_id + session_id 做 FULL OUTER JOIN 拆成 matched / session_only / bet_only
--    並把 bet_only 回接 raw t_session 的 dedup 後版本，判斷是 deleted/canceled 還是 raw session 缺失。
--    逐案分解表為 2026-05-13 全量掃描快照；數字與上式「當前 fixture 重算」可能因上游重跑而漂移。
--    詳細逐案輸出請參考：doc/fnd18_profile_vs_bet_root_cause_cases.md
```

### [FND-19] `t_casino_txn` CDC 去重、PIT 時間與 player-level 清洗契約
```sql
-- 資料來源：data/new tables/t_casino_txn__part_202605.parquet
-- 1) 基本 row / logical ID / 時間範圍
SELECT
  COUNT(*) AS rows,
  COUNT(DISTINCT casino_txn_id) AS distinct_casino_txn_id,
  COUNT(DISTINCT uuid) AS distinct_uuid,
  COUNT(DISTINCT player_id) AS distinct_player_id,
  MIN(gaming_day) AS min_gaming_day,
  MAX(gaming_day) AS max_gaming_day,
  MIN(start_dtm) AS min_start_dtm,
  MAX(start_dtm) AS max_start_dtm,
  MIN(__etl_insert_Dtm) AS min_etl_insert,
  MAX(__etl_insert_Dtm) AS max_etl_insert
FROM read_parquet('data/new tables/t_casino_txn__part_202605.parquet');

-- 2) CDC 欄位完整性：本批 __ts_ms 100% NULL，不可用於排序
SELECT __op, __deleted,
       COUNT(*) AS n,
       SUM(CASE WHEN __ts_ms IS NULL THEN 1 ELSE 0 END) AS ts_ms_null_n,
       MIN(__etl_insert_Dtm) AS min_etl,
       MAX(__etl_insert_Dtm) AS max_etl
FROM read_parquet('data/new tables/t_casino_txn__part_202605.parquet')
GROUP BY ALL
ORDER BY n DESC;

-- 3) Delete-aware dedup：先排除任何帶 delete marker 的 logical transaction，再取最新版本
WITH ranked AS (
  SELECT t.*,
         MAX(CASE WHEN __op = 'd' OR __deleted = 'True' THEN 1 ELSE 0 END)
           OVER (PARTITION BY casino_txn_id) AS has_delete,
         ROW_NUMBER() OVER (
           PARTITION BY casino_txn_id
           ORDER BY __etl_insert_Dtm DESC NULLS LAST,
                    updated_dtm DESC NULLS LAST
         ) AS rn
  FROM read_parquet('data/new tables/t_casino_txn__part_202605.parquet') t
),
clean_base AS (
  SELECT *
  FROM ranked
  WHERE rn = 1 AND has_delete = 0
)
SELECT
  COUNT(*) AS cleaned_ids,
  SUM(CASE WHEN start_dtm IS NULL THEN 1 ELSE 0 END) AS start_null_n,
  SUM(CASE WHEN txn_value IS NULL THEN 1 ELSE 0 END) AS value_null_n,
  SUM(CASE WHEN action = 'SUBMIT' AND status <> 'CANCELED' THEN 1 ELSE 0 END) AS submit_not_canceled_n,
  SUM(CASE WHEN action = 'SUBMIT' AND status <> 'CANCELED' AND player_id IS NOT NULL THEN 1 ELSE 0 END) AS submit_not_canceled_player_n,
  SUM(CASE WHEN action = 'SUBMIT' AND status <> 'CANCELED' AND player_id IS NOT NULL AND type IN ('BUYIN','CASHOUT') THEN 1 ELSE 0 END) AS player_cashflow_n,
  SUM(CASE WHEN action = 'SUBMIT' AND status <> 'CANCELED' AND player_id IS NOT NULL AND type IN ('BUYIN','CASHOUT','CHANGE') THEN 1 ELSE 0 END) AS player_cashflow_plus_change_n,
  SUM(CASE WHEN CAST(txn_value AS DOUBLE) < 0 THEN 1 ELSE 0 END) AS negative_value_n,
  SUM(CASE WHEN CAST(txn_value AS DOUBLE) = 0 THEN 1 ELSE 0 END) AS zero_value_n,
  SUM(CASE WHEN action = 'CANCEL' OR status = 'CANCELED' THEN 1 ELSE 0 END) AS cancel_or_canceled_n
FROM clean_base;

-- 4) Type-specific 狀態：BUYIN 的 Cash 常以 SUBMITTED + buyin_status=SUCCESS 表示有效；
--    TD_FILL / TD_CREDIT / TRANSFER 幾乎沒有 player_id，不適合第一版 player-level feature。
WITH ranked AS (
  SELECT t.*,
         MAX(CASE WHEN __op = 'd' OR __deleted = 'True' THEN 1 ELSE 0 END)
           OVER (PARTITION BY casino_txn_id) AS has_delete,
         ROW_NUMBER() OVER (
           PARTITION BY casino_txn_id
           ORDER BY __etl_insert_Dtm DESC NULLS LAST,
                    updated_dtm DESC NULLS LAST
         ) AS rn
  FROM read_parquet('data/new tables/t_casino_txn__part_202605.parquet') t
),
clean_base AS (
  SELECT *
  FROM ranked
  WHERE rn = 1 AND has_delete = 0
)
SELECT type,
       COUNT(*) FILTER (WHERE action='SUBMIT' AND status <> 'CANCELED' AND player_id IS NOT NULL) AS valid_player_rows,
       COUNT(*) FILTER (WHERE action='SUBMIT' AND status='COMPLETED' AND player_id IS NOT NULL) AS completed_player_rows,
       COUNT(*) FILTER (WHERE action='SUBMIT' AND status='SUBMITTED' AND player_id IS NOT NULL) AS submitted_player_rows,
       COUNT(*) FILTER (WHERE action='SUBMIT' AND status='SUBMITTED' AND buyin_status='SUCCESS' AND player_id IS NOT NULL) AS submitted_success_player_rows
FROM clean_base
WHERE start_dtm IS NOT NULL AND txn_value IS NOT NULL
GROUP BY type
ORDER BY valid_player_rows DESC;
```
