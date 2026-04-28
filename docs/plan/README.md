# Docs Plan Coordination

## 1. Precedence Rule

When documents in this directory conflict, the newer generated or updated document is authoritative for the conflicting topic.

Older documents remain useful as design history, but they should not override newer architecture boundaries, stage definitions, API contracts, or acceptance criteria.

中文口径：`docs/plan` 下如果出现方案冲突，以后生成或后更新的文档为准。旧文档保留为设计历史，但不能覆盖新的架构边界、阶段定义、API 契约或验收标准。

## 2. Current Authority Map

- `docs/plan/1.local-llm-nq-strategy-system.md`: authoritative for the v1 research system, data pipeline, Strategy Spec, backtesting, validation, leaderboard, paper replay, and no-live-execution boundary.
- `docs/plan/2.macro-event-aware-nq-strategy-optimization.md`: authoritative for structured macro event inputs, event context, event policy concepts, and event-risk attribution.
- `docs/plan/3.runtime-market-monitor-optimization.md`: authoritative for runtime market snapshots, key-level scanning, strong-signal review, and paper-only runtime observation.
- `docs/plan/5.agent-driven-trading-system-optimization.md`: authoritative for strategy module memory, 5m/15m cadence, trade intent boundaries, and the high-level research-to-execution layering.
- `docs/plan/4.nt8-live-integration-roadmap.md`: authoritative for post-v1 execution readiness, NT8 gateway evolution, paper shadow, sim, micro-live, controlled live, incident response, and execution safety.
- `docs/plan/6.integrated-trading-system-implementation-plan.md`: authoritative for cross-document implementation sequencing, phase gates, and immediate landing order.
- `docs/plan/7.current-trading-system-optimization-roadmap.md`: authoritative for current completeness assessment, remaining optimization priorities, and next implementation milestones.
- `docs/plan/8.llm-trigger-gate-optimization-plan.md`: authoritative for triggered LLM gating, target-frequency strategy pool selection, token-cost control, memory backfill, and forward-test iteration.
- `docs/plan/9.llm-trading-optimization-factory.md`: authoritative for the full feature-mining, strategy-generation, backtest-validation, paper/sim/live replay, anti-overfit, and LLM-assisted optimization loop.
- `docs/plan/10.strategy-search-best-practices-optimization-plan.md`: authoritative for first-search failure analysis, online best-practice research synthesis, executable feature/grammar upgrades, inverse mutation, pre-screening, and the next strategy-search optimization plan.
- `docs/plan/11.nq-historical-data-acquisition-plan.md`: authoritative for NQ historical data source selection, FirstRate/Databento data layering, free-data boundaries, acquisition sequencing, and quote-level execution validation.
- `docs/plan/12.vol-execution-aware-llm-trading-optimization-plan.md`: authoritative for VOL-centered simple strategy search, OHLCV-to-execution validation boundaries, limit-order modeling, paper shadow LLM review, and the next NQ_CME optimization loop.

## 3. Version Boundaries

- `v1 research`: local data, Strategy Spec validation, bar/tick backtests, rolling validation, leaderboard, paper replay, offline NT export, module memory, macro event context, and runtime monitor reports.
- `v2 execution readiness`: OpenAPI contract hardening, typed clients, paper shadow, sim gateway, reconciliation model, execution audit, and runbooks.
- `v3 controlled live`: micro-live, live profile approval, broker-side protective orders, incident handling, and controlled automation.

No `v1 research` feature should require a live account, Windows-only NT8 runtime, or broker connectivity.

## 4. Contract Source Of Truth

`docs/openapi/tradingllmagent.openapi.yaml` is the source of truth for HTTP API contracts. Plan documents may list intended endpoints, but implementation should update the OpenAPI contract first when endpoint details diverge.

## 5. Implementation Notes

The repository may contain partial implementations of later-stage concepts for contract testing or local simulation. Those local stubs do not imply live execution readiness unless the relevant `v2` or `v3` acceptance criteria and runbooks are complete.

## 6. Immediate Landing Order

The current implementation should follow this order:

1. Stabilize v1 research artifacts: Strategy Spec, module id, leaderboard, report artifacts, hidden final holdout, and reproducibility hashes.
2. Add structured context: macro event calendar, event context, runtime snapshot, key-level scanner, and paper-only monitor reports.
3. Add memory and review layer: strategy cards, module performance memory, LLM report reviews, debate ranking, and global-memory events.
4. Add triggered LLM gate discipline: target-frequency strategy pool, deterministic pre-gates, structured LLM decisions, token accounting, and memory outcome backfill.
5. Add execution contract without live permission: OpenAPI schemas, typed client, `ExecutionIntent`, `RiskDecision`, audit events, and mock/sim gateway contracts.
6. Add NT8 sim boundary: independent gateway process, heartbeat, ACK/NACK, reconciliation, incident events, and sim-only validation.
7. Add controlled live only after gates: paper shadow, micro-live with manual approval, broker-side protection, runbooks, and live profile whitelist.
8. Operate the LLM trading optimization factory: governed feature registry, audited strategy factory, anti-overfit promotion gates, paper/sim/live replay attribution, and memory-driven next-round generation.
9. Upgrade the NQ data foundation: acquire low-cost multi-year 1m bars for search, reserve Databento CME quotes for candidate execution validation, and keep free proxy data out of final profitability claims.
10. Run the VOL/execution-aware loop on `NQ_CME`: expose `bar_volume`, add VOL feature layers, search simple MA/VOL/MACD/RSI/ATR strategy families, validate execution with quote data, and use paper shadow reports for LLM-guided small-step mutations.

Do not start from live order buttons, unrestricted NT8 control, or free-form LLM trading actions. Those are downstream capabilities gated by `docs/plan/4.nt8-live-integration-roadmap.md`.
