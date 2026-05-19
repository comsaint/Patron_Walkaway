# Training Step 3.5 Mid-Term Snapshot Incident - 2026-05-20

本文件記錄 `trainer_hightier` high-tier trainer 在 Step 4 前置 cadence enrich 階段出現的訓練效能事故，並定義修復的 implementation plan。

## Current Status

- 事故狀態：**根因已確認，第一版範圍裁切方案不足，待改為 window-driven day-end snapshot materializer**。
- 直接影響：完整訓練流程在 Step 3 完成後長時間無輸出，使用者中斷執行。
- 暫時繞過方式：`--skip-step4` 可讓 pipeline 使用既有 splits 繼續 Step 5，但會有 splits / enriched feature 陳舊風險，不應作為正式訓練長期方案。

## Symptoms

- 指令：`python -m trainer_hightier.trainer --skip-optuna`
- Step 3 於 `01:15:31` 完成並寫出：
  - `trainer_hightier/artifacts/training_data/training_set.parquet`
  - rows = `1,888,151`
- 使用者於約 `01:34:29` 中斷，總 elapsed 約 `1308.740s`。
- Traceback 顯示被中斷位置為 Step 4 前置 enrich 中的 mid-term daily snapshot materialization：
  - `trainer_hightier/feature_experiment/materialize_mid_term_daily_snapshot.py`
  - `con.execute(COPY (...) TO parquet)`
- 對照測試：
  - `python -m trainer_hightier.trainer --skip-optuna --skip-step4`
  - total elapsed 約 `251.264s`
  - Step 5 LightGBM 本身約 `73.694s`
- 因此主要耗時不是 Feast retrieval，也不是 LightGBM fit，而是 Step 4 前的 mid-term snapshot enrichment path。

## Root Cause Assessment

Step 4 開始前，`trainer.py` 會先檢查 registry baseline 是否包含 `fe__*`。若包含 mid-term features，會呼叫：

- `trainer_hightier.feature_experiment.materialize_mid_term_daily_snapshot.materialize_mid_term_daily_snapshot(...)`
- `trainer_hightier.feature_experiment.dataset_enrich.enrich_training_parquet_with_cadence_suppliers(...)`

目前 mid-term snapshot SQL 的主要問題：

- 對 cleaned bet 全量資料建立 `bets` CTE。
- 從全量 bets 產生 `anchor_days = DISTINCT canonical_id, gday`。
- 對每個 `anchor_day` 再 range join 回 bets / interarrival rows，計算 1d / 7d / 30d 指標。
- `anchor_start` / `anchor_end` 目前由函式支援，但 main trainer 呼叫時未傳入，所以實際等同全量 anchor 計算。
- 即使未來傳入 anchor filter，若 filter 只放在最後 `SELECT * FROM snap_base WHERE ...`，DuckDB 仍需先計算大部分 `snap_base`，效益有限。
- 經對照舊模型 bundle 與 commit history，真正效能回歸來自 `2d8e5a4 feat(training): mid-term daily snapshot cadence and ASOF enrich`：
  - `a0450fc` / `8128a74` 前後的完成訓練仍走 legacy bet-grain `materialize_fe_derived_parquet(...)`，Step 3.5 約 39-50 秒。
  - `2d8e5a4` 將 mid-term baseline features 改走 canonical daily snapshot + ASOF enrich，新增 source-driven `anchor_days × lookback bets` range join。
- 第一版「training universe + date bound」優化不足：
  - 實際 log 顯示 `universe_rows=3582`，但 `bets_gday=[2024-06-07,2026-05-10]` 仍涵蓋幾乎全訓練期。
  - 新增 canonical universe expression join 未移除 range join 核心形狀，可能讓 DuckDB plan 更差。
  - 因此後續修復不可只靠裁切 universe / 日期，必須改寫 materialization 演算法。

## Feature Semantics Decisions

以下決策是後續修復的 governing truth；任何 implementation 不得偏離：

- Daily snapshot 代表 **anchor gaming day 結束後** 的 patron state。
  - `anchor_gaming_day = D - 1` 的 snapshot 應包含 `D - 1` 當天最後一筆 bet。
  - Training target bet on `D` 只能 join `< D` 的 snapshot，因此不會洩漏 target day。
- 第一版不產生「無下注日」的 synthetic snapshot。
  - Snapshot grain 為 `canonical_id + 有下注的 anchor_gaming_day`。
  - 若某天無下注，train/serve ASOF join 使用最近 prior anchor。
- Train 和 serve 必須共用同一個 canonical daily snapshot contract：
  - key: `canonical_id + anchor_gaming_day`
  - join: target `gaming_day` 使用 latest `anchor_gaming_day < target gaming_day`（training）或 equivalent production-safe prior anchor。
  - 不得回到 production 以 live `bet_id` lookup shipped training parquet。
- Materialization algorithm 可改變，但 feature contract 不可改變。
  - 後續應以 window-driven day-end snapshot 取代 current range-join snapshot。

## Contributing Factors

- Mid-term snapshot 沒有 cache / manifest；每次 Step 4 都可能重算。
- Step 3.5 缺少清楚的開始、範圍、cache hit/miss、row count 與 heartbeat log，使用者難以區分「查詢仍在跑」與「卡死」。
- SQL 形態是 anchor-day range join，對大型 cleaned bet dataset 容易造成 CPU / IO / memory 壓力。
- 訓練資料 rows 約 188 萬，但 cleaned bet metadata 約 5.2 億 rows；若 snapshot 以 cleaned bet 全量 canonical/day 為計算範圍，成本會被源資料規模主導。

## Production Impact Assessment

Production refresh 也會透過 `trainer_hightier.serving.production_materialize.materialize_production_mid_term_daily_snapshot(...)`
呼叫共用的 `materialize_mid_term_daily_snapshot(...)`。因此修復不能把 training-only scope 隱含寫入共用 materializer，否則可能造成 production snapshot 漏人或漏 anchor。

主要 production 風險：

- Training-scoped canonical universe 若被 production 誤用，live scoring 會對不在 training set 的 high-ADT patrons 產生系統性 mid-term feature miss。
- Training-scoped mid-term snapshot 若被 `_freeze_deploy_inputs(...)` copy 到 deploy bundle 並寫入 active manifest，可能被 serving 當作 production-compatible `canonical_daily_asof` snapshot。
- Production 目前先 materialize full temp snapshot，再依 ADT allowlist filter；這本身也可能慢，但不可用 training universe 取代 production allowlist universe。
- Cache 若未包含 `scope` 與 universe fingerprint，training cache 與 production cache 可能互相誤命中。

Guardrail 決策：

- Training-scoped snapshot **只能供 Step 4 enrich 使用**，不可 publish / freeze 成 production deploy input。
- Production-compatible snapshot 必須由 production refresh / production materializer 產生，並以 production allowlist universe 與 production manifest metadata 為準。
- 共用 materializer 可以新增通用 optional input（例如 canonical universe parquet），但 caller 必須明確傳入 scope；函式內不可自行推斷 training 或 production scope。

## Decision Log

- 修復策略：改為 **window-driven day-end snapshot materialization**；不再以 `anchor_days LEFT JOIN bets range` 作為主演算法。
- 第一版範圍 / universe bound 方案已證明不足，僅可保留 metadata / guardrail / observability，不可視為效能修復完成。
- Artifact scope：training-scoped mid-term snapshot 不允許進 production bundle / active manifest。
- Production guardrail：本輪至少要防止 training-scoped artifact 污染 production；production allowlist-scoped 效能優化可分 phase，但 API 設計需支援。
- Materializer API：共用函式只接受顯式參數（scope label、canonical universe parquet、anchor range），不讀 training set 或 production config。
- Cache policy：training cache 預設可 reuse；production cache 需更保守，至少不得跨 scope 命中。
- Success gate：`--start-from-features --skip-step5` 不再長時間 silent，且 Step 4 enrich 不得造成 mid-term null coverage 顯著惡化。

## Scope

### In Scope

- 降低 Step 3.5 mid-term snapshot materialization 的 wall time 與記憶體壓力。
- 保持 mid-term features 的 ASOF 語意：
  - training bet `gaming_day` 只能使用 `< gaming_day` 的 prior snapshot。
- 加入可觀測性，讓長查詢前後有明確 log。
- 加入安全快取，避免未變更輸入下重複重算。
- 加入 production guardrail，避免 training-scoped snapshot 被 serving / deploy 誤用。

### Non-Scope

- 不改變模型 feature registry 的 business meaning。
- 不以 silent zero / median imputation 掩蓋缺 snapshot 問題。
- 不把 `--skip-step4` 當作正式解法。
- 不在第一階段大幅重寫整個 feature experimentation pipeline。
- 不用 training universe 取代 production allowlist universe。

## Implementation Plan

### Objective

讓 `python -m trainer_hightier.trainer --start-from-features --skip-step5` 能在 laptop 可接受時間內完成 Step 4 前置 enrich，且 train / serve 都使用一致的 canonical daily day-end snapshot 語意。

### Phase 1: Replace Range Join With Window-Driven Day-End Snapshot

實作方向：

- 重寫 `materialize_mid_term_daily_snapshot._daily_snapshot_sql(...)`：
  - 移除 `anchor_days LEFT JOIN bets AS b ON b.gday BETWEEN ...` 的 range join 主路徑。
  - 先建立 canonicalized bet stream，按 `canonical_id, pcd` 排序。
  - 使用 DuckDB window functions 計算 1d / 7d / 30d 指標。
  - 每個 `canonical_id + gday` 取該日最後一筆 bet 的 window state，輸出為 `canonical_id + anchor_gaming_day`。
- Window frame 應符合「anchor day end」語意：
  - day-end snapshot 應包含 anchor day 當天最後一筆 bet。
  - 例如使用 `RANGE BETWEEN INTERVAL '30 DAY' PRECEDING AND CURRENT ROW` 或等價 inclusive day-end frame。
- 保留必要的 bounds / universe filter 作為輔助減少掃描，但它不是主要效能修復：
  - training caller 可傳 training-scoped canonical universe。
  - production caller 可傳 production allowlist-derived universe（若同輪實作）。
  - bets scan 可用 safe date bounds，但不得裁掉 window history。
- 共用 materializer 只接受顯式 optional input，例如 `canonical_universe_parquet` 與 `snapshot_scope`；不可在函式內讀取 trainer-specific path。

注意事項：

- `dataset_enrich` 的 ASOF 條件是 `anchor_gaming_day < bet.gaming_day`，不是 `<=`。
- 不可把 `anchor_start` 直接設為 `min(training.gaming_day)`；這會切掉最早訓練日需要的 prior snapshot。
- 單純日期限制 / canonical_id semi-join 已證明不足；不得把它當作完成條件。
- Production caller 若使用同一 materializer，必須傳 production allowlist-derived canonical universe，不能重用 training-only universe。

### Phase 1b: Prevent Training Snapshot From Reaching Production

實作方向：

- `_freeze_deploy_inputs(...)` 不得把 `snapshot_scope=training_step4_only` 的 mid-term snapshot copy 成 deploy input。
- 若 mid-term artifact metadata 缺 scope，預設視為不安全；除非 metadata 明確標記 production-compatible。
- Active manifest 不得引用 training-scoped mid-term snapshot。
- 若 bundle 缺 production-compatible mid-term snapshot，serving / deploy 應依既有 snapshot updater 路徑刷新或 gate，而不是 fallback 到 training-scoped artifact。

### Phase 2: Add Mid-Term Snapshot Cache

實作方向：

- 為 `_main_trainer_mid_term_daily_snapshot.parquet` 建立 sidecar manifest。
- Cache key / manifest 至少包含：
  - snapshot scope (`training_step4_only` 或 `production`)
  - canonical universe parquet fingerprint
  - cleaned bet artifact fingerprint
  - canonical mapping parquet fingerprint
  - `MID_TERM_SNAPSHOT_MAX_LOOKBACK_DAYS`
  - anchor range
  - selected mid-term feature columns
  - materializer code hash
- 若 manifest compatible 且 parquet row count / schema quick stat 一致，直接 reuse。

預期效果：

- 第一次修復後仍需 materialize，但後續重跑 `--start-from-features` 不應重算 mid-term snapshot。
- Production cache 不得命中 training cache；若本輪不實作 production cache，仍需確保 scope key 設計可支援後續 production allowlist-scoped cache。

### Phase 3: Add Observability

實作方向：

- 在 Step 3.5 enrich path 加入以下 log：
  - short-term columns count / mid-term columns count
  - training `gaming_day` min/max
  - mid-term anchor range
  - snapshot scope
  - canonical universe row count / source
  - 是否 cache hit
  - materialized row count
  - materialization elapsed seconds
- 對 DuckDB long query 盡量沿用既有 `duckdb_runtime` progress helper，使 terminal 不再長時間完全安靜。

### Phase 4: Further SQL Optimization If Needed

若 window-driven day-end snapshot 仍不足，進一步優化：

- 將部分可加總的特徵拆成 `canonical_id x gaming_day` 日粒度聚合後再 rolling。
- interarrival 類指標仍需從 bet-level `LAG(pcd)` 產生，不能用簡單日聚合取代。
- 保持同一 output contract：`canonical_id + anchor_gaming_day`。

## Validation Plan

### Performance Validation

- Baseline 指令：
  - `python -m trainer_hightier.trainer --start-from-features --skip-step5`
- 修復後記錄：
  - mid-term materialization elapsed seconds
  - Step 4 total elapsed seconds
  - peak memory / OOM 是否改善（若可由系統監控取得）
- 目標：
  - 不再出現 15-20 分鐘無 log 的 silent long query。
  - 重跑時 mid-term snapshot cache hit。

### Correctness Validation

- Row-level spot check：
  - 對同一 `bet_id`，修復前後 mid-term feature 值應一致或差異可由邊界修正解釋。
- Day-end snapshot check：
  - 對小型手算資料，`anchor_gaming_day=D-1` 的 snapshot 必須包含 `D-1` 當天最後一筆 bet。
  - Training target `D` 不得使用 `D` 當天 snapshot。
- ASOF check：
  - `mid_term_anchor_gaming_day < gaming_day`
  - `mid_term_snapshot_age_days >= 1`
- Null coverage check：
  - mid-term feature 缺失率不得因範圍裁切明顯上升。
- Schema gate：
  - registry baseline `fe__*` 欄位仍完整存在於 Step 4 input。
- Scope guardrail check：
  - training-scoped snapshot metadata 必須存在且標記 `snapshot_scope=training_step4_only`。
  - deploy inputs / active manifest 不得引用 training-scoped mid-term snapshot。
- Production safety check：
  - production refresh 仍可用 production allowlist universe 產出 production-compatible snapshot。
  - serving gate / freshness validation 不接受 training-scoped snapshot 作為 production snapshot。

## Risks

- 若 anchor range 或 canonical_id semi-join 設計過窄，可能導致 early training rows 找不到 prior snapshot。
- 若 cache invalidation 不完整，可能使用 stale mid-term features。
- 若 window frame inclusivity 設錯，可能導致 day-end snapshot 少算 anchor day 最後一筆或洩漏 target day；必須用手算資料驗證。
- 若 SQL 重寫未涵蓋 interarrival / prior odds / prior wager 的原始語意，容易引入 feature value drift。
- 若 artifact scope guardrail 漏掉，training-scoped snapshot 可能污染 production deploy，造成 live mid-term features 系統性缺失。
- 若共用 materializer API 混入 trainer-specific path 或 production config，後續維護容易再次發生 scope drift。

## Open Questions

- Step 4 是否需要每次重新 enrich 全量 `training_set.parquet`，或可以針對 unchanged training set 直接 reuse `training_set_fe_enriched.parquet`？
- 是否需要新增 CLI flag 讓使用者明確選擇 `--reuse-mid-term-snapshot` / `--force-mid-term-snapshot-refresh`？
- Production refresh 是否要在同一輪改成 allowlist-universe 前推，或先只加 guardrail 並保留現有 full-temp-then-filter 行為？

