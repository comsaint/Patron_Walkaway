# Short-Term PIT Cache and Materialize Performance - Working Plan

本文件是 **Working / execution plan** 層，對應 [Implementation Plan](../../implementation/active/Short-Term%20PIT%20Cache%20and%20Materialize%20Performance%20-%20IMPLEMENTATION_PLAN.md)。

## Decisions Locked

- Shard key：`payout_complete_dtm` → `YYYYMM`
- Phase 1：month-shard recompute（不做 bet-level delta merge）
- Training batch size 與 scorer cycle 解耦（預設 20_000）
- Full miss 先單工；不啟用 parallel workers
- CLI：`--force-refresh-short-term-pit`；`--ignore-caches` 亦強制 short-term PIT 重算
- Event replay 初版僅作 correctness proof；不得接入 production/cache path
- 下一輪若繼續 replay 方向，只做 indexed replay v2，不再沿用 Python deque emit-time scan
- Indexed replay v2 最新 prototype gate 為 production short PIT 17 欄中的 16 欄；`fe__odds__payout_odds_z__w1h` 暫列 out-of-gate，不代表已正式從 scorer contract 移除
- Step 3.5 接線前必須先通過 coverage expansion gate 與 full-month cold-build gate

## Work Breakdown

| ID | Task | Files | DoD |
|----|------|-------|-----|
| WP-1 | Cache manifest + shard layout | `short_term_pit_cache.py`, `config.py` | Schema versioned; helpers for read/write |
| WP-2 | Reuse gate + invalidation | `short_term_pit_cache.py`, `partition_inventory.py` | Hit/miss per shard with reason codes |
| WP-3 | Partial recompute + assemble | `short_term_pit_cache.py`, `trainer.py` | Miss shards only; merge to legacy parquet |
| WP-4 | Batch iterator + training batch size | `materialize_fe_derived.py`, `config.py` | Single sort per materialize; configurable batch |
| WP-5 | CLI + observability | `trainer.py`, `RUNBOOK.md` | Force flag; metrics in run report |
| WP-6 | Tests | `test_short_term_pit_cache.py` | Hit, force refresh, universe change |
| WP-7 | Event replay v1 correctness prototype | `short_term_pit_replay_prototype.py`, `test_short_term_pit_replay_prototype.py` | 5-column oracle parity on synthetic and 202605 sample |
| WP-8 | Indexed replay v2 feasibility | `short_term_pit_replay_indexed_prototype.py` | NumPy indexed state, phase timing, 202605 regroup benchmark and go/no-go |
| WP-8.7 | Indexed replay emit optimization | `short_term_pit_replay_indexed_prototype.py`, tests | Reduce Python emit overhead; refresh 10k/50k/100k gate16 benchmark |
| WP-9 | Indexed replay coverage expansion | `short_term_pit_replay_indexed_prototype.py`, tests | Add high-risk feature groups with per-group oracle parity after emit path is re-baselined |
| WP-10 | Full-month cold-build gate | indexed prototype, cache miss harness | 202605 full-month parity/perf/memory report before Step 3.5 wiring |

## Iteration Status

- **Iteration A (functional cache):** WP-1–WP-3 — **done**
- **Iteration B (performance):** WP-4 — **done**
- **Iteration C (operability):** WP-5–WP-6 — **done**
- **Iteration D (event replay v1):** WP-7 — **done, correctness pass / performance fail**
- **Iteration E (indexed replay v2):** WP-8 — **done, 16-column gate parity pass / feasibility gates pass / integration gate miss**
- **Iteration F (emit optimization):** WP-8.7 — **pending; next priority before more coverage work**
- **Iteration G (coverage expansion):** WP-9 — **pending; required before any integration**
- **Iteration H (full-month gate):** WP-10 — **pending; required before Step 3.5 wiring**

## Event Replay V1 Result

Prototype file:

- `trainer_hightier/feature_experiment/short_term_pit_replay_prototype.py`

Test file:

- `trainer_hightier/tests/test_short_term_pit_replay_prototype.py`

Benchmark artifacts:

- `out/replay_benchmark_202605/benchmark_report.json`
- `out/replay_benchmark_202605_scaling_summary.json`

202605 real-data benchmark:

| Target rows | Replay | Month-pool bounded DuckDB | Speedup | Parity |
|-------------|--------|---------------------------|---------|--------|
| 1,000 | 5.15s | 3.04s | 0.59x | pass |
| 10,000 | 12.14s | 9.26s | 0.76x | pass |
| 50,000 | 46.70s | 42.09s | 0.90x | pass |

Decision:

- Do not integrate replay v1.
- Correctness passed for the first 5 columns, but speedup missed the 3x gate.
- Main cause: Python row loop + emit-time scans over entity history; the optimized bounded baseline already uses month hot pool reuse and DuckDB vectorized windows.

## Indexed Replay V2 Result

Prototype file:

- `trainer_hightier/feature_experiment/short_term_pit_replay_indexed_prototype.py`

Benchmark artifacts:

- `out/replay_benchmark_202605_indexed_gate16_scaling_summary.json`
- `out/replay_benchmark_202605_indexed_limit10000_gate16_ignore_odds_z_w1h/benchmark_report.json`
- `out/replay_benchmark_202605_indexed_limit50000_gate16_ignore_odds_z_w1h/benchmark_report.json`
- `out/replay_benchmark_202605_indexed_limit100000_gate16_ignore_odds_z_w1h/benchmark_report.json`

202605 real-data benchmark (indexed v2 regrouped gate16 vs month-pool bounded DuckDB):

| Target rows | Indexed replay | Bounded | Speedup | 16-column gate parity |
|-------------|----------------|---------|---------|-----------------------|
| 10,000 | 7.91s | 11.62s | 1.47x | pass |
| 50,000 | 29.98s | 58.97s | 1.97x | pass |
| 100,000 | 47.22s | 130.28s | 2.76x | pass |

Decision:

- Indexed v2 passes parity for all sampled sizes in the current 16-column prototype gate.
- It exceeds the 1.5x / 2x feasibility gates at 50k and 100k, but misses the 3x integration gate at 100k.
- `fe__odds__payout_odds_z__w1h` remains the only out-of-gate production column; this is a prototype decision only, not a contract change.
- Emit phase is now the dominant replay cost at scale (`emit_s` ≈ 4.17s @ 10k, 20.93s @ 50k, 39.33s @ 100k).
- Next step is **not** production integration; first optimize emit, then decide whether to continue coverage expansion or move to a full-month gate.

## Indexed Replay V2 Work Breakdown

| ID | Task | Files | DoD |
|----|------|-------|-----|
| WP-8.1 | Add indexed prototype entrypoint | `short_term_pit_replay_indexed_prototype.py` | Narrow API mirrors v1; no production integration |
| WP-8.2 | Build entity arrays | same file | Pool and targets grouped by `(canonical_id, player_id)`; timestamps stored as int64 ns |
| WP-8.3 | Vectorized 15m/1h windows | same file | `np.searchsorted` + prefix sums; no per-target history scan |
| WP-8.4 | Row-lag parity | same file | Same timestamp / `bet_id` tie behavior matches DuckDB oracle |
| WP-8.5 | Phase timing metrics | same file | Load, sort/group, build arrays, emit, write, oracle, parity timings recorded |
| WP-8.6 | Benchmark + go/no-go | `out/replay_benchmark_202605_indexed_*` | 1k/10k/50k/100k results written with parity summary |
| WP-8.7 | Emit optimization regroup | same file, tests | Eliminate obvious Python row/dict overhead and re-benchmark gate16 |

V2 constraints:

- Do not claim full production coverage from the current 16-column gate result; one production column is still intentionally ignored in prototype benchmarking.
- Keep core computation in NumPy int64/float64 arrays; avoid pandas `Timestamp` / Python object comparisons inside the hot loop.
- Preserve exact PIT semantics: per-target `pool_start`, `scoring_pcd - 1 microsecond`, DuckDB RANGE lower-bound behavior, same timestamp ties, and row-based lag.
- If 100k target sample is below 2x speedup, stop event replay direction and focus on DuckDB query-level optimization.
- If 100k stays below 3x while parity is stable, optimize emit before adding more feature families.

## Indexed Replay Coverage Expansion Plan

Indexed v2 is promising, but it is not yet a drop-in replacement for all short PIT features. After the gate16 regroup result, the next work is to reduce emit overhead first; only then should coverage expand by feature family, stopping on the first family that cannot match bounded DuckDB parity.

### Coverage Risks

- `bet__*` pack risk: existing trial SQL partitions by `canonical_id`, while v2 currently keys arrays by `(canonical_id, player_id)`. Multi-player same-canonical alias cases can undercount unless explicitly handled.
- Null semantics risk: DuckDB `AVG`, `STDDEV_POP`, `MAX`, and ratio expressions treat nulls differently from simple `fillna(0)` prefix sums.
- Window type risk: current v2 covers RANGE count/sum and row-lag; full short PIT also uses ROW windows (`last 3`, `last 5`), session partitions, and gaming-day partitions.
- State size risk: 7d/30d windows require longer source coverage and larger arrays than the current 15m/1h slice.
- Output memory risk: full-month output cannot rely on `list[dict]`; it needs column-array output or chunked writes.

### WP-9 Coverage Tasks

| ID | Task | Required parity cases | DoD |
|----|------|-----------------------|-----|
| WP-9.1 | Complete `bet__` 1h pack | multi-player same canonical, same timestamp ties | `bet__bets_cnt__w1h`, `bet__wager_sum__w1h`, `bet__back_bet_ratio__w1h`, `bet__payout_odds_avg__w1h` parity |
| WP-9.2 | RANGE sum/count ratios | 5m, 15m, 1h, 1d, 7d, 30d boundaries | Count/sum and velocity/ratio columns parity |
| WP-9.3 | AVG / STDDEV / z-score state | null odds/wager, single-row window, empty window | Prefix sum + sumsq + valid-count semantics match DuckDB |
| WP-9.4 | RANGE max state | duplicate max, expired max, null values | `*_to_recent_max_ratio__w1h` parity |
| WP-9.5 | Interarrival arrays | same timestamp, lag2 missing, long gaps | gap, avg/std, z-score, ratio columns parity |
| WP-9.6 | Today counters | HK day boundary, missing `gaming_day_event` fallback | `fe__canonical__*__today` parity |
| WP-9.7 | Session features | multiple sessions, same timestamp within session | `fe__session__*` parity |
| WP-9.8 | Table switch / ROW windows | null table, lag1/lag2 missing, last 5 rows | `fe__tableswitch__*` and last-N parity |
| WP-9.9 | Outcome / theo / stake dynamics | null `casino_win`, `theo_win`, `base_ha`, `is_back_bet` | outcome/theo/stake feature parity |

### WP-9 Go/No-Go

- Each feature family must pass synthetic edge-case parity before real-data benchmark.
- Each family must pass 202605 10k / 50k / 100k bounded oracle parity before being marked supported.
- If a family requires expensive non-vectorized Python scans, keep it on bounded DuckDB and do not claim full replacement.
- If adding coverage drops 50k speedup below 1.5x, stop expansion and reassess hybrid materialization.

## Full-Month Cold-Build Gate

WP-10 validates whether indexed replay is viable as a short PIT cache miss path, not just as a sample benchmark.

Required checks:

- 202605 full-month target set parity against bounded DuckDB for all supported columns.
- Peak memory and output write behavior recorded; no `list[dict]` full-month output path.
- Phase timings include load pool, build arrays, emit, write, oracle, parity compare.
- Output row count and `bet_id` uniqueness match target training parquet.
- Cache shard writer remains unchanged until the full-month gate passes.

WP-10 decision:

- **Integrate candidate** only if full-month parity passes and wall time remains at least 3x faster than month-pool bounded DuckDB for the supported column set.
- **Hybrid candidate** if count/sum/lag families are fast but ROW/session/today families require bounded DuckDB fallback.
- **Stop indexed replay** if full-month memory or parity fails in a way that cannot be fixed without reintroducing emit-time scans.

## Acceptance Checklist

- [x] Unchanged snapshot second run → shard cache hit
- [x] `partition_recompute_months` invalidates impacted shards (+ neighbor rule)
- [x] Batch iterator avoids repeated global sort per chunk
- [x] `--force-refresh-short-term-pit` and `--ignore-caches` documented
- [x] `test_short_term_pit_cache.py` green
- [x] Replay v1 synthetic parity tests green
- [x] Replay v1 202605 oracle parity pass for 1k/10k/50k samples
- [x] Replay v1 go/no-go recorded as `stop_or_optimize`
- [x] Indexed replay v2 implemented without emit-time history scan
- [x] Indexed replay v2 regroup benchmark recorded for 16-column production gate subset
- [x] Indexed replay v2 go/no-go decided from parity and speed gates (`continue_prototype`, not integration candidate)
- [x] Emit optimization re-benchmark completed for 10k/50k/100k gate16 path
- [ ] `bet__` pack alias/canonical parity tests added
- [ ] Indexed replay coverage expanded by feature family with per-family oracle reports
- [ ] Full-month 202605 cold-build parity/perf/memory gate recorded (**in progress:** `benchmark_indexed_replay_full_month_gate()` → `out/replay_benchmark_202605_indexed_full_month_gate16_emit_opt/`, log `out/replay_benchmark_202605_indexed_full_month_gate16_emit_opt.log`, ~3.4M targets)
- [ ] Step 3.5 integration decision made from WP-10 result

## Out of Scope (Phase 2)

- Bet-level delta merge into existing shards
- Per-column backfill without shard rewrite
- Parallel shard workers
- Production integration of replay v1
- Direct Step 3.5 integration of indexed v2 before WP-9/WP-10 gates
- Time-bucket / ASOF approximation for short PIT
