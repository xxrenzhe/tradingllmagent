# Black-Box Assessment: 70% Win Rate at 2R Strategy Request

Date: 2026-05-02
Symbol scope: NQ_CME / MNQ execution path

## Verdict

No strategy in the current evidence set should be promoted as "70% win rate at 2R, non-overfit, live-ready, long-term profitable, black-box tested."

The strongest validated candidates in this repository support a weaker claim: there are NQ strategy families with positive walk-forward or cost-stressed bar-replay evidence, but they do not meet the requested 70% win-rate and 2R profile, and local quote/tick replay evidence is absent.

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
- Quote/tick replay gate: failed because no normalized local quote/tick parquet files are available.

This candidate is profitable under bar-based cost stress, but it is not 70% win rate, not 2R, and not black-box quote-replay validated.

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

## Black-Box Test Result

Black-box execution replay is blocked in the current workspace.

Both execution-stress reports record:

- `quote_file_count`: 0
- `tick_file_count`: 0
- `quote_replay.status`: `blocked_no_quote_or_tick_files`

Without quote/tick files, the repository can stress bar-based fills but cannot prove adverse queue position, spread crossing, or quote-level fill realism. That means "directly live-ready" is not substantiated.

## Practical Strategy Boundary

The only defensible live-facing posture from current evidence is:

- Use `expanded_high_edge_cap24` only as a paper-trading candidate, not a real-money guarantee.
- Trade MNQ first, not NQ, until paper execution slippage and broker fills match assumptions.
- Require a new quote/tick replay pass before production promotion.
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

The engineering-valid output is a rejection of promotion plus a concrete next step: import normalized quote/tick data, run quote-level execution replay, then search only within a locked train/test protocol where 2R and 70% win rate are hard acceptance gates rather than optimization targets.
