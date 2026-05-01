# NQ 1m High-Frequency Optimization Plan

Date: 2026-05-01

## 1. Decision

The original SMC LQ-EM CE strategy cannot satisfy the new hard target of more than 1,000 trades per full year. Its v1 spec caps fills at 2 trades per day, and the corrected historical funnel produces only 4 filled baseline trades across 2010-2026.

The optimization path must therefore shift from low-frequency discretionary SMC pattern translation to a high-frequency NQ 1-minute edge basket. The best current historical candidate is:

- Strategy label: `positive_expanded_edge_top_32`
- Source result: `experiments/profit_mining/expanded_high_edge_strategy_search_2019_2026.json`
- Search script: `scripts/search_expanded_high_edge_strategy.py`
- Hard objective: maximize net PnL after requiring every full year to have more than 1,000 trades and positive net PnL
- Full-year window: 2019-2025
- Partial-year window: 2026 through 2026-04-27, checked with positive YTD PnL and annualized trade count
- Result: passes the historical hard constraints and maximizes net PnL among currently qualified candidates

This is a historical research result, not a future guarantee. It must not be promoted to live or paper auto-submit until walk-forward validation, tick/quote replay, and risk-scaled concurrency limits are completed.

## 2. Internet Research Summary

The external research path supports three implementation choices and one risk-control rule:

- Use ORB and prior-day breakout families as candidate signal sources. ORB is a known intraday strategy class, and the local result confirms it as the largest contributor to the best NQ basket.
- Use intraday momentum and high-volume regime filters. Gao, Han, Li, and Zhou document intraday momentum that is stronger on volatile and high-volume days, which supports using time-of-day, volume, and trend bins instead of raw price rules only.
- Use VWAP only as a contextual intraday benchmark, not as a standalone alpha claim. Busseti and Boyd frame VWAP as an execution benchmark; the local strategy uses VWAP reclaim/bounce as one family inside a broader basket.
- Penalize optimizer selection bias. Bailey and Lopez de Prado show that large strategy sweeps inflate backtest performance unless multiple testing and selection bias are controlled. Therefore, net PnL cannot be the only gate.

References:

- CME NQ contract specification: `https://www.cmegroup.com/markets/equities/nasdaq/e-mini-nasdaq-100.contractSpecs.html`
- Opening range breakout research: `https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4416622`
- Deflated Sharpe Ratio and backtest overfitting: `https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551`
- Market Intraday Momentum: `https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2440866`
- VWAP optimal execution: `https://web.stanford.edu/~boyd/papers/vwap_opt_exec.html`
- NFA hypothetical performance limitations: `https://www.nfa.futures.org/rulebooksql/rules.aspx?RuleID=9025&Section=9`

## 3. Best Historical Candidate

Candidate: `positive_expanded_edge_top_32`

Core metrics:

- Net PnL: `$5,804,050.00` per 1 NQ contract research unit
- Total trades: `46,851`
- Minimum full-year trades: `5,498`
- Profit factor: `1.2699`
- Average trade net PnL: `$123.88`
- Win rate: `52.34%`
- Max drawdown: `$480,126.25`
- Net PnL / max drawdown: `12.09`
- Average hold: `81.35` bars
- Median hold: `98` bars
- Max concurrent positions: `99`
- Edge count: `32`

Yearly validation:

| Year | Trades | Net PnL | Profit Factor | Max Drawdown |
| ---: | ---: | ---: | ---: | ---: |
| 2019 | 5,498 | `$89,935.00` | `1.0875` | `$172,061.25` |
| 2020 | 6,134 | `$718,960.00` | `1.2891` | `$315,828.75` |
| 2021 | 6,150 | `$825,376.25` | `1.3524` | `$185,562.50` |
| 2022 | 7,210 | `$1,570,143.75` | `1.4138` | `$290,920.00` |
| 2023 | 6,410 | `$730,503.75` | `1.2757` | `$310,460.00` |
| 2024 | 6,563 | `$882,780.00` | `1.2714` | `$259,670.00` |
| 2025 | 6,693 | `$968,727.50` | `1.2211` | `$339,425.00` |
| 2026 YTD | 2,193 | `$17,623.75` | `1.0113` | `$475,640.00` |

The constraint pass is thin in 2026. It is positive YTD, but profit factor is only `1.0113` and max drawdown is near the full-period drawdown. Treat 2026 as the immediate robustness problem.

## 4. Strategy Mechanics

Execution model:

- Data: completed `NQ_CME` 1-minute bars, UTC timestamps converted to New York session buckets.
- Evaluation timing: evaluate signal after bar close, enter at next 1-minute open.
- Position direction: long and short edge families.
- Exit model: stop loss, take profit, fixed maximum hold, and date-change flatten.
- Cost model: NQ round-trip cost of `$15`, approximating fees plus one tick each side slippage.
- Risk model: no martingale, no loss doubling, no overnight holding.

Parameter set:

- `max_hold_minutes`: `120`
- `stop_range_multiple`: `6.0`
- `min_stop_points`: `8.0`
- `max_stop_points`: `90.0`
- `flatten_on_date_change`: `true`
- `max_concurrent_positions`: `99`

Edge composition:

- `opening_range_breakout`: 13 edges
- `prior_day_breakout`: 12 edges
- `vwap_pullback_bounce`: 2 edges
- `vwap_reclaim_continuation`: 1 edge
- `range_expansion_continuation`: 1 edge
- `selling_absorption_reversal`: 1 edge
- `session_extreme_reversion`: 1 edge
- `trend_pullback_reclaim`: 1 edge

Session composition:

- `ny_0930_1159`: 17 edges
- `ny_1200_1559`: 15 edges

Take-profit R composition:

- `1.0R`: 4 edges
- `1.25R`: 8 edges
- `1.5R`: 20 edges

## 5. Optimization Path

The best path is not to continue tuning SMC. It is:

1. Lock the target gates before more search.
   - Full years must have `trade_count > 1000`.
   - Full years must have `net_pnl > 0`.
   - Candidate ranking is net PnL only after passing yearly hard gates.
   - Reject candidates with negative 2026 YTD unless 2026 is explicitly excluded as holdout.

2. Promote the expanded high-edge basket into a first-class executable strategy module.
   - Persist the 32 selected edges as a versioned strategy definition.
   - Add deterministic replay tests that verify yearly trade counts and PnL from the result artifact.
   - Add edge-level attribution by year, especially for 2026.

3. Run walk-forward validation before accepting the result.
   - Freeze selection using rolling train windows.
   - Validate on the next unseen year or half-year.
   - Track selected-edge turnover; unstable edge selection means the result is likely overfit.
   - Add Deflated Sharpe Ratio or equivalent multiple-testing penalty to the report.

4. Run tick/quote replay.
   - Replace next-open 1-minute fills with quote-aware fills where data exists.
   - Stress slippage by at least `1x`, `2x`, and `3x`.
   - Reject if yearly positivity disappears under realistic fills.

5. Reduce concurrency for implementability.
   - The net-PnL maximizer uses `max_concurrent_positions=99`, which is a research upper bound.
   - The more practical candidate from the same report is `robust_expanded_positive_edge_top_32` with `max_concurrent_positions=24`.
   - Before paper trading, test `6`, `12`, and `24` concurrency with MNQ sizing and daily loss caps.

6. Only then build TradingView and paper-trading visualizations.
   - TradingView should visualize family triggers and selected edges, not attempt to reproduce the full institutional fill model.
   - Paper trading should start on MNQ, not NQ, with max one risk unit until drift is measured.

## 6. BD Task Breakdown

Use these beads as implementation control points:

- `tradingllmagent-0d0e`: add persistent SMC/high-frequency funnel diagnostics and ablation report.
- `tradingllmagent-yad9`: promote `positive_expanded_edge_top_32` into a versioned executable strategy module.
- `tradingllmagent-2jxx`: run walk-forward and 2026 holdout validation for the expanded high-edge basket.
- `tradingllmagent-02v8`: optimize or replace the expanded basket after walk-forward failure.
- `tradingllmagent-3bxz`: run tick/quote replay and cost-stress validation for the selected basket.
- `tradingllmagent-fgou`: build risk-scaled MNQ paper plan after validation gates pass.

The IBKR paper wiring bead must remain blocked until the validation tasks pass.

## 7. Current Recommendation

If the mandate is strictly “maximize historical net PnL while requiring every full year to have more than 1,000 trades and positive net PnL,” choose `positive_expanded_edge_top_32`.

If the mandate is “prepare something realistic for paper trading,” do not choose the 99-concurrency version yet. The first rolling walk-forward validation failed its positive-OOS-year gate, so the basket needs another optimization pass before tick/quote replay or paper trading.

## 8. Implementation Status

The net-PnL maximizer is now registered in `src/tlm/expanded_high_edge.py` as preset `positive_expanded_edge_top_32`.

Versioned implementation details:

- 32 selected edges are stored as `EXPANDED_HIGH_EDGE_NETMAX_EDGES`.
- Replay parameters are stored as `EXPANDED_HIGH_EDGE_NETMAX_SPEC`: `max_concurrent_positions=99`, `max_hold_minutes=120`, `stop_range_multiple=6.0`, `min_stop_points=8.0`, `max_stop_points=90.0`.
- Historical yearly gate snapshots are stored in `EXPANDED_HIGH_EDGE_NETMAX_YEARLY_RESULTS`.
- `expanded_high_edge_yearly_gate_report()` checks the deterministic yearly gate snapshot.
- `check_expanded_high_edge_replay()` compares a replay result against the versioned yearly baseline and flags drift.
- IBKR strategy construction now reads preset-specific parameters, so selecting `TLM_IBKR_EXPANDED_HIGH_EDGE_PRESET=positive_expanded_edge_top_32` uses the 99-concurrency and 120-minute netmax research settings.
- Unit coverage in `tests/test_expanded_high_edge.py` verifies preset registration, edge-family mix, annual hard gates, and preset-specific IBKR strategy parameters.
- `scripts/validate_expanded_high_edge_netmax.py --fail-on-drift` replays the frozen preset from local 1-minute bars and writes `reports/nq_expanded_high_edge_netmax_replay_2026-05-01.json`.
- The frozen replay currently passes drift validation exactly: `46,851` signals/trades, `$5,804,050.00` net PnL, `5,498` minimum full-year trades, `8/8` positive checked years, and zero yearly baseline mismatches.

This replay validation proves the versioned implementation reproduces the source research artifact. It does not prove robustness because the first rolling walk-forward validation failed.

## 9. Walk-Forward Validation Result

Script: `scripts/walk_forward_expanded_high_edge.py`

Report: `reports/nq_expanded_high_edge_walk_forward_fast_2026-05-01.json`

Method:

- Expanding train windows select candidate groups and combo size only from train years.
- The selected basket is replayed on the next unseen test year.
- Default fast validation uses `max_preselect=40`, `max_specs=6`, `max_hold_minutes=120`, `stop_range_multiple=6.0`, and `max_concurrent_positions` selected from `6`, `12`, `24`, and `99`.
- The report includes selected-edge turnover and a multiple-testing penalty decision.

Summary:

- Decision: `fail`
- Positive OOS years: `4/6`
- Trade-floor years: `6/6`
- OOS total trades: `18,688`
- OOS total net PnL: `$275,842.50`
- Worst OOS year PnL: `-$1,067,145.00`
- Failed positive years: `2022`, `2026`
- Average selected-edge Jaccard similarity: `0.3163`
- Minimum selected-edge Jaccard similarity: `0.0769`
- Multiple-testing penalty decision: `fail`

| Test Year | Trades | Net PnL | Profit Factor | Max Drawdown | Turnover Jaccard |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 2021 | 3,327 | `$354,067.50` | `1.3034` | `$168,736.25` | n/a |
| 2022 | 4,206 | `-$1,067,145.00` | `0.6458` | `$1,204,435.00` | `0.1429` |
| 2023 | 4,402 | `$694,790.00` | `1.4722` | `$278,498.75` | `0.0769` |
| 2024 | 2,867 | `$250,811.25` | `1.1602` | `$336,558.75` | `0.4286` |
| 2025 | 2,729 | `$105,091.25` | `1.0499` | `$345,697.50` | `0.6000` |
| 2026 YTD | 1,157 | `-$61,772.50` | `0.9301` | `$362,885.00` | `0.3333` |

Conclusion:

- The historical in-sample net maximizer satisfies the user's hard historical annual trade/PnL target, but it does not yet satisfy rolling OOS robustness.
- The low selected-edge stability suggests optimizer selection bias and regime dependence.
- Do not promote `positive_expanded_edge_top_32` to paper trading as-is.
- Next optimization bead: `tradingllmagent-02v8`, which blocks tick/quote replay and the MNQ paper plan until an OOS-positive candidate is found.
