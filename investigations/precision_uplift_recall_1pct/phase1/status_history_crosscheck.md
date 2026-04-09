# status_history_crosscheck

| 日期 | 歷史發現 | 當時決策 | 暫緩原因 | 現況是否解除 | 本輪動作 | 備註 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 2026-03-11 | run boundary lookback 對 NaT 與錯誤訊息契約不一致，存在口徑漂移風險（`compute_run_boundary` vs `compute_loss_streak`） | 已提出契約對齊修正建議，優先保證邊界與錯誤訊息語意一致 | 當時優先級低於主流程交付，且對 `precision@1%` 影響未量化 | 否 | 重驗 | 先做小樣本回放，確認是否影響標註窗口邊界樣本 |
| 2026-03-20 | `evaluate` 將 `censored=1` 納入評估，造成指標偏差（應排除 censored） | 已定義為 Major 口徑問題，要求評估排除 censored | 初期分析腳本以「先跑通」為主，未完全對齊訓練/評估契約 | 是 | 沿用 | 視為已確認主因之一，保留為本輪 RCA 證據 |
| 2026-03-20 | 後續修正已將 `evaluate` 改為排除 `censored=1`，並輸出 `n_censored_excluded` | 已落地修正並增加可觀測欄位 | 修正後仍未完成跨窗穩定性驗證 | 部分 | 重驗 | 需要多時間窗重跑確認 uplift 不是單窗假象 |
| 2026-03-20 | label parity（validator vs `compute_labels`）被列為未完成項，含 terminal/censored 邊界對拍 | 列入後續必做，尚未完成結案 | 需跨模組對拍與邊界案例整理，成本較高而延後 | 否 | 沿用 | 升級為 Phase 1 Gate 必做，未完成不可進下一階段 |
| 2026-03-19 | R1/R6 合併樣本分析顯示 `censored==0` 與 JSON 對齊，但仍有少量無 label 樣本 | 先確認大盤對齊，保留少量缺標待追 | 缺標樣本量小，當時判定不阻塞流程 | 否 | 重驗 | 應抽樣檢查缺標是否集中在高分段，避免 top-band 偏差 |
| 2026-03-25 | Validator 拉取視窗 `required_min` 未納入 `VALIDATOR_EXTENDED_WAIT_MINUTES`，可能低估等待窗（延遲標註風險） | 已識別政策一致性缺口，提出修正方向 | 欄位與等待策略牽涉既有任務排程，調整需連動驗證 | 部分 | 沿用 | 視為「延遲標註」主因候選，納入本輪主因排序 |
| 2026-03-25 | Task 9B 追加 `retry_end` = lookahead + extended wait + freshness buffer，補強延遲標註補查策略 | 已實作二階段補查以緩解 delayed label | 補查策略可能增加計算延遲與資源成本 | 部分 | 重驗 | 需補觀測：補查命中率、延遲分佈、對 precision@1% 實際影響 |

<!-- ORCHESTRATOR_RUN_NOTE_START -->
**Last orchestrator run**: `pytest_resume_skip`

- **Gate status**: `FAIL`
- **blocking_reasons**: `['collect_error:E_COLLECT_BACKTEST_METRICS', 'collect_error:E_COLLECT_R1_PAYLOAD', 'collect_error:E_COLLECT_STATE_DB']`
- Please keep narrative cross-check above this block; edit conflicts rare.

<!-- ORCHESTRATOR_RUN_NOTE_END -->
