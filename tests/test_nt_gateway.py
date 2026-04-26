from __future__ import annotations

import unittest

from tlm.nt_gateway import Nt8SimGateway


class Nt8SimGatewayTests(unittest.TestCase):
    def test_market_order_bracket_close_and_flatten(self) -> None:
        gateway = Nt8SimGateway()

        order_ack = gateway.execute(
            {
                "type": "marketOrder",
                "idempotency_key": "order-1",
                "correlation_id": "corr-1",
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
        self.assertEqual(order_ack["idempotency_key"], "order-1")
        self.assertEqual(order_ack["protocol_version"], "nt8-sim.v1")
        self.assertEqual(order_ack["sequence"], 1)
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

    def test_idempotency_reconciliation_and_safe_mode(self) -> None:
        gateway = Nt8SimGateway()
        command = {
            "type": "marketOrder",
            "idempotency_key": "dup-1",
            "account": "Sim101",
            "instrument": "NQ 06-26",
            "action": "buy",
            "qty": 1,
        }

        first = gateway.execute(command)
        duplicate = gateway.execute(command)
        report = gateway.reconciliation_report(
            [{"account": "Sim101", "instrument": "NQ 06-26", "quantity": 0}]
        )
        rejected = gateway.execute(
            {
                "type": "marketOrder",
                "account": "Sim101",
                "instrument": "NQ 06-26",
                "action": "buy",
                "qty": 1,
            }
        )
        flatten = gateway.execute(
            {
                "type": "flatten",
                "account": "Sim101",
                "instrument": "NQ 06-26",
            }
        )

        self.assertEqual(first["status"], "filled")
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["sequence"], first["sequence"])
        self.assertEqual(report["status"], "drift")
        self.assertTrue(report["safe_mode"])
        self.assertEqual(rejected["status"], "rejected")
        self.assertIn("gateway_safe_mode", rejected["errors"])
        self.assertEqual(flatten["status"], "flattened")

    def test_read_only_rejects_commands(self) -> None:
        gateway = Nt8SimGateway()
        incident = gateway.set_read_only(True, "maintenance")
        ack = gateway.execute(
            {
                "type": "marketOrder",
                "account": "Sim101",
                "instrument": "NQ 06-26",
                "action": "buy",
                "qty": 1,
            }
        )

        self.assertEqual(incident["event_type"], "read_only_enabled")
        self.assertEqual(ack["status"], "rejected")
        self.assertIn("gateway_read_only", ack["errors"])


if __name__ == "__main__":
    unittest.main()
