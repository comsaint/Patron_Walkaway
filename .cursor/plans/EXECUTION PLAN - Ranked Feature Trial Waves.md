# EXECUTION PLAN - Ranked Feature Trial Waves（中文）

## 文件層級與目的

本文件屬於 **Working / Execution Plan（執行計畫）**，用於落地執行已排序之 feature 試作。
本計畫對應 `trainer/precision_improvement_plan/Feature Engineering Suggestions.md` 中的排名章節。

## 範圍

- 納入範圍：
  - 僅限 rated patrons。
  - 僅限由 `t_bet` / `t_session` 衍生之特徵，以及由這兩者衍生而來的既有 `player_profile` 欄位。
  - 試作波次、feature screening、go/no-go 決策。
- 不納入範圍：
  - 任何需要 `t_game` 或新增外部資料表的特徵。
  - trainer / scorer / backtester 架構重設計。

## 主要約束與護欄

- 特徵定義與缺值/零分母契約需維持 train-serve-backtest parity。
- 所有 uplift 決策採 rated-only 評估口徑。
- 每個波次需記錄 runtime 與 memory 變化；未經明確同意，不接受顯著回歸。

## 目前進度快照（Status Snapshot）

更新日期：2026-04-23

- Wave 1：🟡 進行中（已完成實作與契約修正，待 screening/ablation）
- Wave 2：⬜ 尚未開始
- Wave 3：⬜ 尚未開始

### Wave 1 詳細狀態

已完成（Done）：
1. 已新增 Wave 1 候選特徵：
   - `net_win_in_run_so_far`
   - `net_win_per_bet_in_run`
   - `wager_slope_w10bets`
   - `wager_w5m_over_w15m`
2. 已完成 run-boundary 家族契約補強：
   - `function_name` 對應修正（`compute_run_boundary_features` wrapper）
   - `casino_win` 輸入欄位補齊
   - `wager` 輸入欄位補齊（避免 `wager_sum_in_run_so_far` 全零）
3. 已補基礎防回歸測試（spec 層），避免 run-boundary `input_columns` 再次漏 `wager`。

進行中（In Progress）：
1. Wave 1 第一輪 screening 與 ablation（尚未產出正式 uplift 結果）。
2. runtime / memory 成本量測與門檻對照（尚待寫入正式紀錄）。

下一步（Next）：
1. 以目前 baseline 執行 Wave 1 screening（rated-only）。
2. 輸出 precision / recall / PR-AUC / alert volume 與成本（runtime、memory）對照。
3. 依證據做 go/no-go，更新 `STATUS.md` 與本文件狀態燈號。

阻塞與風險（Blockers/Risks）：
1. 若未先定義可接受的 runtime/memory 門檻，go/no-go 容易主觀化。
2. 若僅單一切分有提升，可能是切片偶然性而非穩定 uplift。

## 執行波次

### Wave 1（最高優先、預期最快產生增益）

目標特徵：
- `net_win_in_run_so_far`
- `net_win_per_bet_in_run`
- `wager_slope_w10bets`
- `wager_w5m_over_w15m`

任務：
1. 新增候選與契約（含分母為 0 / null handling）。
2. 以現行 baseline 執行 screening 與 ablation。
3. 記錄 uplift、precision/recall 變化、runtime/memory 影響。

退出條件：
- 至少 1 個 Wave 1 特徵在可接受成本下呈現穩定增量 uplift。

### Wave 2（行為狀態 + 個人化延伸）

目標特徵：
- `consecutive_non_win_cnt`
- `push_cnt_w15m`
- `non_win_rate_w15m`
- A4/A5 personalized baseline（使用既有 `player_profile` 欄位組合）

任務：
1. 以明確零分母 / NA 契約完成實作。
2. 以 Wave 1 勝出特徵為固定 baseline 重新 screening。
3. 依資料切分與量體分層做穩定性檢查。

退出條件：
- 至少 1 個 Wave 2 特徵（或組合）在 Wave 1 基礎上有明確增量。

### Wave 3（低優先補強候選）

目標特徵：
- `turnover_per_bet_30d_over_180d`
- `turnover_30d_over_180d`
- `sessions_30d_over_180d`
- `run_loss_acceleration`
- `table_turnover_w5m_over_w15m`（`t_bet` 版本）
- `patron_share_of_table_turnover_w15m`（`t_bet` 版本）

任務：
1. 以小批次（每次 2-3 個）新增並 screening。
2. 若特徵高度共線或冗餘，除非改善營運指標，否則不納入。
3. 收斂可升級至 delivery plan 的短名單。

退出條件：
- 產出具排名理由與成本效益證據的升級短名單。

## 驗證與決策規則

- 主要指標：precision、recall、PR-AUC、alert volume 穩定度。
- 次要指標：校準穩定度、feature importance 一致性、runtime/memory 開銷。
- 升級規則：
  - 僅保留具明確增量 uplift 且營運成本可接受的特徵。
  - 對 uplift 邊際但計算/記憶體成本高的特徵，延後處理。

## 交付物

1. 每個波次的試作紀錄，寫入 `STATUS.md`（參數、特徵、指標、決策）。
2. 最終升級特徵短名單與理由。
3. Precision uplift 相關規劃文件之更新備註。

## 風險與緩解

- 契約漂移風險（NA / 零分母語義不一致）：
  - 緩解：screening 前先定義統一契約，並在三端共用。
- 桌級特徵導致 runtime/memory 回歸：
  - 緩解：Wave 3 升級前，必須通過 overhead 門檻檢查。
- 單一切片過擬合：
  - 緩解：升級前要求跨切分 / 跨時間窗穩定性驗證。

## 假設

- 現行訓練路徑已落實 rated-only early-prune policy。
- 既有 profile 特徵在 rated 路徑可用且符合 PIT 安全。

## 開放問題

- 各波次 runtime/memory 的可接受門檻是否已明確定義。
- 每一輪可升級進下一版 delivery increment 的 feature 上限數量。

## 下一步

啟動 Wave 1 實作與第一輪 screening，完成結果紀錄後再決定 Wave 2 進場清單。
