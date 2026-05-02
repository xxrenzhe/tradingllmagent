#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SESSION_NAME="${TLM_IBKR_TMUX_SESSION:-ibkr_soak_loop}"
PYTHON_BIN="${TLM_PYTHON_BIN:-/usr/local/bin/python3}"

if tmux has-session -t "${SESSION_NAME}" 2>/dev/null; then
  tmux kill-session -t "${SESSION_NAME}"
fi

tmux new-session -d -s "${SESSION_NAME}" -c "${ROOT_DIR}" \
  "env PYTHONPATH=src arch -arm64 ${PYTHON_BIN} -m tlm.cli ibkr loop \
    --symbol MNQ \
    --api-port 8000 \
    --client-id 11 \
    --strategy-family expanded_high_edge \
    --expanded-high-edge-preset expanded_high_edge_cap24_balanced_risk \
    --poll-interval-seconds 15 \
    --review-interval-seconds 300 \
    --max-stale-seconds 30 \
    --max-spread-ticks 4 \
    --signal-regime-cooldown-seconds 300 \
    --take-profit-ticks-multiplier 1.25 \
    --auto-submit"

echo "started ${SESSION_NAME} with expanded_high_edge_cap24_balanced_risk"
