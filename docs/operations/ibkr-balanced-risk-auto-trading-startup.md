# IBKR Balanced-Risk Auto-Trading Startup

This runbook starts the current MNQ paper auto-trading service using `expanded_high_edge_cap24_balanced_risk`.

## Scope

- Broker mode: IBKR Paper.
- Symbol: `MNQ`.
- Strategy preset: `expanded_high_edge_cap24_balanced_risk`.
- Local API: `http://127.0.0.1:8000`.
- TWS API socket: `127.0.0.1:7497`.
- tmux session: `ibkr_soak_loop`.
- Auto-submit: enabled after readiness passes.

Do not use this runbook for live trading.

## Before Market Open

1. Confirm Globex is open or about to open. Weekend gaps are expected for NQ/MNQ.
2. Open TWS or IB Gateway in Paper Trading mode.
3. Confirm TWS API settings:
   - `Enable ActiveX and Socket Clients` is checked.
   - Socket port is `7497`.
   - localhost or `127.0.0.1` is allowed.
   - Read-only API is disabled if paper orders should be submitted.
4. Confirm paper account is selected.
5. Confirm `MNQ` quotes are visible in TWS.

## Start The Service

From repo root:

```bash
cd /Users/jason/Documents/Kiro/tradingllmagent
scripts/launch_ibkr_balanced_risk_paper.sh
```

The script restarts tmux session `ibkr_soak_loop` and launches:

```bash
PYTHONPATH=src arch -arm64 /usr/local/bin/python3 -m tlm.cli ibkr loop \
  --symbol MNQ \
  --api-port 8000 \
  --client-id 11 \
  --strategy-family expanded_high_edge \
  --expanded-high-edge-preset expanded_high_edge_cap24_balanced_risk \
  --poll-interval-seconds 15 \
  --review-interval-seconds 300 \
  --max-stale-seconds 30 \
  --max-spread-ticks 4 \
  --signal-regime-cooldown-seconds 300 \
  --take-profit-ticks-multiplier 1.25 \
  --auto-submit
```

## Verify Trade Readiness

Run:

```bash
scripts/check_ibkr_trade_ready.py --require-auto-submit
```

Required pass conditions:

- `ibkr_api_socket`: `passed=true`
- `poller_api`: `passed=true`
- `preset`: `expanded_high_edge_cap24_balanced_risk`
- `auto_submit`: `true`
- `safe_mode`: `false`
- `readiness`: `ready`
- `missing_requirements`: `[]`

If the script exits with code `2`, the service is not trade-ready.

## Inspect Runtime State

```bash
curl -s http://127.0.0.1:8000/api/gateways/ibkr/poller | python3 -m json.tool
```

High-signal fields:

- `strategy.preset`
- `strategy.max_concurrent_positions`
- `control_state.daily_trade_cap`
- `auto_submit`
- `control_state.safe_mode`
- `last_result.readiness.status`
- `last_result.readiness.missing_requirements`
- `latest_signal.signal_class`
- `latest_signal.reasons`
- `latest_decision`

Expected ready snapshot:

```json
{
  "preset": "expanded_high_edge_cap24_balanced_risk",
  "auto_submit": true,
  "safe_mode": false,
  "readiness_status": "ready",
  "missing_requirements": []
}
```

## Monitor Logs

```bash
tmux capture-pane -t ibkr_soak_loop -p | tail -120
```

Attach interactively only when needed:

```bash
tmux attach -t ibkr_soak_loop
```

Detach from tmux with `Ctrl-b` then `d`.

## Normal Operating Interpretation

- `latest_signal.signal_class = none` means no valid strategy trigger yet.
- `latest_signal.reasons = ["no_expanded_high_edge_match"]` is normal when no edge matches.
- `spread_unavailable`, `bid_missing`, `ask_missing`, or `market_data_stale` means no order can be submitted.
- During weekend or exchange maintenance, market data blockers are expected.

## Pause Or Stop Auto Trading

Disable auto-submit without stopping monitoring:

```bash
curl -s -X POST http://127.0.0.1:8000/api/gateways/ibkr/poller \
  -H "Content-Type: application/json" \
  -d '{"auto_submit": false, "reason": "manual_pause"}' | python3 -m json.tool
```

Stop the loop:

```bash
tmux kill-session -t ibkr_soak_loop
```

Restart after stopping:

```bash
scripts/launch_ibkr_balanced_risk_paper.sh
```

## Emergency Actions

Enter kill switch:

```bash
curl -s -X POST http://127.0.0.1:8000/api/gateways/ibkr/kill-switch \
  -H "Content-Type: application/json" \
  -d '{"reason":"manual_emergency"}' | python3 -m json.tool
```

Check open orders and positions in TWS manually. If a broker-side position exists, flatten it from TWS first, then reconcile the local report.

## Troubleshooting

### `ibkr_api_socket` Fails

Cause: TWS or IB Gateway API socket is not listening on `127.0.0.1:7497`.

Check:

```bash
lsof -nP -iTCP:7497 -sTCP:LISTEN
```

Fix:

- Start TWS or IB Gateway.
- Complete login or update dialogs.
- Enable API socket clients.
- Confirm port `7497`.
- Restart the service after the port is listening.

### `readiness` Is Blocked

Inspect:

```bash
curl -s http://127.0.0.1:8000/api/gateways/ibkr/poller | python3 -m json.tool
```

Common blockers:

- `gateway_safe_mode`: safe mode is active.
- `market_data_not_order_ready`: TWS has no order-ready quote.
- `bid_missing` / `ask_missing` / `last_missing`: no usable quote.
- `spread_unavailable`: cannot size risk because bid/ask are missing.
- `market_data_stale`: quote is too old.

Fix:

- Confirm market is open.
- Confirm TWS market data subscription or delayed data is available.
- Wait for fresh `bid`, `ask`, and `last`.
- Restart loop after TWS reconnects if startup connection failed.

### Weekend Or Maintenance Window

If it is Saturday in Asia/China time or outside CME Globex hours, NQ/MNQ data can pause. In that case the service can stay running, but readiness will block order submission until quotes resume.

## After Startup Checklist

```bash
scripts/check_ibkr_trade_ready.py --require-auto-submit
tmux capture-pane -t ibkr_soak_loop -p | tail -80
curl -s http://127.0.0.1:8000/api/gateways/ibkr/poller | python3 -m json.tool
```

Proceed only when the readiness check passes.

## Current Strategy Facts

`expanded_high_edge_cap24_balanced_risk` keeps the cap24 signal basket but uses lower modeled risk:

- Edge basket: same 32 cap24 edges.
- Max concurrent positions from preset: `18`.
- Max hold minutes: `300`.
- Stop range multiple: `8.0`.
- Backtest net PnL: `4,511,341.25`.
- Backtest max drawdown: `298,258.75`.
- Annual trade floor: passed.
- All checked years positive: passed.

Runtime can still override max concurrency or daily cap if CLI flags are supplied. The provided launcher does not override them, so it uses the preset/default caps.
