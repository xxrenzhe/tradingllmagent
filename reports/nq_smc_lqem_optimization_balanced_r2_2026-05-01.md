# nq_smc_lqem_ce_balanced_r2_20260501 SMC Validation Report

- Symbol: `NQ_CME`
- Window: `2010-06-06` to `2026-04-27`
- Data coverage: 4930/5805 files
- Full-history trades: 35
- Full-history net PnL: -410.00
- Full-history profit factor: 0.9385
- Full-history max drawdown: 2905.00

## Promotion Gates

- minimum_trade_count: FAIL (actual=35, threshold=200)
- positive_expectancy_after_2x_cost: FAIL (actual=-21.714285714285715, threshold=> 0)
- final_holdout_profit_factor: FAIL (actual=0.9285714285714286, threshold=> 1.05)
- yearly_profit_concentration: PASS (actual=0.33189655172413796, threshold=<= 0.40)

## Cost Stress

- 1.0x slippage: trades=35, net_pnl=-410.00, avg_trade=-11.7143
- 2.0x slippage: trades=35, net_pnl=-760.00, avg_trade=-21.7143
- 3.0x slippage: trades=35, net_pnl=-1110.00, avg_trade=-31.7143

## Walk Forward

- Status: ok
- Fold count: 48

## Audited Samples

- Winner 2024-02-15T14:01:00 short net=1185.00 r=2.4688
- Winner 2025-08-28T14:04:00 long net=845.00 r=2.4493
- Winner 2014-10-14T14:18:00 long net=770.00 r=2.4444
- Winner 2022-02-08T14:53:00 short net=760.00 r=2.4516
- Winner 2016-01-21T14:59:00 long net=560.00 r=2.4348
- Loser 2022-10-27T13:36:00 long net=-505.00 r=-1.0306
- Loser 2024-07-30T13:43:00 long net=-460.00 r=-1.0337
- Loser 2023-11-24T14:59:00 long net=-435.00 r=-1.0357
- Loser 2021-02-01T13:31:00 long net=-400.00 r=-1.0390
- Loser 2025-07-21T13:36:00 short net=-365.00 r=-1.0429
