# nq_smc_lqem_ce_strict_small_stop_20260501 SMC Validation Report

- Symbol: `NQ_CME`
- Window: `2010-06-06` to `2026-04-27`
- Data coverage: 4930/5805 files
- Full-history trades: 40
- Full-history net PnL: -410.00
- Full-history profit factor: 0.9250
- Full-history max drawdown: 1975.00

## Promotion Gates

- minimum_trade_count: FAIL (actual=40, threshold=200)
- positive_expectancy_after_2x_cost: FAIL (actual=-20.25, threshold=> 0)
- final_holdout_profit_factor: FAIL (actual=0.0, threshold=> 1.05)
- yearly_profit_concentration: PASS (actual=0.32935560859188545, threshold=<= 0.40)

## Cost Stress

- 1.0x slippage: trades=40, net_pnl=-410.00, avg_trade=-10.2500
- 2.0x slippage: trades=40, net_pnl=-810.00, avg_trade=-20.2500
- 3.0x slippage: trades=40, net_pnl=-1210.00, avg_trade=-30.2500

## Walk Forward

- Status: ok
- Fold count: 48

## Audited Samples

- Winner 2021-05-17T14:50:00 short net=885.00 r=2.9500
- Winner 2014-10-14T14:29:00 long net=765.00 r=2.9423
- Winner 2018-06-18T14:13:00 long net=690.00 r=2.9362
- Winner 2016-01-21T14:59:00 long net=660.00 r=2.9333
- Winner 2023-11-27T14:55:00 long net=480.00 r=2.9091
- Loser 2023-07-07T13:33:00 long net=-315.00 r=-1.0500
- Loser 2023-07-26T14:11:00 short net=-315.00 r=-1.0500
- Loser 2025-07-21T13:42:00 short net=-310.00 r=-1.0508
- Loser 2025-07-24T15:13:00 short net=-305.00 r=-1.0517
- Loser 2023-11-24T14:53:00 long net=-285.00 r=-1.0556
