# player_profile 規格說明書

> **關聯文件**：SSOT §4.3（`ssot/trainer_plan_ssot.md`）、Phase 1 計畫 Step 4（`ssot/patron_walkaway_phase_1.plan.md`）、DEC-011（`.cursor/plans/DECISION_LOG.md`）

---

## 1. 概述

`player_profile` 為 **rated-only**、**player-level** 的快照表，提供每位 `canonical_id` 的歷史行為輪廓，供 Rated 模型使用。訓練與推論時以 **PIT / as-of join** 使用（`snapshot_dtm <= bet_time` 的最新快照），不作為 EntitySet relationship。

---

## 2. 建表前條件

### 2.1 依賴

- **D2 歸戶**：`player_id → canonical_id` mapping（`identity.py` 產出）必須先就緒。
- **Join 路徑**：`t_session` 本身無 `canonical_id`，需經 D2 mapping 取得：
  - `t_session` → FND-01 去重 → FND-02 排除 `is_manual=1` → FND-03 清洗 `casino_player_id` → **D2 mapping** → `canonical_id`
  - 再依 `canonical_id` 做窗口聚合。

### 2.2 資料品質與排除

建表前**必須**套用：

- `is_manual = 0`、`is_deleted = 0`、`is_canceled = 0`
- FND-12 假帳號排除（`SUM(COALESCE(num_games_with_wager,0)) <= 1`）
- 僅納入 `available_time <= snapshot_dtm` 的 sessions（`available_time` = `COALESCE(session_end_dtm, lud_dtm)` + `SESSION_AVAIL_DELAY_MIN`）

### 2.3 Population 約束（Rated-Only）

`player_profile` 僅為 **rated** 玩家建表。具體定義：

- **Rated**：在 D2 mapping 中擁有 `casino_player_id`（即有會員卡/Player Card）的玩家。這些玩家透過 `identity.build_canonical_mapping()` 產出 `canonical_id`。
- **Unrated**：沒有 `casino_player_id` 的匿名下注紀錄。這些玩家的 `canonical_id` 為 `str(player_id)` 的 fallback 值，但 **不納入** player_profile。

**理由**：
1. Unrated 玩家無法跨 session 追蹤身份，歷史行為輪廓不可靠。
2. Unrated 模型僅使用 bet-level / session-level 即時特徵（Track A + Track Human），不需要 player-level profile。
3. 避免浪費計算資源在永遠不會被消費的 profile 上（unrated 在 3 個月資料中可達數十萬筆，但對 rated model 無用）。

**ETL 實作要點**：
- `etl_player_profile.py` 的 `backfill()` 應在 canonical_map join 之後，只處理 `canonical_id` 存在於 rated mapping 中的 sessions。
- Fast-mode 下可進一步從 rated mapping 中 **deterministic 抽樣**（見 DEC-015），以 `canonical_id_whitelist` 參數控制。

### 2.4 Consumer 約束

| Consumer | 是否使用 `player_profile` | 說明 |
|----------|-------------------------------|------|
| **Rated model** | ✅ 使用 | PIT/as-of join，profile 欄位作為 feature |
| **Nonrated model** | ❌ 不使用 | 僅使用 bet-level / session-level 即時特徵 |
| **Scorer（online）** | ✅ 限 rated path | `is_rated_obs = True` 時才查詢 profile |
| **Backtester** | ✅ 限 rated path | 回測時 rated 模型路徑使用 profile |

**影響**：
- `trainer.py` 的 nonrated training path 在合併特徵時，不應嘗試 join `PROFILE_FEATURE_COLS`。
- `scorer.py` 在 `is_rated_obs = False` 時，不應查詢 profile 表或嘗試載入 profile features。

---

## 3. 特徵取捨與理由（Design Rationale）

本節說明在規格制訂與反思過程中，**保留**與**捨棄**的特徵及其原因，供後續實作與演進參考。

### 3.1 捨棄的特徵


| 捨棄特徵                                       | 捨棄原因                                                                                                                                                                              |
| ------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `valid_session_cnt_30d`                    | 與 `sessions_30d` 完全同義（建表前已過濾 DQ），保留會造成冗餘。                                                                                                                                         |
| `avg_bet_winsorized_mean_30d`              | 與 `turnover_per_bet_mean_30d` 皆在量測平均注額；後者由 `turnover_sum / num_bets_sum` 重算，較穩健且避開 `t_session.avg_bet` 的極端值（`doc/FINDINGS.md` 指出該欄位有 50 億異常值）。Phase 1 優先使用 turnover_per_bet_mean。 |
| `turnover_7d_over_prev7d`                  | 7 天窗口對博彩客造訪週期過短，多數玩家 7 天內無活動，ratio 易為 0/0；且需額外定義 `turnover_sum_prev7d` 中間欄位，增加實作與維護成本。改用 `30d_over_180d` 更穩健。                                                                     |
| 場域黏性的 180d 版本（`distinct_table_cnt_180d` 等） | 賭場桌台/區域可能有改裝或重編，180 天累積的 `distinct_table/pit` 容易被實體變動污染；僅保留 30d/90d。                                                                                                              |
| 直接使用 `t_session.avg_bet` 未經 winsorize      | Schema 與 `doc/FINDINGS.md` 指出 `avg_bet` 存在極端異常值，直接平均會污染 profile；改由 turnover/num_bets 重算或先 cap 再平均。                                                                                |
| `chips_in`                                 | 全表多為 0，幾乎無訊號；不納入。                                                                                                                                                                 |


### 3.2 保留的特徵


| 類別                                                     | 保留理由                                                                                   |
| ------------------------------------------------------ | -------------------------------------------------------------------------------------- |
| **RFM 多窗口（7/30/90/180/365d）**                          | 捕捉不同時間尺度的行為；rated 玩家歷史深度足夠（`doc/FINDINGS.md` 顯示中位數 span 約 33 天，44.3% 有 ≥30 天 history）。 |
| **長短期 Ratio（30d/180d）**                                | 比單純絕對值更能捕捉「近期 vs 長期常態」的變化，對 walkaway 前兆訊號強。                                            |
| **actual_rtp、actual_vs_theo_ratio**                    | 玩家心理受「實際贏率 vs 理論期望」影響；實際遠低於理論時易觸發離場。                                                   |
| **avg_session_duration_min**                           | 僅納入 `session_end_dtm IS NOT NULL` 的 session，避免以 `lud_dtm` fallback 造成系統性低估。            |
| **avg_buyin_per_session_30d**                          | 初期以 NULL 率高為由捨棄，但全表約 6700 萬筆有效；buyin 代表「預算心理帳戶」，對 walkaway 具參考價值，列為 Phase 1 可選。        |
| **theo_win_sum**                                       | 與 player_win 搭配衍生 `actual_vs_theo_ratio`，提供「下風期」訊號。                                    |
| **場域黏性（30d/90d）**                                      | 區分死忠單桌客 vs 遊走客；不延至 180d 以避免改裝干擾。                                                       |
| **Phase 2 的 t_bet 欄位（wager_mean_180d、wager_p50_180d）** | 由 bet-level 重算較準確，但需掃 4.38 億筆；Phase 1 以 t_session 為主，Phase 2 再導入。                      |


### 3.3 實作注意事項（反思結論）

- **D2 mapping 為必經路徑**：`t_session` 無 `canonical_id`，需先建 D2 mapping 再聚合。
- **top_table_share 需兩層聚合**：先依 `table_id` 子聚合 `SUM(turnover)`，再取 MAX 除以總 turnover；實作時需特別處理，非簡單單層 GROUP BY。
- **所有除法需 `NULLIF(denominator, 0)`**：避免 0 除錯誤；ratio 中分母為 0 時回傳 NULL 或約定常數。

---

## 4. 主鍵與快照欄位


| 欄位                | 型別        | 來源         | 說明              |
| ----------------- | --------- | ---------- | --------------- |
| `canonical_id`    | VARCHAR   | D2 mapping | 歸戶 ID           |
| `snapshot_date`   | DATE      | 參數         | 快照日期            |
| `snapshot_dtm`    | TIMESTAMP | 參數         | PIT 截止時間（as-of） |
| `profile_version` | VARCHAR   | 參數         | 版本標記            |


---

## 5. Recency


| 欄位                         | 型別  | 來源表           | 計算方式                                                                                                |
| -------------------------- | --- | ------------- | --------------------------------------------------------------------------------------------------- |
| `days_since_last_session`  | INT | **t_session** | `snapshot_date - MAX(session_date)`，`session_date = COALESCE(session_end_dtm::date, lud_dtm::date)` |
| `days_since_first_session` | INT | **t_session** | `snapshot_date - MIN(session_date)`，同上                                                              |


---

## 6. Frequency


| 欄位                 | 型別  | 來源表           | 計算方式                                                              |
| ------------------ | --- | ------------- | ----------------------------------------------------------------- |
| `sessions_7d`      | INT | **t_session** | `COUNT(*)` WHERE session 時間落在 `[snapshot_dtm - 7d, snapshot_dtm]` |
| `sessions_30d`     | INT | **t_session** | 同上，30d                                                            |
| `sessions_90d`     | INT | **t_session** | 同上，90d                                                            |
| `sessions_180d`    | INT | **t_session** | 同上，180d                                                           |
| `sessions_365d`    | INT | **t_session** | 同上，365d                                                           |
| `active_days_30d`  | INT | **t_session** | `COUNT(DISTINCT gaming_day)` 30d                                  |
| `active_days_90d`  | INT | **t_session** | 同上，90d                                                            |
| `active_days_365d` | INT | **t_session** | 同上，365d                                                           |


---

## 7. Monetary（Turnover & Win）


| 欄位                              | 型別      | 來源表           | 計算方式                                         |
| ------------------------------- | ------- | ------------- | -------------------------------------------- |
| `turnover_sum_7d`               | DECIMAL | **t_session** | `SUM(turnover)` 7d                           |
| `turnover_sum_30d`              | DECIMAL | **t_session** | `SUM(turnover)` 30d                          |
| `turnover_sum_90d`              | DECIMAL | **t_session** | `SUM(turnover)` 90d                          |
| `turnover_sum_180d`             | DECIMAL | **t_session** | `SUM(turnover)` 180d                         |
| `turnover_sum_365d`             | DECIMAL | **t_session** | `SUM(turnover)` 365d                         |
| `player_win_sum_30d`            | DECIMAL | **t_session** | `SUM(player_win)` 30d                        |
| `player_win_sum_90d`            | DECIMAL | **t_session** | `SUM(player_win)` 90d                        |
| `player_win_sum_180d`           | DECIMAL | **t_session** | `SUM(player_win)` 180d                       |
| `player_win_sum_365d`           | DECIMAL | **t_session** | `SUM(player_win)` 365d                       |
| `theo_win_sum_30d`              | DECIMAL | **t_session** | `SUM(theo_win)` 30d                          |
| `theo_win_sum_180d`             | DECIMAL | **t_session** | `SUM(theo_win)` 180d                         |
| `num_bets_sum_30d`              | INT     | **t_session** | `SUM(num_bets)` 30d                          |
| `num_bets_sum_180d`             | INT     | **t_session** | `SUM(num_bets)` 180d                         |
| `num_games_with_wager_sum_30d`  | INT     | **t_session** | `SUM(COALESCE(num_games_with_wager, 0))` 30d |
| `num_games_with_wager_sum_180d` | INT     | **t_session** | 同上 180d                                      |


---

## 8. 下注強度


| 欄位                           | 型別      | 來源表           | 計算方式                                                                                   |
| ---------------------------- | ------- | ------------- | -------------------------------------------------------------------------------------- |
| `turnover_per_bet_mean_30d`  | DECIMAL | **t_session** | `turnover_sum_30d / NULLIF(num_bets_sum_30d, 0)`                                       |
| `turnover_per_bet_mean_180d` | DECIMAL | **t_session** | `turnover_sum_180d / NULLIF(num_bets_sum_180d, 0)`                                     |
| `avg_buyin_per_session_30d`  | DECIMAL | **t_session** | `SUM(buyin) / NULLIF(sessions_30d, 0)`；僅 `buyin IS NOT NULL` 的 sessions；**Phase 1 可選** |
| `wager_mean_180d`            | DECIMAL | **t_bet**     | Phase 2：`SUM(wager)/COUNT(*)` 180d，需 join session→canonical_id；wager 可 winsorize       |
| `wager_p50_180d`             | DECIMAL | **t_bet**     | Phase 2：`APPROX_QUANTILE(wager, 0.5)` 180d；同上 join                                     |


---

## 9. 勝負與 RTP 衍生


| 欄位                         | 型別      | 來源表           | 計算方式                                                                      |
| -------------------------- | ------- | ------------- | ------------------------------------------------------------------------- |
| `win_session_rate_30d`     | DECIMAL | **t_session** | `SUM(CASE WHEN player_win>0 THEN 1 ELSE 0 END) / NULLIF(sessions_30d, 0)` |
| `win_session_rate_180d`    | DECIMAL | **t_session** | 同上 180d                                                                   |
| `actual_rtp_30d`           | DECIMAL | **t_session** | `1 + (player_win_sum_30d / NULLIF(turnover_sum_30d, 0))`                  |
| `actual_rtp_180d`          | DECIMAL | **t_session** | 同上 180d                                                                   |
| `actual_vs_theo_ratio_30d` | DECIMAL | **t_session** | `player_win_sum_30d / NULLIF(theo_win_sum_30d, 0)`；負值表示實際表現差於理論預期         |


---

## 10. 短期 vs 長期（Ratio）


| 欄位                               | 型別      | 來源表                      | 計算方式                                                                |
| -------------------------------- | ------- | ------------------------ | ------------------------------------------------------------------- |
| `turnover_per_bet_30d_over_180d` | DECIMAL | **player_profile** | `turnover_per_bet_mean_30d / NULLIF(turnover_per_bet_mean_180d, 0)` |
| `turnover_30d_over_180d`         | DECIMAL | **player_profile** | `turnover_sum_30d / NULLIF(turnover_sum_180d, 0)`                   |
| `sessions_30d_over_180d`         | DECIMAL | **player_profile** | `sessions_30d / NULLIF(sessions_180d, 0)`                           |


---

## 11. Session Duration


| 欄位                              | 型別      | 來源表           | 計算方式                                                                                                           |
| ------------------------------- | ------- | ------------- | -------------------------------------------------------------------------------------------------------------- |
| `avg_session_duration_min_30d`  | DECIMAL | **t_session** | `AVG(DATEDIFF('minute', session_start_dtm, session_end_dtm))`，**僅納入 `session_end_dtm IS NOT NULL` 的 sessions** |
| `avg_session_duration_min_180d` | DECIMAL | **t_session** | 同上 180d                                                                                                        |


---

## 12. 場域黏性


| 欄位                             | 型別      | 來源表           | 計算方式                                                                                                        |
| ------------------------------ | ------- | ------------- | ----------------------------------------------------------------------------------------------------------- |
| `distinct_table_cnt_30d`       | INT     | **t_session** | `COUNT(DISTINCT table_id)` 30d                                                                              |
| `distinct_table_cnt_90d`       | INT     | **t_session** | 同上 90d                                                                                                      |
| `distinct_pit_cnt_30d`         | INT     | **t_session** | `COUNT(DISTINCT pit_name)` 30d                                                                              |
| `distinct_gaming_area_cnt_30d` | INT     | **t_session** | `COUNT(DISTINCT gaming_area)` 30d                                                                           |
| `top_table_share_30d`          | DECIMAL | **t_session** | `MAX(table_turnover_30d) / NULLIF(turnover_sum_30d, 0)`；**需先依 `table_id` 做 `SUM(turnover)` 子聚合，再取最大（兩層聚合）** |
| `top_table_share_90d`          | DECIMAL | **t_session** | 同上 90d                                                                                                      |


---

## 13. 除零與缺值規則

1. 所有除法一律用 `NULLIF(denominator, 0)`；分母為 0 時為 `NULL`。
2. Ratio 特徵中，若分子有值但分母為 0，可選擇填 999（表示「新爆發」）或保留 `NULL`；需統一。
3. LightGBM 可原生處理 `NULL`，無需強制填補。

---

## 14. Phase 1 與 Phase 2


| 階段          | 範圍                                                                                                              |
| ----------- | --------------------------------------------------------------------------------------------------------------- |
| **Phase 1** | 僅用 **t_session**；不掃 **t_bet**。wager 相關以 `turnover_per_bet_mean` 為主。`avg_buyin_per_session_30d` 可選。              |
| **Phase 2** | 可從 **t_bet** 加 `wager_mean_180d`、`wager_p50_180d`；可加 `side_bet_ratio_30d`（`type_of_bet='SIDE_BET'` 下注數 / 總下注數）。 |


---

## 15. 來源表總覽


| 來源表                      | 欄位數 | 說明                                                                                    |
| ------------------------ | --- | ------------------------------------------------------------------------------------- |
| **t_session**            | ~38 | 主力：turnover、player_win、theo_win、num_bets、buyin、session 時間、table/pit/area              |
| **t_bet**                | 2   | Phase 2：wager_mean_180d、wager_p50_180d（經 session→canonical_id join）                   |
| **player_profile** | 3   | 衍生 ratio：turnover_per_bet_30d_over_180d、turnover_30d_over_180d、sessions_30d_over_180d |
| **D2 mapping**           | 1   | canonical_id                                                                          |
| **參數**                   | 3   | snapshot_date、snapshot_dtm、profile_version                                            |


---

## 16. PIT / As-of Join 使用方式

- 快照 `snapshot_dtm` 為當日批次結束時間。
- 對每筆 bet（`bet_time = payout_complete_dtm`），以 `canonical_id` 為 key，選 `snapshot_dtm <= bet_time` 的**最新一筆**快照，將其 profile 欄位貼到該 bet。
- 訓練與推論皆僅使用該 as-of 快照，不得使用 `snapshot_dtm > bet_time` 的未來快照。
