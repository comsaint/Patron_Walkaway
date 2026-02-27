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

---

## 5. 關鍵資料品質護欄 (P0 必處理)

以下問題（詳載於 `doc/FINDINGS.md`）**必須**在建模前處理，否則將污染標籤與特徵：

1. **Session 去重 (FND-01)**：` ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY lud_dtm DESC NULLS LAST, __etl_insert_Dtm DESC) = 1`。必須 SELECT 此二欄位才能執行。
2. **人工帳務調整排除 (FND-02)**：加入 `is_manual = 0` 過濾條件。
3. **casino_player_id 空值清洗 (FND-03)**：`CASE WHEN lower(trim(casino_player_id)) IN ('', 'null') THEN NULL ELSE trim(casino_player_id) END`。
4. **Session 狀態處理 (FND-04)**：**不要**過濾 `status = 'SUCCESS'`。保留所有非人工且 `turnover > 0` 或 `num_games_with_wager > 0` 的 Session。
5. **is_known_player 不可靠 (FND-09)**：絕對不要使用此旗標。一律檢查 `casino_player_id IS NOT NULL`。
6. **bet_reconciled_at 不可用 (FND-06)**：100% 無效值。視為不存在。
7. **Game 表去重 (FND-14)**：使用 `MAX(__ts_ms)` 或 `MAX(__etl_insert_Dtm)` 去重。
8. **全空欄位 (FND-08)**：`bonus`, `tip_amount`, `increment_wager`, `payout_value` 100% 為 NULL，勿使用。

---

## 6. 玩家身份歸戶 (Identity Resolution)

### 6.1 多對多映射問題 (FND-11)
`player_id`（生物辨識）與 `casino_player_id`（會員卡）之間存在**雙向多對多**關係。錯誤分組會切斷 Session 鏈結，產生假的離場標籤（TRN-03）。

### 6.2 ★ 決策點：歸戶策略 (SSOT)
本專案將此「決策點」升格為**策略選項**（含首選與 fallback），以最大化利用 `t_bet` + `t_session` 的訊號，同時降低因身份斷鏈造成的標籤污染與歷史輪廓破碎（FND-11 / TRN-03）。

**本專案首選策略：採用【D2】Canonical ID 歸戶。**

#### 【D2】Canonical ID 歸戶（首選）

**目標**：建立延遲更新的 mapping（`player_id -> canonical_id`，優先 `casino_player_id`），使「同一位有卡客」即使因生物辨識斷鏈重發 `player_id` 或換桌/跨日，也能把其 bet/session 串接到同一個穩定身份下。

**canonical_id 定義（身份優先序）**：
- 若 `casino_player_id` 為有效字串（需先做 FND-03 清洗：把 `'null'`/空字串視為 NULL）→ `canonical_id = casino_player_id`
- 否則（無卡客）→ `canonical_id = player_id`

**mapping 建置（離線增量 job；資料來源以 `t_session` 為主）**：
- 對 `t_session` 套用 FND-01 去重、FND-02 排除人工帳務、FND-03 清洗卡號。
- 抽取所有同時具備 `player_id` 與有效 `casino_player_id` 的 session 作為連結邊：`player_id ↔ casino_player_id`。
- 維護一張可查詢的 `player_id -> canonical_id` 映射表（每日/每小時增量更新均可；依實務延遲需求）。

**M:N 衝突處理（子決策；必須明文固定以避免訓練/服務不一致）**：
- **情境 1：同一 `casino_player_id` 對應多個 `player_id`（斷鏈重發）**：所有這些 `player_id` **都必須**映射到同一 `canonical_id = casino_player_id`。
- **情境 2：同一 `player_id` 對應多個 `casino_player_id`（換卡）**：必須選定一個卡號作為該 `player_id` 的歸屬。建議採用「**最近一次**」規則：以最新出現（例如最大 `session_end_dtm`/`lud_dtm`）的 `casino_player_id` 為準；並在離線報表中列出受影響清單以便稽核。

**Phase 1 對 MVP 的影響**：
- 有卡客的跨日/跨桌歷史輪廓（§8.2.E）將以 `canonical_id` 聚合，避免輪廓被 `player_id` 斷鏈切碎。
- 標籤與回測的分組鍵也應使用 `canonical_id`（對有卡客等同 `casino_player_id`；對無卡客等同 `player_id`），以減少 TRN-03 類假正例。

#### 【D1】純 `player_id` 歸戶（fallback）

**何時使用**：若 Phase 1 期間無法在可接受成本內完成 D2 mapping 的建置與線上查詢，則暫時採 D1 以確保 MVP 可上線。

**作法**：訓練與推論都以 `player_id` 作為唯一玩家鍵，直接串接其 bet 序列。

**已知代價（必須在 Phase 1 風險承擔中明列）**：
- 有卡客可能因 `player_id` 斷鏈重發而使歷史輪廓破碎，並造成標籤/間隔計算偏差（FND-11 / TRN-03）。
- 需要在評估報告中量化受影響比例與對指標的影響，作為升級至 D2 的商業 justification。

### 6.3 假帳號排除 (FND-12)
排除 `session_cnt = 1 AND total_games <= 1` 的假帳號 ID（極可能是 CRM 生成的過客/伴遊 ID）。

### 6.4 即時身份判定邏輯 (若採 D2)
推論時判斷有卡/無卡客：
1. 當前投注的 `session_id` 是否連結到一個已入湖且具 `casino_player_id` 的 `t_session`？
2. 檢查該 `player_id` 是否在任何**先前已完成的** Session 中曾關聯過卡號。
3. 兜底：視為無卡客。

---

## 7. 標籤設計 (Label Design)

### 7.1 標籤定義（防洩漏與右截尾）
Walkaway ground truth 以 `t_bet` 的下注時間序列定義（不依賴 `t_session` 的結束資訊）。
對每位玩家的下注序列 $\{b_i\}$：
1. 若 $b_{i+1} - b_i \ge X$，則 $b_i$ 為一個 **gap start**。
2. 對任一下注樣本時間 $t=b_j$，若存在 gap start $s=b_i$ 使 $s \in [t, t+Y]$，則標記為 `label = 1`（表示可在 $Y$ 分鐘內預警）。否則為 0。

**注意（未來資訊護欄）**：本標籤定義會使用「下一筆下注時間 $b_{i+1}$」等未來資訊來判斷 gap start。這些未來資訊**僅可**用於標籤建構/離線驗證；任何由此衍生的量（例如距離 gap start 的分鐘數、距離下一筆下注的分鐘數）**絕不可**作為模型特徵。

### 7.2 ★ 決策點：右截尾處理 (Right-censoring)
資料窗口末端若缺少未來下注資訊（例如玩家在窗口內最後一筆下注之後的行為落在窗口之外），就無法可靠判斷其是否進入 walkaway gap。若把這類未知直接當作離場，會膨脹正例（TRN-06）。

**本專案決策：採用【C1】延伸拉取（Extended pull）。**

- **【C1】延伸拉取（採用）**：抽取資料時把 window_end 往後延伸至少 $X+Y$（例如額外 1 天）只用於算標籤（用來觀測下一筆下注間隔），但不把延伸區間的樣本納入訓練集。
- **【C2】緩衝區（不採用）**：丟棄 window_end 前 $X+Y$ 分鐘內的樣本（不標註、不訓練）。

---

## 8. 特徵工程策略 (Feature Engineering)

### 8.1 設計原則
- **禁止未來洩漏 (No future leakage)**：線上推論可得的特徵，訓練才可以用。
- **訓練-服務一致性 (Train-Serve Parity)**：特徵計算與 rolling window 邊界在訓練與服務端必須完全一致（TRN-08）。

### 8.2 特徵類別
* **A. 當前投注特徵 (`t_bet`)**：`wager`, `payout_odds`, `base_ha`, `bet_type`, `is_back_bet`, `position_idx`。
* **B. 當前連續投注段（當前「run」）內累積特徵（`t_bet`）**：`cum_bets`, `cum_wager`, `avg_wager_sofar`, `minutes_since_run_start`, `bets_per_minute`, `loss_streak`（需修復 TRN-09 bug）。其中 `minutes_since_run_start` **不得依賴** `t_session.session_start_dtm`（該欄位在 `t_session`，且 `t_session` 可能延遲入湖），而應以線上可得的 `t_bet.payout_complete_dtm` 為基礎，使用狀態/快取記錄「本 run 的第一筆下注時間」來計算。
* **C. 滾動窗口特徵 (`t_bet` 跨 Session)**：過去 5/15/30 分鐘投注次數、10/30 分鐘投注金額。
* **D. 時間上下文**：`time_of_day_sin/cos`。
* **E. 歷史賭客特徵（有卡客，來自已完成的 `t_session`）**：`hist_session_count`, `hist_avg_bet`, `hist_win_rate` 等。
* **F. 牌局/氛圍特徵（來自 `t_game`；後期階段）**：例如同一局/同一桌的玩家數（「氣氛/擁擠度」）、同局總下注量、桌台近期走勢等。此類特徵在本專案**後期**才整合（見 §8.3 決策與 §14 未來方向）。

### 8.3 ★ 決策點：Session 與桌台特徵線上可用性 (S1/S2)
`table_hc`（同桌人數）目前以 `session_end_dtm` 重建，存在未來資料洩漏（TRN-05）。
本專案將此「決策點」升格為**策略選項**（含首選與 fallback），以避免在 Phase 1 引入任何依賴 `t_session.session_end_dtm` 的未來資料洩漏風險（TRN-05）。

**本專案決策：採用【S1】保守路線。**

#### 【S1】保守（採用）
- 線上完全不使用 `session_end`（或任何「距離 session_end」類）訊號。
- `table_hc` 以 `t_bet` 即時計算替代：例如「過去 N 分鐘內，在同一 `table_id` 有投注的不重複 `player_id` 數」。此方式完全即時、可在訓練與服務端保持一致。

#### 【S2】事件時間模擬（不採用；後期才考慮）
- 定義 session 欄位 `available_time`，訓練/回測/推論都嚴格遵守「到可用時間才可使用」限制。
- 僅在 Phase 1 穩定上線且監控齊備後，才評估是否值得引入此工程複雜度。

**關於 `t_game` 的補充決策（分期實作）：**
- **Phase 1（本期）**：不使用 `t_game` 產生的牌局/氛圍特徵，先以 `t_bet`（以及必要時 `t_session` 的歷史輪廓）完成可上線的基礎模型。
- **Phase 2（後期）**：整合 `t_game`，打造「桌台氣氛」類特徵（例如同局玩家數、同局總下注等），再用離線回測驗證增益後納入正式模型。

### 8.4 特徵工程路線比較與分期策略

除了 §8.2 的「手工聚合特徵 + 表格模型」路線外，另一條成熟的路線是**事件序列嵌入 (Event Sequence Embedding)**：將每筆下注視為一個帶有多維上下文的事件，用序列模型（GRU / Transformer）學習多層級嵌入向量（bet → run/session → player），再用嵌入做下游分類。近年在金融交易、電信 churn、反詐等領域已有成熟落地案例（PyTorch-Lifestream [IJCAI 2025]、CASPR [Microsoft]、TransactionGPT 等）。

**為何序列嵌入值得考慮——直觀範例：**
- **玩家 A**：連贏 5 把小注 → 輸 1 把大注。
- **玩家 B**：輸 1 把大注 → 連贏 5 把小注。

在傳統聚合特徵（「過去 6 把平均下注與勝率」）中，兩者看起來一模一樣。但玩家 A 可能正處於挫敗即將離場，而玩家 B 正在回本、情緒完全不同。序列嵌入能捕捉這種節奏與順序的微妙差異。

#### 兩條路線的全面比較

| 面向 | 路線 A：手工聚合特徵 + GBDT | 路線 B：事件序列嵌入 + 深度學習 |
|---|---|---|
| **訊號表達力** | 較弱。聚合統計丟失事件順序與節奏變化。 | 強。天然捕捉序列模式、時間間隔動態、行為節奏漸變。 |
| **特徵工程成本** | 高。需領域專家手動設計、逐一驗證 train-serve parity。 | 低（理論上）。但需設計 tokenization schema 與事件編碼。 |
| **可解釋性** | 高。Feature importance / SHAP 直觀，Host 和管理層容易理解「為何警報響了」。 | 低。黑箱；需額外解釋工具，業務推廣有阻力。 |
| **即時部署難度** | 低。LightGBM < 1ms，特徵可增量更新/快取。 | 中。Forward pass ~幾 ms 可接受（45 秒輪詢週期內綽綽有餘），但序列狀態管理、模型版本控制、回滾策略更複雜。 |
| **洩漏風險** | 已知且可控（已有 TRN-\* 護欄）。 | **更高**。序列切窗、padding、mask/label 定義引入新的洩漏陷阱，工程護欄需更嚴格。 |
| **監控複雜度** | 成熟。特徵分佈、重要性、閾值等標準做法。 | **新維度**。需監控 embedding drift、序列長度漂移、表徵崩壞 (representation collapse) 等。 |
| **冷啟動 / 無卡客** | 可用（靠近期窗口），但跨日人格化弱。 | 短序列仍可產出嵌入但品質較差；無卡客跨日身份不穩限制 player embedding 長期價值。 |
| **與現有系統契合** | 高。現有 scorer 輪詢 + 狀態快取天然支持。 | 中。需新增嵌入生成/快取/版本管理機制。 |
| **迭代速度** | 快。加特徵 → 重訓 → 看結果，循環短。 | 慢。架構調整、預訓練 + 微調流程更長。 |
| **學術成熟度** | 非常成熟。GBDT 在表格資料 benchmark 仍持平或優於 DL（NeurIPS 2022、ICLR 2025）。 | 快速成熟中（CASPR、PyTorch-Lifestream，2024–2025），但 casino 行業落地案例極少。 |

#### 本專案決策：分期混合策略 (Phased Hybrid)

| Phase | 做什麼 | 目標 | 備註 |
|---|---|---|---|
| **Phase 1（本期）** | 手工特徵 A–E + LightGBM | 上線 MVP，打通 pipeline，用高可解釋性建立業務信任。 | 對應 §8.2 與 §9。 |
| **Phase 2（基礎模型驗證後）** | 離線 SSL 預訓練序列嵌入（**僅用 `t_bet`**，避開 `t_session` 延遲問題），產出 Player/Run Embedding（如 64 維向量），**每日批次更新**，推論時當作靜態特徵餵進 GBDT。 | 量化嵌入對 PR-AUC / F-beta 的增益；若有效則長期作為正式方案。 | 此做法避開「即時跑 NN forward pass」的工程風險，把序列模型的增益以最低成本注入現有架構。 |
| **Phase 3（僅在 Phase 2 增益不足時）** | 端到端即時序列模型線上化。 | 完全捕捉即時序列動態。 | 需更嚴的延遲/可靠性/監控與回滾策略；不是必然的演進方向。 |

### 8.5 自動特徵工程探索

計畫使用 `tsfresh` 或 `Featuretools` 探索時間序列候選特徵（滾動、延遲、聚合運算），但產出的候選特徵仍必須通過 §8.1 的護欄（無洩漏、線上可重現），最終以 §8.2 的特徵架構/命名/計算模組落地。不符合者不納入。

---

## 9. 建模方法 (Modeling Approach)

### 9.1 雙模型架構
- **有卡客模型**：使用特徵 A–E。包含歷史賭客輪廓，能利用過去行為模式。
- **無卡客模型**：純粹依靠**當前與近期投注行為**（特徵 A–C），不使用任何來自 `t_session` 的歷史輪廓或 Session 級欄位。
分開模型可避免歷史特徵被大量 NaN 掩蓋，並允許對不同群體分別校準閾值。

### 9.2 演算法與切割
- **基準方案**：LightGBM (支援 `class_weight='balanced'` 處理類別不平衡)。
- **探索方案**：AutoML（需支援時間序列感知的 CV 與自訂指標）。
- **切割方式**：**嚴格按時間切割**（Train 60-70% | Valid 15-20% | Test 15-20%）。不可隨機切分。

### 9.3 Model API Contract（與 `scorer.py` 的介面）

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
  - `doc/model_api_protocol.md` 中列出的特徵（例如 `minutes_since_session_start`, `minutes_to_session_end` 等）代表的是**當前實作狀態的快照**，**不是永久固定的合約**。
  - 新版模型的 **實際特徵集合** 由訓練時的 `FEATURES` 決定，並透過 `/model_info.features` 對外公開；App 端以此為準同步更新 `scorer.py` 的特徵計算邏輯。
  - 在設計新特徵時仍須遵守 §8.1 的護欄（無洩漏、線上可重現）。若現有 API 中某些欄位（如 session-based 欄位）與這些原則衝突，可以在後續 API 版本中調整定義或移除，搭配 `model_version` 與 `/model_info` 一併演進。

---

## 10. 評估與閾值選擇 (Evaluation & Validation)

### 10.1 指標
- **精準度 (Precision)**：核心 KPI。
- **Alert Volume / Coverage**：必要約束，每天需要有足夠的警報量。
- 輔助：PR-AUC, F-beta。

### 10.2 閾值選擇策略 (Thresholding)
現況閾值選擇過度偏向 precision 導致幾乎無警報（TRN-11）。必須引入約束：
1. 定義最低召回率下限（如 ≥ 5-10%）。
2. 或定義最小警報量（每小時全場至少 5-20 個）。
3. 在滿足約束的前提下最大化 Precision 或 F-beta。

#### Phase 1 上線 Gate 與閾值策略（策略選項）

本專案將「閾值策略」升格為**策略選項**（含首選與 fallback），以確保 Phase 1 的成功標準對齊「業務可用」，而非僅追求單一 ML 指標。

**共同前提（不論選哪一種 gate）**：
- gate 與閾值選擇只能在 **validation set** 上完成；test set 僅用於最終報告。
- 必須同時報告：Precision / Recall / PR-AUC / 每小時警報量（或每班警報量）。
- 閾值確定後，在 §10.3 的回測框架下模擬線上輪詢與去重規則，確認警報量與準確率在營運上可接受。

#### 【G1】Precision 下限 + 最小警報量（首選）

**何時使用**：Phase 1 目標是建立 Host 信任並確保「有足夠可行動的警報量」，此 gate 最直接對齊業務需求（Precision over recall + Minimum actionable volume）。

**規則（示意，數字需由業務端給定）**：
- 約束 1：Precision ≥ \(P_{\min}\)（例如 0.70–0.85）
- 約束 2：警報量 ≥ \(V_{\min}\)（例如全場每小時 ≥ 5–20；或每班 ≥ K）
- 在滿足兩者的閾值集合中，選擇能最大化 Recall 或 F\(_\beta\)（\(\beta<1\)）者。

#### 【G2】最低 Recall 下限 + 最大化 Precision（fallback）

**何時使用**：若業務端能先給出「最低覆蓋率」需求（例如至少抓到 X% 的離場），且警報量對營運並非主要限制，可用此 gate。

**規則（示意）**：
- 約束：Recall ≥ \(R_{\min}\)（例如 5–10%）
- 在滿足 recall 下限的閾值集合中，選 Precision 最高者（或最大化 F\(_\beta\)）。

#### 【G3】Precision 下限 + 最大化觸達（第二備援）

**何時使用**：若業務端強烈要求「不要亂報」，但也希望在安全的 precision 範圍內盡可能多抓到可挽留機會，可採此 gate。

**規則（示意）**：
- 約束：Precision ≥ \(P_{\min}\)
- 在滿足 precision 下限的閾值集合中，最大化警報量或 Recall（等價於最大化觸達）。

### 10.3 線上 ground truth 與回測
- 線上驗證：以 `trainer/validator.py` 的 45 分鐘 horizon 驗證語義為準（覆蓋 Y+X 並允許延遲）。
- 回測時須按時間順序處理，套用每次造訪最多 1 個警報的去重邏輯。

---

## 11. 服務架構與訓練-服務一致性

現有的 `scorer.py` 採定期輪詢（約 45 秒）。
強制執行以下一致性（Train-Serve Parity）：
1. **滾動窗口邊界**：須使用一致的包含/不包含邏輯（修正 TRN-08）。
2. **特徵程式碼**：抽取為共用模組 `features.py` 供 trainer 與 scorer 共同匯入。
3. **快取策略**：加入快取元資料（日期範圍、雜湊）驗證，不符時強制失效（修正 TRN-07）。

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
- 可選：僅在同一賭客連續兩個輪詢週期輸出一致 reason 時才展示（降低抖動）。

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

1. **序列嵌入整合（Phase 2）**：依 §8.4 分期策略，在基礎模型驗證後進行離線 SSL 預訓練，產出 Player/Run Embedding 並評估增益。
2. **Game 表整合**：納入牌局層級（開牌結果、桌台走勢、同局玩家數等氣氛特徵）上下文。
3. **跨桌造訪建模**：建模玩家在桌台間移動的旅程。
4. **模型重訓機制**：監控 concept drift 並確立重訓頻率。
5. **警報疲勞管理**：實作冷卻期與公關工作負荷平衡。

---

## 附錄 A：參考文件關係

| 文件 | 角色 |
|---|---|
| `doc/FINDINGS.md` | 資料品質發現與可重現的 SQL 驗證。資料問題的事實來源。 |
| `schema/GDP_GMWDS_Raw_Schema_Dictionary.md` | 表格的完整綱要字典。 |
| `doc/TRAINER_SUMMARY.md` | 現有 trainer 系統架構與模組摘要。 |
| `doc/TRAINER_ISSUES.md` | 過去 trainer 實作的問題日誌（TRN-*）。 |
| `trainer/` | 現有（第一代）程式碼。 |
