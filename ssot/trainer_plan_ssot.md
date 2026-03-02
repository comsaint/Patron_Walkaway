# Patron Walkaway Predictor: Model Training Plan & Design Document

## 0. 文件目的與使用說明

本文件為 **Patron Walkaway Predictor** 的「訓練/標籤/特徵」設計規格與 rationale（單一事實來源，SSOT）。目的在於描述我們賭客離場偵測模型的設計理念與決策依據，作為讓 LLM 或工程師產生詳細實作計畫書（涵蓋資料處理、建模、回測、線上推論與驗證）的唯一規格文件。

> **對齊來源（不可互相矛盾）：**
> - **資料事實與 DQ 決策**：`doc/FINDINGS.md`（FND-*）
> - **欄位/表級語義與可用性**：`schema/GDP_GMWDS_Raw_Schema_Dictionary.md`
> - **既有系統現況與偏差風險**：`doc/TRAINER_ISSUES.md`（TRN-*）、`doc/TRAINER_SUMMARY.md`、`trainer/`

---

## 1. 背景 (Business Context)

銀河娛樂集團 (GEG) 是澳門特別行政區六大綜合度假村營運商之一。本專案聚焦於銀河澳門 (Galaxy Macau) 物業的大眾娛樂場 (Mass Gaming Floor)，特別針對營收核心貢獻者——**百家樂**。

自 2024 年 7 月起，我們逐步在百家樂賭桌部署 **Smart Table** 智慧桌台技術。這些桌台即時擷取投注行為：每一筆下注、派彩、玩家位置及籌碼移動都被數位化並串流至中央資料湖。截至 2026 年 2 月，我們累積約 **19 個月的資料**（2024-07-02 至 2026-02-13），包含約 4.38 億筆投注紀錄及約 7,400 萬筆 Session 紀錄。

業務端希望主動留住即將離開的賭客。目前公關（Host）主要靠直覺與現場觀察來判斷不滿或準備離開的玩家。我們的目標是建立一套資料驅動的即時預警系統，預測賭客是否即將停止投注，讓公關能提早介入挽留。

---

## 2. 名詞定義 (Definitions)

### 2.1 事件時間 (Event time) 與可用時間 (Available time)
為避免未來資料外洩 (leakage)，任何在時間 $t$ 做出的預測，只能使用在 $t$ 前「已可得」的資訊。
- **`t_bet`**：
  - Event time：`payout_complete_dtm`
  - Available time：近似 event time + ~1 分鐘
- **`t_session`**：
  - Event time：`COALESCE(session_end_dtm, lud_dtm)`
  - Available time：通常是 Session 結束後延遲寫入（加 +7 分鐘保守延遲）

### 2.2 業務與實體術語

| 術語 | 定義 |
|---|---|
| **離場事件 (Walkaway event)** | 若玩家在某個時間 $s$ 起，接下來至少 **$X$**（預設=30）分鐘都沒有任何下注（以 `t_bet` 為準），則稱玩家自 $s$ 起進入 walkaway gap。此 $X$ 分鐘間隔閾值即為業務定義的流失標準。 |
| **預測視窗 (Prediction horizon)** | 離場事件發生前的 **$Y$**（預設 = 15 分鐘）時間窗口。警報發得太早（如離場前 60 分鐘）不具可行動性；發得太晚（如已離開後）則無意義。 |
| **預警 (Alert)** | 在每筆下注時間點 $t$，判斷是否存在一個 gap 起點 $s$，使得 $s \in [t, t+Y]$ 且從 $s$ 起連續 $X$ 分鐘無下注。 |
| **觀測點 (Observation point)** | 每一筆投注即為一個觀測點。在每筆投注的時間點，我們計算特徵並（在訓練時）賦予標籤。 |
| **Session** | 單一賭客在單一賭桌上一段連續打牌的時段。紀錄在賭客**離桌後**才完成寫入。 |
| **有卡客 (Rated player)** | 已刷會員卡的賭客（具有效的 `casino_player_id`），可連結歷史行為。 |
| **無卡客 (Non-rated player)** | 未使用會員卡的賭客，僅有 Smart Table 指派的 `player_id`，無歷史檔案。 |
| **營業日 (Gaming day)** | 賭場使用的帳務日；與日曆午夜不對齊。 |
| **造訪 (Visit)** | 賭客在一個營業日內於娛樂場的整段停留時間，可能橫跨多張桌台的多個 Session。 |

---

## 3. 目標與非目標 (Objectives & Non-goals)

### 3.1 主要目標（線上可落地）
建構一套即時偵測系統，對每位活躍投注的賭客預測：
> **在觀測時間 $T$（下注時），該賭客是否會在接下來 $Y$（15）分鐘內停止投注，且至少 $X$（30）分鐘不會回來？**

警報應在賭客離場（walkaway gap 開始）前的最後 $Y$ 分鐘內觸發，讓公關有時間接近並嘗試挽留。

### 3.2 業務約束與 KPI
- **精準度優先於召回率 (Precision over recall)**：假警報會浪費公關頻寬並侵蝕信任感。
- **最低可行動量 (Minimum actionable volume)**：一個精準度 99% 但每天只有 1 個警報的模型是無用的。
- 我們需要**經業務校準的閾值**，使用 F-beta (beta < 1) 或帶有最低召回率約束（例如 recall >= 5-10%）的精準度-召回率曲線。

### 3.3 非目標（暫不做/不可假設）
- **直接使用玩家 PII/會員等級/CRM 事件**：目前因政策無法取用玩家主檔。
- **把 `t_session` 視為即時且完整可用**：Session 很可能是「結束後」才完整入湖，不可依賴其即時性。

---

## 4. 資料來源與即時可用性

資料儲存於 **ClickHouse** 資料庫中。

### 4.1 可用表格

| 表格 | 粒度 | 列數（約） | 本專案角色 | 即時可用性 |
|---|---|---|---|---|
| `t_bet` | 每筆投注一列 | 4.38 億 | **主表**：當前投注行為、標籤計算及所有投注層級特徵。 | 派彩後約 1 分鐘內可用。 |
| `t_session` | 每 Session 一列 | 7,400 萬 | **輔助表**：歷史賭客輪廓及 `player_id` ↔ `casino_player_id` 橋接關係。 | **Session 結束後**延遲約 7 分鐘可用。不可用於即時當前 Session 計算。 |
| `t_game` | 每一局一列 | 2.11 億 | **未來用途**：牌局層級上下文。 | 派彩後約 1 分鐘內可用。 |
| `t_shoe` | 每副牌靴一列 | N/A | **不相關**：與離場預測無關。 | N/A |

### 4.2 事件時間與資料可用性策略 (FND-13)
系統時間戳（`__etl_insert_Dtm`、`__ts_ms`）**受回填作業污染**，絕對不可用於串流模擬。任何特徵工程或標籤建構都必須透過「僅允許在該觀測點的 `event_time + delay` 時間之前已可見的資料」來模擬即時場景。
- **`t_bet` / `t_game`**：`event_time = payout_complete_dtm`，延遲 **+1 分鐘**。
- **`t_session`**：`event_time = COALESCE(session_end_dtm, lud_dtm)`，延遲 **+7 分鐘**（保守值：+15 分鐘）。

### 4.3 時間窗口化抽取與資料量處理 (Time-windowed extraction & data volume)

訓練資料橫跨約 **19 個月、4.38 億筆投注、7,400 萬筆 Session**，無法一次性載入記憶體。以下為 SSOT 對「時間窗口化步驟」與「資料量處理」的強制要求，實作細節（窗口粒度、取樣率）由實作計畫與程式碼約定。

**時間窗口化抽取 (Time-windowed extraction)**  
- 從 ClickHouse 抽取訓練用資料時，**必須**依時間範圍分窗（例如以月或週為單位），逐窗查詢 `t_bet` / `t_session`，不得假設「全時段一次 SELECT」。
- 每個時間窗口的查詢應盡量對齊資料表 partition（例如 `t_bet` / `t_session` 依 `gaming_day` partition），以利 predicate pushdown（如 `payout_complete_dtm` 或 `gaming_day` 範圍條件），降低掃描量與記憶體峰值。
- 標籤與特徵的計算須在**各窗口內**依 §7、§8 的語義執行（含 C1 延伸拉取：該窗口若鄰接下一窗口，延伸區間可落入下一窗口的已拉取資料，由實作計畫定義邊界與重疊規則）。
- **離線訓練資料源（允許本機 Parquet 匯出）**：為加速迭代，允許在訓練/開發環境以「已從 ClickHouse 匯出的完整表 Parquet」（例如放在本機 `.data/` 目錄、或以 DuckDB 掃描）取代即時查詢 ClickHouse；但這僅是 I/O 替代，**不得**改變任何語義與護欄：仍需用同一套時間窗口邊界（§4.3 的集中式定義器）、仍需套用 §5 的 DQ 規則（含 FND-01 去重等）、仍需遵守 §4.2 的 available time / cutoff_time 防漏要求。Production（線上推論/驗證）資料來源一律以 ClickHouse 為準，本機 Parquet 僅用於離線重放與訓練加速。

**資料量處理策略 (Data volume strategy)**  
- **統一的時間折疊與窗口定義器 (Time Fold Splitter / Window Definer)**：**必須**建立一個集中式的模組，負責計算與發放所有時間窗口的邊界（月度 chunk 的 start/end、C1 延伸拉取緩衝、Train/Valid/Test 的 cutoff 點）。所有 ETL、特徵計算與模型交叉驗證都必須嚴格呼叫這個定義器，避免各階段時間切分出現 off-by-one 錯誤或不一致。
  - **邊界語義合約**：核心窗口為 `[window_start, window_end)`。C1 延伸拉取區間 `[window_end, extended_end)` **僅供**觀測未來事件以計算標籤，其產出的樣本**絕對不可**納入當前 chunk 的訓練集。
- **Chunking**：以時間窗口為 chunk，每 chunk 拉取後進行該範圍內的標籤建構與特徵計算；產出的 feature matrix 可寫入磁碟（如 parquet）或串接進單一訓練集，再送入模型。快取 key 建議包含 `(window_start, window_end)` 以符合 TRN-07。
- **取樣 (Sampling)**：若單窗口或合併後仍超出可用記憶體或訓練成本上限，允許在**不破壞時間先後順序**的前提下進行取樣（例如依時間分層抽樣、或對多數類下採樣），並在文件中註明取樣率與適用範圍，評估時須報告取樣對指標的潛在影響。
- **Featuretools / 自動化特徵**：若 DFS 或 cutoff 計算需遍歷大量觀測點，應依同一時間窗口邊界分批呼叫，避免單次傳入全時段 cutoff 表導致 OOM。

上述約定確保「時間依賴」與「大資料量」在設計層級被明確處理，實作計畫須對應落地（窗口大小、重疊、取樣與 I/O 策略）。

---

## 5. 關鍵資料品質護欄 (P0 必處理)

以下問題（詳載於 `doc/FINDINGS.md`）**必須**在建模前處理，否則將污染標籤與特徵：

1. **Session 去重 (FND-01)**：`ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY lud_dtm DESC NULLS LAST, __etl_insert_Dtm DESC) = 1`。必須 SELECT 此二欄位才能執行。
2. **人工帳務調整排除 (FND-02)**：加入 `is_manual = 0` 過濾條件。
3. **casino_player_id 空值清洗 (FND-03)**：`CASE WHEN lower(trim(casino_player_id)) IN ('', 'null') THEN NULL ELSE trim(casino_player_id) END`。
4. **Session 狀態處理 (FND-04)**：**不要**過濾 `status = 'SUCCESS'`。保留所有非人工且 `turnover > 0` 或 `num_games_with_wager > 0` 的 Session。
5. **is_known_player 不可靠 (FND-09)**：絕對不要使用此旗標。一律檢查 `casino_player_id IS NOT NULL`。
6. **bet_reconciled_at 不可用 (FND-06)**：100% 無效值。視為不存在。
7. **Game 表去重 (FND-14)**：使用 `MAX(__ts_ms)` 或 `MAX(__etl_insert_Dtm)` 去重。
8. **全空欄位 (FND-08)**：`bonus`, `tip_amount`, `increment_wager`, `payout_value` 100% 為 NULL，勿使用。
9. **軟刪除旗標 (DQ Rule)**：schema 存在 `is_deleted` 與 `is_canceled`，應驗證其語義並預設過濾（如 `is_deleted = 0 AND is_canceled = 0`）。

---

## 6. 玩家身份歸戶 (Identity Resolution)

### 6.1 多對多映射問題 (FND-11)
`player_id`（生物辨識）與 `casino_player_id`（會員卡）之間存在**雙向多對多**關係。錯誤分組會切斷 Session 鏈結，產生假的離場標籤（TRN-03）。

### 6.2 歸戶策略：Canonical ID (D2)

本專案**採用【D2】Canonical ID 歸戶**為首選策略，以最大化利用歷史訊號，同時降低因生物辨識斷鏈造成的標籤污染（FND-11 / TRN-03）。若實作成本過高，才退回純 `player_id` 歸戶 (D1)。

**1. canonical_id 定義**：
- 優先：FND-03 清洗後的 `casino_player_id`（有卡客）。
- 兜底：`player_id`（無卡客）。

**2. Mapping 建置（離線增量）**：
- 對 `t_session` 套用 FND-01 去重、FND-02 排除人工、FND-03 清洗卡號。
- 取 `player_id ↔ casino_player_id` 連結，維護 `player_id -> canonical_id` 映射表。
- **訓練時防洩漏**：建置 mapping 時，**必須**僅使用 `available_time <= cutoff_dtm` 的 session，確保不使用未來才出現的身份連結（與 §6.4 線上判定保持 parity）。

**3. M:N 衝突處理**：
- **同卡多 ID**：所有 `player_id` 均映射至該 `casino_player_id`。
- **同 ID 多卡（換卡）**：以最近一次（最大 `session_end_dtm`/`lud_dtm`）的卡號為準。

**4. 系統影響**：
- 跨日/桌歷史輪廓（軌道 A `player` 實體）及標籤/回測的分組鍵，均以 `canonical_id` 聚合，減少 TRN-03 假正例。

### 6.3 假帳號排除 (FND-12)
排除 `session_cnt = 1 AND SUM(COALESCE(num_games_with_wager, 0)) <= 1` 的假帳號 ID（極可能是 CRM 生成的過客/伴遊 ID）。

### 6.4 即時身份判定邏輯 (若採 D2)
推論時判斷有卡/無卡客：
1. 當前投注的 `session_id` 是否連結到一個已入湖且具 `casino_player_id` 的 `t_session`？
2. 檢查該 `player_id` 是否在任何**先前已完成的** Session 中曾關聯過卡號。
3. 兜底：視為無卡客。

---

## 7. 標籤設計 (Label Design)

### 7.1 標籤定義（防洩漏與右截尾）
Walkaway ground truth 以 `t_bet` 的下注時間序列定義（不依賴 `t_session` 的結束資訊）。
**排序護欄**：為確保 train-serve parity 與滾動特徵穩定，序列必須嚴格按 `ORDER BY payout_complete_dtm ASC, bet_id ASC` 排序。

對每位玩家的下注序列 $\{b_i\}$：
1. 若 $b_{i+1} - b_i \ge X$，則 $b_i$ 為一個 **gap start**。
2. 對任一下注樣本時間 $t=b_j$，若存在 gap start $s=b_i$ 使 $s \in [t, t+Y]$，則標記為 `label = 1`（表示可在 $Y$ 分鐘內預警）。否則為 0。

**注意（未來資訊護欄）**：本標籤定義會使用「下一筆下注時間 $b_{i+1}$」等未來資訊來判斷 gap start。這些未來資訊**僅可**用於標籤建構/離線驗證；任何由此衍生的量（例如距離 gap start 的分鐘數、距離下一筆下注的分鐘數）**絕不可**作為模型特徵。

### 7.2 右截尾處理 (Right-censoring)

若把窗口末端缺乏未來資訊的行為直接視為離場，會膨脹正例（TRN-06）。
**策略：採用【C1】延伸拉取 (Extended pull)**。
- 抽取資料時，將 window_end 往後延伸至少 $X+Y$（如 1 天），延伸區間**僅用於標籤計算**（觀測下一筆下注間隔），**絕對不可**將其樣本納入訓練集。

---

## 8. 特徵工程策略 (Feature Engineering)

### 8.1 設計原則
- **禁止未來洩漏 (No future leakage)**：線上推論可得的特徵，訓練才可以用。
- **訓練-服務一致性 (Train-Serve Parity)**：特徵計算與 rolling window 邊界在訓練與服務端必須完全一致（TRN-08）。

### 8.2 雙軌特徵工程架構 (Dual-track Feature Engineering)

本專案採用**雙軌並行**的特徵工程架構：**自動化探索（Featuretools DFS）** 負責系統性搜索聚合/窗口/組合特徵空間；**手工向量化特徵** 負責 Featuretools 天然無法或效能極差的狀態機/跨實體邏輯。兩軌共用同一個 `cutoff_time` / 時間窗口框架，產出後 join 成統一的 feature matrix。

> **設計理念**：SSOT 過去列出的 A–E 類特徵僅反映「曾經嘗試過的特徵」，不代表最終或最佳特徵集。本架構的目標是讓機器去發現人腦難以窮舉的聚合模式，同時保留領域知識驅動的狀態特徵。

#### 軌道 A：自動化特徵探索與生產（Featuretools）

**1. EntitySet 建構**
- `t_bet`（target entity）：以 `bet_id` 為 index，`payout_complete_dtm` 為 time_index。
- `t_session`（歷史輪廓）：以 `session_id` 為 index，`COALESCE(session_end_dtm, lud_dtm)` 為 time_index。
- `player`（跨日/跨桌輪廓）：以歸戶後的 `canonical_id` 為 index。此實體提供跨 session 聚合的軸心（見 §6.2）。
- **關係**：
  - **`t_bet.session_id` → `t_session.session_id`**（many-to-one）。`table_id` 僅為共有欄位，不作為 EntitySet 的父子關係鍵。
  - **`t_session.canonical_id` → `player.canonical_id`**（many-to-one）。

**2. Primitives 運用**
- **轉換基元**：`time_since`、`cum_sum`、`cum_mean`、週期性特徵（如 `time_of_day_sin/cos` 註冊為自訂基元）等。
- **聚合基元**：自動生成 `count`, `sum`, `mean`, `max`, `min`, `trend` 等。
- **滾動窗口**：利用 `window_size` 參數（5m, 15m, 30m 等），自動展開近期行為統計。

**3. 防洩漏（Cutoff Time 護欄）**
- 必須嚴格使用 `cutoff_time` 機制。每個觀測點 $t$ 作為 cutoff，工具自動切斷 $t$ 之後的資料。
- `t_session` 資料須額外滿足 `available_time <= cutoff_time` 後才能加入 EntitySet。

**4. 兩階段 DFS 流程（解決單機資源限制）**

訓練環境為**單機**，無法對全量 4.38 億筆一次性做 DFS。採用以下兩階段流程：

- **第一階段 — 探索（在抽樣資料上）**：
  - 對各月度窗口內的多數類（label=0）做時間分層下採樣（保留 10–20%），正例全保留，使每月觀測點從 ~2,300 萬降到幾百萬。
  - 在此抽樣集上跑完整 DFS（建議 `max_depth<=2`，primitives 白名單），產出大量候選特徵。
  - 做 Feature screening（見下方 §8.2.C），選出高潛力候選。
  - 用 `featuretools.save_features(feature_defs)` **將選中特徵的計算圖持久化**。

- **第二階段 — 生產（全量）**：
  - 用 `featuretools.calculate_feature_matrix(saved_feature_defs, entityset)` **直接套用已存的特徵定義**，逐月計算全量資料。
  - **不重新實作**：訓練與推論都使用同一份 saved feature definitions，從根本上消除 train-serve parity 風險。
  - 每月產出 parquet 落盤，合併後送入模型。

#### 軌道 B：手工向量化特徵（Featuretools 無法或不宜處理的邏輯）

以下特徵因涉及**條件重置狀態機**或**跨玩家/跨桌聚合**，Featuretools 天然無法支援或效能極差，**必須**以高效的向量化程式碼（Pandas/Polars）手寫實作：

| 特徵 | 為何不適合 Featuretools | 實作方式 |
|---|---|---|
| `loss_streak` | 需要「遇 WIN 重置、遇 PUSH 條件不重置」的序列狀態機，無對應內建 Primitive | 向量化 Pandas/Polars，嚴格遵守同一 cutoff_time |
| `run_boundary` / `minutes_since_run_start` | 需要「相鄰 bet 間距 ≥ X → 新 run 開始」的序列依賴切割 | 同上 |
| `table_hc`（同桌人數，S1） | 需跨玩家、以 `table_id` 為軸心的滾動聚合；若在 EntitySet 中建 table 實體，每個 cutoff 觸發全桌掃描，效能極差 | 同上 |

**手寫特徵的防漏與 parity 要求**：
- 必須與軌道 A 共用同一個 `cutoff_time` / 時間窗口框架。
- 手寫特徵的計算函數必須抽取至 `features.py`，由 trainer 與 scorer 共同匯入（TRN-05/07/08）。
- 所有手寫特徵的實作必須是**向量化**的，禁止逐列遍歷（Python for-loop / `apply`），以確保在全量資料上可接受的執行效能。

#### C. 特徵篩選 (Feature screening)

軌道 A（DFS）產出的候選特徵數量可能很大，**必須**在送入正式訓練前增加篩選：

1. **第一階段**：依單變量與目標關聯（如 mutual information、變異數門檻）及冗餘剔除（如高相關、VIF）縮減候選集。
2. **第二階段（可選）**：在訓練集上以輕量模型（如 LightGBM）計算 feature importance 或 SHAP，取 top-K 作為最終特徵集。此步驟須僅使用訓練時間區間內的資料，不得使用 valid/test，以符合 §8.1 防洩漏。
3. 最終通過篩選的特徵清單（含軌道 A 篩選後 + 軌道 B 全部）即為 `feature_list.json` 的內容，訓練與推論端僅計算此清單內特徵，以維持 train-serve parity。

### 8.3 Session 與桌台特徵可用性 (S1)

為避免依賴 `session_end_dtm` 導致未來資料洩漏（TRN-05），**採用【S1】保守路線**：
- **禁止使用**：線上完全不使用任何與 `session_end` 相關的特徵。
- **即時替代方案**：`table_hc`（同桌人數）改以 `t_bet` 即時計算（如「過去 N 分鐘內，同 `table_id` 下注的不重複 `player_id` 數」），確保訓練與服務端一致。

**關於 `t_game`（分期實作）**：
- **Phase 1（本期）**：不使用 `t_game` 牌局特徵，專注於 `t_bet` 與 `t_session`。
- **Phase 2（後期）**：再整合 `t_game` 以打造「桌台氣氛」特徵（如同局玩家數、同局總下注）。

### 8.4 特徵工程路線比較與分期策略

除了 §8.2 的「雙軌特徵工程 + GBDT」路線外，另一條成熟的路線是**事件序列嵌入 (Event Sequence Embedding)**：將每筆下注視為一個帶有多維上下文的事件，用序列模型（GRU / Transformer）學習多層級嵌入向量（bet → run/session → player），再用嵌入做下游分類。近年在金融交易、電信 churn、反詐等領域已有成熟落地案例（PyTorch-Lifestream [IJCAI 2025]、CASPR [Microsoft]、TransactionGPT 等）。

**為何序列嵌入值得考慮——直觀範例：**
- **玩家 A**：連贏 5 把小注 → 輸 1 把大注。
- **玩家 B**：輸 1 把大注 → 連贏 5 把小注。

在傳統聚合特徵（即使是 Featuretools 生成的「過去 6 把平均下注與勝率」）中，兩者看起來一模一樣。但玩家 A 可能正處於挫敗即將離場，而玩家 B 正在回本、情緒完全不同。序列嵌入能捕捉這種節奏與順序的微妙差異。

#### 兩條路線的全面比較

| 面向 | 路線 A：自動化特徵工程 (Featuretools) + GBDT | 路線 B：事件序列嵌入 + 深度學習 |
|---|---|---|
| **訊號表達力** | 較弱。聚合統計丟失事件順序與節奏變化。 | 強。天然捕捉序列模式、時間間隔動態、行為節奏漸變。 |
| **特徵工程成本** | 中低。透過 Primitives 與 EntitySet 自動展開空間，但仍需定義 Custom Primitives。 | 低（理論上）。但需設計 tokenization schema 與事件編碼。 |
| **可解釋性** | 高。Feature importance / SHAP 直觀，Host 和管理層容易理解「為何警報響了」。 | 低。黑箱；需額外解釋工具，業務推廣有阻力。 |
| **即時部署難度** | 低。線上維護相同的 EntitySet 狀態更新並計算，LightGBM < 1ms。 | 中。Forward pass ~幾 ms 可接受，但序列狀態管理、模型版本控制、回滾策略更複雜。 |
| **洩漏風險** | **極低**。自動化工具內建嚴格的 Cutoff Time 機制。 | **更高**。序列切窗、padding、mask/label 定義引入新的洩漏陷阱，工程護欄需更嚴格。 |
| **監控複雜度** | 成熟。特徵分佈、重要性、閾值等標準做法。 | **新維度**。需監控 embedding drift、序列長度漂移、表徵崩壞 (representation collapse) 等。 |
| **冷啟動 / 無卡客** | 可用（依賴近期窗口與即時狀態）。 | 短序列仍可產出嵌入但品質較差；無卡客跨日身份不穩限制 player embedding 長期價值。 |
| **與現有系統契合** | 高。現有 scorer 輪詢 + 狀態快取天然支持。 | 中。需新增嵌入生成/快取/版本管理機制。 |
| **迭代速度** | 快。增減 Primitives → 自動生成 → 重訓，循環短。 | 慢。架構調整、預訓練 + 微調流程更長。 |
| **學術成熟度** | 非常成熟。GBDT 在表格資料 benchmark 仍持平或優於 DL（NeurIPS 2022、ICLR 2025）。 | 快速成熟中（CASPR、PyTorch-Lifestream，2024–2025），但 casino 行業落地案例極少。 |

#### 本專案決策：分期混合策略 (Phased Hybrid)

| Phase | 做什麼 | 目標 | 備註 |
|---|---|---|---|
| **Phase 1（本期）** | 雙軌特徵工程（Featuretools DFS 探索/生產 + 手寫狀態特徵）+ LightGBM + Optuna 調參 + visit-level 樣本加權 | 上線 MVP，打通 pipeline，用高可解釋性與系統化探索建立業務信任。 | 對應 §8.2（雙軌架構、兩階段 DFS、`save_features`）、§9.2（Optuna）、§9.3（樣本加權）。 |
| **Phase 2（基礎模型驗證後）** | 離線 SSL 預訓練序列嵌入（**僅用 `t_bet`**），產出 Player/Run Embedding，**每日批次更新**，推論時當作靜態特徵餵進 GBDT。 | 量化嵌入對 PR-AUC / F-beta 的增益；若有效則長期作為正式方案。 | 避開「即時跑 NN forward pass」的工程風險，把序列模型的增益以最低成本注入。 |
| **Phase 3（僅在 Phase 2 增益不足時）** | 端到端即時序列模型線上化。 | 完全捕捉即時序列動態。 | 需更嚴的延遲/可靠性/監控與回滾策略。 |

---

## 9. 建模方法 (Modeling Approach)

### 9.1 雙模型架構
- **有卡客模型**：軌道 A（Featuretools DFS）開放完整 EntitySet 存取權限（`t_bet` + `t_session`），含歷史賭客輪廓與跨日聚合；軌道 B 手寫特徵亦全部可用。
- **無卡客模型**：軌道 A 限制 EntitySet 僅探索 `t_bet` 內部路徑（當前與近期投注行為及轉換特徵），不使用 `t_session` 的歷史輪廓或 Session 級聚合；軌道 B 中 `loss_streak`、`run_boundary` 可用，但 `t_session` 相關手寫特徵不可用。
- 分開模型可避免歷史特徵被大量 NaN 掩蓋，並允許對不同群體分別校準閾值（閾值搜索由 Optuna 統一管理，見 §10.2）。

### 9.2 演算法、調參與切割

**Phase 1 演算法（本期）**：
- **模型**：LightGBM（支援 `class_weight='balanced'` 處理類別不平衡）。
- **超參調優**：使用 **Optuna（TPE Sampler）** 進行超參搜索（`n_estimators`, `learning_rate`, `max_depth`, `num_leaves`, `min_child_samples`, `subsample`, `colsample_bytree`, `reg_alpha`, `reg_lambda` 等），objective 以 validation set 上的 F-beta 或 PR-AUC 為目標。Optuna 同時用於 §10.2 的雙模型閾值搜索。
- **不使用 AutoML（Phase 1）**：為維持「可解釋性」與「最快出結果」，Phase 1 鎖定 LightGBM + Optuna，不引入 AutoML 框架。

**Phase 2+ 演算法（未來）**：
- **集成探索**：引入 **FLAML** 或其他 AutoML 框架，在 LightGBM 基礎上探索 stacking/blending 集成，並支援時間序列感知的 CV 與自訂指標。
- 僅在 Phase 1 模型穩定上線後啟動。

**切割方式**：**嚴格按時間切割**（Train 60-70% | Valid 15-20% | Test 15-20%）。不可隨機切分。

### 9.3 樣本加權策略：長度偏誤校正 (Sample Weighting / Length Bias Correction)

**問題根源**：以「每筆下注為觀測點」時，下注次數多（長 session / 高頻玩家）的賭客在訓練集中貢獻遠多於短暫停留玩家的觀測點，導致模型的 loss 被高頻玩家行為主導（Length Bias）。這使模型對偶爾造訪或短暫停留的玩家泛化能力不足，且指標（precision/recall）容易被少數高頻玩家的表現所左右。

**Phase 1 強制要求**：訓練時必須對每個觀測點計算 **visit-level 反比樣本權重（Inverse Visit-level Sample Weight）**，並傳入模型訓練：

$$w_i = \frac{1}{N_{\text{visit}}(canonical\_id_i,\ gaming\_day_i)}$$

其中 $N_{\text{visit}}$ 為同一 visit（`canonical_id` × `gaming_day`）內的**訓練集**總觀測點數（取樣後計算）。

實作細節：
- **與 `class_weight` 並用**：`class_weight='balanced'` 處理正/負例標籤不平衡；`sample_weight` 處理跨玩家個體頻率不平衡。兩者作用層面不同，可同時套用。LightGBM 透過 `model.fit(..., sample_weight=weights)` 傳入。
- **防洩漏**：`sample_weight` 計算時**僅使用 training window 內的觀測點數**，不可混入 valid/test 資料。
- **取樣一致性**：若 §4.3 的下採樣已執行，$N_{\text{visit}}$ 以取樣**後**的實際觀測點數為準（確保取樣與加權語義一致）。

**評估報告雙口徑（必須同時報告）**：
- **Micro（以觀測點為單位）**：直接計算整體 precision/recall/PR-AUC，反映全量 bet 的模型品質。
- **Macro-by-visit**：對每個 visit 先計算各自指標，再取平均，用以驗證模型對不同停留時長的玩家是否公平泛化。若 Micro 與 Macro 差距顯著，說明 Length Bias 仍未充分校正，應重新審視加權策略。

### 9.4 Model API Contract（與 `scorer.py` 的介面）

模型在服務端以 HTTP 服務形式暴露，與現有 `scorer.py` 透過 API 對接。高層約束如下（細節以 `doc/model_api_protocol.md` 為準）：

- **Endpoint 與責任邊界**
  - `POST /score`：接收一批 bet-level 特徵列並回傳 walkaway 機率與 `alert` flag。  
  - `GET /health`：健康檢查，回報 `status` 與目前 `model_version`。  
  - `GET /model_info`：回報模型型別、版本、當前使用特徵清單與訓練指標。
  - `scorer.py` 負責：從 ClickHouse 抓 raw bets/sessions、維護 session 狀態、計算所有輸入特徵、呼叫 `/score`、去重與寫入 SQLite alerts。  
  - Model Service 負責：模型載入與版本管理、`predict_proba`、閾值管理、輸入 schema 驗證、回傳機率與預設 `alert`。

- **非功能需求**
  - 每批 500–5,000（上限 10,000）列，`POST /score` 在 3 秒內完成回應。
  - scoring 必須 stateless 且 idempotent（同一批輸入 → 同一批輸出）。
  - 缺少或多出特徵欄位時回傳 422（**不**在服務端默默補齊）。

- **特徵清單的角色**
  - `doc/model_api_protocol.md` 中列出的特徵代表的是**當前實作狀態的快照**，**不是永久固定的合約**。
  - 新版模型的 **實際特徵集合** 由 §8.2 的雙軌架構動態產生：軌道 A（Featuretools DFS）的 `save_features` 持久化計算圖 + 軌道 B（手寫特徵）合併後，經 Feature screening（§8.2.C）篩選，輸出為 `feature_list.json` 並透過 `/model_info.features` 對外公開。
  - App 端（`scorer.py`）必須依賴**相同的** saved feature definitions（軌道 A）與 `features.py`（軌道 B）來同步產生線上特徵，確保 train-serve parity。
  - 產出的特徵依然嚴格受制於 §8.1 的護欄（無洩漏、線上可重現），Cutoff Time 機制是確保此合約的核心。

---

## 10. 評估與閾值選擇 (Evaluation & Validation)

### 10.1 指標
- **精準度 (Precision)**：核心 KPI。
- **Alert Volume / Coverage**：必要約束，每天需要有足夠的警報量。
- 輔助：PR-AUC, F-beta。

### 10.2 閾值選擇策略 (Thresholding)

現況閾值過度偏向精準度導致幾乎無警報（TRN-11）。為對齊「業務可用性」，需加入約束條件。

**核心執行機制**：
- **僅限 Validation Set**：閾值只能在 valid set 決定，test set 僅供最終報告。
- **Optuna 2D 搜索**：使用 Optuna TPE Sampler 搜索 `(rated_threshold, nonrated_threshold)`，取代窮舉 grid search（見 §9.2）。
- 必須報告 Precision / Recall / PR-AUC / 總警報量，並依 §10.3 回測評估口徑（每 visit 至多計 1 次 TP）以確認營運可接受度。

**Phase 1 首選策略：【G1】Precision 下限 + 最小警報量**
為建立 Host 信任並確保可行動量，優先採用此 Gate：
- **約束 1**：各模型 Precision 均需 ≥ \(P_{\min}\)（例：0.70–0.85）
- **約束 2**：雙模型總警報量 ≥ \(V_{\min}\)（例：全場每小時 5–20 個）
- **目標**：在滿足約束下，最大化 Recall 或 F\(_\beta\)（\(\beta<1\)）。

*(備援策略：若業務端優先要求覆蓋率，則改採【G2】設定 Recall 下限 ≥ 5-10% 以最大化 Precision；或採【G3】滿足 Precision 下限後單純最大化觸達量。)*

### 10.3 線上 ground truth 與回測
- 線上驗證：以 `trainer/validator.py` 的 45 分鐘 horizon 驗證語義為準（覆蓋 Y+X 並允許延遲）。
- **回測評估口徑**：回測指標計算時，對同一 visit（`canonical_id` × `gaming_day`）至多計入 1 次 True Positive，以避免高頻玩家因大量觀測點而膨脹精準度指標。回測須嚴格按時間順序處理，不得 look-ahead。
  > **重要區分**：此「每 visit 至多計 1 次 TP」的去重僅是**離線評估口徑**（為正確計算 precision / alert volume），**不**意味著線上推論只對每位玩家每次造訪輸出一個 alert。線上是否節流、多久對同一賭客通知一次 Host，屬於產品／前端設計決策，不在本模型規格範圍內。

---

## 11. 服務架構與訓練-服務一致性

現有的 `scorer.py` 採定期輪詢（約 45 秒）。
強制執行以下一致性（Train-Serve Parity）：
1. **滾動窗口邊界**：須使用一致的包含/不包含邏輯（修正 TRN-08）。
2. **特徵程式碼**：抽取為共用模組 `features.py` 供 trainer 與 scorer 共同匯入。
3. **快取策略**：加入快取元資料（日期範圍、雜湊）驗證，不符時強制失效（修正 TRN-07）。
4. **模型 artifact 版本耦合**：每次模型部署必須以**原子單位**交付，包含 `model.pkl` + `saved_feature_defs`（軌道 A）+ `features.py`（軌道 B）+ `feature_list.json` + reason code 映射表，統一以 `model_version` 標識。任何組件版本不匹配時，服務端必須拒絕載入並回報錯誤。

---

## 12. 過去實作教訓與修正清單 (Remediation Checklist)

以下 P0 問題必須在 Planning 中逐條對應落地：
- **TRN-01**：Session 去重未使用 `lud_dtm`（違反 FND-01）。
- **TRN-02**：未排除 `is_manual = 1`（違反 FND-02）。
- **TRN-03**：`player_id` 歸戶造成換卡玩家斷鏈（對齊 FND-11 與 D1/D2）。
- **TRN-05**：不可得的 `session_end` 導致 `table_hc` leakage（對齊 S1/S2）。
- **TRN-06**：窗口末端標籤膨脹（右截尾未處理，對齊 C1/C2）。
- **TRN-07**：快取無窗口一致性檢查，靜默使用舊資料。
- **TRN-08**：Rolling window 邊界語義不一致。
- **TRN-09**：`loss_streak` 因字串比較 bug 永遠為 0。
- **TRN-11**：閾值選擇過度偏向 precision 導致幾乎無警報（對齊 §10.2 閾值策略與 Optuna 搜索）。

### 12.1 採用風險降低：Reason Codes + Confidence/Evidence（不含行動建議）

本系統的最大系統風險之一是 **Host 採用**：警報是否可理解、是否形成信任閉環、以及是否會因誤報而迅速失去信任。為降低此風險，Phase 1 的 alert 應附帶「可被資料支持」的解釋輸出，但**不**在本文件中定義 Host 的具體應對行動（由業務端決定或現場判斷）。

**核心原則（必須遵守）**：
- 解釋只能是**行為描述**，必須能由輸入特徵與模型決策支持。
- 禁止輸出「意圖揣測」型理由（例如「去吃午餐/去洗手間」），因為目前資料（`t_bet`/`t_session`/`t_game`）無法可靠觀測此類意圖，會傷害信任。

**輸出形態（建議最小集合）**：
- **Confidence**：`score`、`threshold`、以及 margin（`score - threshold`）。
- **Evidence**：top-k 影響特徵（例如 3–5 個）及其數值（例如 `bets_last_5m=0`, `loss_streak=6`），供 Host/監控/事後回溯使用。
- **Reason codes（Plan B；首選）**：基於模型解釋（例如 LightGBM + SHAP）的 top 影響特徵，映射到少量穩定的 reason code。

**Reason codes（示意；以 bet-only 特徵為主）**：
- `BETTING_PACE_DROPPING`：近期下注頻率/節奏下降（例如 `bets_last_5m`, `bets_per_minute` 驅動）
- `GAP_INCREASING`：下注間隔變長、接近 walkaway 節奏（由近期窗口統計/節奏特徵驅動）
- `LOSS_STREAK`：連輸或近期輸損壓力（由 `loss_streak` 或近期輸贏相關特徵驅動）
- `LOW_ACTIVITY_RECENTLY`：近期活動顯著偏低（短窗口 counts/wager 低）

**穩定性護欄（避免 reason 亂跳）**：
- reason code 必須由**固定的「特徵 → reason」映射表**產生，且映射表需版本化（隨 `model_version` 管理）。
- 模型服務（`/score` 端點）在每次推論時都**必須輸出**當下的 `reason_codes`、`score` 與 `margin`，不在模型層做「連續輪詢一致才輸出」的過濾。
  > **展示穩定性（產品／前端責任）**：是否只在連續兩輪 reason 一致時才對 Host 展示、或設定通知冷卻期，屬於前端／產品設計決策（`trainer/frontend/` 及相關 UX 邏輯），不在本模型工程規格範圍內。模型輸出需提供充足的 metadata（每次的 `reason_codes`、`score`、`margin`、`model_version`、`scored_at`），讓前端依產品需求決定呈現策略。

### 12.2 持續運營（Scope）：監控 → 校準 → 重訓

本專案是一個 online decision system，不是一次性模型交付。即使 Phase 1 上線，也必須將以下閉環升格為產品生命週期的一部分（此處不規定頻率與實作細節）：
- **監控**：資料分佈與特徵漂移、警報量、precision/recall proxy、reason code 分佈、以及線上 validator 結果。
- **校準**：依業務容量與季節性調整閾值/ gate（仍遵守 §10.2 的策略框架）。
- **重訓**：在 drift 或策略變更時更新模型與特徵集合，並以 `model_version` + `/model_info` 管理演進。

---

## 13. 開放問題與決策點 (Open Questions)

目前尚未定案、需要與業務端協商並在 planning 中落地的問題：
1. **目標函數協商（未決）**：具體的 Precision/Recall 權衡底線為何？每班公關可處理多少警報（目標警報量範圍）？
2. **Phase 1 Gate 選擇（未決）**：在 §10.2 的策略選項中，選定哪一個 gate 作為 Phase 1 的上線標準（建議預設採 **G1**，除非業務端明確要求其他 gate）。

---

## 14. 未來方向 (Future Directions)

1. **AutoML 集成探索（Phase 2）**：引入 **FLAML** 或其他 AutoML 框架，在 Phase 1 的 LightGBM 基礎上探索 stacking/blending 集成，並支援時間序列感知的 CV 與自訂指標。僅在 Phase 1 模型穩定上線且具備基準指標後啟動。
2. **序列嵌入整合（Phase 2–3）**：依 §8.4 分期策略，在基礎模型驗證後進行離線 SSL 預訓練，產出 Player/Run Embedding 並評估增益。
3. **Game 表整合**：納入牌局層級（開牌結果、桌台走勢、同局玩家數等氣氛特徵）上下文。
4. **跨桌造訪建模**：建模玩家在桌台間移動的旅程。
5. **模型重訓機制**：監控 concept drift 並確立重訓頻率。
6. **警報疲勞管理（產品／前端 scope）**：冷卻期設計、公關工作負荷平衡與通知節流策略，屬於產品與前端（`trainer/frontend/`）的決策範疇，不在本模型訓練計畫規格內。模型層僅保證每次推論誠實輸出分數與 reason codes，頻率與呈現方式由產品端定義。

---

## 附錄 A：參考文件關係

| 文件 | 角色 |
|---|---|
| `doc/FINDINGS.md` | 資料品質發現與可重現的 SQL 驗證。資料問題的事實來源。 |
| `schema/GDP_GMWDS_Raw_Schema_Dictionary.md` | 表格的完整綱要字典。 |
| `doc/TRAINER_SUMMARY.md` | 現有 trainer 系統架構與模組摘要。 |
| `doc/TRAINER_ISSUES.md` | 過去 trainer 實作的問題日誌（TRN-*）。 |
| `doc/DECISION_LOG.md` | 關鍵架構/策略決策的記錄與理由（DEC-*）。 |
| `trainer/` | 現有（第一代）程式碼。 |
