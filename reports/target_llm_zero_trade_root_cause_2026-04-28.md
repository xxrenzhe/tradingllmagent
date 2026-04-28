# Target LLM Zero-Trade Root Cause

Date: 2026-04-28

## Symptom

The `target_llm_grammar_search_2018` discovery run evaluated 224 trials across 28 generated grammar seeds and inverse-signal mutations. All trials reported zero test trades, so test Sharpe, win probability, and profit factor were unavailable.

## Root Cause

The generated `signal_grammar` seeds hard-code a liquidity filter:

```json
{"feature": "spread_ticks", "op": "<=", "value": 8}
```

That threshold is too strict for the current `NQmain` dataset. `NQmain` is configured as Dukascopy `USATECHIDXUSD`, a Nasdaq 100 CFD proxy, not executable CME NQ futures. During the 2018 test windows, the CFD proxy's RTH bar-level average spread was about 11.3 to 12.3 ticks, so `spread_ticks <= 8` rejected every test-session bar before entry rules could be evaluated.

## Evidence

Data coverage was not the blocker:

- Fold 0 test (`2018-06-25` to `2018-07-04`): 10 existing bar files, 3528 session bars.
- Fold 1 test (`2018-07-05` to `2018-07-14`): 10 existing bar files, 3085 session bars.

The spread filter blocked all test bars:

- Fold 0 test: `spread_ticks <= 8` passed 0 of 3528 session bars; median spread was 12.01 ticks.
- Fold 1 test: `spread_ticks <= 8` passed 0 of 3085 session bars; median spread was 12.28 ticks.
- Validation: only 6 session bars passed `spread_ticks <= 8`.
- Holdout: only 3 session bars passed `spread_ticks <= 8`.

Entry rules were not inherently silent. Re-evaluating representative family grammars on the combined test windows with only the spread filter removed produced many raw entry hits:

- Gap fade or continuation: 8522 no-spread filter plus entry hits.
- Intraday momentum: 1645 hits.
- Opening range breakout: 2391 hits.
- Regime-filtered mean reversion: 1965 hits.
- Time-of-day edge: 5837 hits.
- Trend pullback: 3261 hits.
- Volatility expansion: 2228 hits.

## Contributing Factors

- `src/tlm/strategy_generation.py` uses a fixed `spread_ticks <= 8` gate for every generated grammar family.
- `src/tlm/features.py` computes `spread_ticks` as `avg_spread / tick_size`; with `tick_size = 0.25`, a 3-point CFD spread becomes roughly 12 ticks.
- `configs/symbols.yaml` correctly labels `NQmain` as a CFD proxy, but the generator does not calibrate liquidity gates from the observed spread distribution.
- The search report's zero-trade result is therefore a screening artifact, not proof that all entry concepts have no signal.

## Fix Direction

- Calibrate generated spread gates per symbol and provider, for example use a quantile-based threshold from train/validation data instead of a fixed constant.
- Separate CME futures assumptions from CFD proxy assumptions; do not reuse futures-like spread thresholds on `USATECHIDXUSD`.
- Add a signal-health diagnostic before expensive walk-forward validation that reports filter pass counts, entry pass counts, and post-filter raw candidate counts by split.
- Fail fast when any universal filter has zero pass count in validation or test-like screening windows.
