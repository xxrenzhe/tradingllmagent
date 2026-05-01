from __future__ import annotations

import unittest

from scripts.walk_forward_expanded_high_edge import (
    build_walk_forward_specs,
    parse_float_grid,
    parse_scan_type_counts,
)


def _single(candidate_id: str, scan_type: str, avg_pnl: float) -> dict:
    return {
        "source_candidate_id": candidate_id,
        "avg_hold_bars": 10,
        "constraints": {
            "positive_years": 6,
            "full_year_min_trades": 100,
        },
        "edges": [{"scan_type": scan_type}],
        "metrics": {
            "avg_trade_net_pnl": avg_pnl,
            "net_pnl": avg_pnl * 100,
            "profit_factor": 1.2,
        },
    }


class WalkForwardExpandedHighEdgeTests(unittest.TestCase):
    def test_parse_scan_type_counts(self) -> None:
        self.assertEqual(
            parse_scan_type_counts(("prior_day_breakout=2", "donchian20_breakout=0"), option_name="--x"),
            {"prior_day_breakout": 2, "donchian20_breakout": 0},
        )

    def test_parse_float_grid(self) -> None:
        self.assertEqual(parse_float_grid("1.25, 1.5", option_name="--tp"), (1.25, 1.5))

    def test_build_specs_respects_scan_type_caps_and_minimums(self) -> None:
        singles = [
            _single("prior-1", "prior_day_breakout", 10),
            _single("prior-2", "prior_day_breakout", 9),
            _single("orb-1", "opening_range_breakout", 8),
            _single("orb-2", "opening_range_breakout", 7),
            _single("vwap-1", "vwap_pullback_bounce", 6),
        ]

        specs = build_walk_forward_specs(
            singles,
            train_years=(2024, 2025),
            selection_profile="net",
            min_edge_count=4,
            max_scan_type_counts={"prior_day_breakout": 1},
            min_scan_type_counts={"prior_day_breakout": 1},
        )

        self.assertEqual(specs[0]["candidate_ids"], ("prior-1", "orb-1", "orb-2", "vwap-1"))


if __name__ == "__main__":
    unittest.main()
