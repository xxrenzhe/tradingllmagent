# 当前交易系统完整性评估与优化路线图

> 协调说明：本文晚于当前 `docs/plan` 下其他方案生成。本文是当前系统“下一轮优化优先级、完整性差距、落地阶段和验收标准”的权威文档；若与更早文档冲突，以本文对优化顺序和验收口径为准。具体领域细节仍以各专项文档为参考。

## 1. 当前结论

当前系统已经完成“本地研究闭环 + 受控执行契约 + NT8 sim/mock 边界”的主体骨架，但还不是完整生产交易系统。

更准确的状态是：

- 研究系统已经可用：Strategy Spec、数据管道、bar/tick 回测、rolling validation、final holdout 隔离、leaderboard、报告 artifact 和测试都已具备。
- 策略沉淀已经起步：已有 module catalog、strategy card、module performance memory，但缺少完整的模块版本治理和晋级/退役状态机。
- 宏观事件与运行期监控已有基础：能解析事件、生成 event context、扫描关键位、输出 signal class，但事件 policy 还没有深度进入回测撮合和执行链路。
- 执行契约已有安全底座：ExecutionIntent、RiskDecision、GatewayCommand、OpenAPI 基础 schema、paper shadow audit 和 live readiness gate 已具备。
- NT8 目前只是本地 sim/mock：已有命令集、幂等、safe mode、read-only、reconciliation，但还没有真实 NT8 AddOn 或独立 gateway 进程。
- 实盘阶段尚未完成：micro-live、controlled-live 只有准入 gate、runbook 和默认阻断，没有真实 NT8、broker、交易日样本和事故演练验证。

因此，后续优化不能从“加真实下单按钮”开始，而应优先补齐影响研究可信度、运行期一致性和执行安全性的中间层。

## 2. 完整性分级

| 能力域 | 当前完整度 | 主要依据 | 主要缺口 |
| --- | --- | --- | --- |
| 数据下载与标准化 | 70% | Dukascopy tick、bar 聚合、质量报告已有 | 长周期真实数据覆盖、缺口修复、rollover/CFD proxy 偏差治理不足 |
| Strategy Spec 与校验 | 75% | 受控 family、反马丁、参数预算、代码注入拒绝已有 | YAML parser、schema version 演进、模块强绑定仍需加强 |
| 回测与验证 | 75% | rolling validation、final holdout、tick replay、成本模型已有 | 事件 policy 未深度进入撮合，tick replay 仅部分 family 原生支持 |
| Leaderboard 与报告 | 75% | candidate/freeze-confirmed 分层、artifact、manifest 已有 | 报告对比、回归分析、LLM review artifact 仍薄 |
| 模块记忆 | 55% | module catalog、performance memory 汇总已有 | 模块版本、promotion gates、退役、复测周期缺失 |
| 宏观事件 | 50% | event calendar、context、字段别名、release window 已有 | 官方源导入、事件归因回测、point-in-time actual/surprise 治理缺失 |
| Runtime monitor | 45% | snapshot、key-level scanner、signal class 已有 | 强信号 LLM 复核、paper intent 链路、持续运行和历史索引不足 |
| Execution/Risk | 55% | intent、risk gate、gateway command、audit 已有 | 状态机持久化、approval queue、risk profile registry 不足 |
| NT8 gateway | 30% | 本地 sim gateway 命令和安全状态已有 | 独立进程、真实 NT8 AddOn、断线重连现场验证缺失 |
| WebUI | 35% | 研究控制台已有 | Execution Readiness、intent queue、incident timeline、monitor view 不足 |
| 实盘 readiness | 15% | runbook 和 live gate 已有 | 外部 NT8/broker 验证、paper shadow 天数、micro-live 样本全部未完成 |

## 3. 优化原则

- 先提高研究结论可信度，再增加执行能力。
- 先补 contract 和 state machine，再接真实平台。
- 先让 paper shadow 可重放，再允许 sim gateway 承接策略信号。
- 先让模块可晋级/退役，再让 LLM 基于模块生成 live-bound 策略。
- 先做事件和成本的保守压力测试，再考虑提高交易频率。
- 所有 live 能力默认关闭，只有 readiness gate 明确通过才允许进入下一阶段。

## 4. 第一优先级：事件 policy 接入回测和归因

### 问题

当前事件能力主要停留在 calendar、context 和 monitor 阻断。Strategy Spec 可以声明 `event_policy`，但 bar/tick backtest 还没有完整执行事件窗口内的阻断、降权、延迟、滑点扩展和归因统计。

### 优化内容

- 在 bar backtest 中按 timestamp join event context。
- 支持 `new_entries: block`、`require_extra_confirmation`、`reduce_signal_weight`。
- 在 tick replay 中支持事件窗口滑点扩展。
- 每笔 trade 增加事件字段：`event_state_at_entry`、`active_event_ids_at_entry`、`event_policy_action`。
- leaderboard 增加事件风险列：`event_dependency_ratio`、`non_event_sharpe`、`event_window_drawdown`。
- 报告展示事件窗口和非事件窗口表现对比。

### 验收标准

- 高影响事件 release window 内默认不产生新开仓。
- 事件窗口被阻断的信号能进入 audit/report。
- 剔除事件窗口后策略仍能单独计算表现。
- test 和 final holdout 的事件细节仍不泄露给同轮 LLM。

## 5. 第二优先级：OpenAPI 从局部契约升级为全系统契约

### 问题

当前 OpenAPI 主要覆盖 execution/gateway 基础路径，尚未完整覆盖 data、tasks、research、leaderboard、artifacts、events、monitor、paper replay 和 readiness。

### 优化内容

- 补齐以下 API contract：
  - data symbols、quality、download/build-bars task。
  - research run/propose/iterate、leaderboard、artifacts。
  - events validate/list/build-context。
  - monitor once/run/replay/report。
  - execution intents、paper-shadow、readiness。
  - NT8 gateway health/accounts/instruments/commands/reconciliation/incidents。
- 增加 schema：`Task`、`ResearchRunRequest`、`LeaderboardReport`、`EventCalendar`、`MonitorReport`、`ReadinessDecision`。
- 前端 API client 不再手写散落路径，统一从 contract 封装。
- 添加 contract tests，确保 FastAPI 返回字段与 OpenAPI 保持一致。

### 验收标准

- 所有 WebUI 调用路径在 OpenAPI 中有定义。
- execution/gateway schema 包含 `schema_version`、`protocol_version`、`correlation_id`、`idempotency_key`。
- contract tests 覆盖核心 API response shape。

## 6. 第三优先级：完整 paper shadow 链路

### 问题

当前 paper shadow 主要是 intent audit，不是完整的 frozen strategy -> market snapshot -> signal -> risk -> hypothetical fill -> drift report 链路。

### 优化内容

- 新增 `paper_shadow_run` 状态表或 JSONL artifact。
- 从 freeze-confirmed strategy card 启动 paper shadow。
- 每个 runtime snapshot 经过 Signal Engine 生成 `trade_intent_candidate`。
- Risk Gateway 输出 `risk_approved` 或 `risk_rejected`。
- Hypothetical fill 使用当前 bid/ask、spread、slippage model 和 bracket。
- 生成 drift report：回测成本假设 vs 运行期 spread/slippage/hypothetical fill。
- 支持 replay：同一 snapshot + strategy hash 可重放出同一 intent 和 risk decision。

### 验收标准

- paper shadow 不提交任何 gateway live command。
- 每个 intent 都能追溯到 strategy hash、module_id、snapshot hash、risk decision。
- 连续 N 天运行后可生成 spread/slippage drift 统计。
- 出现 data stale、event blackout、kill switch 时只记录阻断，不生成可执行命令。

## 7. 第四优先级：模块版本治理和晋级/退役状态机

### 问题

当前 module memory 能汇总实验表现，但缺少模块生命周期。随着策略越来越多，如果没有版本、状态和复测机制，memory 会变成不可治理的历史记录堆。

### 优化内容

- 为模块增加字段：
  - `module_version`
  - `status`: `draft`、`testing`、`candidate`、`freeze_confirmed`、`paper_shadow`、`retired`
  - `promotion_gates`
  - `retirement_reasons`
  - `last_retest_at`
  - `next_retest_due`
- 实现 module registry 文件或 SQLite 表。
- 将 module performance memory 汇总回写 module status。
- 支持退役条件：样本外退化、事件窗口风险过高、滑点敏感、样本不足、重复信号。
- LLM 只能对 `testing/candidate` 模块提出变体，不能直接生成 live-bound Strategy Spec。

### 验收标准

- 每个 freeze-confirmed strategy 必须有 `module_id` 和模块版本。
- 模块状态变化必须可审计。
- retired 模块不能进入 runtime monitor 或 execution intent。

## 8. 第五优先级：运行期 LLM structured review

### 问题

当前 monitor 能输出 `strong_review`，但还没有文档中定义的 bull/bear/risk/summarizer 结构化复核 artifact。

### 优化内容

- 新增 `MonitorReviewRequest` 和 `MonitorReviewResult` schema。
- 固定输出：
  - `bull_case`
  - `bear_case`
  - `risk_review`
  - `decision_summarizer`
- action 只允许：`no_action`、`observe`、`paper_allow`、`paper_block`、`research_candidate`。
- 记录 prompt hash、response hash、model、temperature、snapshot hash。
- 不可解析输出必须拒绝或重试。
- `research_candidate` 必须进入 Phase 1 研究链路，不能直接进 leaderboard。

### 验收标准

- LLM 复核不输出真实下单命令。
- 高影响事件 release window 内只允许风险复核，不允许 `paper_allow`。
- 每个 strong review 都有 invalidation 和 risk review。

## 9. 第六优先级：Execution state machine 与 approval queue

### 问题

ExecutionIntent 目前有基础状态和 risk gate，但还没有持久化状态机、approval queue、profile registry 和状态迁移审计。

### 优化内容

- 建立 intent 状态机：
  - `created`
  - `risk_rejected`
  - `risk_approved`
  - `pending_human_approval`
  - `human_rejected`
  - `approved`
  - `submitted`
  - `accepted`
  - `partially_filled`
  - `filled`
  - `cancel_requested`
  - `cancelled`
  - `failed`
  - `reconciled`
- 新增 SQLite 表：
  - `execution_intents`
  - `risk_decisions`
  - `human_approvals`
  - `gateway_commands`
  - `order_updates`
  - `execution_audit_events`
- 实现状态迁移函数，禁止跳过 risk 或 approval。
- WebUI 增加 approval queue，但不允许手写自由订单。

### 验收标准

- `risk_rejected` 是终态，不能在原 intent 上覆盖。
- `approved` 必须绑定 human approval 或 automation profile。
- `filled/cancelled` 只能来自 gateway/order update 或 reconciliation。

## 10. 第七优先级：WebUI 从研究台扩展为 readiness 控制台

### 问题

当前 WebUI 是研究控制台，能看任务、leaderboard、paper replay 和导出，但不具备完整运行期/执行 readiness 视图。

### 优化内容

- 新增 Runtime Monitor 页面：
  - snapshot
  - key levels
  - event context
  - signal class
  - strong review records
- 新增 Execution Readiness 页面：
  - gateway health
  - active mode
  - risk profile
  - kill switch
  - pending intents
  - approval queue
  - reconciliation status
  - incident timeline
- 新增 Module Memory 页面：
  - module status
  - pass rate
  - rejection reasons
  - next retest due
- 明确隐藏 live order 按钮；只显示经过 intent/risk/approval 的有限操作。

### 验收标准

- UI 不出现任意 Buy/Sell 自由下单入口。
- 所有执行相关操作都显示 mode、profile、risk decision 和 audit id。
- 用户能在 UI 上判断系统是否仍处于 paper-only、sim 或 live-gated 状态。

## 11. 第八优先级：独立 NT8 Gateway 工程

### 问题

当前 `src/tlm/nt_gateway.py` 是本地 sim/mock，不是真实 NinjaTrader 8 gateway。它适合 contract testing，但不能代表 NT8 live readiness。

### 优化内容

- 新增 `tools/nt8-gateway` 或独立 C# AddOn 项目。
- 固化 gateway protocol：
  - local HTTP/WebSocket 或 named pipe。
  - schema version、protocol version、monotonic sequence。
  - heartbeat、account snapshot、instrument discovery。
  - order update append-only event。
- 真实 NT8 gateway 只允许 sim account 起步。
- 实现断线重连、重复 idempotency key、cancel-all、flatten、read-only、safe mode。
- 任何人工在 NT8 内操作都必须被识别为 external intervention。

### 验收标准

- 连续 5 个交易日或 200 个 sim command 无未解释重复提交。
- reconcile drift 为 0；任何 drift 必须进入 incident。
- gateway 不和 LLM、backtest、research worker 共享进程。

## 12. 第九优先级：数据质量和成本模型校准

### 问题

当前默认使用 Dukascopy `USATECHIDXUSD` 作为 NQ proxy。它可以用于研究，但不能等同 CME NQ/MNQ 可执行行情。成本模型也需要用 sim/paper/micro-live 结果持续校准。

### 优化内容

- 长周期数据下载覆盖率报告。
- 缺口修复和重复 tick 清理。
- CFD proxy 与真实 CME NQ/MNQ 差异标记。
- 成本模型版本化：fees、spread、slippage、tick value、point value。
- 从 paper shadow、NT8 sim、micro-live 回写实际 spread/slippage 分布。
- leaderboard 支持按成本模型版本重算。

### 验收标准

- 每个 experiment 必须记录 data quality report hash。
- 每个 leaderboard row 必须显示成本模型版本。
- 成本压力测试失败的策略不能进入 freeze-confirmed。

## 13. 第十优先级：真实外部验收阶段

这些阶段不能只靠代码完成，必须依赖 Windows、NT8、sim account、真实行情和人工验收。

### NT8 Sim 验收

- 独立 gateway 运行。
- 只连接 sim account。
- 连续 5 个交易日或 200 个 sim command。
- 验证断线重连、重复提交、cancel-all、flatten、read-only。

### Paper Shadow 验收

- 连续至少 10 个交易日。
- 覆盖开盘、午盘、收盘、高波动和高影响事件窗口。
- 每个 intent 可重放。
- drift report 可解释。

### Micro Live 验收

- 最小仓位，优先 MNQ。
- 每笔人工确认。
- broker-side protective stop 必须存在。
- 至少 20 笔或 10 个交易日。
- 任一 incident 后暂停新增 live intent。

### Controlled Live 验收

- 最近 30 个交易日或 100 笔样本满足 profile 阈值。
- 自动执行仅限 strategy/profile 白名单。
- 任意高风险 drift 自动降级 paper_shadow。

## 14. 推荐实施顺序

### Milestone A：研究可信度增强

1. 事件 policy 接入 bar/tick backtest。
2. 事件窗口和非事件窗口归因报告。
3. 成本模型版本化和压力测试报告增强。
4. OpenAPI 覆盖 research/events/monitor/reports。

### Milestone B：运行期闭环

1. 完整 paper shadow 状态机。
2. runtime monitor 历史索引。
3. strong review LLM structured artifact。
4. research candidate 回流主研究链路。

### Milestone C：执行安全

1. ExecutionIntent 持久化状态机。
2. Risk profile registry。
3. approval queue。
4. incident timeline 和 readiness decision API。

### Milestone D：平台接入

1. 独立 NT8 sim gateway。
2. sim account 长周期验证。
3. paper shadow 连续交易日验证。
4. micro-live 外部验收。

## 15. 不建议立即做的事

- 不立即加真实 Buy/Sell 按钮。
- 不让 LLM 直接调用 gateway command。
- 不用当前 sim gateway 冒充真实 NT8 readiness。
- 不把 53% 胜率作为晋级充分条件。
- 不扩大策略参数网格来追历史最优。
- 不在事件窗口追求单根 K 线突破策略。
- 不把未完成 paper shadow 的策略推进 micro-live。

## 16. 最小下一步

最小下一步应是：

1. 在 backtest 中接入 event context 和 event policy。
2. 增加事件归因指标和 report artifact。
3. 补 OpenAPI 的 events/monitor/research/reports contract。
4. 实现 paper shadow run artifact 和 replay consistency check。

这四项完成后，系统会从“本地研究骨架”进一步变成“能持续观察、复盘、阻断和迭代的 paper-only 交易系统”。在此之前，不应推进真实 NT8 live。

## 17. 本轮 repo-local 落地状态

前一轮已完成本文第一至第七优先级的本地代码落地。本轮继续完成第八至第十优先级在当前仓库内可以实现的部分：独立 NT8 gateway 协议脚手架、成本校准 artifact、外部验收证据门禁、readiness API/UI 补齐，以及最终验证。

这里的“100% 落地”定义为：所有能在当前 macOS/Python/React 仓库内实现和自动化验证的 contract、artifact、状态机、测试、脚手架和前端入口已经完成；真实 NT8 AddOn 编译安装、Windows/NinjaTrader 8 运行、broker 连接、连续交易日样本和 micro-live/controlled-live 交易结果必须由外部环境产生证据，再由本仓库的 evidence gate 判定是否通过。系统不会用本地 sim/mock 冒充 live readiness。

| 阶段 | 本轮状态 | 落地内容 |
| --- | --- | --- |
| 事件 policy 接入回测和归因 | 已落地 | bar/tick backtest 支持 `event_contexts`，trade 增加事件字段，release window 阻断信号进入 `event_attribution.blocked_trades`，指标只统计未阻断交易。 |
| OpenAPI 全系统契约 | 已落地 | `docs/openapi/tradingllmagent.openapi.yaml` 覆盖 data、tasks、research、leaderboard、artifacts、events、monitor、paper、execution、readiness、module memory 和 NT8 sim gateway。 |
| 完整 paper shadow 链路 | 已落地 | `paper_shadow_run` artifact 记录 strategy hash、module、snapshot hash、risk decision、hypothetical fill、drift report、replay key，并显式保证不生成 live gateway command。 |
| 模块版本治理 | 已落地 | module registry 支持 `module_version`、生命周期状态、promotion gates、retirement reasons、retest schedule、audit events 和 runtime 准入判断。 |
| 运行期 structured review | 已落地 | monitor review 固定输出 bull/bear/risk/decision/invalidation，action 限定在 paper/research 范围，高影响 release window 禁止 `paper_allow`。 |
| Execution state machine | 已落地 | SQLite 表、risk/approval/gateway/order/audit 记录和状态迁移约束已实现，`risk_rejected` 终态、`approved` 必须绑定 approval 或 automation profile。 |
| WebUI readiness 控制台 | 已落地 | 前端新增 Runtime Monitor、Execution Readiness、Module Memory 三个运行期视图；仍不提供任意 Buy/Sell 自由下单入口。 |
| NT8 独立 gateway 工程 | repo-local 已落地，外部待编译验收 | 新增 `tools/nt8-gateway` C# AddOn skeleton、`nt8-gateway.v1` 协议 manifest、sim-only account guard、append-only gateway event、order update 和 external intervention 检测。真实 NT8 编译安装仍需 Windows/NT8。 |
| 数据质量和成本模型校准 | 已落地 | 新增 cost calibration sample、spread/slippage 分布、推荐成本模型、cost model version hash、data quality hash、CFD proxy vs CME executable warning artifact。 |
| 真实外部验收阶段 | evidence gate 已落地，真实证据待外部产生 | 新增 external validation artifact，对 paper shadow、NT8 sim、micro-live、controlled-live 分别检查交易日/样本/覆盖窗口/断线重连/idempotency/flatten/broker stop/incident 等证据。缺证据默认 blocked。 |
| Readiness API/UI 完整面 | 已落地 | API 增加 approval queue、external validation、cost calibration、gateway order updates、incident timeline；UI 增加 gateway state、approval queue、incident/order update、cost calibration 视图。 |

本轮验证命令：

```bash
PYTHONPATH=src:tests python3 -m unittest discover -s tests
npm run build
git diff --check
```

本轮最终验证结果：

- `PYTHONPATH=src:tests python3 -m unittest discover -s tests`：121 tests OK。
- `npm run build`：Vite production build OK。
- `git diff --check`：OK。
