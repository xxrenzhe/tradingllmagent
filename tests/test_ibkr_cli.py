from __future__ import annotations

import os
import types
import unittest
from unittest.mock import patch

from tlm.cli import build_parser, cmd_ibkr_loop


class IbkrCliTests(unittest.TestCase):
    def test_parser_exposes_ibkr_loop_entrypoint(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["ibkr", "loop", "--symbol", "MNQ", "--auto-submit"])

        self.assertEqual(args.ibkr_command, "loop")
        self.assertEqual(args.symbol, "MNQ")
        self.assertTrue(args.auto_submit)
        self.assertTrue(args.connect)
        self.assertEqual(args.api_port, 8000)
        self.assertEqual(args.ibkr_port, 7497)
        self.assertEqual(args.strategy_family, "low_r_regime_basket")
        self.assertEqual(args.low_r_preset, "simple_robust_low_r")

    def test_parser_exposes_ibkr_soak_monitor(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "ibkr",
                "soak-monitor",
                "--api-base",
                "http://127.0.0.1:8011",
                "--output-dir",
                "experiments/ibkr_paper/soak_test",
                "--max-samples",
                "1",
            ]
        )

        self.assertEqual(args.ibkr_command, "soak-monitor")
        self.assertEqual(args.api_base, "http://127.0.0.1:8011")
        self.assertEqual(args.output_dir, "experiments/ibkr_paper/soak_test")
        self.assertEqual(args.max_samples, 1)
        self.assertEqual(args.max_stale_seconds, 30)

    def test_ibkr_loop_sets_runtime_env_and_runs_uvicorn(self) -> None:
        run_calls: list[dict[str, object]] = []
        fake_app = object()

        def fake_import(name: str):
            if name == "uvicorn":
                return types.SimpleNamespace(
                    run=lambda app, host, port: run_calls.append({"app": app, "host": host, "port": port})
                )
            if name == "tlm.api":
                return types.SimpleNamespace(create_app=lambda: fake_app)
            raise ModuleNotFoundError(name)

        args = build_parser().parse_args(
            [
                "ibkr",
                "loop",
                "--symbol",
                "MNQ",
                "--api-port",
                "8011",
                "--ibkr-port",
                "7497",
                "--client-id",
                "21",
                "--poll-interval-seconds",
                "3",
                "--review-interval-seconds",
                "120",
                "--max-stale-seconds",
                "9",
                "--auto-submit",
            ]
        )

        with patch.dict(os.environ, {}, clear=True):
            with patch("tlm.cli.importlib.import_module", side_effect=fake_import):
                result = cmd_ibkr_loop(args)

                self.assertEqual(result, 0)
                self.assertEqual(os.environ["TLM_IBKR_POLLER_ENABLED"], "1")
                self.assertEqual(os.environ["TLM_IBKR_POLL_SYMBOL"], "MNQ")
                self.assertEqual(os.environ["TLM_IBKR_STRATEGY_FAMILY"], "low_r_regime_basket")
                self.assertEqual(os.environ["TLM_IBKR_LOW_R_PRESET"], "simple_robust_low_r")
                self.assertEqual(os.environ["TLM_IBKR_POLL_INTERVAL_SECONDS"], "3.0")
                self.assertEqual(os.environ["TLM_IBKR_REVIEW_INTERVAL_SECONDS"], "120.0")
                self.assertEqual(os.environ["TLM_IBKR_READINESS_MAX_STALE_SECONDS"], "9")
                self.assertEqual(os.environ["TLM_IBKR_AUTO_SUBMIT"], "1")
                self.assertEqual(os.environ["TLM_IBKR_AUTO_CONNECT"], "1")
                self.assertEqual(os.environ["TLM_IBKR_PORT"], "7497")
                self.assertEqual(os.environ["TLM_IBKR_CLIENT_ID"], "21")

        self.assertEqual(run_calls, [{"app": fake_app, "host": "127.0.0.1", "port": 8011}])


if __name__ == "__main__":
    unittest.main()
