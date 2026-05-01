# NQ SMC LQ-EM CE v1 Implementation Plan

## 1. Objective

This document converts the PhotonTrading SMC strategy into a deterministic NQ futures implementation that can be backtested, validated with quote or tick replay, and routed to IBKR paper trading through bracket orders.

The first production candidate is `smc_lqem_ce_v1`: a rule-based subset of the PhotonTrading framework using market structure, premium/discount, unmitigated order blocks, liquidity sweep, and CE confirmation. It intentionally excludes discretionary course concepts that do not yet have explicit formulas, such as advanced order flow, discretionary supply/demand flips, and subjective sweep zones.

The implementation target is:

- Research symbol: `NQ_CME`
- Paper/runtime symbol: `MNQ_IBKR`
- Base timeframe: completed 1-minute bars
- Higher timeframe: completed 15-minute bars built from 1-minute bars
- Execution order: parent limit order with stop and take-profit bracket
- Default deployment path: backtest, quote/tick replay, MNQ paper soak, then live review

## 2. Source Mapping

The strategy is based on `docs/PhotonTrading/smc_strategy.md`, especially the sequence:

- 15-minute market structure direction
- 15-minute discount or premium POI
- unmitigated order block
- PBL liquidity sweep
- 1-minute ChoCh confirmation
- limit entry at the 1-minute order block
- cancellation if target is reached before fill or higher timeframe structure invalidates

`docs/PhotonTrading/lightglow.md` is used only as an algorithm reference for pivots, structure tags, order block storage, FVG detection, and premium/discount zones. It must not be copied into backtests as-is because the Pine implementation includes charting concerns and higher-timeframe `lookahead_on` behavior that would create forward-looking bias in Python.

External contract references:

- CME NQ: E-mini Nasdaq-100 futures use a 0.25 index point minimum tick and $20 per index point multiplier.
- CME MNQ: Micro E-mini Nasdaq-100 futures use a 0.25 index point minimum tick and $2 per index point multiplier.
- Runtime defaults must still come from local configs, currently `configs/symbols.yaml` and `configs/costs.yaml`.

Official CME references:

- https://www.cmegroup.com/markets/equities/nasdaq/e-mini-nasdaq-100.contractSpecs.html
- https://www.cmegroup.com/markets/equities/nasdaq/micro-e-mini-nasdaq-100.contractSpecs.html

## 3. Non-Negotiable Engineering Constraints

The strategy is only considered implemented when all of these are true:

- No future bars are used in signal timing. A pivot at bar `i` with right confirmation `N` becomes usable only after bar `i + N` closes.
- All price levels are rounded to `tick_size`.
- All risk calculations use futures point value, not forex pips.
- Backtest and runtime share the same SMC primitives and state machine.
- Every order has a server-side or broker-side protective stop and take-profit before submission is marked valid.
- Every signal emits an audit object containing source bars, selected pivots, selected OB, sweep evidence, entry zone, stop, target, and invalidation rules.
- Backtest results include cost stress using current NQ/MNQ cost models.
- Paper trading starts on `MNQ_IBKR` with one contract maximum until paper gates pass.

## 4. Instrument and Session Rules

### 4.1 Price Units

Use these local defaults:

- `NQ_CME`: `tick_size=0.25`, `point_value=20`, `tick_value=5`
- `MNQ_IBKR`: `tick_size=0.25`, `point_value=2`, `tick_value=0.5`

Formulas:

```text
ticks = round(price_distance / tick_size)
points = ticks * tick_size
risk_usd = abs(entry_price - stop_price) * point_value * contracts
contracts = floor(max_trade_risk_usd / (abs(entry_price - stop_price) * point_value))
```

Default runtime sizing:

- Paper: `MNQ_IBKR`, `contracts=1`, no percentage sizing until execution is stable.
- Research: calculate normalized risk using `NQ_CME` point value, but report results per one NQ contract.
- Live candidate: enable percentage sizing only after MNQ paper gates pass.

### 4.2 Trading Windows

The default NQ implementation should focus on regular-session liquidity and avoid the least stable parts of the day:

- Session timezone: `America/New_York`
- Long/short signal windows: `09:35-11:30` and `13:30-15:30`
- No new entries: after `15:30`
- Flatten deadline: `15:55`
- Optional overnight research window: disabled in v1

News handling:

- Use existing macro event context if available.
- Block new entries from 10 minutes before to 10 minutes after high-impact releases.
- Keep existing protective orders active during news unless the kill switch or flatten rule triggers.

## 5. Data Contract

The SMC engine consumes canonical 1-minute bars with:

```text
symbol, timestamp, open, high, low, close, bid_close, ask_close, tick_count, avg_spread
```

Rules:

- `timestamp` is the completed bar timestamp.
- 15-minute bars are created only from completed 1-minute bars.
- A 15-minute bar ending at `10:00` can be evaluated only after its close is known.
- Tick/quote replay is required before any live candidate because limit order fill quality cannot be trusted from OHLC alone.
- Roll handling follows the existing `NQ_CME` data pipeline; execution uses resolved concrete `MNQ_IBKR` contract month or local symbol.

## 6. Core Data Structures

Required Python dataclasses:

```text
Bar(time, open, high, low, close, volume_proxy, bid_close, ask_close)
Pivot(time, price, kind, source_timeframe, confirmed_at, index, broken)
StructureEvent(time, direction, event_type, pivot, close_price, source_timeframe)
OrderBlock(time, direction, low, high, body_low, body_high, origin_index, created_by, mitigated, invalidated)
FairValueGap(time, direction, low, high, origin_index)
LiquiditySweep(time, direction, pbl_price, sweep_extreme, reclaim_close, reclaim_bars)
SmcSetup(direction, htf_ob, pbl, sweep, htf_range, discount_premium_context)
SmcSignal(direction, entry_price, stop_price, take_profit_price, quantity_hint, reason, audit)
SmcState(state, active_setup, pending_signal, active_bracket_id)
```

`direction` values are `long` and `short`. Structure event types are `BOS` and `CHOCH`.

## 7. Algorithm Definitions

### 7.1 Pivot Detection

Parameters:

```text
htf_swing_left = 3
htf_swing_right = 3
ltf_swing_left = 2
ltf_swing_right = 2
```

Definitions:

- Swing high at index `i`: `high[i] > max(high[i-left:i])` and `high[i] > max(high[i+1:i+right+1])`
- Swing low at index `i`: `low[i] < min(low[i-left:i])` and `low[i] < min(low[i+1:i+right+1])`
- Confirmed time: close time of bar `i + right`
- Live use: the pivot is not visible until confirmed time.

Tie handling:

- If equal highs or lows occur inside the confirmation window, keep the earliest pivot only when the distance is at least `equal_level_tolerance_ticks=2`.
- Otherwise ignore the pivot to avoid unstable structure churn.

### 7.2 BOS and ChoCh

Use candle close only.

Bullish break:

```text
close > pivot_high.price + break_buffer_ticks * tick_size
```

Bearish break:

```text
close < pivot_low.price - break_buffer_ticks * tick_size
```

Default:

```text
break_buffer_ticks = 1
```

Tagging:

- Bullish break while HTF trend is bearish or neutral: `CHOCH`
- Bullish break while HTF trend is bullish: `BOS`
- Bearish break while HTF trend is bullish or neutral: `CHOCH`
- Bearish break while HTF trend is bearish: `BOS`

The trend flips after a valid `CHOCH`; continuation is confirmed by a subsequent `BOS`.

### 7.3 Premium and Discount

Use the most recent confirmed HTF swing range:

```text
range_low = latest confirmed swing low
range_high = latest confirmed swing high
equilibrium = (range_low + range_high) / 2
```

Long POI filter:

```text
ob_midpoint <= equilibrium
```

Short POI filter:

```text
ob_midpoint >= equilibrium
```

Reject setup if:

- swing range is missing
- `range_high <= range_low`
- range size is below `min_htf_range_ticks=80`

### 7.4 Fair Value Gap and Displacement

Bullish FVG:

```text
low[i] > high[i-2]
```

Bearish FVG:

```text
high[i] < low[i-2]
```

Because NQ often trends without clean textbook gaps on 1-minute bars, FVG is not a hard requirement in v1. Use a displacement filter instead:

```text
abs(close[break_bar] - open[break_bar]) >= displacement_atr_multiple * atr_14
```

Default:

```text
displacement_atr_multiple = 0.50
fvg_required = false
```

If `fvg_required=true`, the FVG must occur between the OB origin and the break bar.

### 7.5 Order Block

Bullish HTF OB:

- Created after a bullish `BOS` or bullish `CHOCH`.
- Find the last bearish candle between the broken pivot origin and the break candle.
- Zone defaults to the defensive body-to-wick range:

```text
zone_low = candle.low
zone_high = max(candle.open, candle.close)
```

Bearish HTF OB:

- Created after a bearish `BOS` or bearish `CHOCH`.
- Find the last bullish candle between the broken pivot origin and the break candle.
- Zone defaults to:

```text
zone_low = min(candle.open, candle.close)
zone_high = candle.high
```

Quality filters:

- OB height must be between `min_ob_ticks=8` and `max_ob_ticks=120`.
- Break candle must pass the displacement filter.
- Optional volume proxy: `tick_count >= sma(tick_count, 50) * 1.05`.

Mitigation:

- Bullish OB remains unmitigated until a later bar has `low <= zone_high`.
- Bearish OB remains unmitigated until a later bar has `high >= zone_low`.
- The first mitigation touch can activate a setup only if the sweep and session filters are valid.
- If price closes through the far side before LTF confirmation, the OB is invalidated.

### 7.6 PBL and Liquidity Sweep

For long setups:

- PBL is the latest confirmed LTF or HTF swing low formed after HTF OB creation and before first OB touch.
- PBL price must be above `htf_ob.zone_high + pbl_clearance_ticks * tick_size`.
- Default `pbl_clearance_ticks = 4`.

Long sweep:

```text
bar.low < pbl.price - sweep_buffer_ticks * tick_size
and bar.close > pbl.price
```

Short setup is inverse:

```text
bar.high > pbl.price + sweep_buffer_ticks * tick_size
and bar.close < pbl.price
```

Default:

```text
sweep_buffer_ticks = 1
max_reclaim_bars = 3
```

If wick sweep does not reclaim on the same bar, allow reclaim within `max_reclaim_bars` only if price has not closed beyond the HTF OB far side.

### 7.7 CE Confirmation on 1-Minute Bars

After sweep and HTF OB touch, monitor 1-minute structure.

Long CE:

- Identify the latest confirmed 1-minute swing high after the sweep.
- Trigger when a 1-minute candle closes above that swing high plus `break_buffer_ticks`.
- The 1-minute ChoCh must occur before price hits the HTF target.
- Build the micro bullish OB from the last bearish candle between the post-sweep low and the ChoCh candle.

Short CE is inverse.

Reject CE if:

- micro OB height is below `min_micro_ob_ticks=4`
- micro OB height is above `max_micro_ob_ticks=60`
- the resulting stop distance is outside allowed risk bounds
- spread exceeds `max_spread_ticks=4`
- session is outside the allowed signal window

### 7.8 Entry, Stop, Target, and Cancellation

Long entry:

```text
entry = round_to_tick(micro_ob.zone_high)
stop = round_to_tick(min(micro_ob.zone_low, sweep.sweep_extreme) - stop_buffer_ticks * tick_size)
```

Short entry:

```text
entry = round_to_tick(micro_ob.zone_low)
stop = round_to_tick(max(micro_ob.zone_high, sweep.sweep_extreme) + stop_buffer_ticks * tick_size)
```

Defaults:

```text
stop_buffer_ticks = 4
min_stop_ticks = 8
max_stop_ticks = 80
min_reward_r = 2.0
default_take_profit_r = 3.0
pending_ttl_bars = 10
```

Target priority:

- Nearest opposing confirmed HTF weak high or weak low with at least `min_reward_r`.
- Prior 15-minute swing high for long or swing low for short.
- Fallback fixed `default_take_profit_r`.

V1 uses a single take-profit order because the current IBKR bracket path is single target. Partial exits can be implemented later as multiple OCA child orders or multiple bracket drafts, but they are not required for v1.

Cancel pending order if:

- target is touched before parent limit fill
- parent is not filled within `pending_ttl_bars`
- micro invalidation closes beyond stop side before fill
- HTF opposite BOS occurs
- session reaches no-new-entry cutoff
- risk gateway reports stale market data, spread violation, missing contract, kill switch, or safe mode

## 8. State Machine

States:

```text
IDLE
FINDING_HTF_POI
WAITING_FOR_PBL
WAITING_FOR_SWEEP
MONITORING_LTF_CHOCH
ORDER_PENDING
IN_POSITION
COOLDOWN
```

Transitions:

- `IDLE -> FINDING_HTF_POI`: a valid HTF trend and swing range exist.
- `FINDING_HTF_POI -> WAITING_FOR_PBL`: a valid unmitigated HTF OB passes premium/discount and quality filters.
- `WAITING_FOR_PBL -> WAITING_FOR_SWEEP`: a PBL forms above a bullish OB or below a bearish OB.
- `WAITING_FOR_SWEEP -> MONITORING_LTF_CHOCH`: sweep reclaims and price touches the HTF OB.
- `MONITORING_LTF_CHOCH -> ORDER_PENDING`: LTF ChoCh creates a valid micro OB and bracket plan.
- `ORDER_PENDING -> IN_POSITION`: parent limit fills.
- `ORDER_PENDING -> COOLDOWN`: any cancellation rule triggers.
- `IN_POSITION -> COOLDOWN`: stop, take profit, flatten, or manual kill switch completes the bracket.
- `COOLDOWN -> IDLE`: cooldown bars have elapsed and no stale state remains.

Default:

```text
cooldown_bars_after_cancel = 5
cooldown_bars_after_exit = 15
max_setups_per_day = 3
max_filled_trades_per_day = 2
```

## 9. Risk Controls

Research defaults:

- one NQ contract equivalent for reporting
- cost model `nq_conservative_v1`
- stress tests at 1x, 2x, and 3x configured slippage

MNQ paper defaults:

- one MNQ contract
- one open bracket maximum
- auto-submit allowed only after readiness is green
- daily loss stop: `-3R` or configured USD equivalent
- daily order reject stop: 2 rejected bracket builds
- stale data cutoff: existing IBKR readiness threshold

Live candidate defaults:

- start with MNQ, not NQ
- max risk per trade: 0.25% of net liquidation
- max daily loss: 1.0% of net liquidation
- max weekly loss: 2.0% of net liquidation
- max contracts: bounded by both risk formula and explicit gateway cap

Risk formula:

```text
stop_points = abs(entry_price - stop_price)
per_contract_risk = stop_points * point_value
contracts = floor(min(account_risk_usd, daily_remaining_risk_usd) / per_contract_risk)
```

Reject if `contracts < 1`.

## 10. Backtest Requirements

The backtester must support:

- new family `smc_lqem_ce`
- completed-bar 1-minute loop
- 15-minute aggregation with no lookahead
- limit order pending state
- intrabar conservative fill assumptions
- stop/target conflict resolution
- event policy attribution
- signal health report
- state transition audit export

Conservative OHLC fill rules:

- Entry limit fills only if the next bars trade through the limit price.
- If stop and target both touch in the same bar after entry, assume stop first unless tick replay is available.
- If parent limit and target touch in the same bar before a known fill sequence, do not count target first; mark as ambiguous and require tick replay for validation.

Validation windows:

- minimum train window: 2 years
- validation window: 6 months
- test window: 6 months
- final holdout: latest 12 months
- embargo: 5 trading days between windows

Minimum research gates:

- at least 200 completed trades across full in-sample and validation
- positive expectancy after 2x cost stress
- no single year contributes more than 40% of net profit
- final holdout profit factor above 1.05
- max drawdown less than 2.5x annualized expected profit
- median trade duration below 45 minutes unless explicitly approved

## 11. Runtime Integration

Runtime should reuse existing IBKR gateway pieces:

- `IbkrPaperGateway` contract readiness
- resolved front-month `MNQ_IBKR` contract
- bracket order build and submit
- order cancellation
- kill switch and safe mode
- bracket order report

Required new runtime adapter:

```text
SmcRuntimeLoop
```

Responsibilities:

- warm-start 1-minute history
- build 15-minute bars
- update shared SMC state
- emit `SmcSignal`
- convert signal to gateway bracket payload
- cancel stale pending brackets
- record audit artifacts under the existing paper run directory

Bracket payload mapping:

```text
action = BUY for long, SELL for short
entry_order_type = LMT
entry_price = signal.entry_price
stop_price = signal.stop_price
take_profit_price = signal.take_profit_price
quantity = signal.quantity_hint or risk-sized quantity
```

## 12. File-Level Implementation Map

Expected code changes:

- `src/tlm/smc.py`: pivots, structure events, order blocks, FVG, premium/discount, sweep detection, signal dataclasses.
- `src/tlm/smc_state.py`: event-driven state machine and audit objects.
- `src/tlm/backtest.py`: register `smc_lqem_ce` and call the SMC backtest runner.
- `src/tlm/strategy.py`: allow `smc_lqem_ce` family and validate SMC parameter schema.
- `src/tlm/ibkr_paper.py` or a sibling runtime module: wire SMC signal generation into paper decision cycles.
- `strategies/nq_smc_lqem_ce_v1.yaml`: first executable strategy spec.
- `tests/test_smc.py`: deterministic algorithm tests.
- `tests/test_smc_backtest.py`: backtest and no-lookahead tests.
- `tests/test_ibkr_smc_runtime.py`: bracket payload, cancellation, and readiness tests.

No separate Backtrader dependency is required. The existing project backtester and gateway should be extended instead.

## 13. Test Matrix

Unit tests:

- pivot is confirmed only after right-side bars close
- equal highs/lows do not create duplicate unstable pivots
- BOS and ChoCh use close, not wick
- bullish and bearish OB zones are selected correctly
- mitigation touch activates once and then consumes the zone
- sweep requires pierce and reclaim
- LTF ChoCh creates a micro OB only after valid sweep
- stop, target, and entry prices round to tick

Backtest tests:

- 1-minute to 15-minute aggregation has no lookahead
- pending limit order fills conservatively
- ambiguous same-bar stop/target is marked correctly
- cancellation fires when target is hit before fill
- event policy blocks new entries during high-impact windows
- signal audit contains all required fields

Runtime tests:

- SMC signal maps to valid IBKR bracket payload
- invalid spread or stale market data rejects signal
- pending bracket cancels on TTL expiry
- kill switch cancels brackets and flattens position
- paper loop does not submit if readiness is incomplete

## 14. Acceptance Gates

Implementation gate:

- all SMC unit tests pass
- strategy spec validator accepts `nq_smc_lqem_ce_v1.yaml`
- backtest runner produces deterministic trades on synthetic data
- no-lookahead test fails if right-side pivot confirmation is removed

Research gate:

- run on full available `NQ_CME` 1-minute history
- run walk-forward and final holdout
- run 1x, 2x, 3x cost stress
- produce strategy report with trade distribution, yearly performance, drawdown, R distribution, and setup audit samples

Execution validation gate:

- quote/tick replay confirms candidate fill quality
- MNQ paper dry-run builds bracket drafts without auto-submit
- MNQ paper auto-submit runs at least 20 RTH sessions
- no orphan bracket orders
- no missing stop or target after accepted parent order
- realized slippage stays within configured stress envelope

Promotion gate:

- human review of paper report
- live disabled by default
- first live phase uses MNQ only
- NQ full-size trading requires a separate bd issue and explicit approval

## 15. Default Parameter Block

```json
{
  "strategy_family": "smc_lqem_ce",
  "symbol": "NQ_CME",
  "timeframe": "1m",
  "direction": "long_short",
  "session": {
    "timezone": "America/New_York",
    "trade": "09:35-11:30,13:30-15:30",
    "flatten": "15:55"
  },
  "parameters": {
    "htf_minutes": 15,
    "htf_swing_left": 3,
    "htf_swing_right": 3,
    "ltf_swing_left": 2,
    "ltf_swing_right": 2,
    "break_buffer_ticks": 1,
    "min_htf_range_ticks": 80,
    "min_ob_ticks": 8,
    "max_ob_ticks": 120,
    "min_micro_ob_ticks": 4,
    "max_micro_ob_ticks": 60,
    "pbl_clearance_ticks": 4,
    "sweep_buffer_ticks": 1,
    "max_reclaim_bars": 3,
    "stop_buffer_ticks": 4,
    "min_stop_ticks": 8,
    "max_stop_ticks": 80,
    "min_reward_r": 2.0,
    "default_take_profit_r": 3.0,
    "pending_ttl_bars": 10,
    "max_spread_ticks": 4,
    "max_setups_per_day": 3,
    "max_filled_trades_per_day": 2,
    "fvg_required": false,
    "displacement_atr_multiple": 0.5,
    "relative_tick_count_min": 1.05
  },
  "risk": {
    "position_sizing": {
      "type": "fixed_contracts",
      "contracts": 1
    },
    "max_position_contracts": 1,
    "max_daily_loss_r": 3
  },
  "cost_model": "nq_conservative_v1"
}
```

## 16. Known Limits

This plan does not claim that SMC has edge on NQ before testing. It only makes the strategy mechanically implementable and testable.

The first version deliberately avoids:

- subjective discretionary POI selection
- advanced order-flow tape reading
- partial exits
- pyramiding
- overnight execution
- full-size NQ live trading

These can be added after v1 generates audited evidence.

## 17. bd Work Breakdown Reference

Implementation work is tracked in bd, not in markdown task lists. The related bd epic and child issues created for this plan are the source of truth for sequencing, ownership, and completion state.

Issue map:

| bd issue | Scope | Depends on |
| --- | --- | --- |
| `tradingllmagent-i7m4` | Epic: implement NQ SMC LQ-EM strategy | Child issue IDs are recorded in epic notes |
| `tradingllmagent-ysm0` | SMC primitives and no-lookahead data model | None |
| `tradingllmagent-kxa9` | SMC LQ-EM state machine | `tradingllmagent-ysm0` |
| `tradingllmagent-ukc7` | Backtest integration and strategy spec | `tradingllmagent-kxa9` |
| `tradingllmagent-8nr8` | Validation reports and cost stress | `tradingllmagent-ukc7` |
| `tradingllmagent-pwo4` | IBKR paper runtime wiring | `tradingllmagent-ukc7` |
| `tradingllmagent-hokm` | MNQ paper soak and promotion review | `tradingllmagent-8nr8`, `tradingllmagent-pwo4` |
