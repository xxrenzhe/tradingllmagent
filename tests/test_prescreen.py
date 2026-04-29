from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from tlm.backtest import Trade
from tlm.metrics import calculate_metrics
from tlm.prescreen import build_pre_screen_report


class CheapPreScreenTests(unittest.TestCase):
    def test_prescreen_rejects_negative_gross_edge_and_inverse_better(self) -> None:
        start = datetime(2025, 1, 2, 13, 30)
        trades = [
            Trade(
                symbol="NQmain",
                side="long",
                entry_time=start + timedelta(minutes=index),
                exit_time=start + timedelta(minutes=index + 1),
                entry_price=100,
                exit_price=99,
                contracts=1,
                gross_pnl=-20,
                fees=5,
                slippage_cost=10,
                net_pnl=-35,
                entry_reason="signal_grammar_long",
                exit_reason="stop_loss",
            )
            for index in range(3)
        ]
        metrics = calculate_metrics([trade.net_pnl for trade in trades], [100000, 99965, 99930, 99895], 100000, 1)
        inverse_metrics = calculate_metrics([10, 10, 10], [100000, 100010, 100020, 100030], 100000, 1)

        report = build_pre_screen_report(trades, metrics, round_trip_cost=15, inverse_metrics=inverse_metrics)

        self.assertFalse(report["passed"])
        self.assertIn("negative_gross_edge", report["reasons"])
        self.assertIn("inverse_signal_better", report["reasons"])
        self.assertIn("stop_loss_ratio_above_limit", report["reasons"])
        self.assertEqual(report["entry_reason_counts"]["signal_grammar_long"], 3)
        self.assertEqual(report["exit_reason_counts"]["stop_loss"], 3)
        self.assertEqual(report["metrics"]["stop_loss_ratio"], 1.0)

    def test_prescreen_passes_diversified_positive_gross_edge(self) -> None:
        start = datetime(2025, 1, 2, 13, 30)
        trades = []
        equity = [100000]
        pnls = []
        for index in range(40):
            net_pnl = 20
            trades.append(
                Trade(
                    symbol="NQmain",
                    side="long" if index % 2 else "short",
                    entry_time=start + timedelta(days=index % 10, minutes=index),
                    exit_time=start + timedelta(days=index % 10, minutes=index + 1),
                    entry_price=100,
                    exit_price=102,
                    contracts=1,
                    gross_pnl=35,
                    fees=5,
                    slippage_cost=10,
                    net_pnl=net_pnl,
                    entry_reason="feature_signal",
                    exit_reason="take_profit",
                )
            )
            pnls.append(net_pnl)
            equity.append(equity[-1] + net_pnl)
        metrics = calculate_metrics(pnls, equity, 100000, 10)

        report = build_pre_screen_report(trades, metrics, round_trip_cost=15)

        self.assertTrue(report["passed"])
        self.assertGreater(report["metrics"]["cost_coverage"], 2)
        self.assertEqual(report["metrics"]["stop_loss_ratio"], 0.0)
        self.assertEqual(report["exit_reason_counts"]["take_profit"], 40)

    def test_prescreen_rejects_insufficient_mid_price_edge_when_provided(self) -> None:
        start = datetime(2025, 1, 2, 13, 30)
        trades = []
        equity = [100000]
        pnls = []
        for index in range(40):
            trades.append(
                Trade(
                    symbol="NQmain",
                    side="long",
                    entry_time=start + timedelta(days=index % 10, minutes=index),
                    exit_time=start + timedelta(days=index % 10, minutes=index + 1),
                    entry_price=100,
                    exit_price=102,
                    contracts=1,
                    gross_pnl=35,
                    fees=5,
                    slippage_cost=10,
                    net_pnl=20,
                    entry_reason="feature_signal",
                    exit_reason="take_profit",
                )
            )
            pnls.append(20)
            equity.append(equity[-1] + 20)
        metrics = calculate_metrics(pnls, equity, 100000, 10)

        report = build_pre_screen_report(
            trades,
            metrics,
            round_trip_cost=15,
            mid_trade_pnls=[2.0] * len(trades),
        )

        self.assertFalse(report["passed"])
        self.assertIn("insufficient_mid_price_edge", report["reasons"])
        self.assertEqual(report["metrics"]["avg_mid_trade"], 2.0)


if __name__ == "__main__":
    unittest.main()
