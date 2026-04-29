# Target LLM Grammar Search 2018

Date: 2026-04-28

## Objective

Use the LLM-assisted strategy discovery loop to search NQ 1-minute intraday strategies that satisfy all target gates:

- Annual test trades greater than 1000.
- Test Sharpe greater than 2.
- Test win probability greater than 53%.
- Additional robustness gates: profit factor greater than 1.2, max drawdown at or below 10000, positive-year ratio at least 60%, final holdout Sharpe decay at or below 0.5, parameter combinations at or below 200, and at least one non-overlapping test fold.

## Run

- Experiment id: `target_llm_grammar_search_2018`
- Data: `NQmain`, `1m`, `2018-05-24` to `2018-07-26`
- Seed root: `experiments/target_llm_grammar_search_seeds`
- Selected seeds: 28 of 29 files; `manifest.json` was skipped because it is not a strategy spec
- Families covered: gap fade or continuation, intraday momentum, opening range breakout, regime-filtered mean reversion, time-of-day edge, trend pullback, volatility expansion
- Search budget: 2 rounds per seed, 2 trials per round, local deterministic LLM adapter, inverse-signal mutations enabled
- Total completed trials: 224
- Summary artifact: `experiments/target_llm_grammar_search_2018/target_discovery.json`

## Result

No strategy met the target.

- Stop reason: `budget_exhausted`
- Qualified strategies: 0
- Passed trials: 0 of 224
- Best test annual trades: 0
- Best test Sharpe: unavailable because all test folds had zero trades
- Best test win probability: unavailable because all test folds had zero trades
- Best validation candidate family: `trend_pullback`, with validation Sharpe `-5.0949`, validation PnL `-31.7006`, test trades `0`, test PnL `0`, and holdout PnL `-128.9357`

## Failure Attribution

Every trial failed the same hard gates:

- `annual_trades_below_target`: 224 of 224
- `sharpe_below_target`: 224 of 224
- `win_probability_below_target`: 224 of 224
- `profit_factor_below_target`: 224 of 224
- `positive_year_ratio_below_target`: 224 of 224
- `final_holdout_sharpe_decay_above_target`: 224 of 224
- `cost_sensitivity_failed`: 224 of 224

The attribution report also shows that every family produced zero test trades and failed the cheap pre-screen:

- `trade_count_below_prescreen_minimum`: 224 of 224
- `negative_gross_edge`: 224 of 224
- `insufficient_cost_coverage`: 224 of 224

## Interpretation

This run did not fail because a nearly profitable candidate missed Sharpe by a small margin. It failed earlier: the generated signal grammars were too restrictive or misaligned with the selected 2018 test windows, producing no trades in the hidden test folds. The validation and holdout evidence was also negative, so there is no defensible candidate to promote.

The next search iteration should focus on increasing valid signal frequency before optimizing Sharpe:

- Add a signal-health gate before full walk-forward validation: require minimum raw entry events and minimum cost-adjusted gross edge on train and validation.
- Expand seed grammars toward higher-frequency intraday patterns: opening auction imbalance, VWAP reversion bands, micro pullbacks after impulse bars, liquidity sweep reversal, range compression breakout, and time-of-day scalps.
- Relax overly narrow filters in generated seeds: spread, minutes-to-close, volatility percentile, and multi-condition `all` clauses should be bounded by minimum event counts.
- Add mutation types beyond inverse signal: threshold widening, filter removal, time-window shifts, stop/take-profit ratio sweeps, and symmetric long/short simplification.
- Prefer a two-stage budget: first screen thousands of cheap raw-signal candidates, then run expensive walk-forward only on candidates with enough trades and positive net edge after conservative costs.

## Status

The search infrastructure executed end to end with LLM proposals, generated grammar seeds, inverse-signal mutations, walk-forward validation, hidden final holdout evaluation, pre-screen attribution, and experiment archiving. The target requirement is not satisfied by this run.
