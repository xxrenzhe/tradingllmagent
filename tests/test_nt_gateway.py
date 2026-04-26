from __future__ import annotations

import unittest

from tlm.nt_gateway import Nt8SimGateway


class Nt8SimGatewayTests(unittest.TestCase):
    def test_market_order_bracket_close_and_flatten(self) -> None:
        gateway = Nt8SimGateway()

        order_ack = gateway.execute(
            {
                "type": "marketOrder",
                "account": "Sim101",
                "instrument": "NQ 06-26",
                "action": "buy",
                "qty": 2,
                "namePrefix": "orb",
            }
        )
        bracket_ack = gateway.execute(
            {
                "type": "bracket",
                "account": "Sim101",
                "instrument": "NQ 06-26",
                "stop": 18800,
                "limit": 18820,
            }
        )
        close_ack = gateway.execute(
            {
                "type": "closeQty",
                "account": "Sim101",
                "instrument": "NQ 06-26",
                "qty": 1,
            }
        )
        flatten_ack = gateway.execute(
            {
                "type": "flatten",
                "account": "Sim101",
                "instrument": "NQ 06-26",
            }
        )

        self.assertEqual(order_ack["status"], "filled")
        self.assertEqual(order_ack["positions"][0]["quantity"], 2)
        self.assertEqual(bracket_ack["status"], "accepted")
        self.assertTrue(bracket_ack["brackets"][0]["oco"])
        self.assertEqual(close_ack["positions"][0]["quantity"], 1)
        self.assertEqual(flatten_ack["positions"][0]["quantity"], 0)

    def test_batch_and_cancel_by_name_prefix(self) -> None:
        gateway = Nt8SimGateway(accounts=["Sim101", "Sim102"])
        batch_ack = gateway.execute(
            {
                "type": "marketBatch",
                "accounts": ["Sim101", "Sim102"],
                "items": [
                    {
                        "instrument": "NQ 06-26",
                        "action": "sell_short",
                        "qty": 1,
                        "namePrefix": "batch",
                    }
                ],
            }
        )
        cancel_ack = gateway.execute({"type": "cancelOrders", "namePrefix": "batch"})

        self.assertEqual(batch_ack["status"], "filled")
        self.assertEqual(len(batch_ack["orders"]), 2)
        self.assertEqual(cancel_ack["status"], "cancelled")
        self.assertEqual(len(cancel_ack["orders"]), 2)

    def test_rejects_unknown_or_invalid_commands(self) -> None:
        gateway = Nt8SimGateway()

        unknown = gateway.execute({"type": "unsupported"})
        invalid_account = gateway.execute(
            {
                "type": "marketOrder",
                "account": "LiveAccount",
                "instrument": "NQ 06-26",
                "action": "buy",
                "qty": 1,
            }
        )
        invalid_bracket = gateway.execute(
            {
                "type": "bracket",
                "account": "Sim101",
                "instrument": "NQ 06-26",
                "stop": 18800,
            }
        )

        self.assertEqual(unknown["status"], "rejected")
        self.assertIn("unsupported_command", unknown["errors"][0])
        self.assertEqual(invalid_account["status"], "rejected")
        self.assertIn("account_not_allowed", invalid_account["errors"][0])
        self.assertEqual(invalid_bracket["status"], "rejected")
        self.assertIn("stop_and_limit_required", invalid_bracket["errors"])


if __name__ == "__main__":
    unittest.main()
