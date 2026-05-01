# NQ Hybrid Strategy Optimization Report

Date: 2026-05-02

## Executive Decision

当前不建议把 `walk_forward_orb_2026_min13` 和 `expanded_high_edge_cap24` 直接混合上线。

实测结果显示，`expanded_high_edge_cap24` 的高收益主要来自 `prior_day_breakout` 暴露和更高并发，但这部分在 rolling out-of-sample 中不稳定。强制加入 1-2 条 `prior_day_breakout` 后，交易频次可以达标，但至少一个 OOS 年份转负；这不满足“收益更高、风险更低”的上线标准。

当前可部署基准仍应保持为 `walk_forward_orb_2026_min13`：它不是净收益最高的历史方案，但它是本轮实验中唯一同时通过 rolling OOS 全年为正和交易频次门槛的策略。

## Tested Variants

| Variant | OOS Net PnL | OOS Positive Years | Trade Floor | Worst OOS Year | Total Trades | Decision |
|---|---:|---:|---:|---:|---:|---|
| `walk_forward_orb_2026_min13` | 285,980.00 | 6/6 | 6/6 | 3,623.75 | 11,070 | Keep as deployable baseline |
| `hybrid_prior_cap1_min13` | 534,953.75 | 5/6 | 6/6 | -35,098.75 | 11,716 | Reject, 2021 negative |
| `hybrid_prior_cap1_fasttp_min13` | 251,200.00 | 5/6 | 6/6 | -377.50 | 9,651 | Reject, 2024 negative |
| `hybrid_prior_cap2_fasttp_min13` | 362,871.25 | 5/6 | 6/6 | -60,315.00 | 10,620 | Reject, 2021 negative |
| `hybrid_stable_prior_cap1_fasttp_min13` | -399,372.50 | 2/6 | 5/6 | -199,485.00 | 12,203 | Reject |
| `hybrid_stress_prior_cap1_fasttp_min13` | -363,745.00 | 3/6 | 6/6 | -158,660.00 | 13,420 | Reject |
| `orb_floor_fasttp_min13` | -61,598.75 | 3/6 | 6/6 | -219,835.00 | 20,438 | Reject |
| `orb_fullgrid_fasttp_min13` | 934,575.00 | 3/6 | 4/6 | -277,973.75 | 14,235 | Reject, overfit-like profile |

## Interpretation

`hybrid_prior_cap2_fasttp_min13` is the closest high-return hybrid. It improves total OOS net PnL versus the baseline, but it fails the risk requirement because 2021 loses 60,315. This is not a small noise-level miss; it means the extra `prior_day_breakout` exposure can dominate the ORB core during unfavorable regimes.

`orb_fullgrid_fasttp_min13` produces the highest aggregate net PnL, but it fails three OOS years and misses the trade floor in 2024 and 2025. This is a classic optimization trap: the total is driven by a few strong years, while regime reliability deteriorates.

The useful conclusion is not “prior-day is useless.” The conclusion is narrower: `prior_day_breakout` cannot be forced into the production basket by count alone. It needs a separate replay guard, regime activation rule, and execution stress validation before it can become an overlay.

## Implementation Changes

`scripts/walk_forward_expanded_high_edge.py` now supports constrained hybrid search:

| Parameter | Purpose |
|---|---|
| `--max-scan-type-count prior_day_breakout=N` | Caps how many edges from a signal family can enter a selected basket |
| `--min-scan-type-count prior_day_breakout=N` | Forces a minimum number of edges from a signal family for controlled hybrid tests |
| `--take-profit-r-grid 1.25,1.5` | Narrows TP search for faster screening while preserving default behavior when omitted |

These parameters make future hybrid sweeps reproducible without changing previous defaults.

## Recommended Production Path

Keep `walk_forward_orb_2026_min13` as the production candidate until a hybrid passes all gates:

| Gate | Requirement |
|---|---|
| Rolling OOS profitability | 2021, 2022, 2023, 2024, 2025, and annualized 2026 all net positive |
| Frequency | Each full year > 1,000 trades; 2026 annualized > 1,000 trades |
| Cost stress | 1x passes; 2x should keep all years positive; 3x failure must be explicitly documented |
| Drawdown | No OOS year may improve total PnL by taking materially worse drawdown than the baseline |
| Execution | Quote-level spread, latency, partial-fill, and slippage replay must pass before IBKR promotion |

## BD Execution Breakdown

Tracked follow-up work:

| Bead | Purpose | Acceptance |
|---|---|---|
| `tradingllmagent-b3se` | Add cached walk-forward hybrid optimizer | Run cap/profile grid from one command and emit a JSON leaderboard |
| `tradingllmagent-p2lp` | Validate prior-day overlay with replay guard | Compare guarded overlay versus baseline at 1x/2x/3x costs with all OOS years positive |
| `tradingllmagent-l5zx` | Add quote-level execution stress replay | Replay spread, latency, partial-fill, and adverse excursion scenarios before IBKR promotion |

## Current Recommendation

Do not replace `walk_forward_orb_2026_min13` with `expanded_high_edge_cap24` or the tested hybrids.

The best optimization path is to keep the ORB core as the primary strategy and treat `prior_day_breakout` as a separately gated overlay. A hybrid should only be promoted if it beats 285,980 OOS net PnL while preserving 6/6 positive OOS years, 6/6 frequency pass, and acceptable 2x execution-cost behavior.
