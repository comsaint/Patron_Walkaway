# Feast Production Feasibility Spike — Decision Record

**Date:** 2026-05-20  
**Scope:** Experimental train–serve parity probe (Option C: production compute path + Feast online store)  
**Status:** Spike complete; **adopted for scorer v2 integration** (mid/long Feast online lookup)

## Context

After production snapshot / source-mirror issues, we probed whether **ClickHouse → DuckDB feature materialization → Feast online lookup** can meet daily refresh constraints for the **high-ADT allowlist** universe only (`adt_allowed_players_q0p9.parquet`).

Spike runners (probe only):

| Spike | Module | Report artifact |
|-------|--------|-----------------|
| Mid-term (16 `fe__*` columns) | `trainer_hightier.feature_experiment.feast_mid_term_spike` | `trainer_hightier/artifacts/feast/mid_term_spike_report.json` |
| Long-term (3 `patron__*__w180d_m1snap`) | `trainer_hightier.feature_experiment.feast_long_term_spike` | `trainer_hightier/artifacts/feast/long_term_spike_report.json` |

Archived copies used for this record: repo-root `batch2000.json` (mid-term), `long-term.json` (long-term).

**Non-goals of the spike:** wiring Feast into `trainer_hightier.serving.scorer`, replacing deploy snapshot refresh, or validating `wider_sample` / full-population scoring.

## Decision (updated 2026-05-20)

1. **Adopt Feast online lookup for scorer v2 mid/long suppliers** per `Scorer Runtime Contract - SSOT.md` and
   `Scorer v2 Feast Runtime - WORKING_PLAN.md`. Refresh/materialize still uses CH → DuckDB → Feast; scorer runtime
   does not recompute mid/long features.
2. **Short-term `fe__*` Feast online lookup is out of scope for scorer v2 first slice**; production scorer v2 uses
   the bounded PIT builder for the currently deployed model feature set only. Legacy short-term Parquet is not a
   production runtime supplier or fallback.
3. **NULL policy for production scoring:** cell-level NULL (including structural `prior_*`) is allowed with audit;
   Feast entity row missing skips the row and writes prediction-log audit status; batch entity-missing rate **> 10%**
   hard-fails the cycle.
4. **Do not** use frozen training Parquet bundles or `wider_sample` as the production gate; production scope stays
   **ADT allowlist only**.

## Test configuration (common)

- **Universe:** `sample_mode=small_sample` → ADT allowlist (`snapshot_scope=production` for mid-term).
- **ClickHouse:** chunked `player_id IN (...)` (72 chunks × 500 ids).
- **Mid-term anchor:** 1 gaming day (`2026-05-19`); bet lookback 32d (`2026-04-18` … `2026-05-19`).
- **Long-term lookback:** 180d session window (`2025-11-21` … `2026-05-19`).
- **Lookup:** single batched `get_online_features` with `entity_rows` as dict-of-lists (Feast 0.63).

## Mid-term spike results (`batch2000.json`, 2026-05-20)

| Metric | Value |
|--------|-------|
| Verdict | **marginal** |
| CH bet export | 146.8s, 9,938,427 rows |
| DuckDB snapshot compute | 218.5s, 885 snapshot rows |
| Feast spike rows (latest anchor / patron) | 885 |
| Feast materialize | 1.6s |
| Online lookup (885 patrons, 16 features) | 346 ms batch (~0.39 ms/entity) |
| Full-schema lookup OK rows | 755 / 885 (85.3%) |

**Missing features (lookup):**

| Feature group | Missing count / 885 |
|---------------|---------------------|
| `fe__prior_wager_mean_w30d`, `fe__prior_wager_std_w30d`, `fe__prior_odds_mean_w30d`, `fe__prior_odds_std_w30d` | 130 each (~14.7%) |
| `fe__interarrival_avg_w7d`, `fe__interarrival_std_w7d` | 4 each (~0.5%) |
| All other mid-term spike columns | 0 |

**Interpretation:**

- **Latency / daily budget:** CH + compute ≈ **6.1 min** — within the stated **5–8 min** allowlist refresh budget; Feast online latency is not the bottleneck.
- **Coverage:** 885 patrons had a mid-term row for anchor `2026-05-19` vs ~35k allowlist player ids (~2.5% active that gaming day). Remaining allowlist patrons need an explicit scorer policy (no row vs impute vs exclude).
- **Verdict driver:** heuristic marks **marginal** when any feature has lookup nulls; driven by **`prior_*` w30d**, not performance.
- **Bugs fixed during spike (for reruns):** chunked CH `IN` lists; Feast `UnixTimestamp` for `fe__max_pcd_w7d` / `fe__min_pcd_w7d`; `get_online_features` dict-of-lists entity rows.

## Long-term spike results (`long-term.json`, 2026-05-20)

| Metric | Value |
|--------|-------|
| Verdict | **pass** |
| CH session export | 221.2s, 6,609,298 rows (`rows_dropped_on_sanitize=0`) |
| DuckDB slow patron compute | 4.4s |
| Full slow snapshot rows | 52,410 (`canonical_id` × last full month anchor) |
| Feast spike rows (latest anchor / patron) | 23,696 |
| Latest anchor | `2026-05-19` |
| Feast materialize | 5.1s |
| Online lookup (2000 patrons, 3 features) | 287 ms batch (~0.14 ms/entity) |
| Lookup OK rows | 2000 / 2000; **zero** missing per feature |

**Interpretation:**

- **Latency / daily budget:** CH + compute ≈ **3.8 min** (Feast apply ~16s additional on cold schema registration).
- **Compute vs mid-term:** long-term DuckDB path is cheap once sessions are exported; **session CH export dominates** (~90% of wall time).
- **Coverage:** ~23.7k canonical patrons with a 180d slow feature vs ~35k allowlist (~68% session-backed); much wider than single-day mid-term (885).
- **Lookup sample:** batch capped at 2000 of 23,696 Feast rows; all sampled patrons had non-null slow features.
- **CH export fix:** session export uses raw CH columns + pandas sanitize (no `TRY_CAST` in ClickHouse).

## Combined operational picture

| Layer | Mid-term | Long-term | Notes |
|-------|----------|-----------|-------|
| Dominant cost | CH bets + DuckDB | CH sessions | Same allowlist chunking |
| Typical wall time (core) | ~6 min | ~4 min | Sequential sum ~**10 min** if both full-export daily |
| Feast online | OK | OK | Scorer v2 integration in progress |
| Main product risk | `prior_*` nulls + low daily active % | Lower null rate; wider historical coverage |

## Implications for architecture

**Supports:**

- Option C is viable for **allowlist-only** scoring if CH export is shared or incremental and NULL semantics match training.
- Window-driven mid-term materializer (`materialize_mid_term_daily_snapshot`, `snapshot_scope=production`) and canonical slow patron ASOF (`materialize_slow_patron_180d_canonical_asof`) produce Feast-loadable Parquet.

**Does not yet prove:**

- End-to-end deploy refresh under `trainer_hightier.deploy.main` supervisor.
- Bet-grain short-term `fe__*` via Feast (still PIT builder in serving plan).
- Full allowlist online lookup at 23k+ entities in one request (spike sampled 2000 for long-term).

**Rejected / deferred:**

- Frozen dev/training parquet as production scoring source.
- `wider_sample` as production feasibility gate.
- Legacy anchor×range-join mid-term path for production.

## Promotion / integration gates (scorer v2)

1. ~~Align mid-term **`prior_*` NULL** handling~~ — **done:** cell-level NULL allowed + audit; entity row missing
   skip; >10% batch hard fail (SSOT).
2. Document allowlist patrons **without** mid-term row on scoring day vs patrons **without** slow snapshot — enforced
   via entity-missing skip + batch threshold.
3. Shared or incremental CH export for bets + sessions to avoid ~10 min sequential full pulls (refresh plane).
4. Optional: full allowlist Feast lookup benchmark (e.g. batch size 23,696 or multi-batch p95).
5. Scorer v2 working plan tracks mock → real Feast adapter, cursor fix, and dry-run validation.
6. **Future must-do:** post-startup scheduled/daemon Feast refresh (deploy startup refresh is not enough for daily production).
7. Deploy integration must use bundle-local Feast paths and persist latest readiness payload/hash to `feature_state.db`;
   `feast_online_readiness.json` remains the latest gate snapshot.

## Implementation artifacts

- Feast repo: `trainer_hightier/feast_repo/definitions.py` (`mid_term_daily_spike_features`, `long_term_slow_spike_features`)
- Tests: `trainer_hightier/tests/test_feast_mid_term_spike.py`, `test_feast_long_term_spike.py`

## Related documents

| Layer | Document |
|-------|----------|
| SSOT (Feast mid/long adopted) | `Scorer Runtime Contract - SSOT.md` |
| Production snapshot policy | `Mid-Term Feature Snapshot - DECISION_RECORD.md` |
| Scorer v2 execution | `Scorer v2 Feast Runtime - WORKING_PLAN.md` |
| Mid-term incident / cadence | `Training Step 3.5 Mid-Term Snapshot Incident - 20260520.md` |

## Open follow-ups

- Refresh-plane readiness metadata for scorer startup gates (Feast anchor / coverage).
- CH session/bet incremental export for daily refresh under laptop RAM limits.
- Re-run mid-term spike after training-aligned NULL policy is decided; target **pass** or accepted **marginal** with documented business waiver.
