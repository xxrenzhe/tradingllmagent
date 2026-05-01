# NQ SMC LQ-EM CE Optimization Report

Decision: `reject_for_paper_trading`

No tested parameter set passed minimum trade count, positive expectancy after 2x slippage, and final holdout profit factor gates.

## Results

- `nq_smc_lqem_ce_v1`: trades=22, net=-2470.00, pf=0.44431946006749157, 2x_avg=-122.27272727272727, holdout_pf=0.0
- `nq_smc_lqem_ce_relaxed_sweeps_20260501`: trades=58, net=-2220.00, pf=0.7819253438113949, 2x_avg=-48.275862068965516, holdout_pf=0.7015384615384616
- `nq_smc_lqem_ce_balanced_r2_20260501`: trades=35, net=-410.00, pf=0.9384846211552889, 2x_avg=-21.714285714285715, holdout_pf=0.9285714285714286
- `nq_smc_lqem_ce_strict_small_stop_20260501`: trades=40, net=-410.00, pf=0.9249771271729186, 2x_avg=-20.25, holdout_pf=0.0

## Selected For Review

- `nq_smc_lqem_ce_balanced_r2_20260501` is the least-bad variant by holdout PF, but remains rejected for paper trading.

## Next Change Families

- session/timezone alignment audit before more parameter search
- target model using opposing HTF liquidity instead of fixed R only
- OB definition sensitivity and displacement filter ablation
- PBL/sweep definition ablation with quote replay fill checks
