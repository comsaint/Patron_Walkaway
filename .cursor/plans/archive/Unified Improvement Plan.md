統一改進計劃（**v2**）：整合 codebase 檢視、效能與可觀測性討論後的單一路線圖。v1 曾建議提前過濾 `bets`/`new_bets`，已作廢（見 Task 1「原計劃問題」）。

**執行狀態（2026-03-21，對齊 `.cursor/plans/PLAN.md`）**

| Task | 狀態 |
|------|------|
| T1 Scorer 安全裁切 | ✅ 已完成 |
| T2 Backtester → MLflow | ✅ 已完成 |
| T3 Validator precision 歷史化 | ✅ 已完成 |
| T4 Prediction log 聚合 | ✅ 已完成 |

**索引**：本計劃條目亦摘要於 [PLAN.md](PLAN.md)（統一改進計劃 v2 表）。

***

# 統一改進計劃：Patron Walkaway Detection（v2）

***

## Task 1：Scorer 效能優化（安全版）

**原計劃問題（v1，勿再採用）**：在 `canonical_map` 建好後立刻過濾 `bets`/`new_bets`，會破壞兩個關鍵依賴：

1. `update_state_with_new_bets(conn, bets, ...)` 用**完整 `bets`** 更新 `session_stats`；若 `bets` 只含 rated，unrated 的 session 累計將不再寫入 state，連動破壞 `get_session_totals` / `get_historical_avg`。
2. `build_features_for_scoring` 裡 `groupby("session_id")` 的 rolling 統計（`cum_bets`、`bets_last_*m`、`wager_last_*m`）是對整個 session 內**所有注單**計算的；提前過濾 unrated 注單會讓 rated 玩家看到的 session 特徵失真，破壞 train–serve parity。

**修訂後做法**：保持 `bets`、`update_state_with_new_bets`、`build_features_for_scoring` **完全不變**；只在**兩個最重的步驟前**裁切 `features_all`：

```text
# 原有不變：
bets, sessions = fetch_recent_data(...)
new_bets = update_state_with_new_bets(conn, bets, ...)   # 完整 bets
features_all = build_features_for_scoring(bets, ...)      # 完整 bets

# UNRATED_VOLUME_LOG：必須在裁切 features_all 之前完成。
# 使用「完整 features_all ∩ new_bets」計算 n_rated / n_unrated（及 unrated 玩家數等），
# 存入變數；若先裁成 rated-only 再交集 new_bets，本輪 unrated 新單會消失，telemetry 會壞。

features_all = features_all[features_all["canonical_id"].isin(rated_canonical_ids)]

# 以下兩步只處理 rated 列：
compute_track_llm_features(features_all, ...)
_load_profile_for_scoring(...) + _join_profile(...)
```

**效能節省範圍**：Track LLM（DuckDB）與 player_profile PIT join 依 rated 列比例下降。`build_features_for_scoring` 仍跑完整資料（向量化 rolling，成本相對低）。

**修改位置**：`trainer/serving/scorer.py` → `score_once()`（裁切 + 調整 UNRATED_VOLUME_LOG 與裁切的相對順序；行數略多於「僅一行 filter」）。

***

## Task 2：Backtester 結果接入 MLflow（可觀測性 P0）

**問題**：`trainer/training/backtester.py` 計算了 `test_ap`、`test_precision` 等指標，但只寫入 `out_backtest/backtest_metrics.json`，與 MLflow run 脫節。

**修改位置**：`trainer/training/backtester.py`

**做法**：

1. `from trainer.core.mlflow_utils import log_metrics_safe, has_active_run`
2. 在 backtest 結果寫入 JSON 之後，`if has_active_run():` 再 `log_metrics_safe(...)`
3. **Metric 命名**加 `backtest_` 前綴（例如 `backtest_ap`、`backtest_precision`、`backtest_recall`、`backtest_fbeta_05`、`backtest_threshold`），避免與 trainer 端指標撞名，MLflow UI 易辨識
4. 無 active run 時 no-op

**NaN / 非有限值**：`log_metrics_safe` 行為以 `trainer/core/mlflow_utils.py` 與 `tests/unit/test_mlflow_utils.py` 為準；專案中對 NaN/inf 仍有 xfail 類測試。實作時宜在 backtest 端**盡量只傳有限 float**，或合併前過濾，不完全依賴 utils 未來演進。

**預期效果**：同一 MLflow run 可同時對照 train 與 backtest 指標，支援 train/backtest gap 跨版本追蹤。

***

## Task 3：Validator Precision 歷史化（可觀測性 P1）

**問題**：`validate_once()` 的 Cumulative Precision 僅 `logger.info`，無時序記錄。

**修改位置**：`trainer/serving/validator.py`（`validate_once()` + `get_db_conn()`）

**做法**：

1. 新增 `validator_metrics` 表（例如 `recorded_at TEXT, model_version TEXT, precision REAL, total INTEGER, matches INTEGER`）
2. **Schema 遷移**：`CREATE TABLE IF NOT EXISTS validator_metrics (...)` 放在 `get_db_conn()`，與現有 `processed_alerts`、`validation_results` 等建表方式一致
3. 每次算出 precision 後 INSERT
4. **`model_version` 語意**：從當次 validation 視窗內、alerts 的**最近一筆** `model_version` 讀取；語意為「驗證當下認定的版本」，在 schema 與函式 docstring 中寫清楚
5. **`alerts.model_version` 欄位**：生產上通常由 scorer 的 `init_state_db()` 對 `alerts` 做 Phase-1 欄位遷移；`validator.py` 內有 `_ALERTS_PHASE1_COLS` 定義但未接遷移。若存在「僅 validator 先建立 DB」的路徑，需補上與 scorer 一致的 `alerts` 遷移，或改從其他 SSOT 取版本
6. **MLflow**：production 無長駐 active run；若要上 MLflow 需獨立週期性 eval job **另起 run**——**不在本 Task 範圍**（Phase 3 可選）

**預期效果**：可查 `validator_metrics` 看 precision 趨勢；日後接 Grafana / BI。

***

## Task 4：Prediction Log 聚合外送（可觀測性 P2）

**問題**：`prediction_log` 有完整欄位但未聚合，alert rate、分數分佈等不可見。

**修改位置**：`trainer/serving/scorer.py`（`score_once()` 末段 + `_ensure_prediction_log_table` 或鄰近 helper）

**做法**：

1. 新增 `_export_prediction_log_summary(pl_path, model_version, scored_at)`（或等價命名）
2. 每次寫完 prediction_log 後，查最近 N 分鐘聚合：`alert_rate`、`mean_score`、`mean_margin`、`rated_obs_count` 等
3. 寫入 `prediction_log_summary` 表；對 `recorded_at`、`model_version` **建索引**（與現有 `prediction_log` 上 `idx_prediction_log_scored_at`、`idx_prediction_log_model_version` 一致思路）
4. **解讀**：滑動窗（建議 N=60 分鐘）與 scorer 採樣間隔（config 常見約 45 秒）高度重疊，圖表當**近似儀表板**而非獨立樣本；適合偵測漂移（alert rate 崩零/飆升），不適合當精確統計推論

**預期效果**：時序上的 alert rate / 分數概況可監控，及早發現異常。

***

## 執行順序建議

| Task | 修改位置 | 難度 | 備註 |
|---|---|---|---|
| T1 安全版 | `scorer.py` `score_once()` | 低–中 | 含 UNRATED_VOLUME_LOG 順序；與 T2 同 PR |
| T2 Backtester MLflow | `backtester.py` | 低 | 與 T1 同 PR |
| T3 Validator 歷史化 | `validator.py` 建表 + INSERT | 中 | 獨立 PR |
| T4 Prediction log 聚合 | `scorer.py` helper + summary 表 | 中 | 獨立 PR |

T3 與 T4 可並行開發，互不相依。
