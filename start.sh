#!/usr/bin/env bash
set -euo pipefail
PAPERFLOW_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$PAPERFLOW_ROOT"
if [ ! -x .venv/bin/python ] || [ ! -f .venv/.paperflow-ready ]; then
    python3 scripts/bootstrap.py
    touch .venv/.paperflow-ready
fi
exec .venv/bin/python run.py "$@"
