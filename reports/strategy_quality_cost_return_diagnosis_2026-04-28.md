# Strategy Quality and Post-Cost Return Diagnosis

Date: 2026-04-28

## Scope

This diagnosis analyzes `target_llm_grammar_search_spread16_2018`, the rerun after the `NQmain` spread gate fix.

The goal was to determine why all 224 trials remained unprofitable even though the zero-trade filter artifact was fixed.

## Summary

The current strategy families are not losing only because costs are high. They have approximately zero mid-price directional edge before costs, then become strongly negative after realistic execution costs.

Across all test trades:

- Test trades analyzed: `97,664`
- Average mid-price directional PnL: `-0.0208 USD/trade`
- Average bid/ask execution cost embedded in gross PnL: `28.7805 USD/trade`
- Average gross PnL after bid/ask execution: `-28.8014 USD/trade`
- Average explicit fees and slippage: `15.0000 USD/trade`
- Average net PnL: `-43.8014 USD/trade`
- Mid-price win rate: `34.67%`
- Net win rate: `12.30%`

## Cost Decomposition

| Family | Avg Mid PnL | Avg Spread Cost | Avg Gross PnL | Explicit Cost | Avg Net PnL | Net Win Rate |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| gap_fade_or_continuation | -1.8436 | 24.3965 | -26.2402 | 15.0000 | -41.2402 | 9.52% |
| intraday_momentum | 0.0483 | 28.9952 | -28.9469 | 15.0000 | -43.9469 | 14.20% |
| opening_range_breakout | 2.2144 | 29.6314 | -27.4170 | 15.0000 | -42.4170 | 13.33% |
| regime_filtered_mean_reversion | -0.5613 | 29.0670 | -29.6283 | 15.0000 | -44.6283 | 10.93% |
| time_of_day_edge | 0.0953 | 28.2365 | -28.1412 | 15.0000 | -43.1412 | 12.08% |
| trend_pullback | -0.6336 | 27.7346 | -28.3682 | 15.0000 | -43.3682 | 12.29% |
| volatility_expansion | -0.1251 | 29.3097 | -29.4348 | 15.0000 | -44.4348 | 11.41% |

The only family with a noticeably positive mid-price average was opening range breakout at `2.2144 USD/trade`, which is still far too small to cover even a low-cost execution assumption.

## Cost Sensitivity

If execution used mid-price fills and ignored the CFD bid/ask spread:

- No-cost mid-price average: `-0.0208 USD/trade`
- Mid-price minus `5 USD`: `-5.0208 USD/trade`
- Mid-price minus `10 USD`: `-10.0208 USD/trade`
- Mid-price minus current explicit `15 USD`: `-15.0208 USD/trade`

This means the strategy set has no durable gross edge. Lowering costs helps the reported PnL, but it does not turn the current signal set into a profitable system.

## Strategy Quality Failure Modes

The common failure modes are consistent across all families:

- `negative_gross_edge`: 224 of 224 trials.
- `insufficient_cost_coverage`: 224 of 224 trials.
- `top_day_pnl_concentration`: 224 of 224 trials.
- Test Sharpe gate failed: 224 of 224 trials.
- Profit factor gate failed: 224 of 224 trials.
- Validation Sharpe gate failed: 224 of 224 trials.

Exit and holding-time behavior also show poor signal quality:

- Stop-loss exits: `84,064` of `97,664` trades, about `86.08%`.
- Take-profit exits: `11,920` trades, about `12.21%`.
- Max-holding exits: `1,680` trades, about `1.72%`.
- Trades closed within 1 minute: `56,768`, about `58.13%`.
- Trades closed within 3 minutes: `74,944`, about `76.73%`.

The current grammars are effectively high-frequency, low-edge scalps on a wide-spread CFD proxy. They enter often, exit quickly, and hit stops far more often than take-profit.

## Original vs Inverse Signals

Inverse mutations were not enough to fix the problem:

- Original signals averaged `-0.1646 USD/trade` on mid-price movement.
- Inverse signals averaged `0.1230 USD/trade` on mid-price movement.
- Both values are too close to zero to survive costs.

This is not mainly a direction-flip problem. The tested predicates do not isolate moves large enough or persistent enough to cover execution.

## Root Causes

1. Signal predicates are too broad and fire too frequently.
2. Entry rules do not require expected move size to exceed spread plus slippage.
3. Exit rules are too tight for the proxy spread and one-minute noise.
4. `USATECHIDXUSD` is a wide-spread CFD proxy; using it for high-frequency NQ-like scalps creates a very high hurdle.
5. The current generator optimizes for activity and simple feature combinations, not expected edge per trade.

## Recommended Fixes

1. Add a cheap edge pre-screen before full walk-forward validation:
   - Compute train and validation mid-price edge.
   - Reject candidates with average mid PnL below `15 USD/trade`.
   - Reject candidates whose gross PnL after bid/ask execution is non-positive.
   - Reject candidates with stop-loss exit ratio above `65%`.

2. Generate lower-frequency, higher-conviction strategies:
   - Cap max trades per day at `5-12` for CFD-proxy searches.
   - Require volatility or expected move filters so projected move is at least `2x` round-trip cost.
   - Add confirmation filters such as VWAP reclaim plus trend regime, opening range retest, or post-impulse pullback.

3. Separate data-source assumptions:
   - If the target is CME NQ futures, use futures-grade data or a futures-like synthetic spread model.
   - If the target is the CFD proxy, strategy generation must assume much wider spread and avoid one-minute scalps.

4. Feed diagnostics back to the LLM:
   - Include avg mid PnL, avg spread cost, avg net PnL, stop-loss ratio, hold-time distribution, and inverse-signal comparison in proposal feedback.
   - Penalize candidates that only increase trade count without improving per-trade edge.

## Status

The spread-gate bug is fixed. The remaining problem is real strategy quality: current generated signals have no meaningful mid-price edge and cannot cover bid/ask spread, slippage, and fees.
