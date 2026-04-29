from __future__ import annotations

import unittest

from tlm.attribution import build_search_attribution_report


class SearchAttributionTests(unittest.TestCase):
    def test_attribution_groups_failures_by_family_feature_and_mutation(self) -> None:
        result = {
            "strategy_spec": {
                "strategy_family": "intraday_momentum",
                "feature_set": [{"name": "return_5m"}, {"name": "vwap_dist"}],
                "signal_grammar": {
                    "entry": {"long": {"all": [{"feature": "return_5m", "op": ">", "value": 2}]}},
                    "filters": {"all": [{"feature": "spread_ticks", "op": "<=", "value": 2}]},
                },
                "mutation": {"type": "inverse_signal"},
            },
            "gates": {"passed": False, "reasons": ["net_pnl_test", "positive_year_ratio"]},
            "pre_screen_report": {"reasons": ["negative_gross_edge", "inverse_signal_better"]},
            "aggregate_test_metrics": {
                "trade_count": 40,
                "net_pnl": -100,
                "sharpe": -1,
                "annual_trades": 1500,
                "avg_trade_net_pnl": -2.5,
            },
        }

        report = build_search_attribution_report([result])

        self.assertEqual(report["result_count"], 1)
        self.assertEqual(report["by_family"]["intraday_momentum"]["count"], 1)
        self.assertEqual(report["by_feature"]["return_5m"]["count"], 1)
        self.assertEqual(report["by_feature"]["spread_ticks"]["count"], 1)
        self.assertEqual(report["by_mutation"]["inverse_signal"]["count"], 1)
        self.assertEqual(report["failure_modes"]["negative_gross_edge"], 1)
        self.assertEqual(report["failure_modes"]["wrong_direction"], 1)
        self.assertEqual(report["failure_modes"]["overtrading"], 1)


if __name__ == "__main__":
    unittest.main()
