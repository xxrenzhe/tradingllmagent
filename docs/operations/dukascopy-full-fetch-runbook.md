# Dukascopy Full Fetch Runbook

## Active Run

- Symbol: `NQmain`
- Instrument: `USATECHIDXUSD`
- Date range: `2012-02-01` to `2026-04-26`
- Data root: `data`
- tmux sessions:
  - `tlm_dukascopy_2012_2015`
  - `tlm_dukascopy_2016_2017`
  - `tlm_dukascopy_2018_2019`
  - `tlm_dukascopy_2020`
  - `tlm_dukascopy_2021_2022`
  - `tlm_dukascopy_2023_2024`
  - `tlm_dukascopy_2025_2026`
- Run ids:
  - `dukascopy-NQmain-shard-2012-2015`
  - `dukascopy-NQmain-shard-2016-2017`
  - `dukascopy-NQmain-shard-2018-2019`
  - `dukascopy-NQmain-shard-2020`
  - `dukascopy-NQmain-shard-2021-2022`
  - `dukascopy-NQmain-shard-2023-2024`
  - `dukascopy-NQmain-shard-2025-2026`
- Python command: `arch -arm64 python3`
- Sleep prevention: `caffeinate -dimsu`
- Current pass: `MAX_ATTEMPTS=1` initial sweep, so provider timeout/503 dates are skipped quickly and left for later backfill.
- Bars: disabled for this run; this run fetches raw hourly `.bi5` files and normalized daily tick parquet.

## Progress Checks

```bash
tmux list-sessions
for session in tlm_dukascopy_2012_2015 tlm_dukascopy_2016_2017 tlm_dukascopy_2018_2019 tlm_dukascopy_2020 tlm_dukascopy_2021_2022 tlm_dukascopy_2023_2024 tlm_dukascopy_2025_2026; do tmux capture-pane -t "$session" -p | tail -n 20; done
for file in logs/data-fetch/dukascopy-NQmain-shard-*.status.tsv; do echo "== $file"; tail -n 20 "$file"; done
for file in logs/data-fetch/dukascopy-NQmain-shard-*.log; do echo "== $file"; tail -n 40 "$file"; done
find data/normalized/ticks/NQmain -mindepth 1 -maxdepth 1 -type d | wc -l
du -sh data
df -h .
python3 scripts/list_dukascopy_backfill_days.py --data-root data --status-dir logs/data-fetch --symbol NQmain --from 2012-02-01 --to 2026-04-26
```

## Stop

```bash
for session in tlm_dukascopy_2012_2015 tlm_dukascopy_2016_2017 tlm_dukascopy_2018_2019 tlm_dukascopy_2020 tlm_dukascopy_2021_2022 tlm_dukascopy_2023_2024 tlm_dukascopy_2025_2026; do tmux kill-session -t "$session"; done
```

## Resume

```bash
tmux new-session -d -s tlm_dukascopy_2012_2015 "cd /Users/jason/Documents/Kiro/tradingllmagent && RUN_ID=dukascopy-NQmain-shard-2012-2015 DATA_ROOT=data DATE_FROM=2012-02-01 DATE_TO=2015-12-31 BUILD_BARS=0 MIN_FREE_GB=8 MAX_ATTEMPTS=1 PYTHON_BIN='arch -arm64 python3' caffeinate -dimsu bash scripts/fetch_dukascopy_full_nqmain.sh"
tmux new-session -d -s tlm_dukascopy_2016_2017 "cd /Users/jason/Documents/Kiro/tradingllmagent && RUN_ID=dukascopy-NQmain-shard-2016-2017 DATA_ROOT=data DATE_FROM=2016-01-01 DATE_TO=2017-12-31 BUILD_BARS=0 MIN_FREE_GB=8 MAX_ATTEMPTS=1 PYTHON_BIN='arch -arm64 python3' caffeinate -dimsu bash scripts/fetch_dukascopy_full_nqmain.sh"
tmux new-session -d -s tlm_dukascopy_2018_2019 "cd /Users/jason/Documents/Kiro/tradingllmagent && RUN_ID=dukascopy-NQmain-shard-2018-2019 DATA_ROOT=data DATE_FROM=2018-01-01 DATE_TO=2019-12-31 BUILD_BARS=0 MIN_FREE_GB=8 MAX_ATTEMPTS=1 PYTHON_BIN='arch -arm64 python3' caffeinate -dimsu bash scripts/fetch_dukascopy_full_nqmain.sh"
tmux new-session -d -s tlm_dukascopy_2020 "cd /Users/jason/Documents/Kiro/tradingllmagent && RUN_ID=dukascopy-NQmain-shard-2020 DATA_ROOT=data DATE_FROM=2020-01-01 DATE_TO=2020-12-31 BUILD_BARS=0 MIN_FREE_GB=8 MAX_ATTEMPTS=1 PYTHON_BIN='arch -arm64 python3' caffeinate -dimsu bash scripts/fetch_dukascopy_full_nqmain.sh"
tmux new-session -d -s tlm_dukascopy_2021_2022 "cd /Users/jason/Documents/Kiro/tradingllmagent && RUN_ID=dukascopy-NQmain-shard-2021-2022 DATA_ROOT=data DATE_FROM=2021-01-01 DATE_TO=2022-12-31 BUILD_BARS=0 MIN_FREE_GB=8 MAX_ATTEMPTS=1 PYTHON_BIN='arch -arm64 python3' caffeinate -dimsu bash scripts/fetch_dukascopy_full_nqmain.sh"
tmux new-session -d -s tlm_dukascopy_2023_2024 "cd /Users/jason/Documents/Kiro/tradingllmagent && RUN_ID=dukascopy-NQmain-shard-2023-2024 DATA_ROOT=data DATE_FROM=2023-01-01 DATE_TO=2024-12-31 BUILD_BARS=0 MIN_FREE_GB=8 MAX_ATTEMPTS=1 PYTHON_BIN='arch -arm64 python3' caffeinate -dimsu bash scripts/fetch_dukascopy_full_nqmain.sh"
tmux new-session -d -s tlm_dukascopy_2025_2026 "cd /Users/jason/Documents/Kiro/tradingllmagent && RUN_ID=dukascopy-NQmain-shard-2025-2026 DATA_ROOT=data DATE_FROM=2025-01-01 DATE_TO=2026-04-26 BUILD_BARS=0 MIN_FREE_GB=8 MAX_ATTEMPTS=1 PYTHON_BIN='arch -arm64 python3' caffeinate -dimsu bash scripts/fetch_dukascopy_full_nqmain.sh"
```

The script skips existing non-empty daily tick parquet files, retries failed dates up to `MAX_ATTEMPTS`, and stops automatically when free disk space drops below `MIN_FREE_GB`.

## Backfill Queue

Use the backfill helper to summarize failed days without parquet output:

```bash
python3 scripts/list_dukascopy_backfill_days.py --data-root data --status-dir logs/data-fetch --symbol NQmain --from 2012-02-01 --to 2026-04-26
```

`failed_ranges` are dates with a latest logged `failed` status and no normalized parquet.
`untracked_ranges` are future dates not yet attempted by any shard.
