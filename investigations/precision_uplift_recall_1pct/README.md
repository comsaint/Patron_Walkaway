# Precision Uplift Investigation

This folder hosts the execution artifacts for improving `precision@recall=1%`.

## Goal

- Raise `precision@recall=1%` from ~40% to `>=60%`.
- Keep gains stable across time windows (forward/purged validation).

## Source Plans

- Sprint plan: `.cursor/plans/PLAN_precision_uplift_sprint.md`
- Execution plan: `investigations/precision_uplift_recall_1pct/EXECUTION_PLAN.md`

## Working Structure

- `phase1/` - RCA, label/data contract checks, STATUS history crosscheck
- `phase2/` - high-leverage modeling tracks (A/B/C)
- `phase3/` - feature deepening, targeted slice improvements, ensemble refinement
- `phase4/` - candidate freeze, multi-window playback, go/no-go package

## Required Outputs by Cadence

- Weekly checkpoint with:
  - updated main metric (`precision@recall=1%`)
  - slice ranking changes
  - keep/drop/iterate decisions with evidence
- Phase 1 checkpoint must include `status_history_crosscheck`.

## Decision Rules

- If Phase 1 shows label/data-contract bottleneck as primary, reorder timeline:
  - prioritize data/label fixes first
  - defer model expansion tracks
- No verbal-only conclusions. Every decision must be backed by files under this folder.
