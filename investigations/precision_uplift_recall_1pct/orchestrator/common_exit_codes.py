"""Shared integer CLI exit codes for precision-uplift ``run_pipeline.py``.

Used by ``--phase phase1``, ``--phase phase2``, and ``--phase all`` where the
historic orchestrator contract uses the same integers (**2** config, **3**
preflight, **6** dry-run **NOT_READY**). Phase 1 T8A uses **11**
(``EXIT_PHASE1_AUTONOMOUS_PENDING``) and **12** (``EXIT_PHASE1_AUTONOMOUS_MID_NOT_ELIGIBLE``).

**Phase 1-only** named exits (**4** / **5**) are defined here so call sites read
``EXIT_PHASE1_*`` instead of bare literals. **Important:** integer **4** is also
``phase2_exit_codes.EXIT_RESUME_BUNDLE_LOAD_FAILED`` and **5** is also
``phase2_exit_codes.EXIT_PHASE2_RUNNER_SMOKE_FAILED`` — same numbers,
**different** failing steps; triage must use ``run_state.steps`` / stderr context.
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_CONFIG_INVALID = 2
EXIT_PREFLIGHT_FAILED = 3
# Phase 1 ``_main_phase1``: r1_r6_mid_snapshot or r1_r6_analysis step failed.
EXIT_PHASE1_MID_OR_R1_FAILED = 4
# Phase 1 ``_main_phase1``: backtest step failed.
EXIT_PHASE1_BACKTEST_FAILED = 5
EXIT_DRY_RUN_NOT_READY = 6
# Phase 1 ``_main_phase1``: ``--mode autonomous`` without ``--dry-run`` (T8A supervisor
# long-run not implemented yet; run_state may include ``phase1_autonomous`` stub).
EXIT_PHASE1_AUTONOMOUS_PENDING = 11
# Phase 1 ``--autonomous-mid-r1-once``: ``observe_context.mid_snapshot_eligible`` is False.
EXIT_PHASE1_AUTONOMOUS_MID_NOT_ELIGIBLE = 12
