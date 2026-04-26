from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


@dataclass(frozen=True)
class PaperReplayResult:
    starting_equity: float
    ending_equity: float
    realized_pnl: float
    trade_count: int
    account: dict[str, Any]
    orders: list[dict[str, Any]]
    fills: list[dict[str, Any]]
    positions: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "starting_equity": self.starting_equity,
            "ending_equity": self.ending_equity,
            "realized_pnl": self.realized_pnl,
            "trade_count": self.trade_count,
            "account": self.account,
            "orders": self.orders,
            "fills": self.fills,
            "positions": self.positions,
        }


def load_backtest_result(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("trades"), list):
        raise ValueError("strategy-id must point to a backtest result JSON with a trades list")
    return payload


def replay_trades(trades: Sequence[dict[str, Any]], starting_equity: float = 100_000) -> PaperReplayResult:
    equity = starting_equity
    orders: list[dict[str, Any]] = []
    fills: list[dict[str, Any]] = []
    positions: list[dict[str, Any]] = []
    for index, trade in enumerate(trades):
        net_pnl = float(trade["net_pnl"])
        contracts = int(trade["contracts"])
        entry_order_id = f"order_{index:06d}_entry"
        exit_order_id = f"order_{index:06d}_exit"
        position_id = f"position_{index:06d}"
        entry_action = "buy" if trade["side"] == "long" else "sell_short"
        exit_action = "sell" if trade["side"] == "long" else "buy_to_cover"
        orders.extend(
            [
                {
                    "order_id": entry_order_id,
                    "position_id": position_id,
                    "symbol": trade["symbol"],
                    "side": trade["side"],
                    "action": entry_action,
                    "contracts": contracts,
                    "order_type": "market",
                    "status": "filled",
                    "submitted_time": trade["entry_time"],
                    "filled_time": trade["entry_time"],
                    "fill_price": float(trade["entry_price"]),
                    "risk_fields": _trade_risk_fields(trade),
                },
                {
                    "order_id": exit_order_id,
                    "position_id": position_id,
                    "symbol": trade["symbol"],
                    "side": trade["side"],
                    "action": exit_action,
                    "contracts": contracts,
                    "order_type": "market",
                    "status": "filled",
                    "submitted_time": trade["exit_time"],
                    "filled_time": trade["exit_time"],
                    "fill_price": float(trade["exit_price"]),
                    "risk_fields": _trade_risk_fields(trade),
                },
            ]
        )
        equity += net_pnl
        fills.append(
            {
                "fill_id": f"fill_{index:06d}",
                "position_id": position_id,
                "entry_order_id": entry_order_id,
                "exit_order_id": exit_order_id,
                "symbol": trade["symbol"],
                "side": trade["side"],
                "contracts": contracts,
                "entry_time": trade["entry_time"],
                "exit_time": trade["exit_time"],
                "entry_price": float(trade["entry_price"]),
                "exit_price": float(trade["exit_price"]),
                "entry_reason": trade.get("entry_reason", "unknown"),
                "exit_reason": trade["exit_reason"],
                "gross_pnl": float(trade["gross_pnl"]),
                "fees": float(trade["fees"]),
                "slippage_cost": float(trade["slippage_cost"]),
                "net_pnl": net_pnl,
                "equity_after": equity,
            }
        )
        positions.append(
            {
                "position_id": position_id,
                "symbol": trade["symbol"],
                "side": trade["side"],
                "contracts": contracts,
                "status": "closed",
                "opened_time": trade["entry_time"],
                "closed_time": trade["exit_time"],
                "entry_price": float(trade["entry_price"]),
                "exit_price": float(trade["exit_price"]),
                "realized_pnl": net_pnl,
                "equity_after_close": equity,
            }
        )
    account = {
        "mode": "paper_replay",
        "starting_equity": starting_equity,
        "ending_equity": equity,
        "realized_pnl": equity - starting_equity,
        "open_position_count": 0,
        "closed_position_count": len(positions),
    }
    return PaperReplayResult(
        starting_equity=starting_equity,
        ending_equity=equity,
        realized_pnl=equity - starting_equity,
        trade_count=len(fills),
        account=account,
        orders=orders,
        fills=fills,
        positions=positions,
    )


def _trade_risk_fields(trade: dict[str, Any]) -> dict[str, Any]:
    return {
        "stop_loss": trade.get("stop_loss") or trade.get("stop_price"),
        "take_profit": trade.get("take_profit") or trade.get("target_price"),
        "max_loss": trade.get("max_loss"),
        "exit_reason": trade.get("exit_reason"),
    }


def export_ninjatrader_signals(
    trades: Sequence[dict[str, Any]],
    export_format: str,
    account: str,
    instrument: str,
) -> str:
    if export_format == "csv":
        return export_ninjatrader_csv(trades, account, instrument)
    if export_format == "oif":
        return export_ninjatrader_oif(trades, account, instrument)
    raise ValueError(f"Unsupported NinjaTrader export format: {export_format}")


def export_ninjatrader_csv(
    trades: Sequence[dict[str, Any]],
    account: str,
    instrument: str,
) -> str:
    buffer = io.StringIO()
    fieldnames = [
        "time",
        "account",
        "instrument",
        "action",
        "quantity",
        "order_type",
        "price",
        "time_in_force",
        "stop_loss",
        "take_profit",
        "max_loss",
        "offline_only",
        "risk_review_required",
        "signal_name",
        "source_trade_id",
    ]
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    for index, trade in enumerate(trades):
        for row in _signal_rows(index, trade, account, instrument):
            writer.writerow(row)
    return buffer.getvalue()


def export_ninjatrader_oif(
    trades: Sequence[dict[str, Any]],
    account: str,
    instrument: str,
) -> str:
    lines = []
    for index, trade in enumerate(trades):
        for row in _signal_rows(index, trade, account, instrument):
            lines.append(
                ";".join(
                    [
                        "PLACE",
                        row["account"],
                        row["instrument"],
                        row["action"],
                        row["quantity"],
                        row["order_type"],
                        row["price"],
                        row["stop_loss"],
                        row["time_in_force"],
                        row["take_profit"],
                        row["signal_name"],
                        (
                            f"{row['source_trade_id']}|offline_only={row['offline_only']}"
                            f"|risk_review_required={row['risk_review_required']}"
                            f"|max_loss={row['max_loss']}"
                        ),
                    ]
                )
            )
    return "\n".join(lines) + ("\n" if lines else "")


def _signal_rows(
    index: int,
    trade: dict[str, Any],
    account: str,
    instrument: str,
) -> list[dict[str, str]]:
    contracts = str(int(trade["contracts"]))
    entry_action = "BUY" if trade["side"] == "long" else "SELLSHORT"
    exit_action = "SELL" if trade["side"] == "long" else "BUYTOCOVER"
    source_trade_id = f"trade_{index:06d}"
    risk_fields = _trade_risk_fields(trade)
    risk_payload = {
        "time_in_force": "DAY",
        "stop_loss": _risk_value(risk_fields["stop_loss"]),
        "take_profit": _risk_value(risk_fields["take_profit"]),
        "max_loss": _risk_value(risk_fields["max_loss"]),
        "offline_only": "true",
        "risk_review_required": "true",
    }
    return [
        {
            "time": trade["entry_time"],
            "account": account,
            "instrument": instrument,
            "action": entry_action,
            "quantity": contracts,
            "order_type": "MARKET",
            "price": "0",
            "signal_name": trade.get("entry_reason", "entry"),
            "source_trade_id": source_trade_id,
            **risk_payload,
        },
        {
            "time": trade["exit_time"],
            "account": account,
            "instrument": instrument,
            "action": exit_action,
            "quantity": contracts,
            "order_type": "MARKET",
            "price": "0",
            "signal_name": trade["exit_reason"],
            "source_trade_id": source_trade_id,
            **risk_payload,
        },
    ]


def _risk_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value)
