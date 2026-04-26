# NT8 Gateway Runbook

This runbook is the minimum gate before any `micro_live` or `controlled_live` profile can be enabled.

## Required Checks

- Verify the gateway is an independent process, not inside `src/tlm` research workers.
- Verify `health.status` is `ok`, `read_only` is false, and `safe_mode` is false.
- Submit duplicate commands with the same `idempotency_key` and confirm no duplicate fill.
- Validate `cancelOrders`, `flatten`, `flattenBatch`, `closeQty`, and `bracket` in sim.
- Run reconciliation against expected positions; any drift must enter safe mode.
- Confirm `kill switch` blocks new intent submission.
- Confirm broker-side protective stop or OCO protection exists before live entry.

## Incident Rules

- `kill switch` blocks new submits but does not flatten automatically.
- `cancel-all` cancels working orders without changing existing positions.
- `flatten` cancels working orders and reduces position only with manual confirmation or emergency profile.
- `read-only` allows state sync only.
- `reduce-only` allows only risk-reducing actions.

No live profile is valid until the completed evidence is recorded in the readiness gate.
