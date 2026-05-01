# nq_smc_lqem_ce_relaxed_sweeps_20260501 SMC Validation Report

- Symbol: `NQ_CME`
- Window: `2010-06-06` to `2026-04-27`
- Data coverage: 4930/5805 files
- Full-history trades: 30
- Full-history net PnL: -2945.00
- Full-history profit factor: 0.3527
- Full-history max drawdown: 3045.00

## Promotion Gates

- minimum_trade_count: FAIL (actual=30, threshold=200)
- positive_expectancy_after_2x_cost: FAIL (actual=-108.16666666666667, threshold=> 0)
- final_holdout_profit_factor: FAIL (actual=0.0, threshold=> 1.05)
- yearly_profit_concentration: FAIL (actual=0.5495495495495496, threshold=<= 0.40)

## Cost Stress

- 1.0x slippage: trades=30, net_pnl=-2945.00, avg_trade=-98.1667
- 2.0x slippage: trades=30, net_pnl=-3245.00, avg_trade=-108.1667
- 3.0x slippage: trades=30, net_pnl=-3545.00, avg_trade=-118.1667

## Walk Forward

- Status: ok
- Fold count: 48

## Audited Samples

- Winner 2019-06-21T13:43:00 long net=595.00 r=1.9508
- Winner 2019-02-13T20:09:00 short net=295.00 r=1.9032
- Winner 2019-03-08T18:32:00 long net=295.00 r=1.9032
- Winner 2010-07-23T17:43:00 long net=125.00 r=0.4902
- Winner 2012-03-15T15:09:00 long net=85.00 r=1.7000
- Loser 2015-08-27T17:35:00 long net=-595.00 r=-1.0259
- Loser 2020-04-27T17:31:00 short net=-585.00 r=-1.0263
- Loser 2026-04-09T13:48:00 long net=-530.00 r=-1.0291
- Loser 2019-08-06T17:31:00 short net=-440.00 r=-1.0353
- Loser 2019-08-12T14:15:00 short net=-345.00 r=-1.0455
