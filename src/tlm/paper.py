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
    fills: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "starting_equity": self.starting_equity,
            "ending_equity": self.ending_equity,
            "realized_pnl": self.realized_pnl,
            "trade_count": self.trade_count,
            "fills": self.fills,
        }


def load_backtest_result(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("trades"), list):
        raise ValueError("strategy-id must point to a backtest result JSON with a trades list")
    return payload


def replay_trades(trades: Sequence[dict[str, Any]], starting_equity: float = 100_000) -> PaperReplayResult:
    equity = starting_equity
    fills: list[dict[str, Any]] = []
    for index, trade in enumerate(trades):
        net_pnl = float(trade["net_pnl"])
        equity += net_pnl
        fills.append(
            {
                "fill_id": f"fill_{index:06d}",
                "symbol": trade["symbol"],
                "side": trade["side"],
                "contracts": int(trade["contracts"]),
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
    return PaperReplayResult(
        starting_equity=starting_equity,
        ending_equity=equity,
        realized_pnl=equity - starting_equity,
        trade_count=len(fills),
        fills=fills,
    )


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
                        "0",
                        "DAY",
                        "",
                        row["signal_name"],
                        row["source_trade_id"],
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
        },
    ]
