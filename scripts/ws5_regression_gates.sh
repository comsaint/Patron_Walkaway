#!/usr/bin/env bash
# WS5 (MVP Replace Trainer v2): regression gates — do not edit .cursor/plans/* from here.
#
# Usage (from repo root):
#   bash scripts/ws5_regression_gates.sh fast
#   bash scripts/ws5_regression_gates.sh fast --tb=short
#   bash scripts/ws5_regression_gates.sh optional   # slow / heavy; see comments below
#
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
MODE="${1:-fast}"
shift || true

case "$MODE" in
  fast)
    exec python -m pytest \
      tests/unit/test_workstream_a_bridge_manifest.py \
      tests/unit/test_cross_entry_preflight.py \
      tests/integration/test_trainer.py::TestRefactorGuardrailsInputSources \
      -q "$@"
    ;;
  optional)
    # Release / full-confidence (not daily laptop gate). Run manually when needed:
    #   python -m pytest tests/integration/test_run_mvp_e2e_regression.py -m slow -q
    #   python -m pytest tests/unit/ -q
    cat <<'EOF'
WS5 optional gate (manual):

  python -m pytest tests/integration/test_run_mvp_e2e_regression.py -m slow -q
  python -m pytest tests/unit/ -q

Prereqs: duckdb, clickhouse-connect (where applicable), synthetic MVP inputs per test docstrings.
EOF
    exit 0
    ;;
  *)
    echo "usage: $0 {fast|optional} [extra pytest args]" >&2
    exit 2
    ;;
esac
