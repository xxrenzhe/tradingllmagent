# Final Strategy Objective Gap Audit

Date: 2026-05-03

## Objective

Train an NQ/MNQ strategy that is ready for promotion only if all of these are true:

1. At least 70% after-cost win rate.
2. At least 2R reward profile.
3. Not overfit: train-only selection with locked holdout or walk-forward replay.
4. Long-term profitable across available historical `data/bars`.
5. Black-box tested with the provided Databento tick/MBP data at `data/raw/databento/GLBX-20260502-QG6TRKVV9Q.zip`.
6. Execution/cost stress passed.
7. Directly usable for paper/live promotion.

The user also allowed a fallback if the strict target could not be found:

1. At least 55% after-cost win rate.
2. At least 1.5R reward profile.
3. Same non-overfit, long-term, black-box, and live-readiness requirements.

## Prompt-To-Artifact Checklist

| Requirement | Evidence | Result |
| --- | --- | --- |
| Historical bars consumed | `reports/nq_low_frequency_bar_2r_search_2026-05-03.json`, `reports/nq_expanded_high_edge_walk_forward_2r_gate_2026-05-02.json`, `reports/low_r_15r_subset_walk_forward_55wr_2026-05-03.json` | Covered, but no qualifying strategy |
| Tick/MBP window consumed | `reports/nq_expanded_high_edge_mbp1_quote_coverage_audit_2026-05-02.json`, `reports/nq_mbp1_microstructure_2r_full_grid_candidate_index_search_2026-05-03.json`, `reports/nq_tick_derived_intraday_search_55wr_15r_2026-05-03.json` | Covered |
| 70% win-rate gate | `reports/70wr_2r_objective_completion_audit_2026-05-02.json` | Failed |
| 2R reward gate | `reports/70wr_2r_objective_completion_audit_2026-05-02.json` | Failed |
| Non-overfit train/test or walk-forward gate | `reports/70wr_2r_objective_completion_audit_2026-05-02.json` | Failed because no selected candidate survives gates |
| Long-term profitability gate | `reports/70wr_2r_objective_completion_audit_2026-05-02.json` | Failed |
| Execution/cost stress gate | `reports/70wr_2r_objective_completion_audit_2026-05-02.json` | Failed |
| Live-ready gate | `reports/70wr_2r_objective_completion_audit_2026-05-02.json` | Failed |
| 55% / 1.5R fallback gate | `reports/55wr_15r_objective_completion_audit_2026-05-03.json` | Failed |

## Key Findings

- The strict completion audit reports `achieved: false`; failed requirements are `win_rate_70pct`, `reward_2r`, `non_overfit_walk_forward`, `long_term_profitability`, `execution_cost_stress`, and `live_ready`.
- The relaxed completion audit reports `achieved: false`; failed requirements are `reward_1_5r`, `win_rate_55pct`, `non_overfit_walk_forward`, `long_term_profitability`, and `live_ready`.
- The recent MBP1 quote coverage audit passed: 52 quote days were analyzed and the available tick/quote window is no longer a data-availability blocker.
- Direct MBP1 2R searches failed. The full-grid candidate-index report used 3,488,665 one-second quote snapshots and found no train-selected 70% / 2R holdout candidate.
- Tick-derived intraday search failed. It covered 52 quote days and 60,413 minute quote bars; no train-selected 55% / 1.5R holdout candidate was found.
- Low-frequency bar search failed. It covered 4,068 regular-session daily bars from 2010-06-07 through 2026-04-27; strict 70% / 2R failed every OOS test year and fallback 55% / 1.5R failed most OOS years.
- The best relaxed low-R walk-forward evidence remains below threshold: 4,736 OOS trades, 49.16% win rate, and failed test years 2023-2026.

## Completion Decision

The objective is not achieved. There is no strategy artifact in the repository that satisfies the strict 70% win-rate / 2R / non-overfit / long-term profitable / black-box tested / live-ready requirements.

The relaxed 55% win-rate / 1.5R fallback is also not achieved. Several families are profitable in aggregate, but none pass all train-only walk-forward, win-rate, trade-floor, profitability, and promotion-readiness gates.

## Productive Next Input

Further local work is unlikely to be productive without changing at least one constraint or adding new evidence. The next useful input should be one of:

- More tick data covering multiple years, so black-box execution evidence can match the historical walk-forward period.
- Permission to lower the win-rate or R target further and optimize for expectancy, drawdown, and capacity instead.
- Permission to use a portfolio/ensemble objective instead of a single strategy meeting every yearly win-rate gate.
- A specific externally supplied strategy hypothesis to validate under the existing audit framework.

Until then, no IBKR paper/live hardening task should be treated as satisfying the original trading-strategy objective, because there is no qualifying candidate to promote.
