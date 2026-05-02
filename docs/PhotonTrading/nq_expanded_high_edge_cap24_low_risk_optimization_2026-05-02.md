# NQ expanded_high_edge_cap24 Low-Risk Optimization

Date: 2026-05-02

## Conclusion

目标应改为保留 `expanded_high_edge_cap24` 的 32 条高收益边缘，不再强行压回 `walk_forward_orb_2026_min13`。

本轮最可执行的优化不是删掉 `prior_day_breakout`，而是调整 cap24 的执行风险参数：

| Preset | Use Case | Net PnL | Max Drawdown | PnL/DD | Trade Count | Decision |
|---|---|---:|---:|---:|---:|---|
| `expanded_high_edge_cap24` | Current high-return baseline | 5,371,751.25 | 397,986.25 | 13.50 | 33,238 | Baseline |
| `expanded_high_edge_cap24_return_guard` | Keep high return, reduce risk slightly | 5,475,715.00 | 369,783.75 | 14.81 | 30,983 | Recommended first paper test |
| `expanded_high_edge_cap24_balanced_risk` | Cut drawdown materially | 4,511,341.25 | 298,258.75 | 15.13 | 25,739 | Use if drawdown is the priority |

`expanded_high_edge_cap24_return_guard` is the best answer to “保持 cap24 高收益基础上降低风险”：net PnL is higher than current cap24 by 103,963.75, max drawdown is lower by 28,202.50, and all checked years remain positive with >1,000 annual trades.

## Implemented Presets

### `expanded_high_edge_cap24_return_guard`

Execution parameters:

| Parameter | Value |
|---|---:|
| Edge basket | Same 32 cap24 edges |
| Max concurrent positions | 24 |
| Max hold minutes | 300 |
| Stop range multiple | 8.0 |
| Min stop points | 8.0 |
| Max stop points | 90.0 |

Yearly replay:

| Year | Trades | Net PnL | Profit Factor | Max Drawdown |
|---|---:|---:|---:|---:|
| 2019 | 3,398 | 146,065.00 | 1.1758 | 99,732.50 |
| 2020 | 4,079 | 1,004,995.00 | 1.4795 | 290,465.00 |
| 2021 | 3,870 | 1,064,108.75 | 1.5758 | 156,230.00 |
| 2022 | 4,488 | 1,066,186.25 | 1.3326 | 242,837.50 |
| 2023 | 3,920 | 790,127.50 | 1.3768 | 137,511.25 |
| 2024 | 4,338 | 739,100.00 | 1.2645 | 217,190.00 |
| 2025 | 5,117 | 662,960.00 | 1.1710 | 271,625.00 |
| 2026 | 1,773 | 2,172.50 | 1.0014 | 317,577.50 |

### `expanded_high_edge_cap24_balanced_risk`

Execution parameters:

| Parameter | Value |
|---|---:|
| Edge basket | Same 32 cap24 edges |
| Max concurrent positions | 18 |
| Max hold minutes | 300 |
| Stop range multiple | 8.0 |
| Min stop points | 8.0 |
| Max stop points | 90.0 |

Yearly replay:

| Year | Trades | Net PnL | Profit Factor | Max Drawdown |
|---|---:|---:|---:|---:|
| 2019 | 2,816 | 112,318.75 | 1.1610 | 84,047.50 |
| 2020 | 3,376 | 787,437.50 | 1.4479 | 240,635.00 |
| 2021 | 3,182 | 849,276.25 | 1.5527 | 126,140.00 |
| 2022 | 3,705 | 838,161.25 | 1.3143 | 185,472.50 |
| 2023 | 3,189 | 672,855.00 | 1.3927 | 108,973.75 |
| 2024 | 3,583 | 659,385.00 | 1.2866 | 191,295.00 |
| 2025 | 4,396 | 542,520.00 | 1.1600 | 217,270.00 |
| 2026 | 1,492 | 49,387.50 | 1.0384 | 257,240.00 |

## Risk Notes

The optimized presets are based on bar-level replay. They improve 1x modeled risk-return, but they should not be treated as live-ready until quote/tick replay is available.

Important caveat: the current cap24 baseline passes 1x/2x/3x bar-level cost stress, while the new optimized variants improve 1x drawdown but are more sensitive in 2019 and 2026 under stricter cost assumptions. That means the correct implementation path is paper validation first, not immediate live promotion.

## Deployment

For paper trading validation, set:

```bash
export TLM_IBKR_STRATEGY_FAMILY=expanded_high_edge
export TLM_IBKR_EXPANDED_HIGH_EDGE_PRESET=expanded_high_edge_cap24_return_guard
```

If drawdown reduction is more important than absolute return, use:

```bash
export TLM_IBKR_EXPANDED_HIGH_EDGE_PRESET=expanded_high_edge_cap24_balanced_risk
```

## Acceptance Gates Before Promotion

| Gate | Requirement |
|---|---|
| Paper duration | At least 2 market weeks |
| Slippage | Compare modeled entry/exit with IBKR fills |
| Frequency | 2026 annualized trade rate remains >1,000 |
| Daily loss | Stop auto-submit if daily realized + unrealized PnL breaches account risk cap |
| Quote replay | Must run after normalized quote/tick files are available |

## Recommendation

Start with `expanded_high_edge_cap24_return_guard` in IBKR paper. It preserves the cap24 high-return profile and improves modeled 1x risk-return without changing the signal basket. Keep `expanded_high_edge_cap24_balanced_risk` as the fallback if paper drawdown or concurrent exposure remains too high.
