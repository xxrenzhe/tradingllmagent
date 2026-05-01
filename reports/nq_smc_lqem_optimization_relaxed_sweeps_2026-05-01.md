# nq_smc_lqem_ce_relaxed_sweeps_20260501 SMC Validation Report

- Symbol: `NQ_CME`
- Window: `2010-06-06` to `2026-04-27`
- Data coverage: 4930/5805 files
- Full-history trades: 58
- Full-history net PnL: -2220.00
- Full-history profit factor: 0.7819
- Full-history max drawdown: 4770.00

## Promotion Gates

- minimum_trade_count: FAIL (actual=58, threshold=200)
- positive_expectancy_after_2x_cost: FAIL (actual=-48.275862068965516, threshold=> 0)
- final_holdout_profit_factor: FAIL (actual=0.7015384615384616, threshold=> 1.05)
- yearly_profit_concentration: FAIL (actual=0.46564885496183206, threshold=<= 0.40)

## Cost Stress

- 1.0x slippage: trades=58, net_pnl=-2220.00, avg_trade=-38.2759
- 2.0x slippage: trades=58, net_pnl=-2800.00, avg_trade=-48.2759
- 3.0x slippage: trades=58, net_pnl=-3380.00, avg_trade=-58.2759

## Walk Forward

- Status: ok
- Fold count: 48

## Audited Samples

- Winner 2021-11-12T15:18:00 long net=850.00 r=1.5741
- Winner 2025-08-28T14:04:00 long net=665.00 r=1.9559
- Winner 2024-03-26T13:35:00 short net=655.00 r=1.9552
- Winner 2014-10-17T14:00:00 long net=615.00 r=1.9524
- Winner 2014-10-14T14:18:00 long net=605.00 r=1.9516
- Loser 2021-08-03T13:44:00 long net=-595.00 r=-1.0259
- Loser 2024-05-16T14:05:00 short net=-565.00 r=-1.0273
- Loser 2018-10-05T13:31:00 short net=-560.00 r=-1.0275
- Loser 2025-07-21T13:31:00 short net=-550.00 r=-1.0280
- Loser 2024-10-11T13:32:00 long net=-545.00 r=-1.0283
