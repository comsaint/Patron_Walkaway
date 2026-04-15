"""Integer process exit codes for ``run_pipeline.py --phase phase2``.

These values are the stable CLI contract for operators and log triage, alongside
string ``run_state["steps"][*]["error_code"]`` (e.g. ``E_PHASE2_BACKTEST_JOBS``).

``run_pipeline._main_phase2`` returns **shared** codes from ``common_exit_codes``
for invalid config, preflight failure, and dry-run **NOT_READY**; plus Phase 2
specific codes for resume bundle load (**4**), runner smoke (**5**), trainer jobs
(**7**), backtest / artifact paths (**8**), and optional gate policy exits
(**9** / **10**). Integers **2** / **3** / **6** match ``--phase phase1`` and
``--phase all`` (see ``common_exit_codes``).

Gate-driven exits **9** / **10** apply only when ``--phase2-fail-on-gate-fail`` /
``--phase2-fail-on-gate-blocked`` are set; see ``run_pipeline.phase2_gate_cli_exit_code``.
"""

from __future__ import annotations

import common_exit_codes as _common_exit_codes

# Re-export shared CLI integers for `from phase2_exit_codes import ...` callers.
EXIT_CONFIG_INVALID = _common_exit_codes.EXIT_CONFIG_INVALID
EXIT_PREFLIGHT_FAILED = _common_exit_codes.EXIT_PREFLIGHT_FAILED
EXIT_DRY_RUN_NOT_READY = _common_exit_codes.EXIT_DRY_RUN_NOT_READY

EXIT_RESUME_BUNDLE_LOAD_FAILED = 4
EXIT_PHASE2_RUNNER_SMOKE_FAILED = 5
EXIT_PHASE2_TRAINER_JOBS_FAILED = 7
EXIT_PHASE2_BACKTEST_OR_ARTIFACT_FAILURE = 8
EXIT_PHASE2_GATE_FAIL = 9
EXIT_PHASE2_GATE_BLOCKED = 10

# When Phase 2 exits with the given code solely because this step failed (typical paths).
PHASE2_FAILURE_STEP_CLI_EXITS: dict[str, int] = {
    "phase2_runner_smoke": EXIT_PHASE2_RUNNER_SMOKE_FAILED,
    "phase2_trainer_jobs": EXIT_PHASE2_TRAINER_JOBS_FAILED,
    "phase2_per_job_backtest_jobs": EXIT_PHASE2_BACKTEST_OR_ARTIFACT_FAILURE,
    "phase2_backtest_jobs": EXIT_PHASE2_BACKTEST_OR_ARTIFACT_FAILURE,
}
