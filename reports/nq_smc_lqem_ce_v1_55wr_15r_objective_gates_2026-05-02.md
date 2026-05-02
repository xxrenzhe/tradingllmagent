# nq_smc_lqem_ce_v1 SMC Validation Report

- Symbol: `NQ_CME`
- Window: `2019-01-01` to `2026-04-27`
- Data coverage: 2280/2674 files
- Full-history trades: 3
- Full-history net PnL: -525.00
- Full-history profit factor: 0.0000
- Full-history max drawdown: 525.00

## Promotion Gates

- minimum_trade_count: PASS (actual=3, threshold=1)
- positive_expectancy_after_2x_cost: FAIL (actual=-185.0, threshold=> 0)
- final_holdout_profit_factor: FAIL (actual=None, threshold=> 1.05)
- yearly_profit_concentration: PASS (actual=0.0, threshold=<= 0.40)

## Objective Gates

- minimum_trade_count: PASS (actual=3, threshold=1)
- full_history_win_rate_ge_target: FAIL (actual=0.0, threshold=>= 0.55)
- full_history_net_r_p75_ge_target: FAIL (actual=-1.0949675324675325, threshold=>= 1.5)
- walk_forward_test_win_rate_ge_target: FAIL (actual=0.0, threshold=>= 0.55 on every test fold)
- walk_forward_test_net_r_p75_ge_target: FAIL (actual=-1.0535714285714286, threshold=>= 1.5 on every test fold)
- final_holdout_win_rate_ge_target: FAIL (actual=None, threshold=>= 0.55)
- final_holdout_net_r_p75_ge_target: FAIL (actual=None, threshold=>= 1.5)

## Cost Stress

- 1.0x slippage: trades=3, net_pnl=-525.00, avg_trade=-175.0000
- 2.0x slippage: trades=3, net_pnl=-555.00, avg_trade=-185.0000
- 3.0x slippage: trades=3, net_pnl=-585.00, avg_trade=-195.0000

## Walk Forward

- Status: ok
- Fold count: 14

## Audited Samples

- Winner 2019-03-29T17:31:00 short net=-105.00 r=-1.1667
- Winner 2020-11-12T18:31:00 long net=-125.00 r=-1.1364
- Winner 2022-03-11T20:02:00 long net=-295.00 r=-1.0536
- Loser 2022-03-11T20:02:00 long net=-295.00 r=-1.0536
- Loser 2020-11-12T18:31:00 long net=-125.00 r=-1.1364
- Loser 2019-03-29T17:31:00 short net=-105.00 r=-1.1667
