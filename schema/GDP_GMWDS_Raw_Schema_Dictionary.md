# GDP_GMWDS_Raw 資料庫綱要文件

**系統脈絡：** 本資料集代表百家樂賭場管理系統（CMS），層級架構為：`t_shoe` (牌靴) → `t_game` (牌局) → `t_session` (玩家時段) → `t_bet` (單筆下注)。

**資料實況（本次 Parquet 匯出）**：
- 時間欄位在 Parquet 中以 `timestamp[ms, tz=UTC]` 儲存；如需對齊原系統/ClickHouse 的 `Asia/Shanghai`，請於使用端做時區轉換。
- `gaming_day`（本批資料）觀察範圍約為 **2024-07-02 ~ 2026-02-13**。
- **統計方法聲明**：本文件中的 `值域/格式/枚舉`、最大/最小值與 NULL 佔比等描述，皆是透過 **全表掃描（Full Table Scan）** 與 **Parquet 物理層 Metadata** 計算得出（例如掃描 4.38 億筆 t_bet 與 7400 萬筆 t_session），**並非**抽樣推估，因此枚舉值是窮盡的。唯獨 `範例值` 欄位是為了展示格式而提取的少數量本。

---

## 1. t_shoe (牌靴維度表)
**說明：** 記錄實體牌靴的生命週期與總體數據（通常一副牌靴包含 8 副撲克牌）。

**表級資訊（簡版）**
- **粒度（Grain）**：一筆 = 一副牌靴（shoe）的生命週期。
- **唯一鍵（參考）**：`shoe_id`（原始 ClickHouse 設計亦包含 `shoe_start_dtm` 作為排序/分區關聯欄位）。
- **資料可用性**：此專案未提供 `t_shoe` 的 Parquet 檔；以下僅列出欄位名稱（其餘資訊留空，待後續補齊）。

| 欄位名稱 | 型別 | 可空 | 業務定義 | 值域/格式/枚舉 | 範例值 | 注意事項/已知問題 |
|---|---|---|---|---|---|---|
| `shoe_id` |  |  |  |  |  |  |
| `casino_win` |  |  |  |  |  |  |
| `current_card_count` |  |  |  |  |  |  |
| `initial_card_count` |  |  |  |  |  |  |
| `shoe_end_dtm` |  |  |  |  |  |  |
| `shoe_start_dtm` |  |  |  |  |  |  |
| `table_id` |  |  |  |  |  |  |
| `__ts_ms` |  |  |  |  |  |  |
| `__op` |  |  |  |  |  |  |
| `__deleted` |  |  |  |  |  |  |
| `__etl_insert_Dtm` |  |  |  |  |  |  |

---

## 2. t_game (牌局維度表)
**說明：** 記錄單一牌局（Hand）的發牌結果、時間戳記與總體財務結算。

**表級資訊（簡版）**
- **粒度（Grain）**：一筆 = 一局（hand / game）。
- **唯一鍵（參考）**：`game_id`（原始 ClickHouse 設計亦包含 `gaming_day` 作為排序/分區關聯欄位）。
- **資料可用性**：此專案未提供 `t_game` 的 Parquet 檔；以下僅列出欄位名稱（其餘資訊留空，待後續補齊）。

| 欄位名稱 | 型別 | 可空 | 業務定義 | 值域/格式/枚舉 | 範例值 | 注意事項/已知問題 |
|---|---|---|---|---|---|---|
| `game_id` |  |  |  |  |  |  |
| `game_uuid` |  |  |  |  |  |  |
| `table_id` |  |  |  |  |  |  |
| `table_type` |  |  |  |  |  |  |
| `shoe_id` |  |  |  |  |  |  |
| `dealer_id` |  |  |  |  |  |  |
| `supervisor_id` |  |  |  |  |  |  |
| `gaming_day` |  |  |  |  |  |  |
| `num_positions` |  |  |  |  |  |  |
| `num_players` |  |  |  |  |  |  |
| `total_turnover` |  |  |  |  |  |  |
| `total_pushed_wagers` |  |  |  |  |  |  |
| `total_contra_wagers` |  |  |  |  |  |  |
| `total_pushed_contra_wagers` |  |  |  |  |  |  |
| `casino_win` |  |  |  |  |  |  |
| `theo_win` |  |  |  |  |  |  |
| `table_exposure` |  |  |  |  |  |  |
| `player_score` |  |  |  |  |  |  |
| `banker_score` |  |  |  |  |  |  |
| `outcome` |  |  |  |  |  |  |
| `is_player_pair` |  |  |  |  |  |  |
| `is_banker_pair` |  |  |  |  |  |  |
| `num_cards_drawn` |  |  |  |  |  |  |
| `card_p1` |  |  |  |  |  |  |
| `card_p2` |  |  |  |  |  |  |
| `card_p3` |  |  |  |  |  |  |
| `card_b1` |  |  |  |  |  |  |
| `card_b2` |  |  |  |  |  |  |
| `card_b3` |  |  |  |  |  |  |
| `prev_game_end_dtm` |  |  |  |  |  |  |
| `card_p1_draw_dtm` |  |  |  |  |  |  |
| `card_p2_draw_dtm` |  |  |  |  |  |  |
| `card_p3_draw_dtm` |  |  |  |  |  |  |
| `card_b1_draw_dtm` |  |  |  |  |  |  |
| `card_b2_draw_dtm` |  |  |  |  |  |  |
| `card_b3_draw_dtm` |  |  |  |  |  |  |
| `card_p1_turn_dtm` |  |  |  |  |  |  |
| `card_p2_turn_dtm` |  |  |  |  |  |  |
| `card_p3_turn_dtm` |  |  |  |  |  |  |
| `card_b1_turn_dtm` |  |  |  |  |  |  |
| `card_b2_turn_dtm` |  |  |  |  |  |  |
| `card_b3_turn_dtm` |  |  |  |  |  |  |
| `t4_take_begin_dtm` |  |  |  |  |  |  |
| `t4_take_end_dtm` |  |  |  |  |  |  |
| `rb_take_begin_dtm` |  |  |  |  |  |  |
| `game_start_dtm` |  |  |  |  |  |  |
| `payout_complete_dtm` |  |  |  |  |  |  |
| `table_limits` |  |  |  |  |  |  |
| `table_template` |  |  |  |  |  |  |
| `table_name` |  |  |  |  |  |  |
| `game_status` |  |  |  |  |  |  |
| `adjusted_turnover` |  |  |  |  |  |  |
| `supervisor_first_name` |  |  |  |  |  |  |
| `supervisor_last_name` |  |  |  |  |  |  |
| `dealer_first_name` |  |  |  |  |  |  |
| `dealer_last_name` |  |  |  |  |  |  |
| `bonus` |  |  |  |  |  |  |
| `is_lucky_six` |  |  |  |  |  |  |
| `is_commissioned` |  |  |  |  |  |  |
| `pit_name` |  |  |  |  |  |  |
| `gaming_area` |  |  |  |  |  |  |
| `dealer_employee_number` |  |  |  |  |  |  |
| `supervisor_employee_number` |  |  |  |  |  |  |
| `gaming_day_first_game` |  |  |  |  |  |  |
| `include_in_aggregation` |  |  |  |  |  |  |
| `game_errors` |  |  |  |  |  |  |
| `game_variance` |  |  |  |  |  |  |
| `is_lucky8_player` |  |  |  |  |  |  |
| `is_lucky8_banker` |  |  |  |  |  |  |
| `dealer_cards` |  |  |  |  |  |  |
| `game_type` |  |  |  |  |  |  |
| `dealer_cards_sum` |  |  |  |  |  |  |
| `game_result` |  |  |  |  |  |  |
| `shoe_game_count` |  |  |  |  |  |  |
| `game_variant` |  |  |  |  |  |  |
| `dealer_cards_metadata` |  |  |  |  |  |  |
| `lucky_card` |  |  |  |  |  |  |
| `prg_template_id` |  |  |  |  |  |  |
| `__ts_ms` |  |  |  |  |  |  |
| `__op` |  |  |  |  |  |  |
| `__deleted` |  |  |  |  |  |  |
| `__etl_insert_Dtm` |  |  |  |  |  |  |

---

## 3. t_session (玩家打牌時段表)
**說明：** 記錄單一玩家在單一賭桌上一段連續打牌的過程（公關評級依據）。

**表級資訊（簡版）**
- **資料來源**：`data/gmwds_t_session.parquet`（約 74,359,529 列）。
- **粒度（Grain）**：一筆 = 一位玩家在同一張桌的一段連續時段（評級/Rating 的基本單位）。
- **唯一鍵（參考）**：`session_id`（原始 ClickHouse 設計亦包含 `gaming_day` 作為排序/分區關聯欄位）。
- **時區**：Parquet 時間欄位為 UTC；下游使用/對帳若以 `Asia/Shanghai` 為準，需統一轉換。
- **已知資料狀況（本批觀察）**：
  - **`session_id` 重複**：全表約有 27.8 萬個 `session_id` 發生重複寫入（受影響約 56 萬列）。原因包含純 ETL 堆疊以及狀態/金額更新；使用時必須以 `MAX(lud_dtm)` 去重。
  - **人工帳務調整（`is_manual=1`）**：全表約 283 萬筆為人工紀錄，其特徵為 `turnover=0` 且沒有經過任何局數，但 `player_win` 包含全表最極端的數值（例如贏 59 億、輸 22 億）。這代表財務作帳，計算投注量與平均表現時應排除。
  - **`casino_player_id` 缺失**：NULL 約 27.38%，且存在字串值 `null`（約 0.77%），下游 join 前應視為缺失並清理。
  - **時間與計數異常**：`completion_dtm` 空值率接近 100%；`num_games_with_wager` 觀察到負數（僅發生於 `is_manual=1` 中）。

| 欄位名稱 | 型別 | 可空 | 業務定義 | 值域/格式/枚舉 | 範例值 | 注意事項/已知問題 |
|---|---|---|---|---|---|---|
| `session_id` | Int64 | N | 時段紀錄唯一識別碼。 | 觀察範圍（metadata）：101001 ~ 213451688 | `101001` | [DQ Rule] **Uniqueness (去重)**：使用時必須用 `MAX(lud_dtm)` 取得最新一筆，避免事後更正與 ETL 堆疊造成的重複（約 27.8萬 ID 被重複）。 |
| `table_id` | Int32 | Y | 關聯的賭桌 ID。 | 觀察範圍（metadata）：1005 ~ 152651001 | `1005` |  |
| `player_id` | Int64 | Y | Smart Table 系統內部玩家識別碼（非 loyalty 會員號）。 | 觀察到 `-1`（placeholder）；其餘約 -1 ~ 172596779（metadata） | `172596` | `-1` 建議視為未知/缺失（全表計算僅 14 筆）。 |
| `is_known_player` | Int32 | Y | 旗標：是否為具名會員（非散客）。 | 0/1 | `1` |  |
| `session_start_dtm` | DateTime64(3, UTC) | Y | 時段開始時間。 | 本批範圍（metadata）：2024-07-02T14:20:38Z ~ 2026-02-13T04:42:52Z | `2024-07-02T14:20:38Z` | Parquet 為 UTC；對應營業邏輯可能需轉 `Asia/Shanghai`。 |
| `first_wager_game_start_dtm` | DateTime64(3, UTC) | Y | 首次下注的牌局開始時間。 | 可為 NULL（metadata NULL=4,093,913） |  |  |
| `clockin_event_id` | String | Y | 入座/Clock-in 事件 ID。 |  |  |  |
| `clockin_event_dtm` | DateTime64(3, UTC) | Y | Clock-in 事件時間。 | 可為 NULL（metadata NULL=44,855,105） |  |  |
| `gaming_day` | Date32 | N | 營業日/賬務日。 | 本批範圍（metadata）：2024-07-02 ~ 2026-02-13 | `2026-02-13` |  |
| `num_games_elapsed` | Int32 | Y | 玩家在座期間經過的總局數（含未下注局）。 | 0 ~ 132（metadata） | `0` |  |
| `num_games_with_wager` | Int32 | Y | 玩家實際有下注的局數。 | 觀察到負值（metadata min=-8），max=29766 |  | [DQ Rule] **Validity (大於等於零)**：若為負數，皆出現在 `is_manual=1` 的人工調整紀錄中。 |
| `last_wager_game_end_dtm` | DateTime64(3, UTC) | Y | 最後一次下注的牌局結束時間。 | 可為 NULL（metadata NULL=4,093,913） |  |  |
| `chips_in` | Decimal(19,4) | Y | 帶上桌的初始籌碼總值。 | 本批多為 0（metadata min=max=0；NULL=49） | `0.0000` |  |
| `player_win` | Decimal(19,4) | Y | 玩家總淨贏損（正為贏、負為輸）。 | 本批範圍（全表計算）：-2222222220.0000 ~ 5970398940.0000 |  | [DQ Rule] **Consistency (過濾人工帳務)**：極端值全數來自 `is_manual=1`。作大盤財務分析可含入，但做打牌行為分析時必須排除人工紀錄。 |
| `turnover` | Decimal(19,4) | Y | 總投注額。 | 0.0000 ~ 200500000.0000（metadata） | `0.0000` |  |
| `num_bets` | Int32 | Y | 總下注次數。 | 0 ~ 570（metadata） | `0` |  |
| `avg_bet` | Decimal(19,4) | Y | 平均每注金額。 | 0.0000 ~ 5002192092.0000（metadata；NULL=13,598） |  | Max 異常偏大，可能與分母為 0/極小值或修補邏輯相關。 |
| `turnover_pushed_wagers` | Decimal(19,4) | Y | 因和局等原因退回的投注額。 | 0.0000 ~ 29800000.0000（metadata；NULL=45） | `0.0000` |  |
| `turnover_contra_wagers` | Decimal(19,4) | Y | 對沖下注總額。 | 0.0000 ~ 6860000.0000（metadata；NULL=49） | `0.0000` |  |
| `turnover_pushed_contra_wagers` | Decimal(19,4) | Y | 退回的對沖下注總額。 | 0.0000 ~ 3370000.0000（metadata；NULL=49） | `0.0000` |  |
| `adjusted_turnover` | Decimal(19,4) | Y | 調整後的總投注額。 | 0.0000 ~ 200500000.0000（metadata） | `0.0000` |  |
| `casino_open_rating_id` | String | Y | 賭場系統的開台評級 ID。 |  |  |  |
| `casino_close_rating_id` | String | Y | 賭場系統的關台評級 ID。 |  |  |  |
| `position_label` | String | Y | 玩家座位標籤。 |  |  |  |
| `clockin_event_username` | String | Y | 協助入座打卡的操作員帳號。 |  |  |  |
| `theo_win` | Decimal(19,4) | Y | 賭場在此時段的理論贏（預期利潤）。 | 0.0000 ~ 2694528.5000（metadata） | `0.0000` |  |
| `irc_number` | String | Y | IRC（Internal Rating Card）號碼。 |  |  |  |
| `player_name` | String | Y | 玩家姓名。 |  |  | PII：若對外分享需遮罩。 |
| `casino_player_id` | String | Y | Loyalty 會員號（刷卡/插卡後取得）。 | 可能為 NULL；另有字串 `null` 需視為缺失 |  | [DQ Rule] **Completeness (清洗)**：字串 'null' 佔 0.77%，NULL 佔 27.4%。關聯主檔或計算獨立訪客前必須統一視為缺失。同一玩家被派多 ID 情況少，但存在時間序交疊使用現象。 |
| `table_name` | String | Y | 賭桌名稱。 |  |  |  |
| `buyin` | Decimal(19,4) | Y | 買碼總額（上桌換籌碼）。 | 0.0000 ~ 25656646.0000（metadata；NULL=1,645,264） | `0.0000` |  |
| `bonus` | Decimal(19,4) | Y | 該時段獲得的總獎金。 | 本批多為 0（metadata min=max=0；NULL=49） | `0.0000` |  |
| `turnover_nn` | Decimal(19,4) | Y | 泥碼（NN）投注額。 | 本批固定 0（metadata min=max=0） | `0.0000` |  |
| `casino_loss_from_nn` | Decimal(19,4) | Y | 賭場因泥碼賠付造成的損失。 | 本批固定 0（metadata min=max=0） | `0.0000` |  |
| `num_games_cash` | Int32 | Y | 現金碼下注的局數。 | 0 ~ 108（metadata） | `0` |  |
| `theo_win_cash` | Decimal(19,4) | Y | 來自現金碼的理論贏。 | 0.0000 ~ 2694528.5000（metadata） | `0.0000` |  |
| `nn_taken` | Decimal(19,4) | Y | 賭場殺掉/贏走的泥碼總額。 | 本批固定 0（metadata min=max=0） | `0.0000` |  |
| `isnotified` | Int32 | Y | 旗標：是否已發送通知。 | 本批固定 0（metadata min=max=0） | `0` | 若系統理論上可為 1，建議後續確認此批匯出是否遺漏。 |
| `is_deleted` | Int32 | Y | 旗標：評級是否被刪除。 | 0/1（metadata） | `0` |  |
| `session_end_dtm` | DateTime64(3, UTC) | Y | 時段結束時間。 | 可為 NULL（metadata NULL=89,988） |  |  |
| `clockout_event_id` | String | Y | 離座/Clock-out 事件 ID。 |  |  |  |
| `completion_dtm` | DateTime64(3, UTC) | Y | 評級結算完成時間。 | 幾乎全為 NULL（metadata NULL=74,355,987） |  |  |
| `is_canceled` | Int32 | Y | 旗標：評級是否被取消。 | 0/1（metadata） | `0` |  |
| `status` | String | Y | 時段寫入/處理狀態。 | 常見：空字串、`SUCCESS`、`PROVISIONAL_REJECT`、`PROVISIONAL_SUCCESS`、`PROVISIONAL_PENDING` | `SUCCESS` | [DQ Rule] **Validity (過濾不合格)**：空字串筆數極多（約 4201萬），應與業務確認這是否為『進行中』或『作廢』；若要做報表，可能只能使用 `SUCCESS` 的。 |
| `table_ip` | String | Y | 賭桌設備 IP。 |  |  |  |
| `created_user_id` | Int32 | Y | 建立此紀錄的員工 ID。 | 稀疏欄位（metadata NULL=71,521,958） |  |  |
| `edited_user_id` | Int32 | Y | 最後編輯的員工 ID。 | 稀疏欄位（metadata NULL=71,591,816） |  |  |
| `submitted_user_id` | Int32 | Y | 送出評級的員工 ID。 | 稀疏欄位（metadata NULL=71,572,454） |  |  |
| `approved_user_id` | Int32 | Y | 核准評級的員工/主管 ID。 | 稀疏欄位（metadata NULL=71,524,512） |  |  |
| `created_by_first_name` | String | Y | 建立者名。 |  |  | PII：對外分享需遮罩。 |
| `created_by_last_name` | String | Y | 建立者姓。 |  |  | PII：對外分享需遮罩。 |
| `crtd_dtm` | DateTime64(3, UTC) | Y | 紀錄建立時間。 | 本批範圍（metadata）：2024-07-02T14:21:24Z ~ 2026-02-13T04:42:52Z |  |  |
| `lud_dtm` | DateTime64(3, UTC) | Y | 紀錄最後更新時間。 | 可為 NULL（metadata NULL=1,293） |  |  |
| `submitted_dtm` | DateTime64(3, UTC) | Y | 評級送出時間。 | 稀疏欄位（metadata NULL=71,541,816） |  |  |
| `approved_dtm` | DateTime64(3, UTC) | Y | 評級核准時間。 | 稀疏欄位（metadata NULL=71,524,561） |  |  |
| `approved_user_username` | String | Y | 核准者帳號。 |  |  |  |
| `rating_status` | String | Y | 評級狀態。 | 常見：`CLOSED`、`CANCELED`、`OPEN`、`PENDING`（多數為 NULL） | `CLOSED` | NULL 約 71,521,958。 |
| `is_manual` | Int32 | Y | 旗標：是否為人工建立的評級。 | 0/1 | `0` | [DQ Rule] **Consistency (財務調整)**：`1` 表示純帳務調整；其特徵為 `turnover=0`、經過局數=0，但包含極端的 `player_win`。做行為分析必須排除 `is_manual=1`。 |
| `seat_label` | String | Y | 座位實體標籤。 |  |  |  |
| `chipset_labels` | String | Y | 使用的籌碼組標籤。 |  |  |  |
| `adjusted_hands_played` | Int32 | Y | 手動調整後的打牌局數。 | 0 ~ 108（metadata） | `0` |  |
| `cash_buyins` | Decimal(19,4) | Y | 僅現金換籌碼的金額。 | 0.0000 ~ 25656646.0000（metadata；NULL=49） | `0.0000` |  |
| `ranking` | String | Y | 玩家等級。 |  |  |  |
| `hands_played_adjustment` | Decimal(19,4) | Y | 打牌局數的數值調整量。 | 本批固定 0（metadata min=max=0） | `0.0000` |  |
| `shoe_id` | String | Y | 關聯的牌靴（字串格式；可能跨靴）。 |  |  |  |
| `pit_name` | String | Y | 所在的區域/坑位名稱。 |  |  |  |
| `gaming_area` | String | Y | 所在的博彩區名稱。 |  |  |  |
| `walk_in` | String | Y | 帶入桌的狀態/籌碼清單。 |  |  |  |
| `walk_with` | String | Y | 帶離桌的狀態/籌碼清單。 |  |  |  |
| `player_win_updated` | Decimal(19,4) | Y | 更新/修正後的玩家淨贏損。 | 本批全為 NULL（metadata NULL=74,359,529） |  | 若理論上應有值，代表此批匯出未填或流程未啟用。 |
| `verified_status` | String | Y | 驗證狀態。 | 常見：`VERIFIED`（多數為 NULL） | `VERIFIED` | NULL 約 69,156,208。 |
| `game_type` | String | Y | 遊戲類型。 | 常見：`BACCARAT` | `BACCARAT` |  |
| `group_code` | String | Y | 玩家所屬旅行團/洗碼團代碼。 |  |  |  |
| `rep_code` | String | Y | 負責公關/代理的代碼。 |  |  |  |
| `avg_cash_bet` | Decimal(19,4) | Y | 現金碼的平均每注金額。 | 0.0000 ~ 4083333.0000（metadata） | `0.0000` |  |
| `adjusted_theo_win` | Decimal(19,4) | Y | 調整後的理論贏。 | 0.0000 ~ 2699600.0000（metadata） | `0.0000` |  |
| `color_hsl_code` | String | Y | 系統中代表該玩家的顏色代碼。 |  |  |  |
| `program_id` | Int32 | Y | 參與的行銷計畫 ID。 | 本批全為 NULL（metadata NULL=74,359,529） |  |  |
| `verification_info` | String | Y | 驗證相關備註。 |  |  | 可能包含敏感資訊；分享需審視。 |
| `updated_position_label` | String | Y | 更新後的座位標籤（若有換位）。 |  |  |  |
| `game_variant` | String | Y | 遊戲變體。 | 常見：`baccarat`、少量 `BACCARAT`、或 NULL | `baccarat` | 大小寫不一致，建議標準化。 |
| `issued_token` | Decimal(19,4) | N | 發放的代幣/推廣碼。 | 本批固定 0（metadata min=max=0） | `0.0000` | 若業務上應非固定 0，需確認 ETL/匯出邏輯。 |
| `__ts_ms` | Int64 | Y | CDC 時間戳（毫秒）。 | 稀疏欄位（metadata NULL=72,807,596） |  |  |
| `__op` | String | Y | CDC 操作類型。 | 例如：`c`/`u`/`d` |  |  |
| `__deleted` | String | Y | 軟刪除標記。 |  |  |  |
| `__etl_insert_Dtm` | DateTime64(3, UTC) | N | 匯入資料倉儲的時間。 | 本批範圍（metadata）：2025-05-27T07:45:07Z ~ 2026-02-13T04:43:09Z |  |  |

---

## 4. t_bet (玩家下注紀錄表)
**說明：** 最底層的事實表，記錄每位玩家在每局中的每一筆具體下注行為。

**表級資訊（簡版）**
- **資料來源**：`data/gmwds_t_bet.parquet`（約 438,005,955 列）。
- **粒度（Grain）**：一筆 = 一次具體下注（單注/單一 bet type）。
- **唯一鍵（參考）**：`bet_id`（原始 ClickHouse 設計亦包含 `gaming_day` 作為排序/分區關聯欄位）。
- **時區**：Parquet 時間欄位為 UTC；下游使用/對帳若以 `Asia/Shanghai` 為準，需統一轉換。
- **已知資料狀況（本批觀察）**：
  - `wager_nn` 全為 0（本批）。
  - 多個欄位在本批呈現全 NULL（如 `bonus`、`tip_amount`、`payout_value` 等），可能代表功能未啟用或匯出未帶出。
  - `bet_reconciled_at` 大量為 NULL，且非 NULL 值可能出現 `1970-01-01` 預設時間（需當作未知處理）。

| 欄位名稱 | 型別 | 可空 | 業務定義 | 值域/格式/枚舉 | 範例值 | 注意事項/已知問題 |
|---|---|---|---|---|---|---|
| `bet_id` | Int64 | N | 下注紀錄唯一識別碼。 | 101001 ~ 582554361（metadata） | `101001` |  |
| `is_back_bet` | Int32 | Y | 旗標：是否為飛牌/背後下注（非坐下玩家）。 | 0/1 | `0` |  |
| `base_ha` | Decimal(19,4) | Y | 該玩法的基礎賭場優勢（House Advantage）。 | 0.0100 ~ 0.1830（metadata） | `0.0100` |  |
| `bet_type` | String | Y | 下注類型系統代碼。 | 常見：`BANKER`、`PLAYER`、`BIG_TIGER`、`SMALL_TIGER`、`DRAGON_TIGER`、`SUPER_SEVEN`、`LUCKY_SIX`、`BIG_DRAGON`、`SMALL_DRAGON`、`TIE`、`BANKER_PAIR`、`PLAYER_PAIR` | `BANKER` | 枚舉值建議以全表頻率表定期更新。 |
| `bet_uuid` | String | N | 下注紀錄 UUID。 | UUID 字串 |  |  |
| `bonus` | Decimal(19,4) | Y | 此注獲得的額外獎金。 | 本批全為 NULL（metadata） |  |  |
| `casino_loss_from_nn` | Decimal(19,4) | Y | 賭場因泥碼賠付造成的損失。 | 本批固定 0（metadata） | `0.0000` |  |
| `casino_win` | Decimal(19,4) | Y | 賭場在此注的淨贏損（負數代表玩家贏）。 | 本批範圍（全表計算）：-110000000.0000 ~ 10000000.0000 |  | [DQ Rule] **Consistency (派彩比例)**：極端值雖大（虧損破億），但經實測，最大贏利與 wager 比率恰好為 100 倍以內，完全對應 `payout_odds` 的規則，屬真實邏輯紀錄（無 `is_manual` 灌水現象）。 |
| `commission` | Decimal(19,4) | Y | 抽水金額（例如莊贏抽 5%）。 | 0.0000 ~ 1500000.0000（metadata；NULL=292,154,673） | `0.0000` | NULL 很多，代表多數下注類型不抽水或未填。 |
| `game_id` | Int64 | Y | 關聯牌局 ID。 | 101039 ~ 360154108（metadata） |  |  |
| `is_lump_sum_payout` | Int32 | Y | 旗標：是否為一次性打包派彩。 | 本批固定 0（metadata min=max=0） | `0` |  |
| `max_wager` | Decimal(19,4) | Y | 該位置的最大允許下注額。 | 20000.0000 ~ 1000000.0000（metadata；NULL=266,357,217） |  |  |
| `payout_complete_dtm` | DateTime64(3, UTC) | Y | 派彩完成時間。 | 2024-07-02T14:21:24Z ~ 2026-02-13T03:53:30Z（metadata） |  |  |
| `gaming_day` | Date32 | N | 營業日/賬務日。 | 本批範圍（metadata）：2024-07-02 ~ 2026-02-13 | `2026-02-13` |  |
| `payout_ha` | Decimal(19,4) | Y | 派彩時套用的賭場優勢。 | 0.0200 ~ 0.1900（metadata） |  |  |
| `payout_odds` | Decimal(19,4) | Y | 派彩賠率。 | 0.0000 ~ 100.0000（metadata） |  |  |
| `player_id` | Int64 | Y | Smart Table 系統內部玩家識別碼。 | 觀察到 `-1`（placeholder）；其餘約 -1 ~ 172596756（metadata） |  | `-1` 全表計算約 30 筆。 |
| `position_code` | String | Y | 下注位置代碼。 | 常見：`PLAYER_01`~`PLAYER_06`（另有 NULL） | `PLAYER_03` |  |
| `position_idx` | Int32 | Y | 下注位置索引。 | 0 ~ 6（metadata） | `3` |  |
| `position_label` | String | Y | 下注位置標籤。 | 常見為數字字串（如 `1`~`6`） | `3` |  |
| `session_id` | Int64 | Y | 關聯玩家時段 ID。 | 101001 ~ 213451688（metadata） |  |  |
| `is_settled` | Int32 | Y | 旗標：是否已結算完成。 | 0/1 | `1` |  |
| `status` | String | Y | 下注結果狀態。 | `WIN` / `LOSE` / `PUSH` | `WIN` |  |
| `std_dev` | Decimal(19,4) | Y | 此注的標準差（用於波動/風險計算）。 | 4.6400 ~ 15774300.0000（metadata） |  |  |
| `table_id` | Int32 | N | 關聯賭桌 ID。 | 1005 ~ 152651001（metadata） | `1005` |  |
| `theo_win` | Decimal(19,4) | Y | 此注的理論贏（預期利潤）。 | 0.0600 ~ 716500.0000（metadata） |  |  |
| `theo_win_cash` | Decimal(19,4) | Y | 現金碼部分的理論贏。 | 0.0000 ~ 716500.0000（metadata） |  |  |
| `true_odds` | Decimal(19,4) | Y | 真實賠率/真實機率指標。 | 0.9800 ~ 12.3900（metadata） |  |  |
| `wager` | Decimal(19,4) | Y | 下注金額（現金碼）。 | 5.0000 ~ 10000000.0000（metadata；與全表計算一致） | `5.0000` |  |
| `wager_nn` | Decimal(19,4) | Y | 下注金額（泥碼/洗碼）。 | 本批固定 0（metadata min=max=0） | `0.0000` |  |
| `chips_paid` | String | Y | 派彩籌碼清單（明細）。 |  |  |  |
| `chips_wagered` | String | Y | 下注籌碼清單（明細）。 |  |  |  |
| `chipsvalue_by_chipset` | String | Y | 依籌碼組彙總的價值。 |  |  |  |
| `chipset_label` | String | Y | 籌碼組標籤。 |  |  |  |
| `tip_amount` | Decimal(19,4) | Y | 實體小費金額。 | 本批全為 NULL（metadata） |  |  |
| `chips_tip` | String | Y | 小費籌碼清單。 |  |  |  |
| `bet_cards` | String | Y | 與此注相關的牌面紀錄。 |  |  |  |
| `increment_wager` | Decimal(19,4) | Y | 增量/追加下注額。 | 本批全為 NULL（metadata） |  |  |
| `bet_cards_sum` | Decimal(19,4) | Y | 牌面總和。 | 本批全為 NULL（metadata） |  |  |
| `adjusted_theo_win` | Decimal(19,4) | Y | 調整後的理論贏。 | 0.0600 ~ 716500.0000（metadata） |  |  |
| `short_bet_name_en` | String | Y | 下注類型英文簡寫。 |  |  |  |
| `short_bet_name_zh` | String | Y | 下注類型中文簡寫。 |  |  |  |
| `mixed_stack` | Int32 | Y | 旗標：是否為混合籌碼疊（現金+泥碼等）。 | 0/1 | `0` |  |
| `auto_resolve_stack` | Int32 | Y | 旗標：籌碼疊是否由系統自動解析。 | 0/1 | `1` |  |
| `type_of_bet` | String | Y | 下注大分類。 | 常見：`MAIN_BET`、`SIDE_BET`（另有 NULL） | `MAIN_BET` |  |
| `bet_payout_type` | String | Y | 派彩類型標記。 |  |  |  |
| `payout_value` | Decimal(19,4) | Y | 派彩給玩家的總金額（本金+贏利）。 | 本批全為 NULL（metadata） |  |  |
| `bonus_game_offered` | Int32 | Y | 旗標：是否觸發 Bonus 遊戲。 | 非 NULL 時固定 0（metadata min=max=0；NULL=132,340,805） | `0` |  |
| `is_jackpot` | Int32 | Y | 旗標：是否贏得 Jackpot。 | 非 NULL 時固定 0（metadata min=max=0；NULL=132,340,805） | `0` |  |
| `bet_reconciled_at` | DateTime64(3, UTC) | Y | 下注帳務對帳完成時間。 | 大量 NULL（metadata NULL=184,396,107） |  | [DQ Rule] **Completeness (全廢)**：非 NULL 的 2.5 億筆全為 UNIX epoch `1970-01-01`，有效值=0，不能用於任何對帳過濾。 |
| `__ts_ms` | Int64 | Y | CDC 時間戳（毫秒）。 | 稀疏欄位（metadata NULL=375,189,506） |  |  |
| `__op` | String | Y | CDC 操作類型。 | 例如：`c`/`u`/`d` |  |  |
| `__deleted` | String | Y | 軟刪除標記。 |  |  |  |
| `__etl_insert_Dtm` | DateTime64(3, UTC) | N | 匯入資料倉儲的時間。 | 2025-05-27T08:27:48Z ~ 2026-02-13T03:53:40Z（metadata） |  |  |
