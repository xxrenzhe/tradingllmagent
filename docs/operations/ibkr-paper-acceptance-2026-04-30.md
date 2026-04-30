# IBKR Paper Acceptance Evidence - 2026-04-30

## Scope

This report captures the first end-to-end IBKR Paper execution drill for `docs/plan/15.ibkr-paper-llm-optimization-plan.md`.

## Runtime Evidence

- Environment: IBKR Paper only; live trading remains disabled.
- API socket: TWS API socket was listening on port `7497`.
- Local loop: `tlm ibkr loop --symbol MNQ --api-port 8000 --auto-submit`.
- Account: paper account verified by gateway.
- Contract: `MNQ` resolved and validated as `MNQM6`, expiry `20260618`, tick size `0.25`, point value `2.0`.
- Market data: delayed IBKR data accepted for paper readiness after `bid`, `ask`, and `last` were all present.
- Readiness: gateway reached `ready` with `missing_requirements=[]`.

## Paper Order Drill

- Order type: bracket-protected paper order.
- Entry: `BUY 1 MNQ` parent `MKT`.
- Parent order id: `1`.
- Stop order id: `2`.
- Take-profit order id: `3`.
- Parent fill: `BUY 1 MNQ @ 27396.25`.
- Entry commission: `0.62`.
- Take-profit fill: `SELL 1 MNQ @ 27405.0`.
- Exit commission: `0.62`.
- Realized PnL reported by IBKR account snapshot: `16.26`.
- Final MNQ position: `0`.
- Safety cleanup: direct IBAPI global cancel was requested after the drill; TWS position callback confirmed `MNQ` quantity `0`.

## Code Changes Validated

- IBKR restored farm statuses (`2104`, `2106`, `2158`) are treated as informational, not blocking errors.
- IBKR delayed data notice `10167` no longer completes market data snapshots before prices arrive.
- Market data sync waits for `bid`, `ask`, and `last` before marking a snapshot complete.
- Delayed market data has a 30 second minimum stale tolerance for paper readiness.
- Bracket lifecycle audit now closes local open bracket records when a stop or take-profit child order reaches terminal `Filled` status.
- IBKR broker-assigned bracket order ids are now written back to local bracket drafts after submission, so later `orderStatus` and `execDetails` callbacks reconcile against the actual TWS ids.
- Acceptance counters now treat legitimate IBKR Paper bracket submissions as paper lifecycle events, not live order attempts. Normal duplicate IBKR callbacks are ignored and do not count as unexplained duplicates.

## WebUI QA

- Local WebUI served from `http://127.0.0.1:5173` with the Vite proxy pointing to the running API on `127.0.0.1:8000`.
- Browser QA opened the `Execution` section and clicked `Refresh IBKR Paper`.
- The IBKR Paper panel rendered `ready`, `paper gateway ready`, delayed market data, account/PnL, review, optimizer, promotion gate, and incident fields from current API endpoints.
- Browser console check reported zero errors.
- Screenshot evidence: `docs/operations/ibkr-paper-webui-qa-2026-04-30.png`.

## Validation Commands

- `PYTHONPATH=tests python3 -m unittest tests.test_ibkr_gateway tests.test_tasks_api tests.test_ibkr_paper_loop tests.test_ibkr_soak tests.test_ibkr_cli`
- `python3 -m compileall src/tlm tests`

## Remaining Acceptance Gates

- Five trading day paper-only soak is not yet complete.
- Thirty 5 minute review cycles are not yet complete in one retained run.
- At least 100 readiness checks are not yet complete in the corrected retained soak run.
