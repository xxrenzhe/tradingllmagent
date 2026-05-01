# nq_smc_lqem_ce_balanced_r2_20260501 SMC Validation Report

- Symbol: `NQ_CME`
- Window: `2010-06-06` to `2026-04-27`
- Data coverage: 4930/5805 files
- Full-history trades: 10
- Full-history net PnL: -200.00
- Full-history profit factor: 0.8326
- Full-history max drawdown: 605.00

## Promotion Gates

- minimum_trade_count: FAIL (actual=10, threshold=200)
- positive_expectancy_after_2x_cost: FAIL (actual=-30.0, threshold=> 0)
- final_holdout_profit_factor: FAIL (actual=0.0, threshold=> 1.05)
- yearly_profit_concentration: FAIL (actual=0.8701298701298701, threshold=<= 0.40)

## Cost Stress

- 1.0x slippage: trades=10, net_pnl=-200.00, avg_trade=-20.0000
- 2.0x slippage: trades=10, net_pnl=-300.00, avg_trade=-30.0000
- 3.0x slippage: trades=10, net_pnl=-400.00, avg_trade=-40.0000

## Walk Forward

- Status: ok
- Fold count: 48

## Audited Samples

- Winner 2024-10-28T18:38:00 short net=775.00 r=1.8452
- Winner 2020-05-20T17:34:00 long net=220.00 r=2.3158
- Winner 2015-09-08T17:31:00 long net=-50.00 r=-1.4286
- Winner 2014-05-20T17:31:00 long net=-60.00 r=-1.3333
- Winner 2019-03-29T17:31:00 short net=-100.00 r=-1.1765
- Loser 2025-06-16T17:35:00 short net=-370.00 r=-1.0423
- Loser 2023-11-16T15:50:00 long net=-235.00 r=-1.0682
- Loser 2015-09-02T15:07:00 short net=-155.00 r=-1.1071
- Loser 2020-11-12T18:31:00 long net=-120.00 r=-1.1429
- Loser 2024-05-20T17:49:00 long net=-105.00 r=-1.1667
