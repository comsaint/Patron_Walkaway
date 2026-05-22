# Production Snapshot Serving — Runbook

> Historical runbook for the pre-scorer-v2 Parquet snapshot serving route. The current scorer v2 runtime source of
> truth is [`Scorer Runtime Contract - SSOT.md`](Scorer%20Runtime%20Contract%20-%20SSOT.md). Scorer v2 production
> runtime must not use `fe_short_term_parquet`, `fe_derived_parquet`, or snapshot Parquet layers as feature fallback
> suppliers.
>
> Scorer v2 deploy uses **Feast online startup refresh** (`Feast Online Refresh - IMPLEMENTATION_PLAN.md`, bundle
> `README_DEPLOY.md`). **Future must-do:** scheduled/daemon Feast refresh after startup.

Operational procedures for legacy Parquet snapshot bootstrap, refresh, and degraded scoring.

## Bundle layout (production mirror + snapshots)

Under the **deploy bundle root** (the directory passed to `--bundle-dir`):

| Path | Role |
|------|------|
| `snapshots/active_manifest.json` | SSOT pointers to active parquet layers |
| `source_mirror/cleaned_bet/` | Rolling **cleaned bet** parquet partitions — **required** input for mid-term + short-term production refresh |
| `source_mirror/cleaned_session.parquet` | Compact **cleaned session** mirror — **required** input for slow monthly refresh |
| `local_state/feature_state.db` | Manifest publish + job log + `feature_state_meta` (supervisor / mirror status) |

Mirror paths and retention defaults are defined in `trainer_hightier.config` (`production_cleaned_bet_mirror_dir`, `production_cleaned_session_mirror_parquet`, retention day fields). They are **not** controlled via environment variables.

**First deploy / cold start:** the model bundle ships seed parquet under `snapshots/` and a valid `active_manifest.json`. Operators must still **populate `source_mirror/`** (ETL or copy from a trusted bounded window) before refresh can succeed; otherwise startup **hard-failure** repair or background refresh will fail with an actionable `[source_mirror]` error.

## First deploy

1. Train model bundle with cadence-aware Step 3.5 (short-term + mid-term snapshots in `deploy_inputs/`).
2. For the historical Parquet route, build deploy bundle and confirm `snapshots/active_manifest.json` includes:
   - `mid_term_snapshot_parquet`
   - `fe_short_term_parquet` (or legacy `fe_derived_parquet`) only for the old Parquet route; not for scorer v2 production readiness
   - `slow_patron_parquet` with `slow_patron_grain=canonical_asof`
3. **Seed `source_mirror/`** with cleaned bet partitions and `cleaned_session.parquet` meeting schema + coverage (see `trainer_hightier/serving/production_source_mirror.py` and `MANIFEST_INVENTORY.md`).
4. Run deploy (`trainer_hightier.deploy.main`); preflight validates frozen artifacts. In `all` / `scorer` modes, the **refresh supervisor** starts by default and runs **startup hard-failure repair** (missing/invalid/hard-cap) synchronously; **`stale_allowed` does not block startup**.
5. If preflight reports `stale_allowed`, scoring may proceed with degraded warnings once startup hard checks pass.
6. Inspect `feature_state.db` meta / logs:
   - Scorer: `mid_term_freshness_status`, `slow_freshness_status`, `snapshot_scoring_degraded`
   - Supervisor (table `feature_state_meta`): `refresh_supervisor_last_check_iso`, `mid_term_refresh_last_attempt_iso`, `slow_refresh_last_attempt_iso`, `slow_refresh_last_check_day`, `source_mirror_bet_status`, `source_mirror_session_status` (keys defined in `trainer_hightier/serving/contracts.py`)
7. Confirm `prediction_log` rows include snapshot metadata columns.

## Daily mid-term refresh (04:00 after 03:00 gaming-day close)

Background supervisor retries on its poll interval (default 300s) after 04:00 HK when mid-term policy requires refresh.

Manual / external scheduler:

```bash
python -m trainer_hightier.serving.snapshot_updater --refresh-mid-term --production
```

Bootstrap first deploy (anchors `{D-2, D-1}`):

```bash
python -m trainer_hightier.serving.snapshot_updater --refresh-mid-term --production --bootstrap-mid-term
```

On failure: last good manifest is preserved; scorer continues with stale-but-allowed status until hard cap (3 days default).

## Monthly slow refresh

Slow eligibility is evaluated **at most once per calendar day** (HK) in the deploy supervisor loop.

```bash
python -m trainer_hightier.serving.snapshot_updater --refresh-slow --production
```

Slow layer allows 1-day monthly grace; stale-but-allowed up to 3 days (defaults). Monthly anchor semantics: **last full month data relative to today** (not gaming day). For example if today is May 10th, the snapshot must be computed using data of last month's end (Apr 30th) and backward; the snapshot is expected to be scheduled on May 1st so it covers full Apr data. See `slow_patron_180d_monthly` contract YAML.

## Hard-cap breach

When `mid_term_freshness_status` or `slow_freshness_status` is `hard_cap_breached`:

1. Scorer stops with actionable error (no silent zero/median fallback).
2. **MVP:** there is **no** manual manifest override flag — operator must fix mirror coverage / ClickHouse export, run targeted refresh successfully, or ship a new bundle.
3. Do not repoint manifest to training bet-grain artifacts. For scorer v2, do not route around Feast/PIT readiness by enabling legacy Parquet fallback.

## Missing / all-null feature family

Hard failures (immediate stop):

- Missing parquet for required layer
- Wrong grain (`bet_grain` slow patron in production)
- All-null `fe__*` or `patron__*` after join

## Prediction log audit

Filter degraded scoring cycles:

```sql
SELECT scored_at, bet_id, mid_term_freshness_status, slow_freshness_status
FROM prediction_log
WHERE snapshot_scoring_degraded = 1
ORDER BY scored_at DESC
LIMIT 100;
```

## Refresh failure checklist

1. Check `feature_state.db` → `snapshot_job_log` for error detail.
2. Verify `source_mirror/cleaned_bet/` and `source_mirror/cleaned_session.parquet` plus ADT allowlist paths; read `source_mirror_*_status` in `feature_state_meta`.
3. Re-run targeted refresh (`--refresh-mid-term` or `--refresh-slow`) or fix deploy process lock (`.snapshot_refresh_supervisor.lock` under `snapshots/`).
4. Confirm `active_manifest.json` version updated and anchor max moved forward.

## Debug: disable in-process supervisor

```bash
python -m trainer_hightier.deploy.main --bundle-dir /path/to/bundle --no-refresh-supervisor ...
```

Use only when an external job owns all refresh cadence.

## Scorer v2 — Feast online mid bootstrap / daily refresh

For model `20260520-032615-df799bd` and later scorer v2 bundles using Feast mid lookup (not snapshot manifest mid parquet). See also [Mid-Term Feast Train-Serve Parity Incident - 20260522.md](Mid-Term%20Feast%20Train-Serve%20Parity%20Incident%20-%2020260522.md).

**Bootstrap** (first deploy, `--force-feast-refresh`, or mid coverage < 95% of allowlist):

```bash
python -m trainer_hightier.serving.feast_online_refresh \
  --layers mid --source clickhouse --bootstrap-mid --apply-schema \
  --feast-repo /path/to/bundle/feast_repo \
  --adt-allowlist /path/to/bundle/mapping/adt_allowed_players_q0p99.parquet \
  --canonical-mapping /path/to/bundle/mapping/canonical_player_mapping.parquet
```

**Daily incremental** (carry-forward; does not reset Feast online store):

```bash
python -m trainer_hightier.serving.feast_online_refresh \
  --layers mid --source clickhouse --skip-apply \
  --feast-repo /path/to/bundle/feast_repo \
  --adt-allowlist /path/to/bundle/mapping/adt_allowed_players_q0p99.parquet \
  --canonical-mapping /path/to/bundle/mapping/canonical_player_mapping.parquet
```

Post-refresh validation:

```bash
python -m trainer_hightier.serving.audit_production_readiness ...
python -m trainer_hightier.serving.audit_supplier_root_cause ...
```

Readiness gate now requires `mid_cell_null_rate` < 5% on allowlist smoke sample, not only `entity_missing_rate`.
