from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tlm.calibration import (
    build_cost_calibration_artifact,
    load_cost_calibration_samples,
    summarize_cost_calibration,
    write_cost_calibration_artifact,
)
from tlm.config import CostModelConfig


def baseline_cost_model() -> CostModelConfig:
    return CostModelConfig(
        name="nq_conservative_v1",
        tick_size=0.25,
        point_value=20,
        tick_value=5,
        slippage_ticks_per_side=1,
        round_trip_fees_usd=5,
    )


def calibration_samples() -> list[dict]:
    return [
        {
            "source": "paper_shadow",
            "timestamp": "2026-04-27T13:30:00Z",
            "instrument": "NQ 06-26",
            "observed_spread_ticks": 1,
            "observed_slippage_ticks": 0.5,
            "intent_id": "intent_1",
        },
        {
            "source": "nt8_sim",
            "timestamp": "2026-04-27T13:35:00Z",
            "instrument": "NQ 06-26",
            "observed_spread_ticks": 3,
            "observed_slippage_ticks": 2,
            "intent_id": "intent_2",
        },
    ]


class CostCalibrationTests(unittest.TestCase):
    def test_cost_calibration_summary_recommends_conservative_slippage(self) -> None:
        summary = summarize_cost_calibration(calibration_samples(), baseline_cost_model())

        self.assertEqual(summary["sample_count"], 2)
        self.assertEqual(summary["source_counts"], {"paper_shadow": 1, "nt8_sim": 1})
        self.assertGreater(summary["spread_ticks"]["p95"], 2.8)
        self.assertGreater(summary["slippage_ticks"]["p95"], 1.9)
        self.assertEqual(summary["recommended_cost_model"]["name"], "nq_conservative_v1_calibrated")
        self.assertGreaterEqual(summary["recommended_cost_model"]["slippage_ticks_per_side"], 1.9)
        self.assertTrue(summary["cost_model_version_hash"])

    def test_cost_calibration_artifact_marks_proxy_market_difference(self) -> None:
        artifact = build_cost_calibration_artifact(
            baseline=baseline_cost_model(),
            samples=calibration_samples(),
            data_quality_report={"status": "ok", "data_version_hash": "quality_hash"},
            proxy_instrument="USATECHIDXUSD",
            executable_instrument="CME_NQ",
        )

        self.assertEqual(artifact["artifact"], "cost_calibration")
        self.assertTrue(artifact["proxy_warning"])
        self.assertEqual(artifact["proxy_warning_reason"], "research_proxy_differs_from_executable_market")
        self.assertEqual(artifact["data_quality_status"], "ok")
        self.assertTrue(artifact["data_quality_report_hash"])
        self.assertTrue(artifact["artifact_hash"])

    def test_cost_calibration_jsonl_and_artifact_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            samples_path = root / "samples.jsonl"
            samples_path.write_text(
                "\n".join(json.dumps(sample, sort_keys=True) for sample in calibration_samples()) + "\n",
                encoding="utf-8",
            )
            loaded = load_cost_calibration_samples(samples_path)
            artifact = build_cost_calibration_artifact(
                baseline=baseline_cost_model(),
                samples=loaded,
                data_quality_report={"status": "ok"},
                proxy_instrument="CME_NQ",
                executable_instrument="CME_NQ",
            )
            output = root / "cost_calibration.json"
            write_cost_calibration_artifact(output, artifact)
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(len(loaded), 2)
        self.assertFalse(payload["proxy_warning"])
        self.assertEqual(payload["summary"]["sample_count"], 2)


if __name__ == "__main__":
    unittest.main()
