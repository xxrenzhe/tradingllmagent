from __future__ import annotations

import unittest

from tlm.mutations import generate_controlled_mutations, inverse_signal_mutation
from tlm.strategy import parse_strategy_spec

from test_strategy_backtest import base_spec


class StrategyMutationTests(unittest.TestCase):
    def test_inverse_signal_mutation_swaps_entry_rules_and_records_lineage(self) -> None:
        payload = base_spec()
        payload["direction"] = "long"
        payload["entry"] = {"long": {"all": [{"left": "close", "op": ">", "right": "open"}]}}
        payload["signal_grammar"] = {
            "entry": {"long": {"all": [{"feature": "return_5m", "op": ">", "value": 2}]}},
            "filters": {},
        }
        spec = parse_strategy_spec(payload)

        mutation = inverse_signal_mutation(spec)

        self.assertEqual(mutation.mutation_type, "inverse_signal")
        self.assertEqual(mutation.strategy.direction, "short")
        self.assertIn("short", mutation.strategy.entry)
        self.assertNotIn("long", mutation.strategy.entry)
        self.assertIn("short", mutation.strategy.raw["signal_grammar"]["entry"])
        self.assertEqual(mutation.strategy.raw["mutation"]["parent_hash"], mutation.parent_hash)
        self.assertEqual(mutation.strategy.raw["mutation"]["mutation_id"], mutation.mutation_id)

    def test_generate_controlled_mutations_returns_inverse_candidate(self) -> None:
        spec = parse_strategy_spec(base_spec())

        mutations = generate_controlled_mutations(spec)

        self.assertEqual([mutation.mutation_type for mutation in mutations], ["inverse_signal"])


if __name__ == "__main__":
    unittest.main()
