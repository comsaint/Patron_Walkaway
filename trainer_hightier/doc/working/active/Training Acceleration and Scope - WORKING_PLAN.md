# trainer_hightier - Training Acceleration and Scope - Working Plan

本文件屬於 **Working / Execution Plan 層**，承接：

- SSOT：[`Training Acceleration and Scope - SSOT.md`](../../ssot/Training%20Acceleration%20and%20Scope%20-%20SSOT.md)
- Implementation Plan：[`Training Acceleration and Scope - IMPLEMENTATION_PLAN.md`](../../implementation/active/Training%20Acceleration%20and%20Scope%20-%20IMPLEMENTATION_PLAN.md)

本文件只定義可執行任務拆解、順序、DoD、驗證與 blocker 管理；不重寫 scope 或 architecture。

---

## 1) 範圍與護欄

### 1.1 In scope

- `TrainingScopePolicy` / `SamplePolicy` config object 與 fingerprint
- target horizon 只縮 target rows，不截斷 primitive source history
- Step 3.5 short-PIT miss-path 改接 indexed replay
- full-month gate / waiver artifact 正式化（WP-10 治理決策落 code）
- train-only negative downsampling artifact layer
- feature screening hook + manifest contract（預設關閉）
- **incident-driven data integrity test / gate hardening**（非新 scope；補強 cleaned-data row loss 與 pre-train fail-fast，見 Stage 2 `TA-WP-2.9`～`2.11`、Stage 4 `TA-WP-4.9`～`4.10`）

### 1.2 Out of scope

- 改 walkaway label business definition
- 改 production scorer runtime contract
- harmonize batch-local vs full-month canonical fanout（`bet__*` waiver 維持特例）
- 通用 waiver framework
- feature screening 完整 top-k rollout 或直接改 registry baseline
- bounded DuckDB 作 miss-path automatic fallback

### 1.3 已鎖定決策

| 項目 | 決策 |
|------|------|
| Step 3.5 miss-path | 預設 `indexed replay`（`emit_opt`） |
| Gate | `fe__*` hard parity；`legacy bet 1h pack` 採 **特例釘死 waiver** |
| Waiver 形式 | 不做通用 framework；只接受已文件化 WP-10 root cause 類型 |
| Failure mode | fail-fast；無 bounded auto fallback |
| Neg downsampling | 僅 speed / iteration run；**release / promoted run 維持 `neg_sample_frac=1.0`** |
| Val/test | 永不採樣 |
| Refit-on-train+val | 若有 refit，val 不 downsample |
| Feature screening | Phase 4 hook only；預設關閉；只產 manifest |
| Policy config | Python dataclass；不用 env var |
| Default horizon | MVP `recent_full_months=3`，`include_current_partial_month=true` |
| Completeness | dev/benchmark `warn`；release 建議 `strict`（可後續治理） |

### 1.4 上位追溯

| 層級 | 文件 | 職責 |
|------|------|------|
| SSOT | `Training Acceleration and Scope - SSOT.md` | policy truth、TA-001～017、DL-001 |
| Implementation Plan | `Training Acceleration and Scope - IMPLEMENTATION_PLAN.md` | module boundary、phase strategy |
| Working Plan | **本檔** | ticket 拆解、順序、DoD、驗證 |

若實作與上位文件衝突，不得在工作層直接改寫 scope；先回寫 SSOT 或 Implementation Plan。

---

## 2) 任務總覽

| Batch | Stage | 主題 | 狀態 |
|-------|-------|------|------|
| A | 1 | Policy foundation + reporting skeleton | **pending** |
| A | 2 | WS-A target scope / completeness / split | **pending** |
| B | 3 | WS-B Step 3.5 indexed replay miss-path | **in progress**（quantile reuse 已完成；實機 smoke 待驗） |
| B | 4 | WS-C gate / waiver / report formalization | **pending** |
| C | 5 | WS-D train-only negative downsampling | **pending** |
| D | 6 | WS-E feature screening hook | **pending** |
| — | 2+4 | Incident-driven data integrity hardening（cross-cutting） | **pending** |

**前置已完成：** WP-10 full-month gate（202605，10.28× speedup，`fe__*` pass，`bet__*` waiver accepted，DL-001）。

**Incident-driven hardening（cross-cutting，非新 scope）：** 承接 [`.cursor/plans/INCIDENT.md`](../../../../.cursor/plans/INCIDENT.md) 的 guardrail 補強，對齊 Implementation Plan WS-A（target-vs-source / completeness）與 WS-C（Step 4.5 fail-fast gate separation）。任務分散在 Stage 2（L0 cleaned row integrity、target boundary）與 Stage 4（raw-source sanity、pre-train integration）；**不得**用固定歷史 run 指標（例如 `518 days`）當 assertion，改驗證 policy contract。

**既有實作錨點（新任務在此基礎上補測試，不重寫架構）：**

- `trainer.py`：training scope horizon filter、completeness audit；Step 4.5 `_maybe_run_pre_train_feature_gate` fail-fast before Step 5
- `06_verify_training_serving_parity.py`：`run_pre_train_feature_gate()`、`run_raw_source_w1h_sanity_check()`
- `tests/test_bet_preprocess.py`：`test_consolidate_staged_bucket_partition_dirs_preserves_multi_shard_rows` 已覆蓋 consolidation regression

---

## 3) Stage 1 — Policy Foundation and Reporting Skeleton

**目標：** 固定 policy 介面與 run artifact schema，後續 workstream 只填值、不回頭改 contract。

| ID | 任務 | Files | DoD |
|----|------|-------|-----|
| TA-WP-1.1 | 定義 `TrainingScopePolicy` dataclass | `config.py` | 含 `recent_full_months`, `include_current_partial_month`, `as_of_date`, `data_completeness_mode`, resolved `target_months` |
| TA-WP-1.2 | 定義 `SamplePolicy` dataclass | `config.py` | 含 `neg_sample_frac`, `neg_sample_seed`, `neg_sample_scope=train_only` |
| TA-WP-1.3 | 定義 screening hook policy stub | `config.py` | `feature_screening_enabled=false` 預設；fingerprint helper 預留 |
| TA-WP-1.4 | Policy fingerprint helpers | `config.py` 或小型 helper module | 產出 `training_scope_policy_fingerprint`, `sample_policy_fingerprint`, `feature_selection_policy_fingerprint` |
| TA-WP-1.5 | Trainer policy resolution wiring | `trainer.py` | run 開始時 resolve policies；傳入 downstream steps |
| TA-WP-1.6 | Run manifest / report skeleton | `reporting/writer.py`, `trainer.py` | 預留欄位：target months, completeness, cache hit/miss, gate/waiver summary |
| TA-WP-1.7 | Release guard for sampling | `trainer.py` 或 `05_lgbm_train.py` | promoted/release run 若 `neg_sample_frac != 1.0` → fail-fast |
| TA-WP-1.8 | Focused tests | `tests/test_training_scope_policy.py`（新建） | fingerprint 穩定、default values、release guard |

**Stage 1 DoD：**

- config / trainer / reporting 間 policy 介面固定
- fingerprint 清楚區分 target scope、sampling、screening，未混入 primitive cache fingerprint
- dry-run 可輸出最小 run artifact schema

**驗證：**

```bash
pytest trainer_hightier/tests/test_training_scope_policy.py -q
```

---

## 4) Stage 2 — WS-A Target Scope / Completeness / Split

**目標：** `recent_full_months=3` 只縮 target rows，primitive history 不受 horizon 截斷。

| ID | 任務 | Files | DoD |
|----|------|-------|-----|
| TA-WP-2.1 | Target-month resolution | `config.py`, `trainer.py` | 由 `as_of_date + recent_full_months + include_current_partial_month` 解析 selected months |
| TA-WP-2.2 | Target-row filtering at assembly | `03_build_training_data.py`, `trainer.py` | 只 filter target rows；primitive materialization 仍依 entity set / source coverage |
| TA-WP-2.3 | Step 3 month pruning at assembly boundary | `03_build_training_data.py`, `trainer.py` | Feast / slow-snap month batching 只 iterate selected target months；不得先全量 assemble 17–18 個月再於 Step 3 後做 horizon filter（patch plan：[Step 3 Month Prune Patch - PATCH_PLAN.md](Step%203%20Month%20Prune%20Patch%20-%20PATCH_PLAN.md)） |
| TA-WP-2.4 | Completeness audit | `03_build_training_data.py` 或 helper | 輸出 expected months、missing `gaming_day_event` dates、per-month row/censored counts |
| TA-WP-2.5 | `warn/strict` branch | `trainer.py` | `strict` 缺 full-month dates → fail；`warn` → report only |
| TA-WP-2.6 | Split reporting | `04_split_dataset.py` | censored exclusion、selected target months、month-level counts 寫入 artifact |
| TA-WP-2.7 | Cache invalidation alignment | `utils/cache_invalidation_v1.py`, `trainer.py` | horizon change 只 invalidate assembly/split/sample/model |
| TA-WP-2.8 | Focused tests | `tests/test_training_scope_policy.py`, split tests, Step 3 integration smoke | horizon resolution、target-vs-source boundary、strict/warn，且 recent horizon run 不得觸發非 target month Feast assembly |
| TA-WP-2.9 | Cleaned bet preprocess end-to-end row integrity | `tests/test_bet_preprocess.py`, `utils/bet_l0_preprocess.py` | synthetic fixture 下，post-DQ / post-dedup / post-scope 後 cleaned row count 與 expected 一致；不得靜默丟 row（補強 consolidation regression 之外的全路徑） |
| TA-WP-2.10 | Cache poisoning / stale-manifest regression | `tests/test_bet_preprocess.py`, `tests/test_session_clean_cache.py` | cleaned output row count 或 fingerprint 與 manifest 不一致時不得 cache hit；不得沿用 corrupt artifacts |
| TA-WP-2.11 | Target-vs-source boundary regression | `tests/test_training_scope_policy.py`, `trainer.py` | `recent_full_months=3` 只縮 target rows；`recent_full_months=None` 不套 horizon filter；horizon change 只 invalidate assembly/split/sample/model，不重算 L0–L5 primitives |

**Stage 2 DoD：**

- `recent_full_months=3` 只影響 target rows
- Step 3 在 assembly boundary 就只物化 / assemble selected target months，不接受先全量月批次再裁 target rows 的實作
- val/test 仍保有最新 eligible uncensored rows
- run artifact 能解釋 selected months、completeness、censored counts
- cleaned bet preprocess 在 synthetic fixture 下 row count 與 expected 一致；stale / corrupt cache 不得 hit
- target horizon 與 primitive source history 邊界有 regression test 覆蓋

**驗證：**

```bash
pytest trainer_hightier/tests/test_training_scope_policy.py -q
pytest trainer_hightier/tests/test_step4_split_dataset.py -q
pytest trainer_hightier/tests/test_bet_preprocess.py -q -k "consolidate or cache or metamorphic"
pytest trainer_hightier/tests/test_session_clean_cache.py -q
# 代表 run：比較 3m vs 6m invalidation 範圍（手動或 integration smoke）
# 代表 run：recent_full_months=3 時，Step 3 日誌 / artifact 僅涵蓋 target months
```

---

## 5) Stage 3 — WS-B Step 3.5 Indexed Replay Miss-Path

**目標：** shard hit path 不變；miss / cold-build / fill 走 indexed replay，且 quantile 升降時 Step 3.5 至少能 partial reuse 既有 shard。

| ID | 任務 | Files | DoD |
|----|------|-------|-----|
| TA-WP-3.1 | Miss branch wiring | `feature_experiment/short_term_pit_cache.py` | miss 改呼叫 `short_term_pit_replay_indexed_prototype`；hit 仍讀 month shard |
| TA-WP-3.2 | Callable contract freeze | `short_term_pit_replay_indexed_prototype.py`, `materialize_fe_derived.py` | 輸入/輸出、column set、emit mode 固定並文件化 |
| TA-WP-3.3 | Materializer fingerprint expansion | `short_term_pit_cache.py` | fingerprint 納入 miss-path engine、`indexed_replay_gate_mode` |
| TA-WP-3.4 | Fail-fast on replay failure | `short_term_pit_cache.py` | 無 bounded silent fallback |
| TA-WP-3.5 | Run report metrics | `trainer.py`, `reporting/writer.py` | shard hit/miss counts、miss reason、replay wall time、materializer choice |
| TA-WP-3.6 | Hit path regression tests | `tests/test_short_term_pit_cache.py` | 既有 hit / force refresh 行為不變 |
| TA-WP-3.7 | Miss-path wiring test | `tests/test_short_term_pit_cache.py` 或新 test | miss 時選 indexed replay；failure propagate |
| TA-WP-3.8 | Quantile-aware manifest contract | `feature_experiment/short_term_pit_cache.py` | manifest / report 可區分 `exact_hit`、`subset_hit`、`delta_fill`、`cold_build`；不得再只靠 exact entity-set fingerprint 判定 hit/miss |
| TA-WP-3.9 | Stricter-quantile subset reuse | `feature_experiment/short_term_pit_cache.py` | `0.95 -> 0.99` 時，若 requested `bet_id` 為既有 looser shard 子集，直接 filter/re-publish；不得重跑 full replay |
| TA-WP-3.10 | Looser-quantile delta-fill hardening | `feature_experiment/short_term_pit_cache.py`, `utils/entity_set_v1.py` | `0.99 -> 0.95` 時，只對新增 target rows 做 fill 並 merge；既有 rows 不重算 |
| TA-WP-3.11 | Requested-universe compatibility checks | `feature_experiment/short_term_pit_cache.py` | subset/delta publish 前驗 output row count、unique `bet_id`、requested column coverage、source/mapping/policy fingerprints |
| TA-WP-3.12 | Quantile change regression tests | `tests/test_short_term_pit_cache.py`, `tests/test_entity_set_v1.py` | 覆蓋 `0.95 -> 0.99` subset reuse、`0.99 -> 0.95` delta fill、exact universe mismatch 但可安全 reuse 的情境 |

### Stage 3 優先順序（建議執行序）

依目前 codebase 狀態（2026-06-09）：

| 優先 | ID | 狀態 |
|------|-----|------|
| P0 | TA-WP-3.8 | **done**：manifest / report 區分 `exact_hit` / `subset_hit` / `delta_fill` / `cold_build` |
| P0 | TA-WP-3.11 | **done**：subset/delta publish 前驗 row count、column coverage、unique `bet_id` |
| P1 | TA-WP-3.9 | **done**：`0.95 -> 0.99` subset filter/re-publish |
| P1 | TA-WP-3.12（subset） | **done**：單元測試覆蓋 |
| P2 | TA-WP-3.10 | **done**：`0.99 -> 0.95` missing-bet delta fill + merge 驗證 |
| P2 | TA-WP-3.12（delta） | **done**：單元測試覆蓋 |
| P2 | TA-WP-3.5 | **done**：`trainer.py` / `writer.py` 輸出 materializer choice、hit reason |
| P3 | TA-WP-3.6, 3.7 | **done**：exact hit 回歸、indexed replay wiring、fail-fast 單元測試 |
| — | TA-WP-3.1～3.4 | **done**（indexed replay miss-path 已接線） |
| — | 實機 smoke | **pending**：`0.95` shard 完成後改 `0.99` 驗 subset-hit；`0.99 -> 0.95` 驗 delta-fill |

**已完成切片：** `3.8 + 3.11 + 3.9 + 3.10 + 3.12 + 3.5 + 3.6/3.7`（程式 + 單元測試）。

**待驗：** 代表 cold-build run 後 quantile-up / quantile-down 實機 log 與 `training_acceleration_policy.cache_hit_miss_summary`。

**Stage 3 DoD：**

- shard hit path 行為不變
- shard miss 走 indexed replay 且 failure fail-fast
- bounded path 僅 benchmark / oracle / diagnosis
- `0.95 -> 0.99` 可走 subset-hit，不得因 quantile 收緊而一律 cold-build
- `0.99 -> 0.95` 可只補 delta rows，不得重算既有 shard rows
- cache artifact / run report 能清楚區分 exact hit、subset hit、delta fill、cold build

**驗證：**

```bash
pytest trainer_hightier/tests/test_short_term_pit_cache.py -q
pytest trainer_hightier/tests/test_entity_set_v1.py -q -k "quantile or delta"
# 代表 cold-build run：確認 artifact 含 replay metrics
# 代表 quantile-up run：確認 `0.95 -> 0.99` 走 subset-hit，而非 full replay
# 代表 quantile-down run：確認 `0.99 -> 0.95` 只補 delta rows
```

---

## 6) Stage 4 — WS-C Gate / Waiver / Report Formalization

**目標：** WP-10 治理決策寫入 code；artifact 與 SSOT DL-001 一致。

| ID | 任務 | Files | DoD |
|----|------|-------|-----|
| TA-WP-4.1 | Refactor gate evaluator | `short_term_pit_replay_indexed_prototype.py` | 拆 output validation / hard parity / waiver / speedup / final decision |
| TA-WP-4.2 | Hard parity on `fe__*` | same | zero mismatch required |
| TA-WP-4.3 | Pinned waiver for `legacy bet 1h pack` | same | scope 限已知 pack；需 root cause doc、ratio 上限、hard parity pass |
| TA-WP-4.4 | Preserve `parity.passed=false` on waiver | same | 不得 silent rewrite 成 full pass |
| TA-WP-4.5 | Report schema formalization | `reporting/writer.py` | `hard_parity_passed`, `waiver_accepted`, `waiver`, `decision_basis`, `final_integration_met` |
| TA-WP-4.6 | Separate Step 4.5 gate | `06_verify_training_serving_parity.py` | 不混入 indexed-vs-bounded cold-build logic |
| TA-WP-4.7 | Gate tests | `tests/test_short_term_pit_replay_indexed_prototype.py` | pass / hard fail / waiver accepted / waiver rejected |
| TA-WP-4.8 | Benchmark replay validation | benchmark harness | 用 WP-10 artifact 或同級 run 驗 decision block |
| TA-WP-4.9 | Raw-source severe-undercount fail test | `tests/test_step06_parity_gates.py`, `06_verify_training_serving_parity.py` | fixture 下 raw recompute 明顯大於 training `bet__bets_cnt__w1h`（例如 125 vs 5）時 `run_raw_source_w1h_sanity_check()` 回傳 `verdict=fail`；`n_rows_compared=0` 亦必須 fail（已有 regression，與 severe undercount 一併覆蓋） |
| TA-WP-4.10 | Pre-train feature gate integration fail-fast | `tests/test_step06_parity_gates.py`, `trainer.py` | `run_pre_train_feature_gate()` 在 raw sanity fail 時 `verdict=fail`；`_maybe_run_pre_train_feature_gate` 拋錯，Step 5 不得開始 |

**Waiver 接受條件（釘死）：**

- scope = `legacy_bet_pack_1h` only
- `fe__*` hard parity passed
- mismatch ratio ≤ 約定上限（WP-10 參考：54 / 3.4M ≈ 0.0016%）
- root cause = batch-local bounded pool fanout vs full-month alias fanout
- 非 scope 欄位 mismatch → fail

**Stage 4 DoD：**

- code 直接輸出與 DL-001 一致 gate artifact
- 非 waiver scope 或 waiver 條件不成立 → fail-fast
- Step 4.5 raw-source sanity 在 severe undercount 或 zero-eligible 時 fail；trainer 在 gate fail 時 halt before Step 5
- indexed-vs-bounded cold-build gate 與 Step 4.5 train-vs-serve gate 維持分離（Implementation Plan §1 dual gate separation）

**驗證：**

```bash
pytest trainer_hightier/tests/test_short_term_pit_replay_indexed_prototype.py -q
pytest trainer_hightier/tests/test_step06_parity_gates.py -q
```

---

## 7) Stage 5 — WS-D Train-Only Negative Downsampling

**目標：** Step 4 與 Step 5 間加入 sampled-train layer；evaluation 不受污染。

| ID | 任務 | Files | DoD |
|----|------|-------|-----|
| TA-WP-5.1 | Sampled-train artifact layer | `trainer.py`, 新 helper 或 step module | Step 4 後產出 sampled train parquet |
| TA-WP-5.2 | Negative-only sampling | same | 正樣本預設全保留；固定 seed |
| TA-WP-5.3 | Step 5 integration | `05_lgbm_train.py` | policy enabled 時讀 sampled train |
| TA-WP-5.4 | Metrics disclosure | `05_lgbm_train.py`, `reporting/writer.py` | pre/post train rows、sample ratio、evaluation unsampled 標記 |
| TA-WP-5.5 | Cache invalidation | `utils/cache_invalidation_v1.py` | `neg_sample_frac` change → 只 invalidate sampled train + model |
| TA-WP-5.6 | Leakage / config guards | `trainer.py` | invalid config fail-fast；release `neg_sample_frac=1.0` |
| TA-WP-5.7 | Focused tests | `tests/test_step5_lgbm_train.py` 或新 test | reproducibility、val/test untouched、release guard |

**Stage 5 DoD：**

- sampling 只作用 train split
- val/test metrics 完全來自 unsampled split
- policy change 不重算 primitive caches

**驗證：**

```bash
pytest trainer_hightier/tests/test_step5_lgbm_train.py -q
```

---

## 8) Stage 6 — WS-E Feature Screening Hook

**目標：** hook + manifest contract；預設 no-op；不動 registry baseline。

| ID | 任務 | Files | DoD |
|----|------|-------|-----|
| TA-WP-6.1 | Trainer hook position | `trainer.py` | Step 4 後、Step 5 前 optional hook |
| TA-WP-6.2 | Manifest contract | helper + docs in WP | `selected_features`, method, evidence refs, fingerprint |
| TA-WP-6.3 | FQG pattern alignment | 參照 `feature_quality_gate.py` | manifest schema 與 experiment pipeline 對齊 |
| TA-WP-6.4 | Report fields | `reporting/writer.py` | `feature_selection_policy_fingerprint`, screening metadata |
| TA-WP-6.5 | Default off behavior | `trainer.py`, `config.py` | 關閉時 baseline path 完全不變 |
| TA-WP-6.6 | Focused tests | new test | no-op regression；manifest-enabled smoke |

**Stage 6 DoD：**

- hook 關閉 = no-op
- hook 開啟只讀 manifest，不直接改 `feature_candidate_registry.yaml`

**驗證：**

```bash
pytest trainer_hightier/tests/test_feature_screening_hook.py -q
```

---

## 9) 建議執行順序

```text
Batch A: TA-WP-1.* → TA-WP-2.*
Batch B: TA-WP-3.* → TA-WP-4.*
Batch C: TA-WP-5.*
Batch D: TA-WP-6.*
```

```mermaid
flowchart TD
    Incident[IncidentLearnings] --> Hardening[DataIntegrityHardening]
    S1[Stage1 PolicySkeleton] --> S2[Stage2 TargetScope]
    Hardening --> S2
    S2 --> S3[Stage3 IndexedReplayMissPath]
    S3 --> S4[Stage4 GateWaiver]
    Hardening --> S4
    S2 --> S5[Stage5 NegDownsample]
    S1 --> S5
    S1 --> S6[Stage6 ScreeningHook]
```

Stage 3–4 與 Stage 5 可在 Batch A 完成後部分並行，但 **Stage 5 不應早於 Stage 2 split 穩定**；Stage 6 不阻塞主線。**Data integrity hardening（`TA-WP-2.9`～`2.11`、`TA-WP-4.9`～`4.10`）** 可與 Stage 2 / 4 並行推進，建議在 corrected-source rebuild 後優先完成 `TA-WP-4.9`～`4.10` 再跑 release training。

---

## 10) 整體驗收門檻

| 項目 | 門檻 |
|------|------|
| Target scope | `recent_full_months=3` 只縮 target rows，且 Step 3 只 assemble target months |
| Primitive reuse | horizon change 不重算 L0–L5 primitives（coverage 足夠時） |
| Step 3.5 miss-path | 預設 indexed replay；`fe__*` hard parity 綠燈 |
| Waiver governance | `parity.passed=false` 保留；`hard_parity_passed` + pinned waiver 可 integration |
| Downsampling | train-only；release `neg_sample_frac=1.0` |
| Screening | 預設關閉且 no-op |
| Reporting | run report 能解釋 time saved、cache reuse、row reduction、gate/waiver decision |
| Data integrity | cleaned preprocess 不得靜默丟 row；stale cache 不得 hit；raw-source sanity / pre-train gate fail-fast 有 regression 覆蓋 |
| Gate separation | indexed-vs-bounded cold-build gate 與 Step 4.5 train-vs-serve gate 分離且各自 fail-fast |

---

## 11) Blockers / Escalation

**立即停止並回頭修正：**

- target scope 導致 primitive cache 被重算
- recent horizon run 仍對非 target months 做 Step 3 Feast / slow-snap assembly
- Step 3.5 hit path regression
- gate artifact 無法區分 `hard_parity_passed` 與 `waiver_accepted`
- sampling 改變 val/test row count 或 metrics contract
- screening hook 關閉時仍改變 baseline path
- cleaned bet preprocess 在 synthetic fixture 下 row count 與 expected 不符
- raw-source sanity `n_rows_compared=0` 或 severe undercount 仍 pass
- pre-train feature gate fail 後 trainer 仍進入 Step 5

**文件 / 實作 drift（需確認 source of truth，不在本次 hardening 內偷改 scope）：**

- Implementation Plan 寫 `SamplePolicy.neg_sample_frac = 1.0`（default off）；目前 `config.py` 為 `0.3`。撰寫 downsampling / release-guard 測試前須先對齊上位文件或回寫 Implementation Plan。

**升級回 SSOT / Implementation Plan：**

- `legacy bet 1h pack` waiver 不再是 isolated 特例
- `recent_full_months=3` 明顯損害模型品質
- release run 必須允許 sampling（與現行 frozen decision 衝突）

---

## 12) 每批交付清單

每一 Batch 完成時至少交付：

1. focused automated tests
2. 一次 representative run 或 benchmark，佐證 artifact 正確
3. 簡短驗收摘要（cache invalidation 範圍、report 新欄位、fail-fast 行為）

---

## 13) 相關文件

| 文件 | 用途 |
|------|------|
| [`Training Acceleration and Scope - SSOT.md`](../../ssot/Training%20Acceleration%20and%20Scope%20-%20SSOT.md) | policy truth |
| [`Training Acceleration and Scope - IMPLEMENTATION_PLAN.md`](../../implementation/active/Training%20Acceleration%20and%20Scope%20-%20IMPLEMENTATION_PLAN.md) | architecture / phases |
| [`Short-Term PIT … WORKING_PLAN.md`](Short-Term%20PIT%20Cache%20and%20Materialize%20Performance%20-%20WORKING_PLAN.md) | WP-10 歷史與 prototype 細節 |
| [`.cursor/plans/INCIDENT.md`](../../../../.cursor/plans/INCIDENT.md) | cleaned-data row loss root cause、remediation、guardrail 需求 |
| `out/replay_benchmark_202605_indexed_full_month_gate16_emit_opt/benchmark_report.json` | DL-001 證據 |
