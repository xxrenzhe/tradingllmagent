# NQ Historical Data Pipeline Runbook

This runbook implements `docs/plan/11.nq-historical-data-acquisition-plan.md`.

## 1. Import FirstRate NQ 1m Bars

Use this after downloading FirstRate Data NQ 1-minute CSV files.

```bash
PYTHONPATH=src python3 -m tlm.cli \
  --data-root data \
  data import-firstrate \
  --symbol NQ_1M \
  --input /path/to/NQ_1min.csv \
  --source-timezone UTC
```

Output partitions:

```text
data/bars/1m/NQ_1M/date=YYYY-MM-DD/part-000.parquet
```

If the vendor file timestamp is exchange-local, set `--source-timezone America/New_York`.

## 2. Check Bar Quality

```bash
PYTHONPATH=src python3 -m tlm.cli \
  --data-root data \
  data bar-quality \
  --symbol NQ_1M \
  --from 2020-01-01 \
  --to 2025-12-31 \
  --timeframe 1m
```

The report flags missing partitions, empty partitions, duplicate timestamps, intraday gaps, invalid OHLC rows, non-positive tick counts, negative spreads, and large price jumps.

## 3. Generate Split Manifest

```bash
PYTHONPATH=src python3 -m tlm.cli \
  --data-root data \
  data split-manifest \
  --symbol NQ_1M \
  --from 2020-01-01 \
  --to 2025-12-31 \
  --timeframe 1m \
  --train-days 730 \
  --validation-days 182 \
  --test-days 182 \
  --step-days 91 \
  --embargo-days 5 \
  --final-holdout-days 365
```

The manifest records `data_version_hash`, source files, missing files, rolling folds, and a policy that keeps `test` and `final_holdout` hidden from LLM feedback.

## 4. Run Strategy Search On NQ_1M

Existing NQmain seed specs can be tested against imported FirstRate data by using `--symbol NQ_1M`.

```bash
PYTHONPATH=src python3 -m tlm.cli \
  --data-root data \
  research discover-target \
  --spec strategies/example_opening_range_breakout.yaml \
  --symbol NQ_1M \
  --from 2020-01-01 \
  --to 2025-12-31 \
  --execution-mode bar \
  --min-sharpe 0.5 \
  --min-annual-trades 300
```

The symbol override is intentional: it lets old seed specs remain reusable while the data path and result metadata use `NQ_1M`.

## 5. Import Databento Quotes

After a candidate survives bar-level search, export a Databento `TBBO` or `MBP-1` CSV window and import it.

```bash
PYTHONPATH=src python3 -m tlm.cli \
  --data-root data \
  data import-databento-quotes \
  --symbol NQ_CME \
  --input /path/to/databento_nq_tbbo.csv
```

Output partitions:

```text
data/normalized/quotes/NQ_CME/date=YYYY-MM-DD/part-000.parquet
```

Supported quote columns include `ts_event`, `ts_recv`, `timestamp`, `bid_px_00`, `ask_px_00`, `bid_sz_00`, `ask_sz_00`, `bid`, `ask`, `bid_size`, and `ask_size`.

## 6. Replay Candidate Trades Against Quotes

```bash
PYTHONPATH=src python3 -m tlm.cli \
  --data-root data \
  data quote-replay \
  --symbol NQ_CME \
  --from 2025-03-19 \
  --to 2025-03-19 \
  --backtest-result experiments/candidate/leaderboard.json \
  --output reports/candidate_quote_replay.json
```

The report calculates validated trade count, missed fills, quote-based gross PnL, gross PnL drift versus bar replay, and average bid/ask cost.

## 7. Worker/API Task Types

The same pipeline is available through queued tasks:

- `data.import_firstrate`
- `data.bar_quality`
- `data.split_manifest`
- `data.import_databento_quotes`
- `data.quote_replay`

Corresponding API paths:

- `POST /api/data/import-firstrate`
- `POST /api/data/bar-quality`
- `POST /api/data/split-manifest`
- `POST /api/data/import-databento-quotes`
- `POST /api/data/quote-replay`

## 8. Promotion Rule

A strategy can move from `NQ_1M` discovery to quote validation only after it has positive gross edge, survives conservative bar-level costs, and remains stable across validation/test windows. A strategy can move beyond quote validation only if `NQ_CME` quote replay preserves positive edge after bid/ask costs and extra slippage stress.
