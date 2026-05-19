# Mid-Term Feature Snapshot — Decision Record

**Date:** 2026-05-19  
**Updated:** 2026-05-21  
**Scope:** Training cadence correction and production snapshot serving lifecycle

## Decision

Adopt **prior-gaming-day daily snapshot** semantics for active mid-term `fe__*` model features in the main trainer path (Step 3.5).

| Feature | Supplier |
|---------|----------|
| `fe__bets_cnt__w1d` | `materialize_mid_term_daily_snapshot` + ASOF enrich |
| `fe__wager_sum__w15m_over_w1d` | short PIT numerator / prior-day snapshot denominator |
| `fe__wager_cv_w7d` | prior-day snapshot |
| `fe__payout_odds_z_prior_w30d` | current bet odds + prior-day snapshot mean/std |
| `fe__interarrival__last_gap_z__w7d` | short PIT interarrival + prior-day snapshot stats |

Short-term `fe__*` remain on bet-grain PIT builder (`materialize_fe_derived_short_term_parquet`).

Production serving uses **shipped snapshots** from the model bundle as the **first-deploy seed**, then maintains layers through **deploy-managed refresh** reading a **compact production source mirror** (not the bundle parquet as long-term truth). The scorer reads the active manifest and joins snapshots; it does not rebuild snapshots itself.

Production snapshot decisions:

- **Initial source:** ship mid-term and long-term snapshots with the model bundle for first deploy / manifest seeding.
- **Continuous maintenance:** `trainer_hightier.deploy.main` starts a refresh supervisor by default (scorer-capable modes); `snapshot_updater` jobs update snapshots and atomically publish `active_manifest.json`. Refresh validates `source_mirror/cleaned_bet/` and `source_mirror/cleaned_session.parquet` (paths from `trainer_hightier.config`).
- **Production universe:** only the high-ADT allowlist canonical universe is materialized.
- **Mid-term source lookback:** computing a latest prior-day snapshot reads the required **32d** (default) cleaned-bet mirror window plus configured retention buffer; output anchors remain `D - 1` for normal scoring.
- **Mid-term bootstrap anchors:** support today live scoring plus small replay; default bootstrap should include latest prior-day and a small replay buffer, not arbitrary historical scoring.
- **Daily refresh timing:** gaming day closes at 03:00; mid-term **background** refresh runs only from **04:00** HK onward when stale/missing policy requires it; poll retries (e.g. every 300s) until success.
- **Deploy startup semantics:** **hard failures** (missing/invalid artifact, staleness past hard cap) trigger **synchronous** targeted refresh before scorer; **failure fails deploy**. **`stale_allowed`** does **not** block startup; background supervisor retries immediately.
- **Mid-term stale behavior:** if refresh is late, continue scoring with the latest available snapshot, but mark the system degraded and warn continuously.
- **Mid-term hard cap:** if mid-term staleness exceeds the configured hard cap, scoring stops until refresh succeeds (**MVP: no manual override**).
- **Long-term monthly anchor:** per patron per calendar month, `anchor_gaming_day = MAX(gaming_day)` in that month (month-end); ASOF uses latest `anchor_gaming_day <=` bet `gaming_day`. Training and production both aggregate from **cleaned session** input when using the monthly materializer path.
- **Long-term monthly grace:** monthly slow snapshot allows 1 day grace.
- **Long-term stale behavior:** if expired, continue scoring with the latest slow snapshot while warning continuously, up to a 3 day hard cap; beyond hard cap, same as mid-term (**MVP: no manual override**).
- **Slow refresh cadence:** supervisor evaluates slow refresh need **at most once per calendar day** (HK), not every mid-term poll.
- **Warning surfaces:** stale snapshot warnings must appear in logs, health/status, and prediction log metadata for all impacted predictions.
- **Refresh ownership:** deploy supervisor + optional CLI `snapshot_updater`; scorer never performs synchronous rebuild.
- **Manifest migration:** add per-layer manifest fields while preserving compatibility with existing `active_manifest.json` keys during migration.

## Rationale

The legacy `materialize_fe_derived_parquet` path computed w1d/w7d/w30d using **per-bet `payout_complete_dtm` rolling windows**, which is fresher than the intended **gaming_day daily snapshot** rule and inconsistent with production mid-term supply design.

Corrected semantics are expected to **change offline feature distributions and model metrics**. Performance shifts must not be interpreted as regressions without comparing against the old unintended freshness.

Production stale snapshots are treated differently from all-null features. A stale-but-present snapshot can still provide non-null, interpretable signals for a short bounded period; an all-null feature family is a supply failure. Therefore the selected production behavior is degraded scoring with explicit warnings until a 3 day hard cap, rather than immediate interruption at the first missed refresh.

## Implementation artifacts

- Registry cadence fields: `trainer_hightier/contracts/feature_candidate_registry.yaml`
- Cadence audit/gates: `trainer_hightier/feature_experiment/feature_cadence.py`
- Mid-term snapshot: `trainer_hightier/feature_experiment/materialize_mid_term_daily_snapshot.py`
- ASOF enrich: `trainer_hightier/feature_experiment/dataset_enrich.py`
- Trainer wiring: `trainer_hightier/trainer.py` Step 3.5
- Audit output: `{training_parquet_dir}/feature_cadence_audit.json`
- Production manifest: per-layer snapshot paths, anchors, freshness metadata, grain metadata, and compatibility aliases for existing manifest keys.
- Production mirror validation: `trainer_hightier/serving/production_source_mirror.py`; supervisor meta keys: `Production Snapshot Serving - MANIFEST_INVENTORY.md`.

## Promotion gate

- Do **not** promote models trained with legacy bet-grain mid-term rolling features from full `materialize_fe_derived_parquet` enrich.
- CI guardrails: `test_feature_cadence.py`, `test_mid_term_snapshot_enrich.py`, `test_mid_term_cadence_guardrails.py`
- Re-run FQG + full train/val/test rebuild under corrected semantics before bundle promotion.
- Production deploy: preflight validates bundle; **hard-failure** snapshot states are repaired synchronously at supervisor startup (or deploy fails).
- If refresh fails but a snapshot is within the configured stale hard cap, deployment/scoring may continue in degraded mode with visible warnings.
- All-null `fe__*` or all-null `patron__*__w180d_m1snap` remains a hard failure.

## Open follow-ups

- Extend health/status API beyond logs / `feature_state_meta` / prediction log for stale snapshot states (if product requires a single dashboard).
- Optional: external orchestrator as alternative to in-process deploy supervisor for multi-instance (would need distributed lock first).
