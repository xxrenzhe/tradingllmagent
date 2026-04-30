# IBKR Paper Runbook

This runbook is the minimum operating procedure for the Mac-local IBKR Paper loop. It does not authorize live trading.

## Scope

- Runtime source of truth: IBKR Paper market data, order callbacks, fills, positions, and account PnL.
- Trading scope: `MNQ`, one paper contract, bracket-protected entries only.
- Signal cadence: local `1m` signal engine.
- LLM cadence: structured `5m` review, using recent bars, signals, real paper fills, commissions, positions, and PnL.
- Automation boundary: fast path may only reduce risk; any risk increase or strategy logic mutation must go through offline validation.

## Mac Prerequisites

- Install IBKR TWS or IB Gateway and log into Paper Trading.
- Enable TWS API socket on localhost and keep the API port fixed, default `7497` for paper.
- Use a dedicated client id, default `11`, and avoid sharing it with manual tools.
- Confirm the account is paper-only before arming the loop.
- Confirm CME real-time market data permission for `MNQ`; delayed or frozen quotes are observation-only.
- If readiness shows IBKR error `10168`, the account lacks subscribed real-time data and delayed data is not enabled in TWS.
- Keep IBKR username, password, account number, and session details outside the repository.

## Startup Checks

1. Start TWS or IB Gateway in Paper Trading mode.
2. Start FastAPI locally.
3. Verify health:
   ```bash
   curl -s http://127.0.0.1:8000/api/gateways/ibkr/health
   ```
4. Connect through the adapter layer only after paper account verification is available.
5. Record or verify contract details for `MNQ`; tick size must be `0.25`, point value must be `2.0`.
6. Record or verify real-time `bid`, `ask`, and `last`; spread must be non-negative and quote age must be within readiness limits.
7. Verify readiness:
   ```bash
   curl -s "http://127.0.0.1:8000/api/gateways/ibkr/readiness?symbol=MNQ&max_stale_seconds=5"
   tlm ibkr readiness --symbol MNQ
   ```
8. In WebUI, open `Execution`, click `Refresh IBKR Paper`, and confirm status is `ready` before paper order testing.

Recommended adapter sync sequence after connect:

- `POST /api/gateways/ibkr/contracts/sync`
- `POST /api/gateways/ibkr/market-data/sync`
- `POST /api/gateways/ibkr/positions/sync`
- `POST /api/gateways/ibkr/account-snapshots/sync`
- `POST /api/gateways/ibkr/runtime-events/sync`

`contracts/sync` now resolves the active front-month `MNQ` contract and feeds its contract month or `localSymbol` into later market-data and bracket-order submits. Manual override is only needed when IBKR returns ambiguous contract details.

When the API server is running, the in-process poller performs the same sync loop automatically. Inspect it with `GET /api/gateways/ibkr/poller`.

## Normal Loop

- Build `1m` bars from IBKR snapshots.
- Generate deterministic signal candidates locally.
- Every `5m`, build a structured review request with recent bars, signals, execution ledger, and risk context.
- Only allow a paper order when review action is `paper_allow` and gateway readiness is `ready`.
- Submit only deterministic bracket order drafts with entry, stop-loss, take-profit, and max holding minutes.
- Use `POST /api/gateways/ibkr/bracket-orders/{bracket_id}/submit` only after the local draft is approved and `contracts/sync` has resolved the active `MNQ` contract month or `localSymbol`.
- After submission, poll `POST /api/gateways/ibkr/runtime-events/sync` to ingest `orderStatus`, `execDetails`, and `commissionReport` into the local execution ledger.
- Record order status, fills, commission, positions, and account snapshots from IBKR callbacks.
- Use `/api/gateways/ibkr/execution-ledger` as the source of real paper PnL for strategy diagnosis.
- Use `/api/ibkr-paper/reports/current` for the runtime promotion-blocker and paper PnL report.
- Create retained run artifacts with `POST /api/ibkr-paper/runs` or `tlm ibkr paper-run --symbol MNQ --quantity 1`.

## Safe Mode Rules

Enter safe mode immediately when any of these occur:

- Non-paper account detected.
- Live trading flag is requested.
- Contract tick size, point value, exchange, or currency mismatches expected `MNQ` spec.
- Market data is delayed, frozen, stale, missing bid/ask/last, or has negative spread.
- Parent/child bracket state is inconsistent.
- Position reconciliation drifts from expected state.
- Daily loss limit, drawdown limit, or manual kill switch is triggered.

In safe mode:

- Block all new opening orders.
- Allow cancel, flatten, reconciliation, and observation.
- Record the incident and keep the WebUI report visible for review.

## Emergency Actions

- Use the WebUI `IBKR Kill Switch` button or:
  ```bash
  curl -s -X POST http://127.0.0.1:8000/api/gateways/ibkr/kill-switch \
    -H "Content-Type: application/json" \
    -d '{"reason":"manual_emergency"}'
  ```
- Confirm open bracket order count is zero.
- Confirm paper position quantity is zero or reconcile the discrepancy.
- Do not exit safe mode until the cause is known and documented.

## Rehearsal Checklist

- Paper account guard blocks a live account fixture.
- Missing `ibapi` or adapter blocks readiness.
- Delayed, stale, and incomplete market data block paper orders.
- Wrong tick size or point value enters safe mode.
- A strong `1m` breakout signal produces a `5m` review request.
- LLM fallback can allow a paper plan but cannot emit IBKR order objects.
- Bracket order report shows open drafts and recent order events.
- Fill, commission, position, and account snapshots update the execution ledger.
- Reconciliation drift enters safe mode.
- Fast-path optimizer applies `observe_only`, `safe_mode`, or other risk reductions and rejects risk increases.
- WebUI `Refresh IBKR Paper` shows readiness, PnL, bracket state, review action, optimizer status, promotion blockers, and incidents.

## Evidence To Retain

- Health and readiness snapshots.
- Contract readiness report.
- Market data readiness report.
- 5m review request and result hashes.
- Bracket order report.
- Execution ledger with fill count, commission, realized PnL, positions, and account snapshots.
- Fast-path optimizer report.
- Incident timeline and resolution notes.
- `experiments/ibkr_paper/{run_id}/daily_report.json` and JSONL artifacts created by the API or CLI run command.

No paper result should be presented as live profitability evidence.
