# 70% Win-Rate / 2R Objective Completion Audit

- Achieved: `False`
- Reason: At least one required objective gate remains unmet; do not promote or mark complete.

## Checklist

- win_rate_70pct: FAIL - Strategy must demonstrate at least 70% win rate after costs.
  Gap: No candidate passes the 70% win-rate gate; expanded-high-edge has no train-selected 70%/2R fold, SMC v1 has 0% full-history win rate, and VOL has no >=70% prescreen row.
- reward_2r: FAIL - Strategy must target and realize a 2R reward profile.
  Gap: Strict 2R expanded-high-edge walk-forward fails, SMC v1 net-R gates are negative rather than >= 2R, and VOL generated specs are below 2R.
- non_overfit_walk_forward: FAIL - Strategy must pass locked train/test or walk-forward validation without post-hoc overfit.
  Gap: No locked walk-forward candidate survives the explicit 70%/2R objective gates; VOL has no final-target rows.
- long_term_profitability: FAIL - Strategy must show long-term positive profitability across years and holdout periods.
  Gap: Strict 2R expanded-high-edge fails positive years; SMC v1 full-history net PnL is negative and final holdout has no trades.
- black_box_tick_test: PASS - Provided Databento tick/quote window must be normalized, audited, and used for black-box execution evidence.
- execution_cost_stress: FAIL - Execution validation must survive cost stress before live readiness.
  Gap: Quote replay passes for the available tick window, but bar-level cost stress still fails.
- live_ready: FAIL - Strategy must be directly usable in live trading with promotion gates passed.
  Gap: Core objective gates and cost-stress gates fail, so live readiness is not established.
