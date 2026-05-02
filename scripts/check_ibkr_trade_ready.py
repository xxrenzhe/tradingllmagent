#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import socket
import sys
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen


EXPECTED_PRESET = "expanded_high_edge_cap24_balanced_risk"


def main() -> int:
    parser = argparse.ArgumentParser(description="Check whether the local IBKR paper loop is trade-ready.")
    parser.add_argument("--api-base", default="http://127.0.0.1:8000")
    parser.add_argument("--ibkr-host", default="127.0.0.1")
    parser.add_argument("--ibkr-port", type=int, default=7497)
    parser.add_argument("--expected-preset", default=EXPECTED_PRESET)
    parser.add_argument("--require-auto-submit", action="store_true")
    args = parser.parse_args()

    checks: list[dict[str, Any]] = []
    ibkr_socket_ready = tcp_listening(args.ibkr_host, args.ibkr_port)
    checks.append(
        {
            "name": "ibkr_api_socket",
            "passed": ibkr_socket_ready,
            "detail": f"{args.ibkr_host}:{args.ibkr_port}",
            "remediation": None if ibkr_socket_ready else "Start TWS/IB Gateway paper API and enable socket clients on port 7497.",
        }
    )

    poller = fetch_json(f"{args.api_base.rstrip('/')}/api/gateways/ibkr/poller")
    poller_ready = isinstance(poller, dict)
    checks.append(
        {
            "name": "poller_api",
            "passed": poller_ready,
            "detail": args.api_base,
            "remediation": None if poller_ready else "Start the IBKR loop API on port 8000.",
        }
    )
    if poller_ready:
        strategy = poller.get("strategy") if isinstance(poller.get("strategy"), dict) else {}
        control_state = poller.get("control_state") if isinstance(poller.get("control_state"), dict) else {}
        readiness = (poller.get("last_result") or {}).get("readiness") if isinstance(poller.get("last_result"), dict) else {}
        readiness_missing = readiness.get("missing_requirements") if isinstance(readiness, dict) else None
        preset = strategy.get("preset")
        checks.extend(
            [
                {
                    "name": "preset",
                    "passed": preset == args.expected_preset,
                    "detail": preset,
                    "remediation": f"Restart loop with --expanded-high-edge-preset {args.expected_preset}.",
                },
                {
                    "name": "auto_submit",
                    "passed": bool(poller.get("auto_submit")) if args.require_auto_submit else True,
                    "detail": bool(poller.get("auto_submit")),
                    "remediation": "Enable --auto-submit only for paper/live-approved operation.",
                },
                {
                    "name": "safe_mode",
                    "passed": not bool(control_state.get("safe_mode")),
                    "detail": bool(control_state.get("safe_mode")),
                    "remediation": "Clear safe mode only after market data and account readiness are valid.",
                },
                {
                    "name": "readiness",
                    "passed": isinstance(readiness, dict) and readiness.get("status") == "ready",
                    "detail": readiness.get("status") if isinstance(readiness, dict) else None,
                    "missing_requirements": readiness_missing,
                    "remediation": "Resolve readiness missing_requirements before expecting orders.",
                },
            ]
        )

    passed = all(bool(check["passed"]) for check in checks)
    payload = {
        "artifact": "ibkr_trade_ready_check",
        "passed": passed,
        "expected_preset": args.expected_preset,
        "checks": checks,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if passed else 2


def tcp_listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def fetch_json(url: str) -> dict[str, Any] | None:
    try:
        with urlopen(url, timeout=2.0) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, json.JSONDecodeError):
        return None


if __name__ == "__main__":
    sys.exit(main())
