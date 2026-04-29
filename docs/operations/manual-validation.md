# Manual Validation Checklist

Use this checklist before enabling any profile above `paper_shadow`.

- Confirm the strategy is freeze-confirmed and has a `strategy_freeze_id`.
- Confirm `ExecutionIntent` includes `correlation_id`, `idempotency_key`, `schema_version`, and `protocol_version`.
- Confirm deterministic `RiskDecision` is `risk_approved`.
- Confirm live profile and account whitelist are explicitly enabled.
- Confirm data freshness, event blackout, spread, slippage, daily loss, and position limits pass.
- Confirm bracket/OCO or broker-side protective stop is present.
- Confirm gateway reconciliation is clean.
- Confirm emergency runbook owner can trigger read-only, cancel-all, and flatten.
