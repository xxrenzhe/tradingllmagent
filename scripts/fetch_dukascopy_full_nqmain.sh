#!/usr/bin/env bash
set -u
set -o pipefail

SYMBOL="${SYMBOL:-NQmain}"
DATA_ROOT="${DATA_ROOT:-data}"
DATE_FROM="${DATE_FROM:-2012-02-01}"
DATE_TO="${DATE_TO:-2026-04-26}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-3}"
MIN_FREE_GB="${MIN_FREE_GB:-8}"
BUILD_BARS="${BUILD_BARS:-0}"
LOG_DIR="${LOG_DIR:-logs/data-fetch}"
RUN_ID="${RUN_ID:-dukascopy-${SYMBOL}-$(date -u +%Y%m%dT%H%M%SZ)}"
LOG_FILE="${LOG_DIR}/${RUN_ID}.log"
STATUS_FILE="${LOG_DIR}/${RUN_ID}.status.tsv"
PYTHON_BIN="${PYTHON_BIN:-python3}"

mkdir -p "${LOG_DIR}"

if [ -f /etc/ssl/cert.pem ] && [ -z "${SSL_CERT_FILE:-}" ]; then
  export SSL_CERT_FILE=/etc/ssl/cert.pem
fi

export PYTHONPATH="${PYTHONPATH:-src}"
read -r -a PYTHON_CMD <<< "$PYTHON_BIN"

free_gb() {
  df -g . | awk 'NR==2 {print $4}'
}

date_range() {
  "${PYTHON_CMD[@]}" - "$DATE_FROM" "$DATE_TO" <<'PY'
from datetime import date, timedelta
import sys

start = date.fromisoformat(sys.argv[1])
end = date.fromisoformat(sys.argv[2])
current = start
while current <= end:
    print(current.isoformat())
    current += timedelta(days=1)
PY
}

tick_output_path() {
  printf "%s/normalized/ticks/%s/date=%s/part-000.parquet" "$DATA_ROOT" "$SYMBOL" "$1"
}

run_cli() {
  "${PYTHON_CMD[@]}" -m tlm.cli --data-root "$DATA_ROOT" "$@"
}

{
  printf "run_id\t%s\n" "$RUN_ID"
  printf "symbol\t%s\n" "$SYMBOL"
  printf "data_root\t%s\n" "$DATA_ROOT"
  printf "date_from\t%s\n" "$DATE_FROM"
  printf "date_to\t%s\n" "$DATE_TO"
  printf "build_bars\t%s\n" "$BUILD_BARS"
  printf "ssl_cert_file\t%s\n" "${SSL_CERT_FILE:-}"
  printf "python_bin\t%s\n" "$PYTHON_BIN"
  printf "started_at\t%s\n" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} >> "$LOG_FILE"

if [ ! -s "$STATUS_FILE" ]; then
  printf "timestamp_utc\tday\tstatus\tattempt\tfree_gb\telapsed_seconds\n" > "$STATUS_FILE"
fi

for day in $(date_range); do
  output="$(tick_output_path "$day")"
  available_gb="$(free_gb)"
  if [ "${available_gb:-0}" -lt "$MIN_FREE_GB" ]; then
    printf "%s\t%s\tstopped_low_disk\t0\t%s\t0\n" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$day" "$available_gb" >> "$STATUS_FILE"
    printf "Stopping: free disk %sGiB is below MIN_FREE_GB=%s\n" "$available_gb" "$MIN_FREE_GB" >> "$LOG_FILE"
    exit 3
  fi

  if [ -s "$output" ]; then
    printf "%s\t%s\tskipped_existing\t0\t%s\t0\n" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$day" "$available_gb" >> "$STATUS_FILE"
    continue
  fi

  attempt=1
  while [ "$attempt" -le "$MAX_ATTEMPTS" ]; do
    start_epoch="$(date +%s)"
    printf "\n[%s] download %s attempt %s\n" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$day" "$attempt" >> "$LOG_FILE"
    if run_cli data download --symbol "$SYMBOL" --from "$day" --to "$day" --granularity tick >> "$LOG_FILE" 2>&1; then
      if [ "$BUILD_BARS" = "1" ]; then
        run_cli data build-bars --symbol "$SYMBOL" --from "$day" --to "$day" --timeframe 1m >> "$LOG_FILE" 2>&1
        run_cli data build-bars --symbol "$SYMBOL" --from "$day" --to "$day" --timeframe 5m >> "$LOG_FILE" 2>&1
      fi
      elapsed="$(( $(date +%s) - start_epoch ))"
      printf "%s\t%s\tdownloaded\t%s\t%s\t%s\n" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$day" "$attempt" "$(free_gb)" "$elapsed" >> "$STATUS_FILE"
      break
    fi

    elapsed="$(( $(date +%s) - start_epoch ))"
    printf "%s\t%s\tfailed\t%s\t%s\t%s\n" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$day" "$attempt" "$(free_gb)" "$elapsed" >> "$STATUS_FILE"
    attempt="$((attempt + 1))"
    sleep "$((attempt * 5))"
  done

  if [ "$attempt" -gt "$MAX_ATTEMPTS" ]; then
    printf "[%s] giving up on %s after %s attempts; continuing\n" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$day" "$MAX_ATTEMPTS" >> "$LOG_FILE"
  fi
done

printf "finished_at\t%s\n" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$LOG_FILE"
