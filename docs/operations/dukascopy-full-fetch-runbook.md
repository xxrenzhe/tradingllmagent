# Dukascopy Full Fetch Runbook

## Active Run

- Symbol: `NQmain`
- Instrument: `USATECHIDXUSD`
- Date range: `2012-02-01` to `2026-04-26`
- Data root: `data`
- tmux session: `tlm_dukascopy_full`
- Run id: `dukascopy-NQmain-full-20120201-20260426`
- Python command: `arch -arm64 python3`
- Sleep prevention: `caffeinate -dimsu`
- Bars: disabled for this run; this run fetches raw hourly `.bi5` files and normalized daily tick parquet.

## Progress Checks

```bash
tmux list-sessions
tmux capture-pane -t tlm_dukascopy_full -p | tail -n 40
tail -n 40 logs/data-fetch/dukascopy-NQmain-full-20120201-20260426.status.tsv
tail -n 80 logs/data-fetch/dukascopy-NQmain-full-20120201-20260426.log
find data/normalized/ticks/NQmain -mindepth 1 -maxdepth 1 -type d | wc -l
du -sh data
df -h .
```

## Stop

```bash
tmux kill-session -t tlm_dukascopy_full
```

## Resume

```bash
tmux new-session -d -s tlm_dukascopy_full "cd /Users/jason/Documents/Kiro/tradingllmagent && RUN_ID=dukascopy-NQmain-full-20120201-20260426 DATA_ROOT=data DATE_FROM=2012-02-01 DATE_TO=2026-04-26 BUILD_BARS=0 MIN_FREE_GB=8 PYTHON_BIN='arch -arm64 python3' caffeinate -dimsu bash scripts/fetch_dukascopy_full_nqmain.sh"
```

The script skips existing non-empty daily tick parquet files, retries failed dates up to `MAX_ATTEMPTS`, and stops automatically when free disk space drops below `MIN_FREE_GB`.
