# NQ SMC LQ-EM CE Optimization Report

Decision: `reject_for_paper_trading`

After fixing session timezone handling, no tested parameter set passed minimum trade count, positive expectancy after 2x slippage, and final holdout profit-factor gates.

## Low Trade Count Recheck

- Root cause fixed: NY session windows are now applied after converting source UTC bars into `America/New_York` session time.
- Baseline funnel: 5,378,727 loaded 1-minute bars -> 951,662 signal-window bars -> 874 HTF POIs -> 868 PBL confirmations -> 11 OB-touching liquidity sweeps -> 7 LTF CHOCH limit signals -> 4 filled trades.
- Interpretation: the v1 rules are too restrictive versus discretionary PhotonTrading chart selection; low trade count is not explained by cost model alone.

## Results

- `nq_smc_lqem_ce_v1`: trades=4, net=-760.00, pf=0.0, 2x_avg=-200.0, holdout_pf=None
- `nq_smc_lqem_ce_relaxed_sweeps_20260501`: trades=30, net=-2945.00, pf=0.35274725274725277, 2x_avg=-108.16666666666667, holdout_pf=0.0
- `nq_smc_lqem_ce_balanced_r2_20260501`: trades=10, net=-200.00, pf=0.8326359832635983, 2x_avg=-30.0, holdout_pf=0.0
- `nq_smc_lqem_ce_strict_small_stop_20260501`: trades=12, net=-195.00, pf=0.821917808219178, 2x_avg=-26.25, holdout_pf=None

## Selected For Review

- `nq_smc_lqem_ce_balanced_r2_20260501` is the least-bad variant by holdout PF and 2x cost average, but remains rejected for paper trading.

## Next Change Families

- session/timezone alignment has been fixed; next review should verify chart-level setup parity
- target model using opposing HTF liquidity instead of fixed R only
- OB definition sensitivity and displacement filter ablation
- PBL/sweep definition ablation with quote replay fill checks
