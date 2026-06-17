# Feature Train-Serve Parity - IMPLEMENTATION_PLAN

This document is the implementation-plan layer for permanently fixing model-feature parity between training and
serving. Slow-varying features are the first confirmed failure, but the mandatory gate must validate **every**
`model.pkl.feature_columns` entry so raw, trial, short-term PIT, Feast mid/slow, and composite features cannot drift
silently.

## Objective

Make every model feature deterministic and identical between training and serving for a given model run.

The governing contract is:

- Step 06 compares production-replayed serving features against the exact training split feature columns for every
  model feature.
- Any non-zero feature-value mismatch is a failure unless explicitly documented as an approved migration exception.
- Feature-specific contracts still apply. Slow features are computed from the **last full calendar month relative to the
  run/serving date**, not from each bet's own `gaming_day` via per-bet ASOF.
- Example: for May scoring, the **target** slow anchor is Apr 30 (data through the completed April calendar month). The
  monthly Feast job is scheduled on the **first `gaming_day` of May** (gap day) to build that snapshot.
- **Month-turn gap day (gaming_day semantics):** the **first `gaming_day` epoch of each calendar month** is reserved for
  Feast to compute the new monthly snapshot. On that gap day, **training and serving both** continue to use the
  **already-published prior-month snapshot** (the previous target anchor, e.g. Mar 31 while Apr 30 is being built).
- From the **second distinct `gaming_day` epoch in the same calendar month** onward, training and serving must use the
  new target anchor (e.g. Apr 30). If the target snapshot is not available by then, the **entire scorer stops** (no
  per-patron mixed anchors, no silent degrade).
- All month-turn boundaries in this plan are keyed by **`gaming_day` calendar month**, not wall-clock calendar day alone.
- A trained model bundle must persist both `slow_anchor_target` and `slow_anchor_effective` (they differ only on gap day).
- Production must fail fast if the online/serving slow values are from the wrong effective anchor, a bet-grain artifact, a
  partial current month, or an unknown training-scoped artifact.

## Current Failure Mode

The current pipeline has allowed several incompatible slow-feature shapes:

- Training slow values can be materialized at bet grain or joined with per-bet ASOF semantics.
- Feast online serving can collapse a multi-anchor artifact into one latest row per canonical without enforcing the
  last-full-month cutoff.
- A model bundle can include `deploy_inputs/slow_patron_180d_monthly.parquet` that lacks `canonical_id` and
  `anchor_gaming_day`, making it impossible to prove the serving snapshot contract.
- Offline backtests can compare production-like values to training values after the model is trained, but this has not
  been a mandatory post-training gate.

These are all variants of the same parity bug: the model feature column name is stable, but the producer/serving
contract behind the value is not stable.

## Target Architecture

### Training Producer

The training pipeline should make each model feature's supplier contract explicit:

- raw/baseline columns: direct source column and type contract;
- trial/short-term columns: event-time PIT contract and bounded pool requirements;
- mid-term and composite columns: upstream snapshot / PIT dependencies;
- slow columns: active last-full-month anchor contract.

For slow features, training uses the same month-turn rules as serving:

- `slow_anchor_target = previous_calendar_month_end(training_run_date)` (e.g. Apr 30 when the run date is in May).
- `slow_anchor_effective = slow_anchor_target` except on the **gap day** (first `gaming_day` of the run's calendar month),
  when `slow_anchor_effective = fallback_slow_calendar_anchor(slow_anchor_target)` (prior published month-end, e.g. Mar 31).
- The slow materializer must filter source sessions to `gaming_day <= slow_anchor_effective` for feature attachment.
- The training set should attach slow features at canonical grain, not by `bet_id`.
- The model output directory should persist:
  - `slow_anchor_target`
  - `slow_anchor_effective`
  - `slow_source_cutoff_date = slow_anchor_effective`
  - `slow_month_turn_phase = gap | post_gap` (derived from run date / sample `gaming_day`)
  - `slow_snapshot_scope = training_model_contract`
  - `slow_snapshot_grain = canonical_active_month`

### Serving / Feast Refresh

The production refresh plane aligns with training on target vs effective anchors:

- `slow_anchor_target` is derived from the current serving context (same formula as training), not from each bet's
  `gaming_day`.
- On the **gap day** (first `gaming_day` of the calendar month), Feast refresh runs to materialize the new monthly
  snapshot for `slow_anchor_target`. Scoring/training reads **`slow_anchor_effective`** (prior published snapshot) until
  that job completes.
- From the **second `gaming_day` epoch in the month** onward, Feast online and bundle artifacts must expose
  `slow_anchor_target` for all required patrons. **Partial coverage is not allowed** (no Mar 31 fallback for missing Apr 30
  rows).
- Reject current-month partial anchors as the target snapshot.
- Reject artifacts missing `canonical_id` or `anchor_gaming_day`.
- If Feast online is used, materialize/upsert the target anchor canonical rows for scorer v2 after gap day; gap-day reads
  may still resolve the prior effective anchor from already-published online rows.
- Readiness metadata must include `slow_anchor_target`, `slow_anchor_effective`, `slow_month_turn_phase`, row count,
  source scope, feature list, and null summary.

### Scoring

The scorer must expose the production supplier values used for all model columns:

- raw/trial/PIT/composite values must be reproducible on the official test split through offline serving replay;
- Feast mid/slow values must include lookup diagnostics and readiness metadata;
- unsupported model features must fail before `predict_proba`;
- No per-bet slow ASOF during live scoring.
- No fallback to bet-grain training slow parquet.
- No silent fill if Feast slow is missing.
- On gap day only, prior-month `slow_anchor_effective` is allowed by contract; after the second `gaming_day` epoch of the
  month, missing `slow_anchor_target` is a **hard stop for the entire scorer** (bootstrap/readiness gate fails; no
  degraded scoring).
- `slow_month_turn_phase`, `slow_anchor_target`, and `slow_anchor_effective` must be logged each scoring cycle.
- Prediction logs should include both anchors (or readiness metadata that identifies them).

## Workstreams

### Workstream A: Contract Hardening

Update feature contracts so each feature family has one owner for time, grain, and supplier semantics:

- `feature_candidate_registry.snapshot.yaml` must classify every `model.pkl.feature_columns` entry to one runtime
  supplier.
- short-term PIT features must specify pool lookback, entity fanout, and tie-break rules.
- mid-term / composite features must specify snapshot dependency and null policy.
- `slow_patron_180d_monthly_features.yaml` should define last-full-month anchor semantics.
- `Scorer Runtime Contract - SSOT.md` should reference the active slow anchor required by scorer v2 readiness.
- Historical documents should point to the SSOT instead of restating older `MAX(gaming_day)` semantics.

Deliverable: contract text and schema fields for `slow_anchor_target`, `slow_anchor_effective`,
`slow_month_turn_phase`, `slow_snapshot_grain`, and `slow_snapshot_scope`.

### Workstream B: Materialization Alignment

Refactor slow materialization so training and production call the same low-level function with explicit inputs:

- `as_of_date` or scoring `gaming_day` (to derive gap vs post-gap)
- `slow_anchor_target` and `slow_anchor_effective`
- source session parquet / ClickHouse export
- canonical mapping
- output grain (`canonical_active_month`)

The materializer must reject broad historical output when invoked for model training or production serving. Backfill or
diagnostic modes may produce multiple anchors, but those outputs must be marked as non-serving artifacts.

Deliverable: one canonical slow artifact shape:

```text
canonical_id
anchor_gaming_day
patron__theo_win_sum__w180d_m1snap
patron__gaming_days_cnt__w180d_m1snap
patron__adt__w180d_m1snap
```

### Workstream C: Bundle And Readiness Gates

Build/deploy gates should block unsafe feature suppliers before scoring starts:

- model feature columns must have exactly one runtime supplier route;
- production replay must be able to compute or fetch every feature column;
- all-null feature families are hard failures;
- per-feature null-rate and value-diff summaries must be emitted;
- Static gate: slow artifact must contain `canonical_id`, `anchor_gaming_day`, and all required `patron__*` columns.
- Anchor gate: production artifacts for deploy must expose `slow_anchor_target` on post-gap days; gap-day bundles may
  legally carry the prior effective anchor only when `slow_month_turn_phase=gap` is recorded in manifest metadata.
- Scope gate: `snapshot_scope` must be production/model-contract safe, not training bet-grain or debug.
- Feast readiness gate: online lookup smoke must verify slow cell null rate and active anchor metadata, not just entity
  presence.

Deliverable: pack/deploy errors that name the exact violating model directory, artifact, anchor, and missing columns.

### Workstream D: Mandatory Step 06 Verification

After Step 5 trains a model, Step 06 must validate the trained model directory before it is considered deployable.

The Step 06 script is:

```bash
python trainer_hightier/06_verify_training_serving_parity.py \
  --model-dir out/models_high_tier_mvp/20260520-032615-df799bd \
  --model-dir out/models_high_tier_mvp/20260522-123028-245bd1f \
  --model-dir out/models_high_tier_mvp/20260522-124003-245bd1f \
  --as-of-date 2026-05-22 \
  --output-json out/feature_parity_verification.json
```

Use `--as-of-date` on a **post-gap** day (second or later `gaming_day` epoch in the month) for deploy gates so
`slow_anchor_target` is the expected last-full-month anchor. Use `--month-turn-phase gap` or `--month-turn-phase auto`
with a gap-day `as_of` when testing month-turn fixtures (`auto` infers from test-split `gaming_day` epochs in the
`--as-of-date` month; unknown month coverage defaults to `post_gap`). Report schema is `feature_parity_verification_v2`.

Step 06 checks:

- every `model.pkl.feature_columns` value in the training test split versus production supplier replay;
- per-feature diff count, diff fraction, train null rate, and serve null rate;
- Feast entity missing, Feast cell null counts, and post-join smoke failures;
- whether the model uses slow columns;
- whether the bundle slow artifact is production-safe canonical anchor grain;
- whether the slow artifact anchor matches `slow_anchor_effective` implied by `--as-of-date` and month-turn phase (post-gap:
  must equal `slow_anchor_target`; gap-day runs must document effective vs target in report metadata);
- whether sampled training/test rows show slow values varying within the same `canonical_id`, which indicates old
  per-bet or per-month ASOF behavior.

The report is intentionally JSON-first so CI, local training, and release scripts can consume it without parsing logs.

### Workstream E: Offline Backtest Extension

Reuse `trainer_hightier.serving.offline_serving_backtest` for heavier end-to-end validation:

- Run a small bounded production replay on each newly trained model.
- Compare all model feature columns from training split vs serving values on the same rows.
- Report per-column parity, slow anchor metadata, entity missing rate, score deltas, and alert flip rate.
- Fail if any required model feature has non-zero train/serve mismatch outside explicit migration exceptions.

This is heavier than Step 06 and should be a release gate or nightly check. Step 06 remains the cheap default gate after
every training run.

## Rollout Phases

### Phase 1: Detect And Block

- Land Step 06 script and run it on known model directories.
- Treat current bet-grain slow artifacts as failing evidence, not as acceptable compatibility.
- Add Step 06 output to model artifacts as `feature_parity_verification.json`.

### Phase 2: Fix Producer Semantics

- Update `slow_patron_180d_monthly.py` and training wiring to produce canonical active-month artifacts.
- Freeze the active slow anchor into `training_metrics.json`, `run_report.json`, and `deploy_inputs/active_manifest.json`.
- Update the contract YAML and tests.

### Phase 3: Fix Serving Semantics

- Update Feast refresh to enforce the same expected active anchor.
- Publish readiness metadata only after anchor, schema, scope, coverage, and null checks pass.
- Remove any fallback to bet-grain slow parquet from scorer v2 production paths.

### Phase 4: End-to-End Regression

- Run Step 06 on all existing model directories used for comparison.
- Run `offline_serving_backtest` on at least one known-good model after the producer/serving fixes.
- Keep a small fixture that would fail under current-month partial-anchor leakage.

## Acceptance Criteria

- A model cannot pass Step 06 when any `model.pkl.feature_columns` value differs between training split and serving
  replay.
- A model using `patron__*__w180d_m1snap` cannot pass Step 06 if its bundled slow artifact is bet-grain.
- Post-gap deploy checks (`--as-of-date` on second+ `gaming_day` epoch of the month): slow artifact distinct anchors must
  equal `slow_anchor_target` (last full calendar month relative to `--as-of-date`); missing target coverage fails.
- Gap-day checks: artifact may equal `slow_anchor_effective` (prior published anchor) only when manifest/report records
  `slow_month_turn_phase=gap`; training and serving must agree on the same effective anchor.
- Training and serving metadata expose the same `slow_anchor_target` and `slow_anchor_effective` for the run context.
- Production readiness simulating post-gap: if `slow_anchor_target` is unavailable, the scorer gate fails closed for the
  **entire** scoring process (not per-patron partial serve).
- Feast readiness cannot pass when entity rows exist but slow feature cells are null or from the wrong anchor.
- The validation can run on a laptop without loading all historical bet/session data into memory.

## Resource Guardrails

- Step 06 reads Parquet schemas and selected columns from a bounded row sample, then replays production suppliers in
  bounded batches.
- Default training/test sample is capped at 200k rows.
- Heavy source-session recomputation belongs in materialization or offline backtest phases, not the default post-training
  gate.

## Performance Note (Production)

- For slow monthly features, computing the **single required active snapshot per canonical patron** should not be the
  primary bottleneck.
- In prior measurements, the dominant cost was source session export / I/O, while DuckDB aggregation and Feast
  materialization were comparatively small.
- Implementation priority should therefore be:
  1. filter early by expected anchor window and scoring universe;
  2. avoid exporting irrelevant sessions;
  3. aggregate once per canonical for the active anchor only;
  4. schedule heavy monthly materialization on the gap `gaming_day`; keep scoring reads on the prior effective anchor until
     post-gap readiness passes.

## Month-Turn Handling (Apr -> May Example)

Month boundaries use **`gaming_day` calendar month**, not wall-clock day alone.

Definitions for May:

- `slow_anchor_target = 2026-04-30` (last full April calendar month).
- `slow_anchor_effective` on the **gap day** = `2026-03-31` (last published snapshot before April's target is ready).
- **Gap day** = the first distinct `gaming_day` date that falls in May (Feast computes Apr 30 snapshot that day).
- **Post-gap** = the second distinct `gaming_day` date in May and all later May `gaming_day` epochs.

Timeline:

| Phase | `gaming_day` (May) | Training / serving read | Feast / materialization |
|-------|--------------------|-------------------------|-------------------------|
| Gap | 1st May epoch | `slow_anchor_effective` (Mar 31) | Build/publish Apr 30 target snapshot |
| Post-gap | 2nd May epoch onward | `slow_anchor_target` (Apr 30) | Must be online; else **entire scorer stops** |

Operational sequence:

1. Detect calendar-month position from the current bet/scoring `gaming_day` (gap vs post-gap).
2. On gap day: run monthly refresh job; attach/serve **effective** anchor only; record `slow_month_turn_phase=gap`.
3. On post-gap day: readiness gate requires full `slow_anchor_target` coverage; if missing, **fail closed for all scoring**
   (no mixed anchors, no degraded mode).
4. Training runs use the same phase detection from run date / training sample `gaming_day` so labels match serving.

Assumption: one gap `gaming_day` is sufficient to finish the monthly snapshot before the second May `gaming_day` epoch
appears in live traffic. If that assumption is violated in operations, post-gap readiness failure is the intentional
signal to fix the Feast pipeline SLA—not to extend fallback.

## Decision Log (Month-Turn)

| Topic | Decision |
|-------|----------|
| Gap day definition | First **`gaming_day` epoch of each calendar month** (not wall-clock day 1 alone). |
| Gap day reads | Training **and** serving use the same **`slow_anchor_effective`** (prior published snapshot). |
| Post-gap requirement | From the **second** `gaming_day` epoch in the month, **`slow_anchor_target` is mandatory**. |
| Missing target post-gap | **Entire scorer stops** (readiness/bootstrap hard fail). |
| Feast `computing` / gaming_day lag fallback | **Removed** — replaced by gap-day / post-gap phases above. |

## Open Questions

- Should Step 06 become a hard failure inside `trainer.py` immediately after Step 5, or first run as a warning-only
  report for one migration cycle?
- Should backfill artifacts with multiple monthly anchors remain supported for diagnostics under a separate
  `snapshot_scope`, or should they be moved out of `deploy_inputs` entirely?
- How to detect "first/second `gaming_day` epoch of month" in code: property timezone + `gaming_day` column only, or also
  align with `serving_gaming_day(close_hour)` for batch scheduling?
