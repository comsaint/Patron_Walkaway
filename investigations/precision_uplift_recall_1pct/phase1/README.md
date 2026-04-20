# Phase 1：根因診斷（RCA）與限制條件

## 為什麼要做這一階段

若未先釐清瓶頸，直接調模型容易把時間花在「標籤延遲、censored 口徑、評估窗不一致」等問題上，結果無法重現或上線後仍誤報。Phase 1 要回答：**主因是模型能力，還是標籤／資料／評估契約？** 並決定後續是否 **Timeline 重排**（先修資料與流程，再進大規模建模）。

## 如何調查

- **歷史對照**：對照 `STATUS.md` 與既有紀錄，標記可沿用／需重驗／已失效的發現（含 label noise、lag、censored 等）。
- **切片分析**：依日期、桌台、玩家層級、活躍度等維度，看 `precision@recall=1%` 與樣本占比，找出 top 拖累切片。
- **標籤品質稽核**：量化 censored／延遲標註；抽樣高分 false positive，區分「標籤未成熟」與「真誤報」。
- **時點對齊**：確認 train／serve／驗證的特徵與標籤時間戳一致，排除 leakage 與口徑漂移。
- **上限重現**：在**固定契約**下重跑 baseline／backtest，確認既有結論可重現，避免單窗幻覺。
- **線上蒐證**（若適用）：搭配 scorer／validator、`run_r1_r6_analysis` 等，補足 production 側證據。

調查過程須與總計畫一致：`.cursor/plans/PLAN_precision_uplift_sprint.md`（單一 SSOT，含 §7 切片與 §8–§12 治理）。

## 預期產出（應填寫檔案）

| 檔案 | 用途 |
| :--- | :--- |
| `status_history_crosscheck.md` | STATUS 歷史與本輪對照 |
| `slice_performance_report.md` | 切片表現與拖累排名 |
| `label_noise_audit.md` | 標籤品質與 censored／延遲量化 |
| `point_in_time_parity_check.md` | 時點／leakage 檢查結論 |
| `upper_bound_repro.md` | baseline／上限重現紀錄 |
| `phase1_gate_decision.md` | Phase 1 Gate 與主因排序 |

## Ad-hoc 快速切片腳本

你若只想快速回答「哪些 segment 錯誤率高」，可直接跑：

`python investigations/precision_uplift_recall_1pct/phase1/analyze_segment_error_rates.py --prediction-log-db <path> --state-db <path> --start-ts <ISO> --end-ts <ISO> --output-json investigations/precision_uplift_recall_1pct/phase1/segment_error_rates.json`

資料源優先序固定為：
1. `prediction_log + state_db`（primary）
2. `--profile-parquet-path`（僅當 profile segment 欄位在 state_db 不可用時）
3. `--use-clickhouse-fallback`（僅當前兩者仍不可用時）

此腳本為 ad-hoc 分析用途，不做 Gate 決策。

## 可依此做出的決策

- **進入 Phase 2**：主因排序完成，且評估契約可信；或模型為主因、資料側無阻斷級問題。
- **Timeline 重排**：`label_noise_audit` 等顯示**標籤／資料流程**為主瓶頸 → 先修標註與契約，暫緩大規模 Phase 2～4 建模衝刺。
- **升級必做項**：`status_history_crosscheck` 顯示歷史曾暫緩且阻礙未解除 → 本輪必解後才能進下一階段。
- **暫停或縮小範圍（scope cut）**：重排後指標仍不收斂 → 暫停大型 ensemble／盲擴特徵，集中修復資料鏈路。

---

## 應填寫檔案（清單）

- `status_history_crosscheck.md`
- `slice_performance_report.md`
- `label_noise_audit.md`
- `point_in_time_parity_check.md`
- `upper_bound_repro.md`
- `phase1_gate_decision.md`
