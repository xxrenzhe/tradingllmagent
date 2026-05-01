# nq_smc_lqem_ce_strict_small_stop_20260501 SMC Validation Report

- Symbol: `NQ_CME`
- Window: `2010-06-06` to `2026-04-27`
- Data coverage: 4930/5805 files
- Full-history trades: 12
- Full-history net PnL: -195.00
- Full-history profit factor: 0.8219
- Full-history max drawdown: 655.00

## Promotion Gates

- minimum_trade_count: FAIL (actual=12, threshold=200)
- positive_expectancy_after_2x_cost: FAIL (actual=-26.25, threshold=> 0)
- final_holdout_profit_factor: FAIL (actual=None, threshold=> 1.05)
- yearly_profit_concentration: FAIL (actual=1.0, threshold=<= 0.40)

## Cost Stress

- 1.0x slippage: trades=12, net_pnl=-195.00, avg_trade=-16.2500
- 2.0x slippage: trades=12, net_pnl=-315.00, avg_trade=-26.2500
- 3.0x slippage: trades=12, net_pnl=-435.00, avg_trade=-36.2500

## Walk Forward

- Status: ok
- Fold count: 48

## Audited Samples

- Winner 2019-02-13T20:09:00 short net=450.00 r=2.9032
- Winner 2019-03-08T18:32:00 long net=450.00 r=2.9032
- Winner 2011-10-31T15:04:00 long net=-55.00 r=-1.3750
- Winner 2014-05-20T17:31:00 long net=-55.00 r=-1.3750
- Winner 2018-06-14T18:26:00 long net=-55.00 r=-1.3750
- Loser 2015-02-26T18:32:00 long net=-255.00 r=-1.0625
- Loser 2020-05-22T14:38:00 short net=-175.00 r=-1.0938
- Loser 2022-03-24T17:31:00 short net=-170.00 r=-1.0968
- Loser 2019-03-29T17:31:00 short net=-95.00 r=-1.1875
- Loser 2015-01-23T19:31:00 long net=-85.00 r=-1.2143
