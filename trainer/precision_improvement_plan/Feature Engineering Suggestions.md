# Feature Engineering Suggestions — Patron Walkaway Predictor

> 文件層級：特徵工程建議（Feature Engineering Suggestions）
> 來源：基於 `trainer/feature_spec/features_candidates.yaml`、現有 `trainer/` 管線與 `schema/GDP_GMWDS_Raw_Schema_Dictionary.md` 的補充建議。
> 術語說明：本文件使用 **run** 或 **visit** 描述「玩家連續下注流程（前後間隔 < 30 分鐘）」，不使用 session（session 在 smart table 資料中以玩家 × 桌台為邊界，換桌即產生新 session，語意不同）。
> 非目標：不取代 `features_candidates.yaml` 的 SSOT 地位；實際納入需經 feature screening、train-serve parity 與資料契約驗證。
> 文件語意：未來可將 `track_human` + `track_llm` 在文件層合併理解為 **short_term_dynamics**，但目前 repo 仍保留既有 track 命名以維持 trainer / scorer / backtester / artifact 相容。`compute_backend`、`materialization_stage` 等 metadata 方向可接受，但在尚無 consumer 前**不建議**先寫死進 YAML spec。

**Metadata**

- 建立日期：2026-04-22
- 最近更新：2026-04-22
- 文件狀態：Working draft / discussion-ready

本文件的目的，是把「值得考慮但尚未納入 SSOT」的特徵工程想法，整理成一份可討論、可篩選、可追溯的候選清單，供後續 feature screening、implementation planning 與 execution planning 參考。它不直接定義最終要上線的特徵，而是幫助團隊辨識哪些訊號已可由現有 schema 支撐、哪些想法需要額外資料表、以及哪些候選雖然有價值，但在落地上仍需補齊 parity、DQ 或 pipeline 契約。

內容上，本文件分成三類：A 類是可由現有 bet / profile 資料直接延伸的候選；B 類是已由現有 schema 支撐、但偏向桌況 / context 的 first-wave 候選；C 類則是需要額外資料表或事件資料才能成立的後續方向。每一類除了列出 feature 想法，也會註記其資料來源、主要風險、落地阻力與目前建議優先序，避免把概念清單誤讀成可以直接搬進 `features_candidates.yaml` 的最終規格。

---

## 類別 A：現有 bet / profile 資料可直接延伸

### A1. Run 內即時 P&L（優先度：🔴 最高）

現有特徵已有 `wager_sum_in_run_so_far` 與 `bets_in_run_so_far`，但缺少損益維度。Run 內累計損益是最直接的離場觸發因子，與 `loss_streak`（序列訊號）互補。

**建議先明確定義中介欄位**

- `player_net_win = -casino_win`
- 其後所有 run-level P&L 衍生特徵都從 `player_net_win` 出發，避免賭場視角 / 玩家視角混淆。

| feature_id | 說明 | 衍生邏輯 |
|---|---|---|
| `net_win_in_run_so_far` | 當前 run 至今的累計淨損益 | `cumsum(player_net_win)` within current run |
| `net_win_per_bet_in_run` | 當前 run 每注平均損益（標準化 run 長度差異） | `net_win_in_run_so_far / bets_in_run_so_far` |
| `run_loss_acceleration` | 最近 5 筆 vs 整個 run 的平均每注損益（損失加速指標） | `net_win_w5bets / net_win_per_bet_in_run` |

**DQ / contract（A1）**

- `net_win_per_bet_in_run` 可為 0（run 內累計損益剛好為 0）。`run_loss_acceleration` 分母為 0 時**不可默默當成有限數值**：需在候選進 screening 前統一約定（例如回 `NaN`、或回 `0` 並在 spec 註明、或加 epsilon / winsorize），且 trainer / scorer / backtester 必須同一契約。

### A2. 注碼趨勢方向（優先度：🔴 最高）

現有特徵能看到注碼水平與波動，但看不到**方向性**。減注趨勢與下注頻率下降通常同時出現，但也有只減注不減頻的玩家，分開捕捉更精確。

| feature_id | 說明 | 衍生邏輯 |
|---|---|---|
| `wager_slope_w10bets` | 最近 10 筆注碼的線性趨勢斜率（負值 = 減碼中） | 對最近 10 筆 `wager` 做 OLS，取斜率 |
| `wager_w5m_over_w15m` | 最近 5 分鐘 vs 15 分鐘平均注碼比（< 1 = 收縮中） | `wager_avg_w5m / wager_avg_w15m` |
| `wager_deceleration` | 注碼斜率的二階差分（斜率本身是否在變陡） | `wager_slope_w10bets` 的 LAG 差分 |

### A3. PUSH 結果與「非 WIN 連續計數」（優先度：🟡 中）

現有特徵把 PUSH 隱含納入分母但未獨立捕捉。在百家樂中，連續 PUSH 是「卡住了」的特有心理狀態，有別於輸和贏。

| feature_id | 說明 | 衍生邏輯 |
|---|---|---|
| `push_cnt_w15m` | 過去 15 分鐘 PUSH（和局）次數 | `SUM(CASE WHEN status='PUSH' THEN 1 ELSE 0 END)` w15m |
| `consecutive_non_win_cnt` | 從最近一次 WIN 到現在，連續非 WIN（LOSE + PUSH）筆數 | Python vectorized，類似 `loss_streak` 但 PUSH 不重置 |
| `non_win_rate_w15m` | 過去 15 分鐘非 WIN 率（含 PUSH） | `(lose_cnt_w15m + push_cnt_w15m) / bets_cnt_w15m` |

### A4. Run 時長相對個人歷史基線（優先度：🟡 中）

這類特徵值得做，但**不再視為低阻力**。它們比較準確的分類是：**小型 pipeline 擴充**。主要成本不在單一 join，而在 trainer / scorer / backtester parity、缺值與零分母契約、以及 screening 是否把 cross-feature 視為正常候選。

| feature_id | 說明 | 衍生邏輯 |
|---|---|---|
| `run_duration_vs_personal_avg` | 當前 run 時長 / 玩家歷史平均 run 時長 | `minutes_since_run_start / avg_run_duration_min_30d` |
| `run_duration_vs_personal_p75` | 當前 run 時長 / 玩家歷史 run 時長 P75 | `minutes_since_run_start / p75_run_duration_min_30d` |
| `bets_in_run_vs_personal_avg` | 當前 run 下注筆數 / 玩家歷史每次 visit 平均筆數 | `bets_in_run_so_far / (num_bets_sum_30d / visits_30d)` |

### A5. 下注頻率相對個人歷史基線（優先度：🟡 中）

現有 `wager_recent_vs_session_avg` 在金額維度做了相對化，但頻率維度尚缺。這類特徵能分辨「這個人本來就下得慢」與「這個人今天比平常慢很多」，但同樣屬於**小型 pipeline 擴充**而非純 YAML 填空。

| feature_id | 說明 | 衍生邏輯 |
|---|---|---|
| `pace_vs_personal_baseline` | 當前下注頻率（bets/min）/ 玩家歷史平均下注頻率 | `(bets_cnt_w15m/15) / (num_bets_sum_30d / active_days_30d / avg_run_duration_min_30d * 60)` |
| `pace_drop_vs_personal_history` | 當前 pace_drop_ratio / 玩家歷史平均 pace_drop_ratio | `pace_drop_ratio / avg_pace_drop_ratio_30d` |

---

## 類別 B：First-wave table/context candidates（已有 schema、定義清楚、不需新資料表）

本類的門檻是：

- 現有 schema 已支撐（以 `t_game` / `t_bet` 為主）
- 不需要新增資料表
- 定義相對清楚，可先進候選池做 screening

### B1. Outcome regime / crowd context（優先度：🔴 最高）

原始構想中的 `table_win_rate_w15m` **不建議保留**。百家樂同一局不同玩家可押不同邊，bet-level 勝率聚合容易變成「玩家押邊分佈」，不是桌況冷熱。

建議改從 `t_game.outcome` 與 `t_game.num_players` 出發：

| feature_id | 說明 | 主要來源 / 衍生邏輯 | 備註 |
|---|---|---|---|
| `current_outcome_streak_len` | 當前連莊 / 連閒 / 連和長度 | 依 `t_game.outcome` 做連續結果計數 | 優先序最高；需明確定義 `VOID` / `UNRESOLVED` 是否跳過或中斷 |
| `banker_rate_w20games` | 最近 20 局開莊比例 | `COUNT(outcome='BANKER') / 20` | 與 streak 互補 |
| `player_rate_w20games` | 最近 20 局開閒比例 | `COUNT(outcome='PLAYER') / 20` | 與 streak 互補 |
| `tie_rate_w20games` | 最近 20 局和局比例 | `COUNT(outcome='TIE') / 20` | 建議排在 banker/player rate 後 |
| `patron_is_sole_player` | 玩家是否獨自在桌 | `t_game.num_players == 1` | 比 `table_hc_w5m == 1` 更直接 |
| `table_num_players` | 當前桌面玩家數 | `t_game.num_players` | 可直接作 raw context |
| `table_num_players_w5m_over_w15m` | 桌面人數趨勢（短窗 / 中窗） | 以 `t_game.num_players` 做 w5m / w15m 聚合比 | 比「誰離桌了幾個」更穩 |

### B2. Table turnover / table share（優先度：🔴 高）

這類特徵的核心不是單純看桌上注碼大小，而是看桌面活躍度與目標玩家是否開始**脫離桌上節奏**。

| feature_id | 說明 | 主要來源 / 衍生邏輯 | 備註 |
|---|---|---|---|
| `table_net_outcome_w15m` | 整桌過去 15 分鐘累計淨輸贏 | 建議優先用 `t_game.casino_win` 聚合，玩家視角可取 `-casino_win` | 需明寫 `RESOLVED` / 去重 contract |
| `table_turnover_w5m_over_w15m` | 桌面活躍度短窗 / 中窗比 | 以 `t_bet.wager` 聚合 `w5m / w15m` | 建議加最小活動門檻，避免冷桌比值亂跳 |
| `patron_share_of_table_turnover_w15m` | 目標玩家佔整桌 turnover 比例 | `patron_wager_sum_w15m / table_wager_sum_w15m` | absolute 版先作獨立候選 |
| `patron_share_vs_personal_baseline` | 目標玩家桌面佔比 vs 個人歷史基線 | `patron_share_of_table_turnover_w15m / personal_avg_share` | personalized 版另立，不預設保留 |

**設計原則**

- `patron_share_of_table_turnover_w15m` 與 `patron_share_vs_personal_baseline` 代表**兩個不同假設**，應分開登錄為獨立候選。
- 不在設計階段先把兩者合併成複合公式，讓 screening / ablation 決定誰有增量訊號。

### B3. 已有 schema 支撐，但定義 / contract 較重（優先度：🟡 中）

以下項目**不需要新資料表**，但相比 B1/B2 更依賴明確 contract，因此建議作為 second-wave，而非第一波必上：

| feature_id | 說明 | 主要來源 / 衍生邏輯 | 主要風險 |
|---|---|---|---|
| `patron_rank_in_table_by_loss` | 目標玩家在同桌中的虧損排名 | 同桌玩家 run-level P&L 排名 | 需先定時間窗、身分鍵與 placeholder 處理 |
| `table_avg_bet_size_w15m` | 同桌其他玩家平均注碼 | 同桌其他玩家 `wager` 聚合 | 可能退化成高活躍戶 / 桌等級 proxy |
| `table_hc_trend` | 桌台人數趨勢 | `compute_table_hc` 已有基礎，待主路徑接線 | offline 可做，但仍需觀察 runtime / parity；**與 B1 `table_num_players_w5m_over_w15m` 語意接近**（皆為「桌面人數 / 擁擠度趨勢」），來源不同（`t_bet` 滾窗 unique players vs `t_game.num_players`），screening 時應擇一或並列比較，避免重複納入 |

**DQ / contract 備註（B1–B3 共用）**

- `t_game` 類特徵需明確定義 `game_id` 去重方式（取最新版本）。
- 需明確定義是否僅納入 `game_status='RESOLVED'`。
- `outcome` 中的 `VOID` / `UNRESOLVED` 不應默默混入 streak 與比例指標。

---

## 類別 C：需要額外資料表（請求優先序）

### C1. 玩家歷史 Visit/Run 層級摘要表（優先度：🔴 高）

**需要的資料表**：每次 visit 的摘要紀錄，包含最終 P&L、持續時間、起訖時間。

| feature_id | 說明 | 需要的欄位 |
|---|---|---|
| `prev_visit_pnl` | 上次 visit 的最終損益（追損動機） | 歷史 visit P&L |
| `cum_pnl_last_3_visits` | 最近三次 visit 累計損益（連輸疲乏感） | 歷史 visit P&L 序列 |
| `visit_loss_limit_est` | 個人歷史止損估算（歷史 visit 最終 P&L 的 P25） | 歷史 visit P&L 分佈 |
| `current_loss_vs_personal_limit` | 當前 run 虧損 / 個人止損估算（接近 1 = 高風險） | 上述兩者結合 |
| `chasing_indicator` | 追損指標：上次虧損 AND 間隔短 AND 當前注碼偏高 | `prev_visit_pnl` + `days_since_last_visit` + `wager` |
| `p75_run_duration_min_30d` | 個人歷史 run 時長 P75（供 A4 使用） | 歷史 run 時長序列 |
| `avg_run_duration_min_30d` | 個人歷史平均 run 時長（供 A4、A5 使用） | 歷史 run 時長序列 |

### C2. Dealer 換班 / 桌台事件資料表（優先度：🟡 中）

**需要的資料表**：dealer 換班紀錄、shuffle 事件時間戳、桌台開關紀錄。

| feature_id | 說明 | 需要的欄位 |
|---|---|---|
| `time_since_last_dealer_change` | 距上次換莊的分鐘數 | dealer 換班時間戳 |
| `bets_since_last_shuffle` | 上次洗牌後的下注筆數（百家樂節奏點） | shuffle 事件時間戳 |
| `patron_win_rate_with_current_dealer` | 與當前莊家的即時勝率（「衰莊」感知） | dealer ID + 換班時間戳 |
| `dealer_tenure_at_table` | 當前莊家已在此桌多久 | dealer 換班時間戳 |

### C3. 跳局（Skip）行為紀錄（優先度：🟡 中，視 smart table 能力而定）

**需要的資料表**：每局的「座位佔用 vs 實際下注」紀錄（若 smart table 有 presence sensor）。開始跳局不下注是非常強的離場前兆，但目前完全無法從下注記錄捕捉。

| feature_id | 說明 | 需要的欄位 |
|---|---|---|
| `skip_count_w10rounds` | 最近 10 局中跳過不下注的次數 | 每局的座位佔用紀錄 |
| `skip_rate_trend` | skip 頻率的上升趨勢 | 上述時間序列 |
| `rounds_since_last_skip` | 距上次跳局的局數間隔 | 上述時間序列 |

### C4. 玩家進出桌事件 / 桌台聚合快照（優先度：🟡 中）

**需要的資料表**：桌台層級聚合快照或玩家進出時間戳。這類資料可補齊現有 schema 尚未直接觀測到的「誰離桌了」與社交傳染訊號。

| feature_id | 說明 | 需要的欄位 |
|---|---|---|
| `num_players_left_table_w30m` | 過去 30 分鐘已離桌的玩家數 | 桌台玩家進出時間戳 |

---

## 請求額外資料表的建議優先序

| 優先序 | 資料表 | 預期影響 | 治理阻力 |
|:---:|---|---|---|
| 1 | 玩家歷史 Visit/Run P&L 摘要 | 🔴 高（個人化止損基準、追損指標） | 低（玩家自身資料） |
| 2 | Dealer 換班 / 桌台事件 | 🟡 中（環境觸發點） | 低（運營系統資料） |
| 3 | 每局座位佔用 vs 下注紀錄 | 🟡 中（跳局行為） | 中（需確認 smart table 能力） |
| 4 | 桌台玩家進出 / 聚合快照 | 🟡 中（社交傳染、離桌事件） | 中（需補事件語義） |

---

## 與現有 Repo 架構的對照

| 類別 | 文件語意 | 現有 repo 對照 | 落地方式 |
|---|---|---|---|
| A1–A3 | Short-term dynamics | 現行 `track_human` / `track_llm` | 新增 YAML candidate 或 `python_vectorized` function |
| A4–A5 | Short-term dynamics × player profile | 現行 cross-feature（需擴充） | 小型 pipeline 擴充；補 trainer / scorer / backtester parity |
| B1–B3 | Table / context within existing schema | 可由 `t_game` / `t_bet` 延伸 | 優先走現有 schema，不先要求新表 |
| C1 | Player profile expansion | `track_profile` 擴充 | 新增 `profile_column` candidates，需新 visit/run 摘要表 join |
| C2–C4 | Event / external context | 新資料來源 | 需新 `source_table` 或事件表 |

---

## 本次收斂結論（供後續更新實作 / 計畫文件時參照）

1. **A1 接受**：`player_net_win = -casino_win` 作為所有 run-level P&L 衍生的基礎欄位。
2. **B1 接受但重寫**：棄用 `table_win_rate_w15m`；桌況序列改走 `current_outcome_streak_len` → `banker/player_rate_w20games` → `tie_rate`。
3. **Turnover share 家族接受但保持可分解**：`patron_share_of_table_turnover_w15m` 先作 absolute 候選；personalized 版另立，不預設保留。
4. **Track 語意合併方向接受，但 metadata 暫不落 spec**：文件可先理解為 `short_term_dynamics / player_profile`；`compute_backend` / `materialization_stage` 僅在有 consumer 時再進 YAML。
5. **A4/A5 重新分級**：視為小型 pipeline 擴充，不再標成低阻力。
6. **補 first-wave table/context family**：至少納入 `current_outcome_streak_len`、`patron_share_of_table_turnover_w15m`、`patron_is_sole_player`、`table_num_players`、`table_num_players_w5m_over_w15m`、`table_net_outcome_w15m`、`table_turnover_w5m_over_w15m`。

---

*建立日期：2026-04-22*  
*術語：run / visit = 玩家連續下注流程（間隔 < 30 分鐘）；session = 玩家 × 桌台邊界（smart table 原始定義，本文件不使用）*  
*範圍：補充建議，不取代 `features_candidates.yaml` SSOT*