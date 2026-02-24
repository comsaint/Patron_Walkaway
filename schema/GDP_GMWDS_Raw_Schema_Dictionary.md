# GDP_GMWDS_Raw 資料庫綱要文件

**系統脈絡：** 本資料集代表百家樂賭場管理系統（CMS），層級架構為：`t_shoe` (牌靴) → `t_game` (牌局) → `t_session` (玩家時段) → `t_bet` (單筆下注)。

**資料實況（本次 Parquet 匯出）**：時間欄位在 Parquet 中以 `timestamp[ms, tz=UTC]` 儲存；如需對齊原系統/ClickHouse 的 `Asia/Shanghai`，請於使用端做時區轉換。`gaming_day` 範圍約為 2024-07-02 至 2026-02-13。

---

## 1. t_shoe (牌靴維度表)
**說明：** 記錄實體牌靴的生命週期與總體數據（通常一副牌靴包含 8 副撲克牌）。

| 欄位名稱 | 資料型別 | 說明 | 範例資料 |
| :--- | :--- | :--- | :--- |
| `shoe_id` | Int64 | 牌靴的唯一識別碼 (Primary Key)。 | |
| `table_id` | Int32 | 該副牌所在的賭桌 ID。 | |
| `shoe_start_dtm` | DateTime64 | 牌靴開始使用的時間 (Primary Key)。 | |
| `shoe_end_dtm` | DateTime64 | 牌靴結束/作廢的時間。 | |
| `initial_card_count` | Int32 | 初始總牌數 (例如 8副牌為 416)。 | |
| `current_card_count` | String | 目前剩餘牌數。 | |
| `casino_win` | Decimal(19,4) | 該牌靴週期內，賭場的總淨贏損。 | |
| `__ts_ms` | Int64 | 原始資料庫 CDC 時間戳 (毫秒)。 | |
| `__op` | String | CDC 操作類型 (如 c, u, d)。 | |
| `__deleted` | String | 軟刪除標記。 | |
| `__etl_insert_Dtm` | DateTime | 資料匯入資料倉儲的時間。 | |

---

## 2. t_game (牌局維度表)
**說明：** 記錄單一牌局（Hand）的發牌結果、時間戳記與總體財務結算。

| 欄位名稱 | 資料型別 | 說明 | 範例資料 |
| :--- | :--- | :--- | :--- |
| `game_id` | Int64 | 牌局的唯一識別碼 (Primary Key)。 | |
| `gaming_day` | Date32 | 營業日/賬務日 (Primary Key)。 | |
| `game_uuid` | String | 牌局的 UUID。 | |
| `table_id` | Int32 | 關聯的賭桌 ID。 | |
| `table_name` | String | 賭桌名稱。 | |
| `table_type` | String | 賭桌類型。 | |
| `table_limits` | String | 賭桌限紅。 | |
| `table_template` | String | 賭桌系統樣板設定。 | |
| `shoe_id` | Int32 | 關聯的牌靴 ID。 | |
| `shoe_game_count` | Int64 | 該局在此牌靴中的順序編號 (第幾把)。 | |
| `pit_name` | String | 所在的區域/坑位名稱。 | |
| `gaming_area` | String | 所在的博彩區名稱。 | |
| `game_status` | String | 牌局狀態 (如 OPEN, CLOSED)。 | |
| `game_type` | String | 遊戲類型 (如 Baccarat)。 | |
| `game_variant` | String | 遊戲變體 (如 免佣百家樂)。 | |
| `game_result` | String | 牌局結果字串。 | |
| `outcome` | String | 輸贏結果 (如 BANKER, PLAYER, TIE)。 | |
| `player_score` | Int32 | 閒家最終點數 (0-9)。 | |
| `banker_score` | Int32 | 莊家最終點數 (0-9)。 | |
| `num_cards_drawn` | Int32 | 本局總發牌數。 | |
| `card_p1` | String | 閒家第 1 張牌。 | |
| `card_p2` | String | 閒家第 2 張牌。 | |
| `card_p3` | String | 閒家第 3 張牌 (補牌)。 | |
| `card_b1` | String | 莊家第 1 張牌。 | |
| `card_b2` | String | 莊家第 2 張牌。 | |
| `card_b3` | String | 莊家第 3 張牌 (補牌)。 | |
| `lucky_card` | String | 幸運牌標記。 | |
| `dealer_cards` | String | 荷官發牌紀錄。 | |
| `dealer_cards_sum` | Decimal(19,4) | 荷官牌面總值。 | |
| `dealer_cards_metadata` | String | 發牌的額外 metadata。 | |
| `is_player_pair` | Int32 | 旗標：閒家是否起手對子。 | |
| `is_banker_pair` | Int32 | 旗標：莊家是否起手對子。 | |
| `is_lucky_six` | Int32 | 旗標：是否符合幸運6 (Super 6) 條件。 | |
| `is_lucky8_player` | Int32 | 旗標：閒家幸運8條件。 | |
| `is_lucky8_banker` | Int32 | 旗標：莊家幸運8條件。 | |
| `is_commissioned` | UInt8 | 旗標：本局是否抽水。 | |
| `total_turnover` | Decimal(19,4) | 本局總投注額 (泥碼+現金碼)。 | |
| `adjusted_turnover` | Decimal(19,4) | 調整後的總投注額。 | |
| `total_pushed_wagers` | Decimal(19,4) | 總退回注額 (如和局退還莊閒下注)。 | |
| `total_contra_wagers` | Decimal(19,4) | 對沖下注總額。 | |
| `total_pushed_contra_wagers` | Decimal(19,4) | 退回的對沖下注總額。 | |
| `casino_win` | Decimal(19,4) | 賭場本局淨贏損。 | |
| `theo_win` | Decimal(19,4) | 賭場本局理論贏 (預期利潤)。 | |
| `table_exposure` | Decimal(19,4) | 賭桌風險敞口 (最大可能賠付額)。 | |
| `game_variance` | Decimal(19,4) | 本局贏損差異值。 | |
| `bonus` | Decimal(19,4) | 本局發出的獎金。 | |
| `dealer_id` | Int32 | 荷官 ID。 | |
| `dealer_first_name` | String | 荷官名。 | |
| `dealer_last_name` | String | 荷官姓。 | |
| `dealer_employee_number` | String | 荷官員工編號。 | |
| `supervisor_id` | Int32 | 監場/主管 ID。 | |
| `supervisor_first_name` | String | 監場名。 | |
| `supervisor_last_name` | String | 監場姓。 | |
| `supervisor_employee_number` | String | 監場員工編號。 | |
| `num_positions` | Int32 | 本局開放下注的位置數。 | |
| `num_players` | Int32 | 本局實際下注玩家數。 | |
| `prev_game_end_dtm` | DateTime64 | 上一局結束時間。 | |
| `game_start_dtm` | DateTime64 | 本局開始時間。 | |
| `card_p1_draw_dtm` | DateTime64 | 閒家第 1 張牌抽出時間。 | |
| `card_p2_draw_dtm` | DateTime64 | 閒家第 2 張牌抽出時間。 | |
| `card_p3_draw_dtm` | DateTime64 | 閒家第 3 張牌抽出時間。 | |
| `card_b1_draw_dtm` | DateTime64 | 莊家第 1 張牌抽出時間。 | |
| `card_b2_draw_dtm` | DateTime64 | 莊家第 2 張牌抽出時間。 | |
| `card_b3_draw_dtm` | DateTime64 | 莊家第 3 張牌抽出時間。 | |
| `card_p1_turn_dtm` | DateTime64 | 閒家第 1 張牌翻開時間。 | |
| `card_p2_turn_dtm` | DateTime64 | 閒家第 2 張牌翻開時間。 | |
| `card_p3_turn_dtm` | DateTime64 | 閒家第 3 張牌翻開時間。 | |
| `card_b1_turn_dtm` | DateTime64 | 莊家第 1 張牌翻開時間。 | |
| `card_b2_turn_dtm` | DateTime64 | 莊家第 2 張牌翻開時間。 | |
| `card_b3_turn_dtm` | DateTime64 | 莊家第 3 張牌翻開時間。 | |
| `t4_take_begin_dtm` | DateTime64 | 收拾籌碼開始時間 (階段4)。 | |
| `t4_take_end_dtm` | DateTime64 | 收拾籌碼結束時間 (階段4)。 | |
| `rb_take_begin_dtm` | DateTime64 | 退回籌碼開始時間。 | |
| `payout_complete_dtm` | DateTime64 | 派彩完成時間。 | |
| `gaming_day_first_game` | UInt8 | 旗標：是否為該營業日的首局。 | |
| `include_in_aggregation` | UInt8 | 旗標：是否計入報表統計。 | |
| `game_errors` | String | 本局發生的硬體/操作錯誤紀錄。 | |
| `prg_template_id` | Int64 | 程式/行銷樣板 ID。 | |
| `__ts_ms` | Int64 | CDC 時間戳 (毫秒)。 | |
| `__op` | String | CDC 操作類型。 | |
| `__deleted` | String | 軟刪除標記。 | |
| `__etl_insert_Dtm` | DateTime64 | 匯入資料倉儲時間。 | |

---

## 3. t_session (玩家打牌時段表)
**說明：** 記錄單一玩家在單一賭桌上一段連續打牌的過程（公關評級依據）。

| 欄位名稱 | 資料型別 | 說明 | 範例資料 |
| :--- | :--- | :--- | :--- |
| `session_id` | Int64 | 時段紀錄唯一識別碼 (Primary Key)。 | |
| `gaming_day` | Date32 | 營業日/賬務日 (Primary Key)。 | |
| `table_id` | Int32 | 關聯的賭桌 ID。 | |
| `table_name` | String | 賭桌名稱。 | |
| `pit_name` | String | 所在的區域/坑位名稱。 | |
| `gaming_area` | String | 所在的博彩區名稱。 | |
| `table_ip` | String | 賭桌設備 IP。 | |
| `shoe_id` | String | 關聯的牌靴 (字串格式，可能橫跨多靴)。 | |
| `player_id` | Int64 | 系統內部玩家 ID。 | |
| `casino_player_id` | String | 賭場業務用的玩家 ID (如會員號)；可能為 NULL（未插卡/無會員）。另觀察到字串值 `null`，建議視為缺失值一併清理。 | |
| `player_name` | String | 玩家姓名。 | |
| `is_known_player` | Int32 | 旗標：是否為具名會員 (非散客)。取值通常為 0/1。 | |
| `irc_number` | String | IRC (Internal Rating Card) 號碼。 | |
| `group_code` | String | 玩家所屬旅行團/洗碼團代碼。 | |
| `rep_code` | String | 負責公關/代理的代碼。 | |
| `program_id` | Int32 | 參與的行銷計畫 ID。 | |
| `ranking` | String | 玩家等級。 | |
| `position_label` | String | 玩家座位標籤 (如 Seat 1)。 | |
| `updated_position_label` | String | 更新後的座位標籤 (若有換位)。 | |
| `seat_label` | String | 座位實體標籤。 | |
| `session_start_dtm` | DateTime64 | 時段開始時間（Parquet 以 UTC 儲存）。 | |
| `session_end_dtm` | DateTime64 | 時段結束時間（Parquet 以 UTC 儲存）；少量為 NULL。 | |
| `clockin_event_dtm` | DateTime64 | 打卡上班/入座時間（Parquet 以 UTC 儲存）。 | |
| `first_wager_game_start_dtm` | DateTime64 | 首次下注的牌局開始時間（Parquet 以 UTC 儲存）。 | |
| `last_wager_game_end_dtm` | DateTime64 | 最後一次下注的牌局結束時間（Parquet 以 UTC 儲存）。 | |
| `completion_dtm` | DateTime64 | 評級結算完成時間（Parquet 以 UTC 儲存）；在本次匯出中多數為 NULL（分層抽樣觀察空值率約 99.99%）。 | |
| `clockin_event_id` | String | 入座事件 ID。 | |
| `clockout_event_id` | String | 離座事件 ID。 | |
| `clockin_event_username` | String | 協助入座打卡的操作員帳號。 | |
| `num_games_elapsed` | Int32 | 玩家在座期間經過的總局數。 | |
| `num_games_with_wager` | Int32 | 玩家實際有下注的局數。 | |
| `num_games_cash` | Int32 | 玩家使用現金碼下注的局數。 | |
| `num_bets` | Int32 | 總下注次數。 | |
| `adjusted_hands_played` | Int32 | 手動調整後的打牌局數。 | |
| `hands_played_adjustment` | Decimal(19,4) | 打牌局數的數值調整量。 | |
| `buyin` | Decimal(19,4) | 買碼總額 (上桌換籌碼)。 | |
| `cash_buyins` | Decimal(19,4) | 僅現金換籌碼的金額。 | |
| `chips_in` | Decimal(19,4) | 帶上桌的初始籌碼總值。 | |
| `turnover` | Decimal(19,4) | 總投注額。 | |
| `adjusted_turnover` | Decimal(19,4) | 調整後的總投注額。 | |
| `turnover_nn` | Decimal(19,4) | 使用泥碼 (Non-Negotiable) 的總投注額。 | |
| `turnover_pushed_wagers` | Decimal(19,4) | 因和局退回的投注額。 | |
| `turnover_contra_wagers` | Decimal(19,4) | 對沖下注總額。 | |
| `turnover_pushed_contra_wagers` | Decimal(19,4) | 退回的對沖下注總額。 | |
| `player_win` | Decimal(19,4) | 玩家總淨贏損 (正為贏，負為輸)。 | |
| `player_win_updated` | Decimal(19,4) | 更新/修正後的玩家淨贏損。 | |
| `theo_win` | Decimal(19,4) | 賭場在此時段賺取的總理論贏。 | |
| `theo_win_cash` | Decimal(19,4) | 來自現金碼的總理論贏。 | |
| `adjusted_theo_win` | Decimal(19,4) | 調整後的理論贏。 | |
| `avg_bet` | Decimal(19,4) | 平均每注金額。 | |
| `avg_cash_bet` | Decimal(19,4) | 現金碼的平均每注金額。 | |
| `casino_loss_from_nn` | Decimal(19,4) | 賭場因泥碼賠付所造成的損失。 | |
| `nn_taken` | Decimal(19,4) | 賭場殺掉/贏走的泥碼總額。 | |
| `bonus` | Decimal(19,4) | 該時段獲得的總獎金。 | |
| `issued_token` | Decimal(19,4) | 發放的代幣/推廣碼。 | |
| `walk_in` | String | 帶入桌的狀態/籌碼清單。 | |
| `walk_with` | String | 帶離桌的狀態/籌碼清單。 | |
| `chipset_labels` | String | 使用的籌碼組標籤。 | |
| `color_hsl_code` | String | 系統中代表該玩家的顏色代碼。 | |
| `game_type` | String | 遊戲類型 (如 Baccarat)。 | |
| `game_variant` | String | 遊戲變體。 | |
| `status` | String | 時段寫入/處理狀態；本次匯出常見：`SUCCESS`、`PROVISIONAL_SUCCESS`、`PROVISIONAL_REJECT`、`PROVISIONAL_PENDING`、空字串（未填）。 | |
| `rating_status` | String | 評級狀態；多為 NULL，常見值：`CLOSED`、`PENDING`、`CANCELED`（分層抽樣觀察空值率約 97.8%）。 | |
| `verified_status` | String | 驗證狀態；本次匯出常見：`VERIFIED` 或 NULL（分層抽樣觀察空值率約 74.7%）。 | |
| `verification_info` | String | 驗證相關備註。 | |
| `casino_open_rating_id` | String | 賭場系統的開台評級 ID。 | |
| `casino_close_rating_id` | String | 賭場系統的關台評級 ID。 | |
| `is_manual` | Int32 | 旗標：是否為人工建立的評級。取值通常為 0/1。 | |
| `is_canceled` | Int32 | 旗標：評級是否被取消。取值通常為 0/1。 | |
| `is_deleted` | Int32 | 旗標：評級是否被刪除。取值通常為 0/1。 | |
| `isnotified` | Int32 | 旗標：是否已發送通知。取值通常為 0/1；分層抽樣觀察皆為 0（可能仍存在其他值）。 | |
| `created_user_id` | Int32 | 建立此紀錄的員工 ID。 | |
| `created_by_first_name` | String | 建立者名。 | |
| `created_by_last_name` | String | 建立者姓。 | |
| `edited_user_id` | Int32 | 最後編輯的員工 ID。 | |
| `submitted_user_id` | Int32 | 送出評級的員工 ID。 | |
| `approved_user_id` | Int32 | 核准評級的員工/主管 ID。 | |
| `approved_user_username` | String | 核准者帳號。 | |
| `crtd_dtm` | DateTime64 | 紀錄建立時間。 | |
| `lud_dtm` | DateTime64 | 紀錄最後更新時間。 | |
| `submitted_dtm` | DateTime64 | 評級送出時間。 | |
| `approved_dtm` | DateTime64 | 評級核准時間。 | |
| `__ts_ms` | Int64 | CDC 時間戳 (毫秒)。 | |
| `__op` | String | CDC 操作類型。 | |
| `__deleted` | String | 軟刪除標記。 | |
| `__etl_insert_Dtm` | DateTime64 | 匯入資料倉儲時間。 | |

---

## 4. t_bet (玩家下注紀錄表)
**說明：** 最底層的事實表，記錄每位玩家在每局中的每一筆具體下注行為。

| 欄位名稱 | 資料型別 | 說明 | 範例資料 |
| :--- | :--- | :--- | :--- |
| `bet_id` | Int64 | 下注紀錄的唯一識別碼 (Primary Key)。 | |
| `gaming_day` | Date32 | 營業日/賬務日 (Primary Key)。 | |
| `bet_uuid` | String | 下注紀錄的 UUID。 | |
| `game_id` | Int64 | 關聯的牌局 ID。 | |
| `session_id` | Int64 | 關聯的玩家時段 ID。 | |
| `player_id` | Int64 | 下注玩家 ID。 | |
| `table_id` | Int32 | 關聯的賭桌 ID。 | |
| `position_code` | String | 下注位置代碼；本次匯出常見形態為 `PLAYER_01` ~ `PLAYER_06`，少量為 NULL。 | |
| `position_idx` | Int32 | 下注位置索引。 | |
| `position_label` | String | 下注位置標籤；本次匯出常見為數字字串（如 `1`~`6`），少量為 NULL。 | |
| `is_back_bet` | Int32 | 旗標：是否為飛牌/背後下注 (非坐下玩家)。取值通常為 0/1。 | |
| `bet_type` | String | 下注類型系統代碼；本次匯出常見：`BANKER`、`PLAYER`、`TIE`、`BANKER_PAIR`、`PLAYER_PAIR`、`LUCKY_SIX`、`BIG_TIGER`、`SMALL_TIGER`。 | |
| `type_of_bet` | String | 下注的大分類；本次匯出常見：`MAIN_BET`、`SIDE_BET`。 | |
| `short_bet_name_en` | String | 下注類型英文簡寫。 | |
| `short_bet_name_zh` | String | 下注類型中文簡寫。 | |
| `wager` | Decimal(19,4) | 下注金額 (現金碼)。 | |
| `wager_nn` | Decimal(19,4) | 下注金額 (泥碼/洗碼)。在本次匯出中觀察到全為 0。 | |
| `max_wager` | Decimal(19,4) | 該位置的最大允許下注額。 | |
| `increment_wager` | Decimal(19,4) | 增量/追加的下注額。 | |
| `payout_value` | Decimal(19,4) | 派彩給玩家的總金額 (本金+贏利)。 | |
| `casino_win` | Decimal(19,4) | 賭場在此注的淨贏損 (負數代表玩家贏)。 | |
| `commission` | Decimal(19,4) | 賭場抽水金額 (如莊贏抽 5%)。 | |
| `bonus` | Decimal(19,4) | 此注獲得的額外獎金。 | |
| `casino_loss_from_nn` | Decimal(19,4) | 賭場因泥碼賠付造成的損失。 | |
| `tip_amount` | Decimal(19,4) | 玩家給予的實體小費金額。 | |
| `base_ha` | Decimal(19,4) | 該玩法的基礎賭場優勢 (House Advantage %)。 | |
| `payout_ha` | Decimal(19,4) | 派彩時套用的賭場優勢。 | |
| `payout_odds` | Decimal(19,4) | 派彩賠率 (如 0.95 或 8.0)。 | |
| `true_odds` | Decimal(19,4) | 真實機率/真實賠率。 | |
| `theo_win` | Decimal(19,4) | 此注的理論贏 (下注額 * base_ha)。 | |
| `theo_win_cash` | Decimal(19,4) | 現金碼部分的理論贏。 | |
| `adjusted_theo_win` | Decimal(19,4) | 調整後的理論贏。 | |
| `std_dev` | Decimal(19,4) | 此注的標準差 (用於計算贏損波動)。 | |
| `bet_cards` | String | 與此注相關的牌面紀錄。 | |
| `bet_cards_sum` | Decimal(19,4) | 牌面總和。 | |
| `chips_wagered` | String | 具體下注的籌碼清單 (RFID 或影像辨識)。 | |
| `chips_paid` | String | 具體派彩的籌碼清單。 | |
| `chips_tip` | String | 作為小費的籌碼清單。 | |
| `chipset_label` | String | 使用的籌碼組標籤。 | |
| `chipsvalue_by_chipset` | String | 依據籌碼組計算的價值。 | |
| `mixed_stack` | Int32 | 旗標：是否為混合籌碼疊 (現金+泥碼)。 | |
| `auto_resolve_stack` | Int32 | 旗標：籌碼疊是否由系統自動解析。 | |
| `bet_payout_type` | String | 派彩類型標記。 | |
| `is_lump_sum_payout` | Int32 | 旗標：是否為一次性打包派彩。 | |
| `bonus_game_offered` | Int32 | 旗標：是否觸發 Bonus 遊戲。 | |
| `is_jackpot` | Int32 | 旗標：是否贏得 Jackpot。 | |
| `status` | String | 下注結果狀態；本次匯出常見：`WIN`、`LOSE`、`PUSH`。 | |
| `is_settled` | Int32 | 旗標：是否已結算完成。取值通常為 0/1（抽樣中以 0 為主）。 | |
| `payout_complete_dtm` | DateTime64 | 派彩完成時間（Parquet 以 UTC 儲存）。 | |
| `bet_reconciled_at` | DateTime64 | 下注帳務對帳完成時間（Parquet 以 UTC 儲存）；在本次匯出中大量為 NULL，且非 NULL 值疑似出現 `1970-01-01` 預設時間，使用時請特別留意。 | |
| `__ts_ms` | Int64 | CDC 時間戳 (毫秒)。 | |
| `__op` | String | CDC 操作類型。 | |
| `__deleted` | String | 軟刪除標記。 | |
| `__etl_insert_Dtm` | DateTime64 | 匯入資料倉儲時間。 | |
