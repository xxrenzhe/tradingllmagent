# 55% Win-Rate / 1.5R Objective Completion Audit

- Achieved: `False`
- Reason: No relaxed 55% win-rate / 1.5R walk-forward candidate passes all required gates.

## Reports

- reports/nq_expanded_high_edge_walk_forward_55wr_15r_hard_gate_2026-05-02.json: family=expanded_high_edge, passed=False, profile=stable, oos_net=-186572.5, min_win_rate=0.4714983713355049
- reports/nq_expanded_high_edge_walk_forward_55wr_15r_net_2026-05-02.json: family=expanded_high_edge, passed=False, profile=net, oos_net=-267310.0, min_win_rate=0.44460712542703756
- reports/nq_expanded_high_edge_walk_forward_55wr_15r_floor_2026-05-02.json: family=expanded_high_edge, passed=False, profile=floor, oos_net=0, min_win_rate=None
- reports/nq_vol_execution_55wr_15r_objective_audit_2026-05-02.json: family=vol_execution, passed=False, profile=None, oos_net=None, min_win_rate=None
- reports/nq_smc_lqem_ce_v1_55wr_15r_objective_gates_2026-05-02.json: family=smc_lqem_ce, passed=False, profile=None, oos_net=-525.0, min_win_rate=None
- experiments/profit_mining/low_r_high_frequency_15r_forced_baseline_2019_2026.json: family=low_r_high_frequency_probe, passed=False, profile=None, oos_net=3418345.0, min_win_rate=0.5084822234253745
- reports/low_r_15r_subset_search_55wr_wide_2026-05-03.json: family=low_r_15r_subset_search, passed=False, profile=None, oos_net=888047.5, min_win_rate=0.5352691593741713
- reports/low_r_15r_subset_walk_forward_55wr_2026-05-03.json: family=low_r_15r_subset_walk_forward, passed=False, profile=None, oos_net=271807.5, min_win_rate=0.4134529147982063
- reports/nq_tick_microstructure_filter_audit_55wr_15r_2026-05-03.json: family=tick_microstructure_filter, passed=False, profile=None, oos_net=7220.0, min_win_rate=0.6111111111111112
- reports/nq_tick_derived_intraday_search_55wr_15r_2026-05-03.json: family=tick_derived_intraday, passed=False, profile=None, oos_net=-3975.0, min_win_rate=0.21951219512195122
- reports/nq_low_frequency_bar_2r_search_2026-05-03.json: family=low_frequency_bar, passed=False, profile=None, oos_net=10440.0, min_win_rate=0.0

## Checklist

- reward_1_5r: FAIL - Strategy search must force a 1.5R or higher take-profit profile.
  Gap: Expanded searches forced 1.5R, but no full strategy family passes 55%/1.5R gates.
- win_rate_55pct: FAIL - Out-of-sample walk-forward test years must each reach at least 55% win rate.
  Gap: No relaxed strategy family passes the 55% win-rate gate.
- non_overfit_walk_forward: FAIL - Selection must be train-only and replayed on next unseen test year.
  Gap: The protocol is walk-forward, but no candidate survives all gates.
- long_term_profitability: FAIL - Relaxed strategy must be profitable across all available out-of-sample years.
  Gap: Relaxed runs still have negative OOS years, no qualifying rows, or negative full-history PnL.
- live_ready: FAIL - Relaxed candidate must be eligible for execution/tick validation and promotion.
  Gap: No relaxed candidate passes the objective gates, so there is no candidate to promote to quote replay/live readiness.
