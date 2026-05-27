# Short-Term PIT Cache and Materialize Performance - Implementation Plan

本文件是 **Implementation Plan 層**，聚焦 `trainer_hightier` Step 3.5 short-term PIT 物化的可重用快取與效能改善策略。  
治理規格（語意、邊界、供應契約）以上位 SSOT 為準：

- `trainer_hightier/doc/Scorer Runtime Contract - SSOT.md`
- `trainer_hightier/doc/Data pipeline - SSOT.md`

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

## WS-C: Observability and operational controls

**Goal**：讓使用者能判斷「是否在工作、為何重算、可預期多久」。

**Approach**

- 固定輸出 cache summary：hit shards、miss shards、partial recompute shards。
- 固定輸出 materialize 心跳與進度估算（每 N batch）。
- 在 run report 寫入 cache 與效能欄位，支援後續比較。

**Deliverables**

- `run_summary` / `run_report` 新增 short cache 與 performance 欄位。
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

