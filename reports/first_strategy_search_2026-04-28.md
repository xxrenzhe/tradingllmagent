# First Strategy Search Report - 2026-04-28

## Objective

Find NQmain 1m intraday strategies with:

- Annualized trades > 1000
- Test Sharpe > 2
- Test win probability > 53%
- Profit factor > 1.2
- Max drawdown <= 10000
- Positive year ratio >= 60%
- Acceptable holdout Sharpe decay, non-overlap test coverage, parameter complexity, and cost sensitivity

## Data Used

Primary search window:

- Symbol: NQmain
- Timeframe: 1m bars
- Date range: 2018-05-24 to 2018-07-26
- Bar rows: 60652
- Tick quality report: `data/quality/NQmain/2018-05-24_2018-07-26.json`
- Tick coverage: 64 of 64 expected files, coverage ratio 1.0
- Quality status: `gaps_or_anomalies` because empty partitions exist on Saturday dates
- Tick rows: 5581435

Earlier 2012 exploratory runs were retained for traceability, but the 2012 data window had a session mismatch with the strategy session and produced mostly zero-trade results.

## Runs Completed

Baseline validation:

- `first_search_momentum_scalp`: 3 trials, 0 passed
- `first_search_generated_orb_01`: 3 trials, 0 passed
- `first_search_mean_reversion_scalp`: 3 trials, 0 passed

Target discovery:

- `first_search_target_discovery`: 72 trials on the 2012 window, 0 qualified
- `first_search_target_discovery_broad_freq`: 80 trials on the 2012 window, 0 qualified
- `first_search_target_discovery_2018_session`: 96 trials on the 2018 full-session window, 0 qualified

The 2018 run selected 24 seeds from 53 local strategy specs across intraday momentum, regime-filtered mean reversion, time-of-day edge, trend pullback, volatility expansion, and generated feature-combo families.

## Best 2018 Candidates

None passed the target gates. The strongest candidates were still materially negative after costs.

| Ranking Lens | Family | Annual Trades | Sharpe | Win Probability | Net PnL | Main Failure |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| Best Sharpe | intraday_momentum | 474.5 | -5.01 | 15.38% | -1828.96 | Too few trades, negative Sharpe, negative PnL |
| Best Win Rate | time_of_day_edge | 273.75 | -8.88 | 33.33% | -821.08 | Too few trades, negative Sharpe, win rate below target |
| Best High-Frequency | volatility_expansion | 9544.75 | -14.27 | 15.87% | -35158.34 | High frequency but severely negative expectancy |
| Best Trend Pullback Frequency | trend_pullback | 2299.5 | -5.58 | 30.95% | -7562.00 | Meets frequency only; fails Sharpe, win rate, PnL |

## Readout

The current seed pool generates enough trading activity in several families, but the edge direction is wrong under the conservative NQ cost model. High-frequency volatility and mean-reversion candidates trade often but show strongly negative expectancy. Lower-frequency time-of-day candidates reduce loss size but cannot meet the annual trade threshold or win-rate target.

This is a valid first search cycle: the pipeline discovered, tested, rejected, and archived candidates against the full gate set. It did not find a deployable or paper-tradable strategy.

## Next Search Direction

The next cycle should not simply increase trial count on the same seed shapes. More useful changes are:

- Add inverse-signal mutation so strongly negative high-frequency candidates can be tested with flipped entry direction.
- Add feature-conditioned exits, especially volatility-scaled stops and time-decay exits.
- Add cost-aware pruning before full validation to reject candidates whose average gross edge cannot survive NQ fees and slippage.
- Expand seed families toward microstructure and session-state features instead of only price-pattern combinations.
- Use longer multi-regime windows after a candidate shows positive expectancy on the short discovery window.

