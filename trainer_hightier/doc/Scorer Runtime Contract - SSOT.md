# Scorer Runtime Contract - SSOT

This document is the current source of truth for `trainer_hightier` scorer packaging and runtime readiness.
Older packaging, self-contained, mid-term snapshot, and registry implementation plans are historical references
unless they agree with this contract.

## Compatibility Policy

Scorer v2 drops unnecessary legacy feature-supplier compatibility. The only compatibility intentionally retained is
the external runtime contract needed by operators and downstream consumers:

- keep the scorer entrypoint and supported CLI shape unless a replacement is explicitly approved.
- keep outward `state.db` alerts / validation schema compatibility.
- do not keep production runtime fallbacks to legacy Parquet suppliers.
- test fixtures may use mocks or fixture data through test-only injection, but legacy Parquet must not enter the
  production scorer control flow.

## Objective

Build and ship a deployable scorer bundle from a development environment that may not have current production
data. Production readiness is checked at deploy/scoring time, where production ClickHouse or production source
mirrors are available.

## Package-Time Contract

`build_deploy_package` produces a **Feast-only production bundle**. Snapshot feature Parquet layers are **not**
packaged; mid/long runtime supply comes from Feast online refresh at deploy time.

The packager must verify that the bundle is structurally usable:

- model files, frozen registry, wheel, mapping, allowlist, and metadata-only manifest are present and readable.
- `requirements.txt` supports production install from PyPI or an internal package index; full third-party wheel
  vendoring is optional backup SOP, not the first-slice completion condition.
- model feature columns can be classified into known suppliers using the frozen registry (Feast / PIT / baseline).
- training-scoped artifacts must not be accepted as production-safe snapshots.
- `snapshots/active_manifest.json` is **metadata-only** (version, coverage, training cutoff, allowlist version audit);
  it must **not** contain `*_parquet` layer path keys.
- missing legacy snapshot layers (`slow_patron_parquet`, `mid_term_snapshot_parquet`, `fe_short_term_parquet`, etc.)
  do **not** block packaging; scorer v2 obtains mid/long from Feast online after deploy startup refresh.
- `fe_short_term_parquet` is not packaged and is not a production scorer v2 supplier.

Bundled directories: `models/`, `mapping/` (including fixed `adt_allowed_players_q0p99.parquet`), `feast_repo/`,
`artifacts/feast/`, `local_state/`, and metadata-only `snapshots/active_manifest.json`.

Package-time output may therefore represent a refresh-required bundle.

## Deploy-Time Contract

Production deploy (`trainer_hightier.deploy.main`) for scorer-capable modes (`all`, `scorer`) owns Feast readiness before scoring starts:

- load the packaged model, frozen registry, canonical mapping, ADT allowlist, and bundle-local Feast repo.
- validate ClickHouse credentials (via bundle `.env` / environment overrides).
- ensure Feast repo / registry / online-store paths resolve bundle-locally and do not retain dev-machine absolute paths.
- **startup Feast online refresh** when readiness is missing, stale, or explicitly forced (`--force-feast-refresh`);
- acquire a bundle-local Feast refresh lock; wait only a short timeout, then fail-fast if another deploy process holds the lock;
- evaluate mid/slow freshness only from config and readiness metadata, not hard-coded deploy thresholds;
- persist the final readiness payload/hash/run id/generated timestamp in `feature_state.db` before atomically publishing
  `feast_online_readiness.json`;
- run deploy Feast readiness gate and allowlist online lookup smoke after refresh;
- **fail-fast** if refresh, readiness DB persistence, JSON publish, or smoke fails — do not start API, validator, or scorer;
- if the model contains short-term `fe__*`, verify the scorer's bounded on-the-fly PIT supplier supports every required short-term column.
- do **not** use legacy Parquet snapshot refresh as the scorer v2 mid/long supplier path;
- stale-but-allowed readiness (within hard cap) may allow degraded scoring with prediction-log audit.

`mode=api` and `mode=validator` alone must not run Feast refresh; they start only after scorer-capable startup succeeds.

**Post-startup refresh (adopted):** `mode=all` / `mode=scorer` start a **Feast refresh supervisor daemon** after startup refresh (default on; `--no-feast-refresh-supervisor` to disable). The supervisor polls `feast_online_readiness.json`, triggers mid/slow `run_feast_online_refresh` on eligibility, uses a non-blocking bundle-local lock, and **fail-soft** on refresh errors (scorer continues with last-good readiness). See [`Feast Post-Startup Refresh Supervisor - IMPLEMENTATION_PLAN.md`](Feast%20Post-Startup%20Refresh%20Supervisor%20-%20IMPLEMENTATION_PLAN.md).

## Scoring-Time Contract

The scorer must never silently fill missing model features:

- every `model.pkl.feature_columns` entry must exist before `predict_proba`.
- required feature families must not be all null after joins.
- wrong-grain or training-scoped mid-term snapshots are hard failures.
- unsupported short-term `fe__*` are hard failures until the on-the-fly PIT supplier implements them.
- prediction logs should expose snapshot freshness/degraded state and missing-feature counts.
- **P1 observability (mid-term Feast)**: each scored batch in ``prediction_log`` records
  ``mid_term_anchor_gaming_day_max``, ``mid_term_snapshot_age_days``, and
  ``mid_null_top_features_json`` (batch-level top null mid ``fe__*`` columns). Combined
  ``feast_online_readiness.json`` mid layer records ``anchor_gaming_day_max``,
  ``expected_anchor_gaming_day``, ``snapshot_age_days``, and ``mid_null_top_features``
  from deploy/refresh smoke ``cell_null_counts``.

### Mid-term train/serve contract (Option A — current production)

This incident decision applies to models trained with **unlimited ASOF** mid-term joins
(e.g. ``20260520-032615-df799bd``) while production scorer v2 supplies mid ``fe__*`` via
Feast online lookup.

| Aspect | Training | Production (scorer v2) |
|--------|----------|------------------------|
| Mid supplier | Step 3.5 / Step 4 ASOF join to latest anchor ``<=`` bet ``gaming_day`` | Feast online ``mid_term_daily_spike_features`` after startup refresh |
| Anchor semantics | Per-row ASOF from full training history | **Carry-forward**: latest materialized anchor per canonical; bootstrap seeds from training mid snapshot + production incremental merge |
| Coverage | All training-eligible canonicals at ASOF | Allowlist canonicals with ``>= 95%`` Feast row coverage after bootstrap |
| Freshness | N/A at scoring time | ``anchor_gaming_day_max`` vs ``expected_anchor_gaming_day`` (calendar or data-bounded when local mirror lags) |
| Null policy | Structural nulls allowed in features | Cell null allowed for ``predict_proba``; entity row missing → skip row; batch entity-missing rate ``> 10%`` → hard fail |
| Observability | Training audit cols on enriched rows | ``prediction_log`` + ``feast_online_readiness.json`` fields above |

**Do not** deploy finite-window-only refresh (e.g. N=7/14 without carry-forward) onto
unlimited-ASOF trained weights without retrain — it changes feature distributions.

**Next model (Option B)**: bounded ASOF with explicit ``N`` in registry; see incident doc P2.

### Scorer v2 Feast missing policy

- **Cell-level NULL** (e.g. structural `prior_*` nulls within an otherwise present entity row): **allowed** for
  `predict_proba`, but prediction log must record per-row missing counts / degraded status.
- **Feast entity row missing** (entire mid-term or long-term family absent from online store for a patron): **skip
  row + write an auditable prediction-log status**; do not score as all-null features.
- **Batch entity-missing rate > 10%**: **hard fail** the scoring cycle (configurable via
  `scorer_feast_entity_missing_fail_fraction`) to surface refresh / key / mapping systemic issues.

## Supplier Rules

- `baseline_model`: supplied from raw ClickHouse/scoring input fields.
- `feast_trial_1h`: supplied online by the serving PIT builder; bundled trial parquet is **not** shipped in
  production bundles and is not a runtime supplier.
- short-term `fe__*`: supplied by the scorer's bounded on-the-fly PIT builder for the currently deployed model
  feature set only. Unsupported short-term columns are hard failures until explicitly implemented. `fe_short_term_parquet`
  is not a production scorer v2 supplier and must not be wired into production runtime fallback.
- mid-term `fe__*`: **scorer v2 adopted supplier** is Feast online lookup (production-scoped materialization +
  refresh plane). Legacy manifest parquet path keys are not packaged and are not production runtime suppliers.
- `patron__*__w180d_m1snap`: **scorer v2 adopted supplier** is Feast online lookup for canonical slow patron
  features. Parquet slow snapshots are not packaged in production bundles and are not runtime substitutes when Feast
  is configured.

Production scorer v2 must **not** fallback to legacy `fe_derived_parquet`, `fe_short_term_parquet`, or training
Parquet for model features, silently or explicitly.

## Non-Goals

- Development packaging does not require latest production data.
- Training snapshots are not production readiness substitutes.
- Packaging is not responsible for rebuilding production snapshots or copying snapshot feature Parquet into bundles.
- Short-term `fe__*` Feast online lookup is not required in scorer v2 first slice; short-term production supply is
  the scorer's bounded on-the-fly PIT builder.

## Related documents

| Layer | Document |
|-------|----------|
| Feast spike decision | `Feast Production Feasibility Spike - DECISION_RECORD.md` |
| Scorer v2 realization | `Scorer v2 Feast Runtime - IMPLEMENTATION_PLAN.md` |
| Feast online refresh realization | `Feast Online Refresh - IMPLEMENTATION_PLAN.md` |
| Execution plan | `Scorer v2 Feast Runtime - WORKING_PLAN.md` |
