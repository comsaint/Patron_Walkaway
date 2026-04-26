# Long History Utilization via Compact Run Summary

> 文件層級：Implementation plan / feature engineering proposal  
> 目的：在不把多年 bet-level 明細全量丟進訓練、避免 OOM 與超長 runtime 的前提下，更有效利用較長歷史資料。  
> 立場：最近數月 bet rows 仍作為 supervised training 主窗；更舊資料優先壓縮成 PIT-safe historical state、player baseline 與 validation evidence。

---

## 1. 背景

目前常用訓練方式只取最近數月，例如：

```bash
python -m trainer.trainer --use-local-parquet --start 2026-01-01 --end 2026-04-02
```

這是合理的資源保護策略，因為直接擴大 bet-level training window 會帶來三個風險：

- **記憶體風險**：多年 bet rows 進 Step 7 / Step 9 特徵矩陣，容易在 laptop 上 OOM。
- **時間風險**：Track LLM / feature screening / GBM bakeoff 的 runtime 會隨 row count 快速放大。
- **模型風險**：舊資料可能來自不同營運 regime、桌況、玩家組成或資料品質狀態，直接加入 supervised rows 可能稀釋最近窗 field-test objective。

因此建議採用「**短窗訓練 rows + 長窗壓縮特徵**」策略。

---

## 2. 核心建議

保留最近 3 個月左右的 bet-level rows 作為主要 supervised training population，並將更久以前的資料預先聚合為小而穩定的 historical artifacts：

- `player_profile`：玩家層級月結 / snapshot profile，繼續走 PIT / as-of join。
- `player_betting_episode_summary`：玩家每次 30 分鐘 gap-based betting episode 一列的 compact 摘要表。
- `player_visit_summary`：玩家每次較寬 casino visit / gaming-day visit 一列的 compact 摘要表。
- `player_history_baseline`：由 profile + episode / visit summary 衍生的個人歷史基準。
- 多窗 validation report：用較舊月份做穩定性檢查，而非盲目擴大主訓練 rows。

優先順序上，`player_betting_episode_summary` 應排在離線 embedding 或多年全量訓練之前，因為它的成本低、可解釋性高，且與現有 LightGBM / feature spec 架構相容。

---

## 3. Compact Episode / Visit Summary

### 3.1 目標

建立一張「每位玩家每次連續下注流程一列」的摘要表，將長歷史 bet data 壓縮到可被 laptop 訓練流程安全 join 的尺寸。

重要修正：不建議用單一 **run / visit** 定義包辦所有長歷史特徵。現有 repo 中 `RUN_BREAK_MIN = WALKAWAY_GAP_MIN = 30`，這很適合作為 walkaway label 對齊的「連續下注 episode」，但不一定等於玩家真正的 casino visit。

建議將行為單位拆成三層，並在欄位命名中明確區分：

- `betting_episode`：同一玩家連續下注，gap < 30 分鐘視為同一段；gap >= 30 分鐘代表 episode 結束。此層最貼近 walkaway label。
- `casino_visit`：較寬的到場旅程，可用同一 gaming day、t_session 語意、或 gap >= 4-6 小時才切分。此層適合 budget、到場頻率與日內疲乏特徵。
- `table_episode`：玩家 × table_id 的連續下注段，換桌即切或在同桌內再套用 gap rule。此層適合 table stickiness、換桌與桌況 context。

因此，30 分鐘定義應保留，但建議在新 summary artifact 中命名為 `betting_episode`，不要直接叫 `visit`。

### 3.2 建議 artifact 分層

初期可先產出兩張 compact summary，再視需要補第三張：

- `player_betting_episode_summary`：每位玩家每個 30 分鐘 gap-based betting episode 一列。
- `player_visit_summary`：每位玩家每個較寬 casino visit / gaming-day visit 一列。
- `player_table_episode_summary`：每位玩家在每張桌的連續下注段一列，可作為 second-wave table behavior artifact。

若為了先做 MVP，也可以先只做 `player_betting_episode_summary`，但文件和欄位命名仍應避免把它稱為 visit。

### 3.3 建議主鍵與時間欄位

- `canonical_id`
- `episode_id` 或 `visit_id`
- `episode_type`：`betting_episode` / `casino_visit` / `table_episode`
- `episode_start_dtm`
- `episode_end_dtm`
- `episode_date`
- `source_min_bet_time`
- `source_max_bet_time`
- `profile_version` 或 `summary_version`

### 3.4 建議摘要欄位

Betting episode 層級：

- `episode_duration_min`
- `episode_bet_count`
- `episode_turnover_sum`
- `episode_avg_wager`
- `episode_p50_wager`
- `episode_max_wager`
- `episode_player_net_win`
- `episode_max_drawdown`
- `episode_loss_streak_max`
- `episode_push_count`
- `episode_table_count`
- `episode_pit_count`
- `episode_last_table_id`

跨 betting episode 序列欄位：

- `prev_episode_player_net_win`
- `prev_episode_duration_min`
- `days_since_prev_episode`
- `cum_pnl_last_3_episodes`
- `cum_pnl_last_5_episodes`
- `avg_episode_duration_last_10`
- `p75_episode_duration_last_10`
- `median_wager_last_10_episodes`
- `loss_episode_count_last_10`
- `big_loss_episode_count_last_10`

Casino visit 層級：

- `visit_duration_min`
- `visit_betting_episode_count`
- `visit_turnover_sum`
- `visit_player_net_win`
- `visit_max_drawdown`
- `visit_first_bet_hour`
- `visit_table_count`
- `days_since_prev_visit`
- `cum_pnl_last_3_visits`
- `avg_visit_turnover_last_10`

個人化止損 / 疲乏訊號：

- `historical_loss_limit_p25`
- `historical_loss_limit_p10`
- `current_loss_vs_historical_loss_limit`
- `stop_after_loss_rate`
- `days_since_big_loss_episode`
- `chasing_indicator`

### 3.5 PIT 契約

所有由 compact summary 產生的 training features 必須遵守：

- 對任一 bet row，只能使用 `episode_end_dtm < bet_time` / `visit_end_dtm < bet_time`，或已確認 available 的過去 episode / visit。
- 若使用 `available_time`，需與現有 `SESSION_AVAIL_DELAY_MIN` / profile DQ 語意一致。
- 當前 betting episode 的即時累計特徵仍由 bet-level Track Human / Track LLM 計算，不應從 summary 表偷看完整當前 episode 結局。
- `current_loss_vs_historical_loss_limit` 之類 cross features，只能用「當前已發生狀態」除以「過去歷史基準」。

這是本方向最重要的防 leakage 要點。

---

## 4. 與現有 Player Profile 的關係

現有 `player_profile` 已經支援 PIT / as-of join，且包含 7 / 30 / 90 / 180 / 365 天尺度的 RFM、turnover、win、RTP、duration 與場域黏性欄位。Compact episode / visit summary 不應取代 profile，而是補足 profile 較難表達的「已完成 betting episode / casino visit 結束狀態序列」。

建議分工如下：

- `player_profile`：穩定的 player-level snapshot，例如長短期 turnover、active days、RTP、黏性。
- `player_betting_episode_summary`：每次 30 分鐘 gap-based betting episode 的結果與節奏，例如上次 episode P&L、最近 3 次 episode 累計輸贏、個人止損分位數。
- `player_visit_summary`：較寬 casino visit / gaming-day visit 的 budget、到場頻率、日內疲乏與到場習慣。
- `player_table_episode_summary`：玩家在特定 table 的連續下注段，用於換桌、桌面黏性與 table context。
- training feature matrix：只 join 已壓縮且 PIT-safe 的 profile / summary features。

---

## 5. 高價值 Cross Features

長歷史資料最有價值的用法，不是讓模型知道「玩家絕對下注多少」，而是讓模型知道「這次行為是否偏離此玩家平常狀態」。

建議優先候選：

- `run_duration_vs_personal_avg`
- `run_duration_vs_personal_p75`
- `bets_in_run_vs_personal_avg`
- `episode_duration_vs_personal_p75`
- `episode_bets_vs_personal_avg`
- `visit_turnover_vs_personal_avg`
- `current_wager_vs_personal_median`
- `current_loss_vs_personal_loss_limit`
- `pace_vs_personal_baseline`
- `pace_drop_vs_personal_history`
- `current_turnover_vs_recent_run_avg`
- `current_table_share_vs_personal_baseline`

這些特徵可以幫助模型區分「本來就慢 / 本來就高額」與「今天異常放慢 / 異常追損」。

---

## 6. History-Depth Bundle

長歷史特徵不應對所有玩家一視同仁。低歷史玩家的 profile 可能很稀疏，高歷史玩家則有足夠資料建立個人 baseline。

建議先以特徵方式落地，而不是立即拆多模型：

- `history_depth_days`
- `historical_episode_count`
- `historical_visit_count`
- `profile_completeness_score`
- `history_depth_bucket`

初始 bucket 可簡化為：

- `cold`: history < 7 days 或 episode / visit count 太少。
- `warm`: history 7-90 days。
- `mature`: history >= 90 days 且 episode / visit count 足夠。

若多窗結果顯示不同 bucket 的最佳特徵或閾值差異很大，再考慮 multi-bundle 或 learned gating。

---

## 7. 不建議優先做的方向

### 7.1 多年 bet-level 全量訓練

不建議直接把所有歷史 bet rows 納入主訓練。若真的要利用舊 rows，應採用：

- 最近 3 個月全量。
- 3-12 個月前按時間衰減抽樣。
- 正例、near-positive、hard negative 優先保留。
- 舊資料 sample weight 顯著低於近期資料。

這只能作為後續實驗，不應取代 compact summary 方向。

### 7.2 離線序列 Embedding 作為第一步

離線 sequence embedding 可能有價值，但工程成本、訓練時間與記憶體風險較高。建議等 `player_betting_episode_summary`、個人 baseline 與多窗 validation 完成後，再評估 embedding 是否仍有明確增量。

---

## 8. Validation Strategy

長歷史特徵上線前，至少需要驗證三件事：

- **PIT correctness**：任一 bet row 不得使用未來 episode / visit 結局、未來 profile snapshot 或整窗 mapping。
- **多窗穩定性**：至少跨數個月份檢查 field-test precision / recall / alert volume，不以單窗 winner 作決策。
- **資源診斷**：新增 summary join 後，Step 7 / Step 9 的 row count、column count、RSS、runtime 不得出現不可接受暴增。

建議評估矩陣：

- Baseline：最近 3 個月 bet rows + 現有 profile。
- Variant A：Baseline + compact betting episode summary features。
- Variant B：Variant A + personal-baseline cross features。
- Variant C：Variant B + history-depth bucket features。
- Variant D：Variant C + wider casino visit summary features。

若 Variant B 或 C 只在單一月份提升，但最差窗惡化，應先 hold，不應直接推入主 bundle。

另建議做 episode-break sensitivity check：保留 label 的 `WALKAWAY_GAP_MIN = 30` 不動，僅針對 historical baseline 額外比較 15 / 30 / 45 / 60 分鐘 episode break 產生的 episode 數、duration 分布、P&L 分布與下游 field-test precision。若 30 分鐘是業務告警定義，label 不應因特徵實驗而改動。

---

## 9. Suggested Implementation Phases

### Phase 1: Compact Summary Artifact

產出 `player_betting_episode_summary` Parquet / DuckDB artifact，先只做離線 training consumption。重點是 PIT-safe、可重建、可檢查 row count 與日期覆蓋。

### Phase 2: Feature Candidates

將 `prev_episode_*`、`cum_pnl_last_*_episodes`、`historical_loss_limit_*` 與 history-depth 欄位加入候選池，經 feature screening 決定是否進 Step 9。

### Phase 3: Cross Features

加入 current betting episode 狀態 vs personal baseline 的 cross features，並補 trainer / backtester / scorer parity。

### Phase 4: Multi-Window Gate

用多窗報表決定是否採納。若收益只來自單窗，先保留為實驗，不直接變成預設。

### Phase 5: Optional Advanced Use

在 compact betting episode summary 穩定後，再考慮 `player_visit_summary`、`player_table_episode_summary`、temporal-decay old-row sampling、multi-bundle、learned gating 或 sequence embedding。

---

## 10. Open Questions

- `betting_episode` 是否沿用 `RUN_BREAK_MIN = WALKAWAY_GAP_MIN = 30`，但在命名上與 `casino_visit` 明確分開？
- `casino_visit` 應依 gaming day、`t_session`、或較長 inactivity gap（例如 4-6 小時）切分？
- `table_episode` 是否換桌即切，或同一 table 內再用 30 分鐘 gap 切分？
- `player_net_win` 是否統一定義為 `-casino_win`，並在所有 episode / visit-level P&L 特徵中沿用？
- 舊資料的 DQ 規則是否與最近資料一致，尤其是 manual / deleted / canceled / unresolved rows？
- Summary artifact 應由 trainer 自動檢查並補建，還是先作為獨立 ETL 產物？
- Scorer 線上環境是否能取得足夠新的 episode / visit summary 與 profile snapshot，或需要部署前 freeze 到 bundle？

---

## 11. 推薦決策

下一步應先做 **compact betting episode summary artifact**，而不是擴大主訓練視窗或導入 embedding。

這條路徑最符合目前限制：

- 可以利用更長歷史。
- 不需要把多年 bet rows 放進訓練矩陣。
- 與既有 `player_profile`、PIT / as-of join、feature screening 和 LightGBM 主路徑相容。
- 對 laptop RAM / runtime 較友善。
- 可解釋性比 embedding 高，較適合先做 field-test precision uplift。
