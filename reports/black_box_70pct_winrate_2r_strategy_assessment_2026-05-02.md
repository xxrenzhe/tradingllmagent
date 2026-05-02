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

Sources:

- `reports/nq_smc_lqem_optimization_balanced_r2_2026-05-01.md`
- `reports/nq_smc_lqem_ce_v1_objective_gates_2026-05-02.json`

- Full-history trades: 10.
- Full-history net PnL: -200.00.
- Profit factor: 0.8326.
- Promotion gates: failed minimum trade count, positive expectancy after 2x cost, final holdout profit factor, and yearly concentration.
- Executable SMC v1 objective-gate check: 4 full-history trades, 0.0% win rate, p75 net R -1.0645.
- SMC v1 objective gates: failed minimum trade count, full-history 70% win-rate gate, full-history 2R net-R gate, walk-forward test 70% win-rate gate, walk-forward test 2R net-R gate, and final-holdout win-rate/R gates.

This independent SMC candidate family is explicitly aligned with an R-style objective, but it fails promotion and also fails the requested 70% win-rate / 2R objective gates.

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

### Locked 70% Win-Rate / 2R Hard Gate

Source: `reports/nq_expanded_high_edge_walk_forward_70wr_2r_hard_gate_2026-05-02.json`

The walk-forward runner now supports explicit train/test win-rate gates, so the 70% target is enforced during selection rather than checked only after the fact. A locked run was executed with `--take-profit-r-grid 2.0`, `--min-train-win-rate 0.70`, and `--min-test-win-rate 0.70`.

- Data scope: `data/bars/1m/NQ_CME`, 2019-01-01 to 2026-04-27.
- Selection method: rolling train years, next-year out-of-sample replay.
- Candidate groups checked: 26,148.
- Train combo attempts: 144.
- Fold result: 6 of 6 folds had `no_train_combo`.
- Gate result: failed; no strategy survived the training-side 70% win-rate / 2R hard gate.

This is stricter than the prior locked 2R report. It shows the current expanded-high-edge candidate universe cannot even produce a train-selected 70% win-rate / 2R candidate under the rolling protocol, so there is no out-of-sample candidate to promote.

### Standalone MBP-1 Microstructure 2R Search

Source: `reports/nq_mbp1_microstructure_2r_search_2026-05-03.json`

The provided two-month Databento MBP-1 quote window was also searched directly as a standalone microstructure strategy source, instead of only using it to replay bar-selected trades.

- Data scope: 52 normalized quote days, 3,488,665 one-second top-of-book snapshots from 2026-03-03 to 2026-05-01.
- Selection method: chronological first half train, second half holdout.
- Execution model: top-of-book bid/ask entries and exits with fixed 2R brackets.
- Fast-grid specs evaluated: 64.
- Best train-ranked spec: short momentum continuation, 48-tick stop, 96-tick target.
- Train result: 351 trades, 37.04% win rate, net PnL $2,540.00.
- Holdout result: 477 trades, 29.98% win rate, net PnL -$23,020.00.
- Gate result: failed; no train-selected standalone MBP-1 2R strategy passed the 70% win-rate, trade-count, and positive-PnL holdout gates.

Reversal extension: `reports/nq_mbp1_microstructure_2r_reversal_search_2026-05-03.json`.

- Added mean-reversion/reversal modes to the same fixed 2R quote strategy search.
- Best train-ranked reversal spec: short reversal, 48-tick stop, 96-tick target.
- Train result: 365 trades, 41.37% win rate, net PnL $13,430.00.
- Holdout result: 627 trades, 30.94% win rate, net PnL -$29,545.00.
- Gate result: failed; reversal improves train PnL but fails the holdout win-rate and profitability gates.

Medium-grid candidate-index search: `reports/nq_mbp1_microstructure_2r_medium_candidate_index_search_2026-05-03.json`.

- Added cached one-second MBP-1 snapshots to avoid repeatedly scanning the full 505,942,666 normalized quote rows.
- Added candidate-index replay to avoid scanning every one-second row for every spec.
- Evaluated the full 1,152-spec medium grid across continuation and reversal modes.
- Best train-ranked spec: short continuation, aligned imbalance >= 0.25, 48-tick stop, 96-tick target.
- Train result: 110 trades, 47.27% win rate, net PnL $7,585.00.
- Holdout result: 320 trades, 31.88% win rate, net PnL -$11,035.00.
- Gate result: failed; still far below the requested 70% win-rate and positive holdout gates.

This closes a separate tick-data path: direct MBP-1 continuation, reversal, and stricter medium-grid microstructure mining also do not produce the requested 70%/2R candidate.

### Relaxed 55% Win-Rate / 1.5R Fallback Gate

Source: `reports/55wr_15r_objective_completion_audit_2026-05-03.json`

After the user allowed relaxing the objective to 55%+ win rate and 1.5R+, the expanded-high-edge walk-forward runner was rerun with `--take-profit-r-grid 1.5`, `--min-train-win-rate 0.55`, and `--min-test-win-rate 0.55`.

- Stable profile: failed; 3 test folds had candidates, OOS net PnL -186,572.50, minimum test-year win rate 47.15%, and no test year passed the 55% win-rate gate.
- Net profile: failed; 2 test folds had candidates, OOS net PnL -267,310.00, minimum test-year win rate 44.46%, and no test year passed the 55% win-rate gate.
- Floor profile: failed; 0 test folds had candidates because every fold returned `no_train_combo`.
- VOL fallback audit: failed; best VOL prescreen win probability was 40.67%, and no VOL strategy met the combined 55% win-rate / 1.5R / quote-paper readiness gates.
- SMC fallback audit: failed; full-history win rate was 0.00%, p75 net-R was negative, and walk-forward/final-holdout relaxed gates failed.

Related low-R high-frequency probe: `experiments/profit_mining/low_r_high_frequency_15r_forced_baseline_2019_2026.json`.

- This forced the known profitable low-R high-frequency basket to a 1.5R cap and kept its prior baseline parameters.
- Full-sample result: net PnL $3,418,345.00, profit factor 1.191, 44,328 trades, 7 of 8 positive years.
- Full-sample win rate: 50.85%, below the relaxed 55% threshold.
- It is not walk-forward-selected and uses `max_concurrent_positions=99`, so it is not live-ready even aside from the win-rate miss.

Low-R subset search: `reports/low_r_15r_subset_search_55wr_wide_2026-05-03.json`.

- Forced 1.5R on the 19-edge low-R basket, enumerated subsets up to 10 edges by approximate single-edge statistics, then exact-replayed the top 80 subsets.
- Passing subsets: 0.
- Best exact subset: edge indexes `[5, 6, 15]`, net PnL $888,047.50, profit factor 1.310, 7,542 trades.
- Best exact subset win rate: 53.53%, still below 55%.
- Best exact subset positive years: 6 of 8, so it also fails long-term stability.

Low-R subset walk-forward search: `reports/low_r_15r_subset_walk_forward_55wr_2026-05-03.json`.

- Forced 1.5R and selected subsets only on train years before exact-replaying the next unseen test year.
- OOS total: 4,736 trades, net PnL $271,807.50, aggregate win rate 49.16%.
- Passing test years: 2021 and 2022 only.
- Failed test years: 2023, 2024, 2025, and 2026.
- Minimum test-year win rate: 41.35%, below the 55% fallback threshold.
- 2025 also failed the trade-floor gate with 232 trades and 271 annualized trades.

Tick microstructure filter audit: `reports/nq_tick_microstructure_filter_audit_55wr_15r_2026-05-03.json`.

- Analyzed the 2-month Databento MBP-1 quote replay window only, using 128 trades with `take_profit_r >= 1.5`.
- Candidate filters used entry-time observable quote features only: spread, top-level depth, aligned imbalance, and pre-entry mid moves.
- Future adverse selection and quote-arrival latency were diagnostic-only and excluded from selection.
- No train-selected filter passed both train and holdout gates with at least 30 trades.
- The best train-ranked rule had 69.23% train win rate and 61.11% holdout win rate, but only 13 train trades and 18 holdout trades, below the trade-count floor.
- Baseline no-filter holdout was profitable with 60.94% win rate, but the train half was 53.13%, so the 2-month tick window does not establish a robust train-selected strategy.

The fallback remains below the acceptance threshold. It produces no candidate eligible for quote replay, paper shadow, or live promotion.

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

### VOL Execution-Aware Strategy Family

Source: `reports/nq_vol_execution_70wr_2r_objective_audit_2026-05-02.json`

The generated VOL execution-aware strategy set was audited separately so the recent two-month tick requirement is not only attached to the expanded-high-edge candidate.

- Strategy specs audited: 50.
- VOL leaderboard rows audited: 50.
- Strategies with at least 70% prescreen win probability and positive net PnL: 0.
- Strategy files with a 2R or higher fixed/grammar reward profile: 0.
- VOL final-target rows: 0.
- Tick window coverage gate: passed, 52 of 52 MBP-1 quote days analyzed from 2026-03-03 to 2026-05-01.
- VOL quote replay artifact: blocked.
- VOL paper shadow artifact: blocked.

This confirms the recent tick data coverage exists, but VOL is still not a qualifying 70% win-rate / 2R strategy family.

### Cached Walk-Forward Grid Check

Source: `reports/nq_expanded_high_edge_cached_walk_forward_grid_small_2026-05-02.json`

A cached walk-forward grid runner was added so feature tables, signal tables, and single-edge train replays can be reused across profile/cap/slippage sweeps. A six-config grid over stress/floor profiles and 1x/2x/3x slippage was run with scan-family caps to avoid repeating the prior over-concentration failure mode.

- Best cached-grid row: failed; 1x slippage, floor profile, positive years 5 of 6.
- Failed positive year: 2022.
- Failed trade-floor years: 2024 and 2025.
- OOS total net PnL: 125,533.75.
- OOS minimum year PnL: -60,235.00.
- OOS trades: 8,333.

The cached grid improves experiment throughput and makes future sweeps less redundant, but the best row still fails the non-overfit promotion gate and is not a 70% win-rate / 2R strategy.

### Historical Cooldown Search

Source files:

- `reports/nq_expanded_high_edge_cooldown_search_exact_2026-05-02.json`
- `reports/nq_expanded_high_edge_cooldown_search_scan_2026-05-02.json`

Same-regime cooldown windows were evaluated on the locked expanded-high-edge walk-forward selections before any larger IBKR paper exposure. This varies only the execution suppression window after the original train-year selection; it does not refit edge selection on test years.

- Exact regime cooldown best: 0 minutes; passed original non-2R walk-forward gates; OOS net PnL 285,980.00.
- Exact 5-minute cooldown: higher OOS net PnL 368,101.25, but failed 2024 trade floor.
- Exact 15-minute and longer cooldowns: failed trade-frequency gates and eventually failed profitability years.
- Scan-only cooldown best: 0 minutes; passed original non-2R walk-forward gates.
- Scan-only 5-minute cooldown: OOS net PnL 327,026.25, but failed 2024 and 2026 trade floors.

Conclusion: the runtime 300-second same-regime cooldown is useful as an operational duplicate-signal guard, but historical replay does not support using cooldown as a promotion improvement. It also does not address the 70% win-rate or 2R requirements.

## Black-Box Test Result

Black-box execution replay is now available for the provided recent MBP-1 zip, but only for the two-month Databento window.

Source: `data/raw/databento/GLBX-20260502-QG6TRKVV9Q.zip`

Normalized quote output:

- Output root: `data/normalized/quotes/NQ_CME`
- Date coverage: 2026-03-03 to 2026-05-01.
- Databento MBP-1 members processed: 52.
- Normalized quote rows: 505,942,666.

Execution replay source: `reports/nq_expanded_high_edge_execution_stress_mbp1_full_2026-05-02.json`

Full-window quote coverage audit: `reports/nq_expanded_high_edge_mbp1_quote_coverage_audit_2026-05-02.json`

- Strategy window replayed: 2026-03-03 to 2026-05-01.
- Expected Databento MBP-1 quote days from zip: 52.
- Analyzed normalized quote days: 52.
- Missing or zero-row quote days: 0.
- Daily quote rows analyzed: 505,942,666.
- Strategy trade days inside tick window: 16.
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

This removes the prior "no quote/tick files" blocker for the recent Databento window, and the full available two-month MBP-1 quote dataset has now been audited day by day. It still does not create a live-ready 70% win-rate, 2R strategy. The strict 2R walk-forward evidence already fails, and the recent MBP-1 replay validates only the 159 strategy trades that occurred inside the 2026 tick window, not the full 2019-2026 out-of-sample history.

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
