# Short-Term PIT Cache and Materialize Performance - Working Plan

本文件是 **Working / execution plan** 層，對應 [Implementation Plan](./Short-Term%20PIT%20Cache%20and%20Materialize%20Performance%20-%20IMPLEMENTATION_PLAN.md)。

## Decisions Locked

- Shard key：`payout_complete_dtm` → `YYYYMM`
- Phase 1：month-shard recompute（不做 bet-level delta merge）
- Training batch size 與 scorer cycle 解耦（預設 20_000）
- Full miss 先單工；不啟用 parallel workers
- CLI：`--force-refresh-short-term-pit`；`--ignore-caches` 亦強制 short-term PIT 重算

## Work Breakdown

| ID | Task | Files | DoD |
|----|------|-------|-----|
| WP-1 | Cache manifest + shard layout | `short_term_pit_cache.py`, `config.py` | Schema versioned; helpers for read/write |
| WP-2 | Reuse gate + invalidation | `short_term_pit_cache.py`, `partition_inventory.py` | Hit/miss per shard with reason codes |
| WP-3 | Partial recompute + assemble | `short_term_pit_cache.py`, `trainer.py` | Miss shards only; merge to legacy parquet |
| WP-4 | Batch iterator + training batch size | `materialize_fe_derived.py`, `config.py` | Single sort per materialize; configurable batch |
| WP-5 | CLI + observability | `trainer.py`, `RUNBOOK.md` | Force flag; metrics in run report |
| WP-6 | Tests | `test_short_term_pit_cache.py` | Hit, force refresh, universe change |

## Iteration Status

- **Iteration A (functional cache):** WP-1–WP-3 — **done**
- **Iteration B (performance):** WP-4 — **done**
- **Iteration C (operability):** WP-5–WP-6 — **done**

## Acceptance Checklist

- [x] Unchanged snapshot second run → shard cache hit
- [x] `partition_recompute_months` invalidates impacted shards (+ neighbor rule)
- [x] Batch iterator avoids repeated global sort per chunk
- [x] `--force-refresh-short-term-pit` and `--ignore-caches` documented
- [x] `test_short_term_pit_cache.py` green

## Out of Scope (Phase 2)

- Bet-level delta merge into existing shards
- Per-column backfill without shard rewrite
- Parallel shard workers
