# Short-Term PIT Cache and Materialize Performance - Implementation Plan

本文件是 **Implementation Plan 層**，聚焦 `trainer_hightier` Step 3.5 short-term PIT 物化的可重用快取與效能改善策略。  
治理規格（語意、邊界、供應契約）以上位 SSOT 為準：

- `trainer_hightier/doc/ssot/Scorer Runtime Contract - SSOT.md`
- `trainer_hightier/doc/ssot/Data pipeline - SSOT.md`

本文件定義 realization strategy、模組邊界、分階段交付、風險與驗證；不展開 ticket 級 task list。

## Context

目前 Step 3.5 short-term path 採 `materialize_fe_derived_short_term_parquet(...)` 的 bounded PIT batch 模式。  
在大訓練集下，批次數可達數百（例如 837 batches），導致：

- 每次 Step 4 前幾乎重做全部 short-term 物化。
- 同步存在多個可優化計算點（batch iterator、重複掃描、batch 粒度過細）。
- 特徵清單與訓練 universe 常變，但 cleaned bet/session 通常增量變更，現況未充分利用這個特性。

## Objective

在不破壞 train-serve parity（bounded PIT 語意）的前提下，將 short-term 物化從「每輪全量重算」改為「可解釋、可局部重算、可觀測」。

## Scope

- 訓練 Step 3.5 short-term PIT cache（`_main_trainer_fe_short_term.parquet`）的快取與增量策略。
- short-term materialize 計算流程效能優化（含 batch iterator 修正與批次策略）。
- 命中/失效診斷、metrics 與 run report 可觀測性。

## Out of Scope

- 變更 short-term feature business semantics。
- 將 short-term 改成 Feast 供應。
- 重寫 mid-term/slow-term contract（僅可重用其快取設計模式）。

## Guiding Constraints

- 必須維持 bounded PIT 語意與 `expand_canonical_aliases=False` 政策一致。
- 必須支援筆電資源限制，避免 OOM（RAM bounded, IO bounded）。
- cache key 與 invalidation 必須 deterministic 且可解釋。

## Target Strategy

### 1) Two-layer output strategy

- **Layer A（重）**：short-term PIT cache store（可分片、可重用、可局部重算）。
- **Layer B（輕）**：`training_set_fe_enriched.parquet` 每輪重建（join Layer A + mid snapshot），不作為主要快取層。

理由：enrich 成本低，複雜性應集中在短期 PIT 主成本路徑。

### 2) Month-sharded cache layout

以訓練 bet 所屬 `YYYYMM` 分片管理 short-term cache，避免單一大檔案任一差異就全失效。

建議邏輯結構：

- `artifacts/training_data/cache/short_term_pit_v1/manifest.json`
- `.../shards/yyyymm=202605/data.parquet`
- `.../shards/yyyymm=202605/shard_manifest.json`

### 3) Cache invalidation dimensions

快取命中需同時滿足：

- **Code/algorithm fingerprint**：short materializer + shared scoring context。
- **Serving policy fingerprint**：lookback hours、batch policy、alias policy。
- **Input source fingerprint**：cleaned bet partition inventory（含受影響月份與必要回補鄰月）。
- **Universe fingerprint**：分片內訓練 key 指紋（`bet_id/player_id/payout_complete_dtm`）。
- **Schema/columns fingerprint**：short 需要欄位與版本。

### 4) Incremental recompute behavior

- 若 source 只新增/變更少數月份，僅重算受影響 shard。
- 若只改 feature baseline（且欄位已存在於 cache），直接重用 Layer A，重跑 enrich + Step 4/5。
- 若新增 short 欄位且 cache 缺欄，重算受影響 shard（非全量）。

## Workstreams

## WS-A: Cache architecture and reuse gate

**Goal**：建立 deterministic、可診斷、可局部重算的 short-term cache。

**Approach**

- 參考 mid-term `try_reuse_*` 模式，新增 short-term cache manifest gate。
- 以分片 manifest + 全域 manifest 管理命中條件與失效原因。
- 將命中/失效原因寫入 log 與 run report。

**Deliverables**

- Short-term cache metadata schema（全域 + shard）。
- Reuse gate API（hit / partial / miss）。
- 訓練流程接線（Step 3.5 呼叫 reuse gate 後再決定重算範圍）。

## WS-B: Compute-path performance optimization

**Goal**：在 cache miss 或 partial miss 時，降低單次重算成本。

**Approach**

- 修正 batch iterator：避免每個 batch 都重做全表排序與 row-number 切片。
- 訓練 materialize batch policy 與 scorer batch policy 解耦（保留語意一致，允許較大訓練批次）。
- 優化 pool 讀取路徑（減少重複 metadata scan / 重複連線開銷）。

**Deliverables**

- 新 batch iterator 實作（一次排序/分塊產生，非 repeated global sort）。
- 訓練專用 batch size 配置（落在 `config.py`，不使用環境變數控制）。
- 效能回歸報告（wall time、CPU time、peak RAM、scan bytes）。

### WS-B2: Event replay / streaming-state prototype

**Goal**：研究是否能以「單次時間序 replay + per-entity rolling state」取代目前 per-batch bounded pool window SQL，在維持 exact per-bet PIT 語意的前提下，降低 full miss short PIT cold-build 成本。

**Positioning**

- 這是 prototype，不是 Phase 3 預設替換路徑。
- 不改 production scorer contract；production 仍使用 live bounded PIT。
- 不採 time-bucket / ASOF approximation；prototype 目標是輸出與現有 bounded DuckDB materializer 對齊。
- 若 prototype 無法通過 parity 或無明顯效能收益，保留現有 month-sharded cache + month-pool reuse 策略。
- 初版 prototype 已驗證 correctness，但未達效能 gate；不得接入 production/cache path。

**Approach**

目前 bounded materializer 對每個 target batch 建立 scoring bounds，從 hot pool 切出每筆 target bet 的歷史視窗，再用 DuckDB window function 計算 short features。Event replay prototype 改成：

1. 對單一 miss month 載入 target month training bets 與必要 lookback cleaned bet rows。
2. 依 `payout_complete_dtm, bet_id` 排序事件流。
3. 以 `player_id` / `canonical_id` policy 維護 rolling state（15m、1h、7d、today、last-event）。
4. 當事件是 target training bet 時，先從 state emit PIT features，再把該事件寫入 state。
5. 輸出仍寫入現有 month shard path，並以現有 short PIT cache manifest 管理命中與失效。

**Prototype scope**

- 先只做 bounded 1-month / fixed sample，避免 full cold build 驗證成本。
- 先覆蓋一小組高價值欄位：
  - `fe__bets_cnt__w15m`
  - `fe__wager_sum__w15m`
  - `fe__time_since_last_bet_sec`
  - `fe__odds__payout_odds_step_ratio`
  - `bet__bets_cnt__w1h`
- 使用既有 bounded DuckDB path 作 oracle；比較同一批 target `bet_id` 的欄位值。
- 初版可用 Python / pandas / NumPy 實作以驗證語意；只有在 parity 成立後才考慮 Numba / Polars / Rust extension 等效能化。

**State model**

- Rolling sum/count：使用 timestamp queue + running sum/count。
- Rolling stddev：使用 timestamp queue + running sum / sumsq / count。
- Rolling max：使用 monotonic deque，避免 max 值過期後需重掃。
- Interarrival：先由 last `pcd` 產生 gap，再將 gap 寫入對應 rolling stats。
- Today counters：按 `canonical_id` / `gaming_day_event` 維護 bets so far、wager so far、first bet time。

**Validation**

- Correctness gate：prototype output vs bounded DuckDB output，核心欄位 mismatch 需可解釋；浮點欄位使用明確 tolerance。
- Tie-break audit：同 timestamp / same canonical / same player rows 必須單獨抽樣檢查，確認是否對齊現有 `ORDER BY pcd, bet_id` 與 `1 MICROSECOND PRECEDING` 語意。
- Performance gate：1-month sample 至少比 month-pool DuckDB bounded path 快 3x，且 peak RAM 不高於現有 path。
- Resource gate：記錄 rows/sec、state key count、peak queue length、peak memory。

**Expected impact**

- 若成功，full miss short PIT cold build 目標從目前 overnight 等級降至數小時級。
- 初版 Python deque replay 未達預期；後續預估不應沿用 2-8x 假設，需由 indexed replay v2 實測重新校準。
- Warm rerun 仍優先依賴現有 month-sharded primitive cache；event replay 主要改善 miss / partial miss 場景。

**Prototype result (202605 real-data sample)**

已建立 `short_term_pit_replay_prototype.py`，在真實 202605 cleaned pool 上對 5 個核心欄位完成 bounded DuckDB oracle parity：

- `fe__bets_cnt__w15m`
- `fe__wager_sum__w15m`
- `fe__time_since_last_bet_sec`
- `fe__odds__payout_odds_step_ratio`
- `bet__bets_cnt__w1h`

Benchmark 結果：

| Target rows | Replay | Month-pool bounded DuckDB | Speedup | Parity |
|-------------|--------|---------------------------|---------|--------|
| 1,000 | 5.15s | 3.04s | 0.59x | pass |
| 10,000 | 12.14s | 9.26s | 0.76x | pass |
| 50,000 | 46.70s | 42.09s | 0.90x | pass |

Result artifacts:

- `out/replay_benchmark_202605/benchmark_report.json`
- `out/replay_benchmark_202605_scaling_summary.json`

**Interpretation**

- Correctness is validated for the first slice, so the idea failed on performance rather than PIT semantics.
- The current bounded baseline is stronger than the original comparison target because month hot pool reuse avoids repeated parquet reads and DuckDB still executes joins/window functions in vectorized C++.
- The Python prototype does not implement true O(1) / O(log n) streaming-state emit. It scans `state.events` at target emission time for 15m, 1h, and row-lag semantics, so high-frequency patrons with queues around 20k rows create substantial Python overhead.
- Exact PIT semantics require per-target `pool_start`, `scoring_pcd - 1 microsecond`, RANGE lower-bound behavior, same-timestamp tie handling, and row-based lag alignment. These constraints make a simple rolling queue insufficient.

**Decision**

WS-B2 initial prototype is a correctness proof only. It should remain out of production and out of the main short PIT cache path unless a second implementation demonstrates clear speedup against the month-pool DuckDB baseline.

**Indexed v2 result (202605 real-data sample, regrouped gate)**

Follow-up prototype `short_term_pit_replay_indexed_prototype.py` replaces deque scans with per-entity NumPy arrays, prefix sums, and `searchsorted`. The latest regroup benchmark no longer treats full 17-column production scorer coverage as the prototype gate. Instead, it uses the 16-column production short PIT gate with `fe__odds__payout_odds_z__w1h` temporarily marked as prototype out-of-gate because that column still has edge mismatches and is low importance in recent models.

| Target rows | Indexed replay | Month-pool bounded DuckDB | Speedup | 16-column gate parity |
|-------------|----------------|---------------------------|---------|-----------------------|
| 10,000 | 7.91s | 11.62s | 1.47x | pass |
| 50,000 | 29.98s | 58.97s | 1.97x | pass |
| 100,000 | 47.22s | 130.28s | 2.76x | pass |

Artifacts:

- `out/replay_benchmark_202605_indexed_gate16_scaling_summary.json`
- `out/replay_benchmark_202605_indexed_limit10000_gate16_ignore_odds_z_w1h/benchmark_report.json`
- `out/replay_benchmark_202605_indexed_limit50000_gate16_ignore_odds_z_w1h/benchmark_report.json`
- `out/replay_benchmark_202605_indexed_limit100000_gate16_ignore_odds_z_w1h/benchmark_report.json`

Interpretation:

- Correctness is now strong for the current prototype decision gate: all 16 in-gate columns match the bounded DuckDB oracle across 10k / 50k / 100k samples.
- This is not yet full production scorer parity. The production short PIT gate remains 17 columns; `fe__odds__payout_odds_z__w1h` is only ignored for prototype go/no-go, not formally removed from the scorer contract.
- Speedup improves with target count and clears the feasibility gates (50k >= 1.5x, 100k >= 2.0x), but it does not clear the 3x integration gate.
- Phase timing shows the main residual bottleneck is now the Python emit path rather than pool loading or oracle comparison.

**Decision update:** indexed v2 should continue as a prototype optimization track, not as an integration candidate yet. The next decision gate is emit-path optimization plus either refreshed 100k benchmark evidence or full-month cold-build profiling before any Step 3.5 wiring is considered.

### WS-B3: Indexed replay / vectorized state prototype

**Goal**：保留 WS-B2 exact PIT 語意，但用 entity-level indexed arrays / prefix sums 取代 Python row-loop + emit-time deque scan，重新驗證 event replay 是否有實際效能空間。

**Approach**

1. 以 DuckDB 載入同一 month-pruned cleaned pool 與 target sample，但只抽 prototype 所需欄位。
2. 將 pool 與 targets 對齊到 `(canonical_id, player_id)`，依 `canonical_id, player_id, payout_complete_dtm, bet_id` 排序。
3. 每個 entity 轉為 NumPy arrays：
   - `pcd_ns`：int64 timestamp。
   - `bet_id`：排序 tie-break key。
   - `wager` / `wager_prefix_sum`。
   - `payout_odds`。
4. 對 entity targets 批次使用 `np.searchsorted` 計算：
   - 15m count/sum。
   - 1h count。
   - row-based lag / odds step。
5. 避免在 target emit 時掃整段 entity history；所有窗口查詢必須是 O(log n) 或 prefix-sum O(1)。
6. 加入 phase timing：load targets、load pool、canonical attach、sort/group、build arrays、emit、write、oracle、parity。

**Prototype scope**

- 不新增 production integration。
- 目前 prototype gate 以 production scorer short PIT 17 欄中的 16 欄為準，暫時排除 `fe__odds__payout_odds_z__w1h`。
- 不使用 time-bucket / ASOF approximation。
- 不引入多檔大型架構；若需要新檔，命名為 `short_term_pit_replay_indexed_prototype.py`。

**Validation gate**

- Parity：目前 202605 regroup benchmark 以 16-column gate 為準，10k / 50k / 100k sample 全欄位 0 mismatch。
- Performance：50k target 至少 1.5x 快於 month-pool bounded DuckDB，100k target 至少 2x 才能保留 replay 方向。
- Final integration gate：仍需達到 3x speedup 且 peak RAM 不高於現有 path。
- 若 16-column gate parity 失敗，或 100k 仍低於 2x，停止 event replay 方向，後續集中在 DuckDB month-pool / query-level optimization。
- 若 parity 穩定但 100k 仍低於 3x，下一輪優先優化 emit path，而不是直接擴更多 feature family。

**Risks**

- Tie-break 或 timestamp precision 導致 train-serve parity drift。
- Rolling max / stddev / interarrival state 實作錯誤。
- 純 Python row loop 可能抵消演算法收益。
- 高頻 patron 的 7d rolling state 可能造成 RAM 壓力。
- Indexed v2 若仍以 pandas object/Timestamp 執行核心 loop，可能重複 WS-B2 的瓶頸；核心計算必須落在 NumPy int64/float64 arrays。

## WS-C: Observability and operational controls

**Goal**：讓使用者能判斷「是否在工作、為何重算、可預期多久」。

**Approach**

- 固定輸出 cache summary：hit shards、miss shards、partial recompute shards。
- 固定輸出 materialize 心跳與進度估算（每 N batch）。
- 在 run report 寫入 cache 與效能欄位，支援後續比較。

**Deliverables**

- `run_report.json`（`summary` / `pipeline_debug` 巢狀）新增 short cache 與 performance 欄位。
- 失效 reason code（例如 `source_changed`, `universe_changed`, `code_changed`, `schema_miss`）。

## WS-D: Validation and safe rollout

**Goal**：確保加速不引入 train-serve drift 或資源風險。

**Approach**

- 新增/擴充測試：cache hit correctness、partial recompute correctness、batch iterator correctness。
- 以 fixed sample 比較舊/新路徑 short 欄位一致性（允許浮點容差）。
- staging rollout：先 dry-run 記錄，不啟用寫回；再逐步啟用。

**Deliverables**

- 測試與基準報告（correctness + performance）。
- rollout 開關策略與回退路徑。

## Milestones

### M1: Cache foundation

- 完成 short-term cache manifest schema 與 reuse gate。
- 可在 unchanged inputs 下 stable cache hit。

### M2: Partial recompute

- 完成 month shard invalidation + partial rebuild + merge。
- source 增量僅重算受影響 shard。

### M3: Compute optimization

- 完成 batch iterator 修正與訓練批次策略優化。
- miss 場景 wall time 顯著下降且 RAM 不超 budget。

### M4: Productionized observability

- run report、reason code、cache hit ratio、perf 指標完整落盤。
- 文檔與 runbook 完成，支持日常診斷。

## Success Criteria

- unchanged inputs 的 Step 3.5 short materialize 進入 cache hit（秒級到分鐘內）。
- source 增量更新時，重算範圍與受影響月份一致（非全量重算）。
- full miss 場景較目前基線顯著縮短（目標至少 2-5x，依資料規模校準）。
- short-term parity 測試維持綠燈，無新增 OOM 或長時間無心跳卡住。

## Risks and Mitigations

- **Risk: stale cache due to incomplete invalidation**  
  Mitigation: 明確分維度 fingerprint + reason code + guard test。

- **Risk: memory pressure after batch-size tuning**  
  Mitigation: 設定上限與 fallback，自動退回保守 batch。

- **Risk: partial recompute merge bug**  
  Mitigation: shard-level row-count/key uniqueness 檢查 + end-to-end parity diff。

- **Risk: policy drift with scorer runtime**  
  Mitigation: shared context contract 與 parity gate 維持單一真相。

## Governance and Ownership

- SSOT owner：runtime contract / data pipeline owner。
- Implementation owner：trainer pipeline owner（Step 3.5 / Step 4 path）。
- Review gates：ML owner（特徵語意）、platform owner（資源與可觀測性）。

## Open Decisions

- shard key 最終採 `gaming_day month` 或 `payout_complete_dtm month`（需固定一種並文件化）。
- full miss 優化是否納入平行 worker（先效能後決策，避免筆電 OOM 風險）。
- 是否提供顯式 CLI 強制重算旗標（建議支援，但預設走自動判定）。

