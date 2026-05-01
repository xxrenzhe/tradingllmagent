# nq_smc_lqem_ce_v1 SMC Validation Report

- Symbol: `NQ_CME`
- Window: `2010-06-06` to `2026-04-27`
- Data coverage: 4930/5805 files
- Full-history trades: 4
- Full-history net PnL: -760.00
- Full-history profit factor: 0.0000
- Full-history max drawdown: 760.00

## Promotion Gates

- minimum_trade_count: FAIL (actual=4, threshold=200)
- positive_expectancy_after_2x_cost: FAIL (actual=-200.0, threshold=> 0)
- final_holdout_profit_factor: FAIL (actual=None, threshold=> 1.05)
- yearly_profit_concentration: PASS (actual=0.0, threshold=<= 0.40)

## Cost Stress

- 1.0x slippage: trades=4, net_pnl=-760.00, avg_trade=-190.0000
- 2.0x slippage: trades=4, net_pnl=-800.00, avg_trade=-200.0000
- 3.0x slippage: trades=4, net_pnl=-840.00, avg_trade=-210.0000

## Walk Forward

- Status: ok
- Fold count: 48

## Audited Samples

- Winner 2019-03-29T17:31:00 short net=-105.00 r=-1.1667
- Winner 2020-11-12T18:31:00 long net=-125.00 r=-1.1364
- Winner 2018-03-26T17:32:00 short net=-235.00 r=-1.0682
- Winner 2022-03-11T20:02:00 long net=-295.00 r=-1.0536
- Loser 2022-03-11T20:02:00 long net=-295.00 r=-1.0536
- Loser 2018-03-26T17:32:00 short net=-235.00 r=-1.0682
- Loser 2020-11-12T18:31:00 long net=-125.00 r=-1.1364
- Loser 2019-03-29T17:31:00 short net=-105.00 r=-1.1667
