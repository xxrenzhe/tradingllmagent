# IBKR Paper Trade Diagnosis - 2026-05-01

## Scope

- Runtime: local IBKR Paper loop on `MNQ`.
- Date reviewed: 2026-05-01.
- Sources: `experiments/ibkr_paper/soak_current/*.json`, local API endpoints, and `experiments/ibkr_paper/soak_current/logs`.

## Current Runtime Action

- Triggered IBKR Paper kill switch at `2026-05-01T13:51:16Z`.
- Gateway entered safe/read-only mode.
- Open bracket orders were cleared.
- Follow-up API check showed `open_bracket_order_count = 0` and tracked `MNQ` position quantity `0`.

## Trade Record Summary

- Latest post-kill ledger snapshot:
  - `fill_count = 42`
  - `completed_bracket_order_count = 20`
  - `open_bracket_order_count = 0`
  - `net_realized_pnl = 466.960001`
  - `total_commission = 26.04`
  - latest account `daily_pnl = 487.72000000010564`
- The main burst happened from about `2026-05-01T13:30Z` to `2026-05-01T13:51Z`.
- All completed brackets in the inspected burst completed through take-profit orders.
- No live order attempts, unexplained duplicate orders, or bracket-child-missing incidents were recorded.

## Findings

1. The strategy over-traded during one continuous `expanded_high_edge:prior_day_breakout` condition.
   - The loop treated each new 1-minute strong signal as eligible for another review and bracket.
   - Current runtime config allowed `max_concurrent_positions = 24` and `daily_trade_cap = 24`, conflicting with the runbook's one-paper-contract operating scope.

2. Safe mode did not stop the fallback review from saying `paper_allow`.
   - The gateway rejected new brackets once safe mode was active, but the review layer still produced a bullish `paper_allow` result.
   - This made the decision log misleading during incident handling.

3. Daily trade cap was not enforced before auto-submit.
   - The control state exposed `daily_trade_cap`, but `run_ibkr_decision_cycle` only checked open bracket count plus open position quantity against `max_concurrent_positions`.

4. Market data readiness became stale between poll cycles.
   - The active loop used `--poll-interval-seconds 60` and `--max-stale-seconds 30`.
   - That configuration can make readiness flip to `market_data_stale` between syncs.

5. Execution fill timestamps were mis-normalized.
   - IBKR execution times such as `20260501  21:30:04` were interpreted as UTC.
   - In this environment that value is local time, so fills appeared eight hours later than their matching order events.

## Fixes Applied

- `src/tlm/ibkr_adapter.py`: parse IBKR execution times as local naive timestamps and convert them to UTC.
- `src/tlm/ibkr_review.py`: block fallback review in safe mode and when daily trade cap is reached.
- `src/tlm/api.py`: pass daily cap state into risk context and prevent bracket construction when the cap is reached.
- Added focused tests for timestamp normalization, safe-mode blocking, and daily-cap blocking.

## Verification

- `PYTHONPATH=src python3 -m unittest tests.test_ibkr_review tests.test_ibkr_adapter tests.test_ibkr_paper_loop`
- Result: `Ran 41 tests ... OK`.

## Remaining Operational Risk

- The currently running process still uses the pre-patch code until restarted.
- Do not restart with `--auto-submit` until the operator chooses a paper scope:
  - one-contract safety mode, or
  - explicit multi-position cap testing.
- If continuing the soak, use a poll interval no greater than the stale threshold, for example `--poll-interval-seconds 15 --max-stale-seconds 30`.
