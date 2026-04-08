# status_history_crosscheck (draft from STATUS.md)

此檔為自動擷取草稿；`本輪動作` 與 `暫緩原因/是否解除` 需人工判定。

| 章節 | 證據片段 | 當時決策 | 暫緩原因 | 現況是否解除 | 本輪動作 | 備註 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Code Review：compute_run_boundary lookback 契約對齊變更（2026-03-11） | ### 1. [邊界] run_boundary lookback 路徑未處理 NaT，與 loss_streak 契約不一致 | TBD | TBD | 否/是 | 沿用/重驗/已失效 | - |
| Code Review：compute_run_boundary lookback 契約對齊變更（2026-03-11） | \| **問題** \| `compute_loss_streak` 的 lookback 已依 Review #1 對「含 NaT 的 group」改走 per-group Python 迴圈，語意明確。`compute_run_boundary` 的 lookback 分支未做類似的 NaT 檢查：`times = pd.to_datetime(grp["payout_complete_dtm"], utc=False)` 後若存在 N | TBD | TBD | 否/是 | 沿用/重驗/已失效 | - |
| Code Review：compute_run_boundary lookback 契約對齊變更（2026-03-11） | \| **問題** \| `test_run_boundary_lookback_hours_overflow_raises_value_error` 僅 `assertIn("lookback", str(ctx.exception).lower())`。若日後有人將錯誤改為 "invalid window" 等，仍會通過測試，但與 `compute_loss_streak` 的「1000 hours」契約不一致，呼叫端也無法依訊息區分「 | TBD | TBD | 否/是 | 沿用/重驗/已失效 | - |
| R1/R6 自動化腳本（sample + autolabel + evaluate + all-in-one）（2026-03-20） | \| `investigations/test_vs_production/checks/run_r1_r6_analysis.py` \| 新增 R1/R6 主腳本並擴充三階段：`sample`（below-threshold 分層抽樣）、`autolabel`（ClickHouse + `trainer.labels.compute_labels` 產生 `bet_id,label,censored`）、`evaluate`（curre | TBD | TBD | 否/是 | 沿用/重驗/已失效 | - |
| Code Review：`run_r1_r6_analysis.py`（2026-03-20） | #### 2) `evaluate` 目前把 `censored=1` 視同可評估樣本（Major, 指標偏差） | TBD | TBD | 否/是 | 沿用/重驗/已失效 | - |
| Code Review：`run_r1_r6_analysis.py`（2026-03-20） | - **問題**：`autolabel` 有輸出 `censored`，但 `evaluate` 只讀 `bet_id,label`，未排除 censored。依 `trainer/labels.py` 設計，censored 應排除於訓練與嚴謹評估；納入會偏移 R1/R6 指標。 | TBD | TBD | 否/是 | 沿用/重驗/已失效 | - |
| Code Review：`run_r1_r6_analysis.py`（2026-03-20） | - `evaluate` 支援可選欄位 `censored`；預設排除 `censored=1`。 | TBD | TBD | 否/是 | 沿用/重驗/已失效 | - |
| Code Review：`run_r1_r6_analysis.py`（2026-03-20） | - 輸出統計明確列出 `n_censored_excluded`。 | TBD | TBD | 否/是 | 沿用/重驗/已失效 | - |
| Code Review：`run_r1_r6_analysis.py`（2026-03-20） | - 若 labels CSV 不含 `censored`，至少在 payload 加 warning。 | TBD | TBD | 否/是 | 沿用/重驗/已失效 | - |
| Code Review：`run_r1_r6_analysis.py`（2026-03-20） | - `test_evaluate_excludes_censored_rows_when_column_present` | TBD | TBD | 否/是 | 沿用/重驗/已失效 | - |
| Code Review：`run_r1_r6_analysis.py`（2026-03-20） | - `test_evaluate_warns_when_censored_column_absent` | TBD | TBD | 否/是 | 沿用/重驗/已失效 | - |
| 將 `run_r1_r6_analysis.py` 風險點轉成最小可重現測試（僅 tests，未改 production）（2026-03-20） | \| `test_evaluate_should_exclude_censored_rows` \| #2 censored 排除 \| labels 含 `censored=1` 時，evaluate 應排除。 \| `xfail` \| | TBD | TBD | 否/是 | 沿用/重驗/已失效 | - |
| Round 1：修改實作以消除 `run_r1_r6_analysis.py` 風險測試失敗（2026-03-20） | 2. **Risk #2（censored 口徑）** | TBD | TBD | 否/是 | 沿用/重驗/已失效 | - |
| Round 1：修改實作以消除 `run_r1_r6_analysis.py` 風險測試失敗（2026-03-20） | - `evaluate` 支援讀取 labels 的可選 `censored` 欄位，預設排除 `censored=1`，並在 payload 增加 `n_censored_excluded`。 | TBD | TBD | 否/是 | 沿用/重驗/已失效 | - |
| 計畫項目狀態（更新） | - `R3`：**進行中**（censored/label parity 仍需完整對拍收斂）。 | TBD | TBD | 否/是 | 沿用/重驗/已失效 | - |
| Plan Remaining Items（剩餘項目） | 1. 依 §4 順序補齊 `R3`（validator vs `compute_labels` 完整對拍，含 terminal/censored 邊界）。 | TBD | TBD | 否/是 | 沿用/重驗/已失效 | - |
| R1/R6 一輪輸出 — CSV 合併後精煉解讀（2026-03-19 窗｜snapshots） | **資料**：`investigations/test_vs_production/snapshots/` 內 `*_sample*.csv`（含 `score`, `bin_id`, `is_alert`）與 `*_labeled*.csv`（`bet_id`, `label`, `censored`）以 `bet_id` inner merge。 | TBD | TBD | 否/是 | 沿用/重驗/已失效 | - |
| R1/R6 一輪輸出 — CSV 合併後精煉解讀（2026-03-19 窗｜snapshots） | **對齊 JSON**：below 合併列數 **3179**（sample 僅 **21** 筆無對應 label）；alert 合併 **787**（**13** 筆無 label）；`censored==0` 與 JSON 一致。 | TBD | TBD | 否/是 | 沿用/重驗/已失效 | - |
| Task 9（進行中，2026-03-25）— Validator ClickHouse bet 拉取視窗：最舊待驗時間窗 + 上限保護（DEC-037） | #### 2. [政策一致性] `required_min` 只用 `LABEL_LOOKAHEAD + freshness`，未納入 `VALIDATOR_EXTENDED_WAIT_MINUTES` | TBD | TBD | 否/是 | 沿用/重驗/已失效 | - |
| Task 9B（進行中，2026-03-25）— `No bet data` 二階段補查（targeted retry，50/輪） | - `retry_end = bet_ts + LABEL_LOOKAHEAD_MIN + VALIDATOR_EXTENDED_WAIT_MINUTES + VALIDATOR_FRESHNESS_BUFFER_MINUTES` | TBD | TBD | 否/是 | 沿用/重驗/已失效 | - |
