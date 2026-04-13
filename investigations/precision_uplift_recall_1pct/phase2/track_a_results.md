# Phase 2 — track_a results

## Run metadata

- **run_id**: `pytest_phase2_resume_no_bundle`
- **bundle status**: `plan_only`
- **track enabled**: `True`

## Experiments (YAML)

- `a0`

## Trainer CLI evidence (T10A)

> Per-experiment ``trainer_params`` and resolved ``trainer.trainer`` argv fingerprint.

> ``trainer_jobs`` not executed: fingerprints below are **planned** (from ``runner.build_phase2_trainer_argv`` on this bundle), not subprocess audit. Run with ``--phase2-run-trainer-jobs`` to record executed argv.

### `a0`

- **YAML `trainer_params`**: *(none — booleans from `resources` only)*

- **argv_fingerprint (planned)**: `1753ad3c7bd5a779c7f334a7`
- **resolved_trainer_argv (planned)**:

```json
[
  "python",
  "-m",
  "trainer.trainer",
  "--start",
  "2026-01-01T00:00:00+08:00",
  "--end",
  "2026-01-08T00:00:00+08:00",
  "--skip-optuna"
]
```


## Per-job training_metrics harvest

> Harvest uses each job's optional ``training_metrics_repo_relative`` (YAML) when set, else ``{logs_subdir_relative}/training_metrics.json``.

- `a0`: **not found** (file not found: C:\Users\longp\Patron_Walkaway\investigations\precision_uplift_recall_1pct\orchestrator\state\pytest_phase2_resume_no_bundle\logs\phase2\track_a\a0\training_metrics.json)

## Per-job backtest preview

> One ``trainer.backtester`` run per ``job_spec`` with ``training_metrics_repo_relative``; each job uses ``--output-dir`` under ``…/logs/phase2/<track>/<exp_id>/_per_job_backtest/`` so ``backtest_metrics.json`` is not overwritten by the next job (shared backtest still uses ``resources.backtest_metrics_path`` / default).

- *(per-job backtests not run; pass ``--phase2-run-per-job-backtests``)*

## Uplift vs baseline (gate)

> First experiment with a PAT@1% preview in **YAML order** is the track baseline; challengers are later experiments with previews. Values come from ``evaluate_phase2_gate`` (``gate.min_uplift_pp_vs_baseline`` in percentage points).

- *(uplift gate not evaluated — needs ``metrics_ingested`` plus per-job backtests with previews)*

## PAT@1% series & std (gate)

> Optional ``bundle['phase2_pat_series_by_experiment']``; std lines come from ``gate['metrics']`` when the uplift/std path ran (limit = ``gate.max_std_pp_across_windows`` in pp).

### Bundle series (this track)

- *(no `phase2_pat_series_by_experiment` entries for this track)*

### Std gate (from evaluate_phase2_gate)

- *(std gate not evaluated — e.g. `plan_only`, or uplift/std prerequisites missing)*

## Metrics (shared backtest)

> **Note**: Values below come from a **single** `trainer.backtester` run over `common.model_dir`, not per-experiment outputs. Per-track differentiation is T10+.

- **Precision @ recall 1% (shared)**: *(not available in ingested backtest_metrics)*

## Gate snapshot

- **gate status**: `BLOCKED`

### Blocking reasons

- `phase2_bundle_plan_only_no_track_metrics`

### Evidence summary

bundle is plan_only; 3 experiment slot(s) declared but no training metrics ingested
