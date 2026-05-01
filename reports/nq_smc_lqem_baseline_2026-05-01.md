# nq_smc_lqem_ce_v1 SMC Validation Report

- Symbol: `NQ_CME`
- Window: `2010-06-06` to `2026-04-27`
- Data coverage: 4930/5805 files
- Full-history trades: 22
- Full-history net PnL: -2470.00
- Full-history profit factor: 0.4443
- Full-history max drawdown: 3160.00

## Promotion Gates

- minimum_trade_count: FAIL (actual=22, threshold=200)
- positive_expectancy_after_2x_cost: FAIL (actual=-122.27272727272727, threshold=> 0)
- final_holdout_profit_factor: FAIL (actual=0.0, threshold=> 1.05)
- yearly_profit_concentration: FAIL (actual=0.8414634146341463, threshold=<= 0.40)

## Cost Stress

- 1.0x slippage: trades=22, net_pnl=-2470.00, avg_trade=-112.2727
- 2.0x slippage: trades=22, net_pnl=-2690.00, avg_trade=-122.2727
- 3.0x slippage: trades=22, net_pnl=-2910.00, avg_trade=-132.2727

## Walk Forward

- Status: ok
- Fold count: 48

## Audited Samples

- Winner 2016-01-21T14:59:00 long net=690.00 r=2.9362
- Winner 2021-01-19T14:40:00 short net=685.00 r=1.8267
- Winner 2020-09-01T14:35:00 short net=360.00 r=2.8800
- Winner 2023-06-23T13:31:00 long net=240.00 r=2.8235
- Winner 2025-12-19T14:56:00 short net=-100.00 r=-1.1765
- Loser 2021-02-01T13:35:00 long net=-405.00 r=-1.0385
- Loser 2026-01-27T14:06:00 long net=-400.00 r=-1.0390
- Loser 2025-07-18T13:52:00 long net=-385.00 r=-1.0405
- Loser 2022-02-28T13:31:00 long net=-345.00 r=-1.0455
- Loser 2020-09-21T14:50:00 long net=-325.00 r=-1.0484
