from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from tlm.smc import Bar
from tlm.smc_state import SmcLqemParameters, SmcLqemStateMachine


def smc_bar(index: int, open_price: float, high: float, low: float, close: float) -> Bar:
    return Bar(
        timestamp=datetime(2025, 1, 2, 9, 30) + timedelta(minutes=index),
        open=open_price,
        high=high,
        low=low,
        close=close,
        tick_count=100 + index,
        avg_spread=0.5,
    )


def fast_parameters() -> SmcLqemParameters:
    return SmcLqemParameters(
        htf_minutes=1,
        htf_swing_left=1,
        htf_swing_right=1,
        ltf_swing_left=1,
        ltf_swing_right=1,
        break_buffer_ticks=0,
        min_htf_range_ticks=4,
        min_ob_ticks=1,
        max_ob_ticks=80,
        min_micro_ob_ticks=1,
        max_micro_ob_ticks=80,
        pbl_clearance_ticks=0,
        sweep_buffer_ticks=0,
        stop_buffer_ticks=1,
        min_stop_ticks=1,
        max_stop_ticks=80,
        default_take_profit_r=2.0,
        pending_ttl_bars=2,
        cooldown_bars_after_cancel=1,
        cooldown_bars_after_exit=1,
    )


def long_setup_bars() -> list[Bar]:
    return [
        smc_bar(0, 101, 102, 100.5, 101),
        smc_bar(1, 100.5, 101, 100, 100.25),
        smc_bar(2, 103, 105, 101, 104),
        smc_bar(3, 104, 104, 99, 103),
        smc_bar(4, 103, 106, 102, 106),
        smc_bar(5, 106, 108, 106, 107),
        smc_bar(6, 107, 108, 105, 106),
        smc_bar(7, 106, 109, 106, 108),
        smc_bar(8, 108, 108, 103.5, 105.5),
        smc_bar(9, 105.5, 109, 105, 107),
        smc_bar(10, 107, 107, 104.5, 105),
        smc_bar(11, 105, 110, 105, 110),
    ]


class SmcStateMachineTests(unittest.TestCase):
    def test_long_setup_moves_through_signal_fill_exit_and_cooldown(self) -> None:
        machine = SmcLqemStateMachine(fast_parameters())
        decisions = [machine.on_bar(item) for item in long_setup_bars()]
        signal_decision = decisions[-1]

        self.assertEqual(
            [transition.to_state for transition in machine.transition_log],
            [
                "FINDING_HTF_POI",
                "WAITING_FOR_PBL",
                "WAITING_FOR_SWEEP",
                "MONITORING_LTF_CHOCH",
                "ORDER_PENDING",
            ],
        )
        self.assertIsNotNone(signal_decision.signal)
        assert signal_decision.signal is not None
        self.assertEqual(signal_decision.signal.direction, "long")
        self.assertEqual(signal_decision.signal.entry_price, 107)
        self.assertEqual(signal_decision.signal.stop_price, 103.25)
        self.assertEqual(signal_decision.signal.take_profit_price, 114.5)
        self.assertIn("htf_ob", signal_decision.signal.audit)
        self.assertIn("micro_ob", signal_decision.signal.audit)
        self.assertIn("pbl", signal_decision.signal.audit)
        self.assertIn("sweep", signal_decision.signal.audit)

        fill_decision = machine.on_bar(smc_bar(12, 109, 110, 107, 109))
        self.assertEqual(fill_decision.transition.to_state if fill_decision.transition else None, "IN_POSITION")
        self.assertEqual(machine.state, "IN_POSITION")

        exit_decision = machine.on_bar(smc_bar(13, 109, 115, 108, 114.75))
        self.assertEqual(exit_decision.exit_reason, "take_profit")
        self.assertEqual(exit_decision.transition.to_state if exit_decision.transition else None, "COOLDOWN")
        self.assertEqual(machine.state, "COOLDOWN")

        idle_decision = machine.on_bar(smc_bar(14, 114, 115, 113, 114))
        self.assertEqual(idle_decision.transition.to_state if idle_decision.transition else None, "IDLE")
        self.assertEqual(machine.state, "IDLE")

    def test_pending_order_cancels_when_target_is_reached_before_fill(self) -> None:
        machine = SmcLqemStateMachine(fast_parameters())
        for item in long_setup_bars():
            machine.on_bar(item)

        cancellation = machine.on_bar(smc_bar(12, 109, 115, 108, 114.75))

        self.assertEqual(cancellation.cancellation_reason, "target_reached_before_fill")
        self.assertEqual(cancellation.transition.to_state if cancellation.transition else None, "COOLDOWN")
        self.assertEqual(machine.state, "COOLDOWN")

        idle_decision = machine.on_bar(smc_bar(13, 114, 115, 113, 114))
        self.assertEqual(idle_decision.transition.to_state if idle_decision.transition else None, "IDLE")
        self.assertEqual(machine.state, "IDLE")

    def test_pending_order_cancels_on_ttl_expiry(self) -> None:
        machine = SmcLqemStateMachine(fast_parameters())
        for item in long_setup_bars():
            machine.on_bar(item)

        self.assertEqual(machine.state, "ORDER_PENDING")
        machine.on_bar(smc_bar(12, 109, 110, 108, 109))
        machine.on_bar(smc_bar(13, 109, 110, 108, 109))
        cancellation = machine.on_bar(smc_bar(14, 109, 110, 108, 109))

        self.assertEqual(cancellation.cancellation_reason, "pending_ttl_expired")
        self.assertEqual(machine.state, "COOLDOWN")


if __name__ == "__main__":
    unittest.main()
