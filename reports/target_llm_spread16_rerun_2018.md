# Target LLM Spread16 Rerun 2018

Date: 2026-04-28

## Objective

Verify the strategy search after fixing the generated `NQmain` spread gate from 8 ticks to 16 ticks.

Target gates remained unchanged:

- Annual test trades greater than 1000.
- Test Sharpe greater than 2.
- Test win probability greater than 53%.
- Profit factor greater than 1.2.
- Positive-year ratio at least 60%.
- Final holdout Sharpe decay at or below 0.5.

## Run

- Experiment id: `target_llm_grammar_search_spread16_2018`
- Seed root: `experiments/target_llm_grammar_search_seeds_spread16`
- Data: `NQmain`, `1m`, `2018-05-24` to `2018-07-26`
- Selected seeds: 28
- Search budget: 2 rounds per seed, 2 trials per round, inverse-signal mutations enabled
- Total trials: 224
- Stop reason: `budget_exhausted`
- Summary artifact: `experiments/target_llm_grammar_search_spread16_2018/target_discovery.json`

## Result

The zero-trade problem is fixed, but no target-qualified strategy was found.

- Qualified strategies: 0
- Passed trials: 0 of 224
- Nonzero test-trade trials: 224 of 224
- Best test Sharpe: `-12.9690`
- Best test win probability: `0.1580`
- Best annualized test trades: `12702.0`

## Family Summary

| Family | Trials | Nonzero Test Trades | Best Test Sharpe | Best Win Probability | Max Annual Trades |
| --- | ---: | ---: | ---: | ---: | ---: |
| gap_fade_or_continuation | 32 | 32 | -13.4004 | 0.1310 | 1533.0 |
| intraday_momentum | 32 | 32 | -12.9690 | 0.1580 | 12702.0 |
| opening_range_breakout | 32 | 32 | -12.9835 | 0.1444 | 6570.0 |
| regime_filtered_mean_reversion | 32 | 32 | -14.4482 | 0.1321 | 10913.5 |
| time_of_day_edge | 32 | 32 | -14.1331 | 0.1500 | 2190.0 |
| trend_pullback | 32 | 32 | -13.7217 | 0.1352 | 9581.25 |
| volatility_expansion | 32 | 32 | -15.8490 | 0.1141 | 12318.75 |

## Interpretation

This rerun confirms the infrastructure fix worked:

- The generated seeds used `spread_gate_ticks = 16`.
- Every completed trial produced nonzero test trades.
- The search is no longer failing because filters block all bars.

The remaining failure is strategy quality. The current seed grammars are too broad and trade too frequently into a high-cost CFD proxy, producing large negative average trade PnL, weak win probability, and strongly negative Sharpe across all families.

## Next Optimization Direction

- Add a cheap pre-screen stage before walk-forward that rejects candidates with negative gross edge after conservative costs.
- Generate lower-frequency variants by adding stricter regime filters, fewer max trades per day, and wider minimum expected move filters.
- Add directional inversion and threshold mutations earlier in the cheap stage, because many current families are persistently negative.
- Separate CFD-proxy discovery from futures discovery; the current `USATECHIDXUSD` proxy has very high effective spread and may be unsuitable for high-frequency one-minute scalps.
- Add objective terms that optimize average trade net PnL and profit factor before Sharpe, because all current candidates lose too much per trade.

## Status

The spread-gate bug is resolved. The target strategy requirement remains unmet because the tested strategy families are unprofitable under the current data and cost model.
