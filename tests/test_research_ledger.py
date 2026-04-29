from __future__ import annotations

import unittest

from tlm.research_ledger import split_years


class ResearchLedgerTests(unittest.TestCase):
    def test_split_years_extracts_all_research_splits(self) -> None:
        years = split_years(
            {
                "folds": [
                    {
                        "train": {"start": "2018-01-01", "end": "2020-12-31"},
                        "validation": {"start": "2021-01-01", "end": "2021-12-31"},
                        "test": {"start": "2022-01-01", "end": "2022-12-31"},
                    }
                ],
                "final_holdout": {"start": "2025-01-01", "end": "2025-12-31"},
            }
        )

        self.assertEqual(years["train"], [2018, 2019, 2020])
        self.assertEqual(years["validation"], [2021])
        self.assertEqual(years["test"], [2022])
        self.assertEqual(years["final_holdout"], [2025])


if __name__ == "__main__":
    unittest.main()
