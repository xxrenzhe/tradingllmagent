# Black-Box Assessment: 70% Win Rate at 2R Strategy Request

Date: 2026-05-02
Symbol scope: NQ_CME / MNQ execution path

## Verdict

No strategy in the current evidence set should be promoted as "70% win rate at 2R, non-overfit, live-ready, long-term profitable, black-box tested."

The strongest validated candidates in this repository support a weaker claim: there are NQ strategy families with positive walk-forward or cost-stressed bar-replay evidence, but they do not meet the requested 70% win-rate and 2R profile. The provided Databento MBP-1 zip has now been normalized and replayed for the available two-month tick/quote window, but that replay does not rescue the failed 70%/2R/live-readiness gate.

## Acceptance Gates

The request is treated as a promotion gate, not as a target to optimize into:

1. Win rate must be at least 70% after costs.
2. Average or fixed reward target must be 2R or higher.
3. Selection must be walk-forward or holdout-based, not fitted on the final test window.
4. Every out-of-sample test year must be profitable after realistic costs.
5. Execution stress must pass 1x, 2x, and 3x slippage or an equivalent adverse-fill model.
6. Quote-level or tick-level black-box replay must be available and pass.
7. The strategy must be suitable for paper/live gating before any real capital exposure.

## Evidence Reviewed

### Walk-Forward Expanded High-Edge Candidate

Source: `reports/nq_expanded_high_edge_walk_forward_no_prior_highvol_donchian_min13_2026-05-01.json`

- Test method: rolling walk-forward, selecting on train years and replaying on the next unseen year.
- Out-of-sample test years: 2021, 2022, 2023, 2024, 2025, 2026.
- Gate result: passed the repository walk-forward positivity and trade-floor gates.
- Out-of-sample net PnL: 285,980.00.
- Out-of-sample total trades: 11,070.
- Worst out-of-sample test-year PnL: 3,623.75.
- Reward grid: not a 2R strategy; tested take-profit values are below 2R in the available reports.
- Execution stress: fails at higher slippage in `reports/nq_expanded_high_edge_execution_stress_2026-05-01.json`.

This is the closest non-overfit positive candidate, but it fails the requested 2R and live black-box requirements.

### Fixed Expanded High-Edge Cost-Stressed Candidate

Source: `reports/nq_expanded_high_edge_cap24_fixed_execution_stress_2026-05-02.json`

- Stress 1x: net PnL 5,371,751.25, win rate 51.19%, profit factor 1.2920.
- Stress 2x: net PnL 5,039,371.25, win rate 51.01%, profit factor 1.2715.
- Stress 3x: net PnL 4,706,991.25, win rate 50.79%, profit factor 1.2514.
- Cost-stress gate: passed.
- Quote/tick replay gate: not proven for this full-history fixed candidate by the current MBP-1 replay, because the available zip only covers 2026-03-03 to 2026-05-01.

This candidate is profitable under bar-based cost stress, but it is not 70% win rate and not 2R.

### SMC LQEM Balanced R2 Candidate

Source: `reports/nq_smc_lqem_optimization_balanced_r2_2026-05-01.md`

- Full-history trades: 10.
- Full-history net PnL: -200.00.
- Profit factor: 0.8326.
- Promotion gates: failed minimum trade count, positive expectancy after 2x cost, final holdout profit factor, and yearly concentration.

This is the only reviewed candidate explicitly aligned with an R2-style objective, and it fails promotion.

### Locked 2R Bar-Level Walk-Forward Check

Source: `reports/nq_expanded_high_edge_walk_forward_2r_gate_2026-05-02.json`

After confirming historical bars exist under `data/bars`, the expanded high-edge walk-forward search was rerun with `--take-profit-r-grid 2.0` to force every selected edge to use a 2R take-profit target.

- Data scope: `data/bars/1m/NQ_CME`, 2019-01-01 to 2026-04-27.
- Selection method: rolling train years, next-year out-of-sample replay.
- Gate result: failed.
- Out-of-sample total net PnL: -330,000.00.
- Positive out-of-sample years: 3 of 6.
- Failed positive years: 2022, 2025, 2026.
- Failed trade-floor year: 2024.
- Best out-of-sample test-year win rate: 52.44% in 2023.
- Worst out-of-sample test-year PnL: -1,026,465.00 in 2022.

This directly addresses the available `data/bars` evidence: even before quote/tick execution replay, the strict 2R bar-level candidate fails both the 70% win-rate requirement and the long-term profitability gate.

### Independent Signal Family Expansion

Source files:

- `reports/nq_expanded_high_edge_walk_forward_newfamilies_2r_defensive_caps_2026-05-02.json`
- `reports/nq_expanded_high_edge_walk_forward_newfamilies_2r_stress3_2026-05-02.json`
- `reports/nq_expanded_high_edge_walk_forward_newfamilies_floor_caps_stress2_2026-05-02.json`
- `reports/nq_expanded_high_edge_walk_forward_newfamilies_floor_caps_stress3_2026-05-02.json`
- `reports/nq_expanded_high_edge_walk_forward_newfamilies_stress3_2026-05-02.json`

The signal search was expanded with RSI and stochastic extreme reversals, moving-average reacceleration, prior-close reclaim, midday z-score reversion, and closing-drive continuation families. These were evaluated through the same rolling train-years / next-year out-of-sample protocol.

- Best strict 2R defensive capped run: failed; positive years 4 of 6; failed 2022 and 2024 profitability; failed 2026 trade floor; OOS net PnL -198,265.00.
- Strict 2R under 3x stress: failed; positive years 3 of 6; failed 2022, 2024, and 2025; OOS net PnL -940,150.00.
- New-family capped 2x stress: failed; positive years 3 of 6; failed 2022, 2025, and 2026; OOS net PnL -258,332.50.
- New-family capped 3x stress: failed; positive years 1 of 6; failed 2021, 2022, 2023, 2025, and 2026; OOS net PnL -313,556.25.

This closes the obvious next attempt: adding independent bar-signal families increased the candidate group count, but it still did not produce a non-overfit 70% win-rate / 2R / cost-stressed strategy.

## Black-Box Test Result

Black-box execution replay is now available for the provided recent MBP-1 zip, but only for the two-month Databento window.

Source: `data/raw/databento/GLBX-20260502-QG6TRKVV9Q.zip`

Normalized quote output:

- Output root: `data/normalized/quotes/NQ_CME`
- Date coverage: 2026-03-03 to 2026-05-01.
- Databento MBP-1 members processed: 52.
- Normalized quote rows: 505,942,666.

Execution replay source: `reports/nq_expanded_high_edge_execution_stress_mbp1_full_2026-05-02.json`

- Strategy window replayed: 2026-03-03 to 2026-05-01.
- Candidate trades in this tick window: 159.
- Quote replay status: passed for available trades.
- Validated trades: 159 of 159.
- Missed quote fills: 0.
- 60-second limit-fill model: 156 filled, 3 missed, 98.11% fill rate.
- Top-of-book size model: 159 of 159 full top-level fills, minimum fill ratio 1.0.
- Latency model: 0s, 1s, and 5s delay scenarios were replayed.
- Adverse excursion model: 1m, 3m, 5m, and 15m horizons were replayed.

The same report still fails overall promotion:

- 1x bar stress: 159 trades, net PnL 10,435.00, failed full walk-forward cost-stress gate.
- 2x bar stress: 159 trades, net PnL 8,845.00, failed full walk-forward cost-stress gate.
- 3x bar stress: 159 trades, net PnL 7,255.00, failed full walk-forward cost-stress gate.
- Decision: `passed: false`, `cost_stress_passed: false`, `quote_replay_passed: true`.

This removes the prior "no quote/tick files" blocker for the recent Databento window, but it does not create a live-ready 70% win-rate, 2R strategy. The strict 2R walk-forward evidence already fails, and the recent MBP-1 replay covers only 159 trades in 2026, not the full 2019-2026 out-of-sample history.

## Practical Strategy Boundary

The only defensible live-facing posture from current evidence is:

- Use `expanded_high_edge_cap24` only as a paper-trading candidate, not a real-money guarantee.
- Trade MNQ first, not NQ, until paper execution slippage and broker fills match assumptions.
- Require quote/tick replay coverage across any future promotion window, not just the recent two-month Databento sample.
- Reject any 70% win-rate at 2R claim unless it passes a locked walk-forward protocol and independent execution replay.

Suggested paper gate:

- Minimum 30 live/paper trading days.
- Minimum 300 filled trades.
- Realized win rate no lower than 45% for the current high-edge family, or no lower than 70% only if a new true-2R strategy is developed.
- Realized profit factor above 1.10 after commissions and measured slippage.
- No single day contributes more than 25% of total net PnL.
- Stop trading automatically after daily loss cap, repeated reject/timeout errors, or realized slippage above the backtest stress envelope.

## Conclusion

The requested deliverable cannot honestly be produced from the current repository evidence. A 70% win-rate, 2R, non-overfit, black-box-tested, live-ready strategy is not present.

The engineering-valid output is a rejection of promotion plus a concrete next step: search only within a locked train/test protocol where 2R and 70% win rate are hard acceptance gates, then require quote/tick replay across the final holdout before paper/live promotion.
