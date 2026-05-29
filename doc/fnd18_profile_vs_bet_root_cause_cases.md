# FND-18 個案根因清單：`canonical_patron_profile` vs cleaned `t_bet`

日期：2026-05-13  
範圍：`|profile_total_theo_win - bet_total_theo_win| >= 100,000` 的 42 位 canonical patrons。

**可重現驗證（clone 後）**：`doc/FINDINGS.md` 附錄 **[FND-18]** 之 DuckDB SQL 使用 `doc/fixtures/fnd18/`（隨 repo 追蹤）；下表數字為當次全量掃描快照，與 fixture 重算之 `theo_gap` 可能因上游重跑而略有差異，但 42 位個案 ID 與 root-cause 分類仍為 SSOT。

## 欄位說明

- `theo_gap`：`profile_total_theo_win - bet_total_theo_win`（負值代表 bet 端較大）。
- `matched_gap`：同一 `canonical_id + session_id` 可配對時，`sum(session_theo - bet_theo)`。
- `session_only_theo`：僅存在於 cleaned `t_session`、在 cleaned `t_bet` 無對應 `session_id` 的 theo。
- `bet_only_delcan_theo`：僅存在於 cleaned `t_bet`，且 raw `t_session` dedup 版本為 `is_deleted=1` 或 `is_canceled=1` 的 theo。
- `bet_only_missing_session_theo`：僅存在於 cleaned `t_bet`，但 raw `t_session` 找不到該 `session_id` 的 theo。
- `bet_only_inactive_gate_theo`：僅存在於 cleaned `t_bet`，且 raw `t_session` 因 activity gate（`turnover<=0` 且 `num_games_with_wager<=0`）會被排除的 theo。
- `dominant_root_cause`：以上絕對值貢獻最大者。

## Root Cause 名稱對照

- `session_delcan_exclusion`：session 在去重後被 deleted/canceled gate 排除，但 bet 仍存在。
- `missing_session_in_raw`：bet 的 session 在 raw session 查無紀錄（來源覆蓋不一致）。
- `session_vs_bet_formula_mismatch`：session 與 bet 在 matched session 上口徑不一致（非單純缺失）。
- `session_only_no_bet`：session 有資料但 bet 沒有（本批主導因子中未成為最大宗）。
- `session_inactive_gate_exclusion`：session 因 activity gate 被排除（本批僅極小量）。

## 42 個高差異個案（逐案）

| canonical_id | theo_gap | matched_gap | session_only_theo | bet_only_delcan_theo | bet_only_missing_session_theo | bet_only_inactive_gate_theo | dominant_root_cause |
|---:|---:|---:|---:|---:|---:|---:|---|
| 23988694 | -682571.0 | -0.0 | 0.0 | 682571.0 | 0.0 | 0.0 | session_delcan_exclusion |
| 91122170 | -661078.4 | -2920.0 | 0.0 | 658158.4 | 0.0 | 0.0 | session_delcan_exclusion |
| 97072359 | -617697.95 | -0.0 | 0.0 | 617697.95 | 0.0 | 0.0 | session_delcan_exclusion |
| 20429876 | -586154.3 | 610603.49 | 0.0 | 32347.27 | 1164410.52 | 0.0 | missing_session_in_raw |
| 20969091 | 529548.47 | 590252.69 | 19206.57 | 46399.97 | 30423.07 | 3087.75 | session_vs_bet_formula_mismatch |
| 23610108 | -525314.38 | 0.0 | 0.0 | 525314.38 | 0.0 | 0.0 | session_delcan_exclusion |
| 23768179 | 486956.31 | 475840.81 | 27515.09 | 0.0 | 16399.59 | 0.0 | session_vs_bet_formula_mismatch |
| 23238747 | 431553.08 | 528205.23 | 7636.73 | 380.53 | 103908.35 | 0.0 | session_vs_bet_formula_mismatch |
| 28800069 | -383784.19 | 217843.3 | 0.0 | 601627.49 | 0.0 | 0.0 | session_delcan_exclusion |
| 25744603 | -374171.32 | -0.0 | 6175.5 | 0.0 | 380346.82 | 0.0 | missing_session_in_raw |
| 97032899 | -316403.49 | -0.0 | 0.0 | 316403.49 | 0.0 | 0.0 | session_delcan_exclusion |
| 20901587 | -303272.77 | 61390.0 | 6175.5 | 15879.1 | 354959.17 | 0.0 | missing_session_in_raw |
| 97079093 | -299713.31 | 418420.22 | 3660.0 | 38175.6 | 683617.93 | 0.0 | missing_session_in_raw |
| 20536493 | -270847.18 | -5289.5 | 0.0 | 265557.68 | 0.0 | 0.0 | session_delcan_exclusion |
| 26882689 | -255780.49 | 17911.39 | 3804.32 | 43664.21 | 233831.99 | 0.0 | missing_session_in_raw |
| 23925550 | -255194.7 | -0.0 | 0.0 | 255194.7 | 0.0 | 0.0 | session_delcan_exclusion |
| 99039538 | -229554.22 | 44380.76 | 0.0 | 205996.6 | 67938.38 | 0.0 | session_delcan_exclusion |
| 99637898 | -224614.07 | 40952.5 | 0.0 | 350.36 | 265216.21 | 0.0 | missing_session_in_raw |
| 26882688 | -171092.83 | 18904.21 | 0.0 | 81.14 | 189915.9 | 0.0 | missing_session_in_raw |
| 99807660 | 170151.86 | 198975.59 | 0.0 | 0.0 | 28823.73 | 0.0 | session_vs_bet_formula_mismatch |
| 21025651 | -167605.09 | 1331.18 | 64.0 | 168234.8 | 765.47 | 0.0 | session_delcan_exclusion |
| 23700268 | -166621.76 | 42156.3 | 0.0 | 135166.24 | 73611.82 | 0.0 | session_delcan_exclusion |
| 91116552 | 166412.73 | 175049.81 | 6001.37 | 12329.71 | 2308.74 | 0.0 | session_vs_bet_formula_mismatch |
| 25737077 | -165987.21 | -7.26 | 0.0 | 40153.9 | 125826.05 | 0.0 | missing_session_in_raw |
| 21325683 | -163072.49 | 50096.91 | 617.55 | 76538.82 | 137248.13 | 0.0 | missing_session_in_raw |
| 99841779 | -157055.27 | 526136.3 | 25010.0 | 143302.96 | 564898.61 | 0.0 | missing_session_in_raw |
| 23431099 | -151558.52 | -0.0 | 74.11 | 150311.54 | 1321.09 | 0.0 | session_delcan_exclusion |
| 97126950 | -149240.97 | -0.0 | 0.0 | 149240.97 | 0.0 | 0.0 | session_delcan_exclusion |
| 97027098 | -148288.42 | 0.0 | 0.0 | 148288.42 | 0.0 | 0.0 | session_delcan_exclusion |
| 97056303 | -145033.17 | 744804.63 | 8662.45 | 454189.44 | 444310.81 | 0.0 | session_vs_bet_formula_mismatch |
| 97099484 | 143655.48 | 143487.98 | 1460.0 | 1292.5 | 0.0 | 0.0 | session_vs_bet_formula_mismatch |
| 89000343 | 142428.07 | 233686.81 | 617.55 | 537.84 | 91338.45 | 0.0 | session_vs_bet_formula_mismatch |
| 23107013 | 134798.99 | 158548.16 | 1308.1 | 2049.38 | 23007.89 | 0.0 | session_vs_bet_formula_mismatch |
| 21415081 | -133629.98 | -3211.97 | 0.0 | 130418.01 | 0.0 | 0.0 | session_delcan_exclusion |
| 23281368 | 131730.85 | 134040.31 | 175.16 | 0.0 | 2484.62 | 0.0 | session_vs_bet_formula_mismatch |
| 1855779 | -128783.96 | 0.0 | 0.0 | 128783.96 | 0.0 | 0.0 | session_delcan_exclusion |
| 23947290 | -125477.0 | 3118.92 | 2515.46 | 83401.76 | 47709.62 | 0.0 | session_delcan_exclusion |
| 99449105 | -121023.52 | -730.0 | 0.0 | 120293.52 | 0.0 | 0.0 | session_delcan_exclusion |
| 20934862 | -116580.58 | -0.0 | 0.0 | 116580.58 | 0.0 | 0.0 | session_delcan_exclusion |
| 44444444 | -107778.97 | 0.0 | 0.0 | 107673.18 | 105.79 | 0.0 | session_delcan_exclusion |
| 20524523 | -105879.04 | 29021.65 | 0.0 | 134900.69 | 0.0 | 0.0 | session_delcan_exclusion |
| 23629410 | -100991.96 | -0.0 | 0.0 | 100991.96 | 0.0 | 0.0 | session_delcan_exclusion |

## 備註

- `dominant_root_cause` 是主因分類，不代表其餘因子為 0。部分個案同時受到多因子影響（例如同時有 matched gap 與 missing-session）。
- 本檔是 FND-18 的個案證據表；高層摘要與建議決策仍以 `doc/FINDINGS.md` 為主。
