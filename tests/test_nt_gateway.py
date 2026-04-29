from __future__ import annotations

from pathlib import Path
import unittest

from tlm.nt_gateway import Nt8SimGateway
from tlm.nt8_protocol import (
    append_gateway_event,
    build_heartbeat,
    build_order_update,
    detect_external_intervention,
    gateway_protocol_manifest,
    validate_gateway_command,
)


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

    def test_protocol_manifest_and_command_validation_enforce_sim_boundary(self) -> None:
        manifest = gateway_protocol_manifest()
        valid = validate_gateway_command(
            {
                "type": "marketOrder",
                "correlation_id": "corr",
                "idempotency_key": "idem",
                "account": "Sim101",
            }
        )
        live = validate_gateway_command(
            {
                "type": "marketOrder",
                "correlation_id": "corr",
                "idempotency_key": "idem",
                "account": "Live101",
            }
        )

        self.assertEqual(manifest["protocol_version"], "nt8-gateway.v1")
        self.assertIn("marketBatch", manifest["commands"])
        self.assertTrue(valid["valid"])
        self.assertFalse(live["valid"])
        self.assertIn("non_sim_account_blocked", live["errors"])

    def test_gateway_events_are_append_only_and_detect_external_intervention(self) -> None:
        heartbeat = build_heartbeat(sequence=1, accounts=["Sim101"], instruments=["NQ 06-26"])
        update = build_order_update(
            sequence=2,
            order_id="order_1",
            status="filled",
            account="Sim101",
            instrument="NQ 06-26",
            command_id="cmd_1",
        )
        manual = build_order_update(
            sequence=3,
            order_id="manual_1",
            status="cancelled",
            account="Sim101",
            instrument="NQ 06-26",
            source="manual",
        )
        log = append_gateway_event([], heartbeat)
        log = append_gateway_event(log, update)

        self.assertEqual([event["sequence"] for event in log], [1, 2])
        self.assertFalse(detect_external_intervention(update, ["cmd_1"]))
        self.assertTrue(detect_external_intervention(manual, ["cmd_1"]))
        with self.assertRaisesRegex(ValueError, "sequence_must_increase"):
            append_gateway_event(log, update)

    def test_sim_gateway_records_order_updates_and_external_interventions(self) -> None:
        gateway = Nt8SimGateway()
        ack = gateway.execute(
            {
                "type": "marketOrder",
                "command_id": "cmd_order",
                "idempotency_key": "order-1",
                "correlation_id": "corr-1",
                "account": "Sim101",
                "instrument": "NQ 06-26",
                "action": "buy",
                "qty": 1,
            }
        )
        external = gateway.record_external_intervention(
            account="Sim101",
            instrument="NQ 06-26",
            order_id="manual_cancel",
            status="cancelled",
            reason="operator_changed_order_in_nt8",
        )

        self.assertEqual(ack["status"], "filled")
        self.assertEqual(gateway.order_updates()["event_count"], 2)
        self.assertEqual(gateway.order_updates()["events"][0]["payload"]["command_id"], "cmd_order")
        self.assertEqual(external["event_type"], "external_intervention")
        self.assertTrue(gateway.safe_mode)

    def test_nt8_gateway_scaffold_files_exist(self) -> None:
        root = Path("tools/nt8-gateway")

        self.assertTrue((root / "TradingLlmAgentGateway.csproj").exists())
        self.assertTrue((root / "GatewayProtocol.cs").exists())
        self.assertIn("nt8-gateway.v1", (root / "GatewayProtocol.cs").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
