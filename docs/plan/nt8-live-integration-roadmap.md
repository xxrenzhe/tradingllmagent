# NinjaTrader 8 实盘集成演进方案

> 协调说明：本文是 NT8、execution readiness、sim、paper shadow、micro-live 和 controlled live 的当前权威文档。若本文与更早的 v1 research 文档冲突，以本文对 post-v1 执行安全的要求为准；若未来生成更新文档，则以后续文档为准。

## 1. 背景与目标

本方案补充 `local-llm-nq-strategy-system.md` 和 `runtime-market-monitor-optimization.md`：当前 v1 保持 paper/replay 和离线 NinjaTrader CSV/OIF 导出边界不变，但后续确定需要接入 NinjaTrader 8 实盘。因此系统必须提前按“可安全演进到实盘”的方式设计接口、审计、权限和回滚，而不是在研究系统里直接增加真实下单按钮。

核心目标：

- 保持研究系统和实盘执行系统隔离。
- 将 NT8 实盘能力做成独立 gateway，而不是嵌入回测或 LLM 模块。
- 所有 live action 默认关闭，必须通过显式 profile、人工确认和风险闸门启用。
- 实盘前先经过 offline export、NT8 sim、paper shadow、micro-live 四个阶段。
- 所有命令、响应、行情快照、策略版本、人工确认和风控检查必须可审计。
- 把原来的“特征、策略、回测、调参”流程升级为“特征、策略、回测、LLM 评估、策略修正、市场观察、报告反馈”的持续闭环。
- 用 LLM 阅读回测、模拟盘和实盘报告，生成结构化改进建议和辩题排序，避免把优化工作退化为盲目调参。

## 2. 关键结论

- 截图中可借鉴的重点是 contract-first、executor API、独立 NT8 gateway、backend rollout 和 manual validation 文档。
- 当前 Python 本地研究闭环不应重构成多语言系统；Python 继续负责研究、回测、leaderboard 和策略冻结。
- 未来 NT8 gateway 可以使用 C#/.NET 更贴近 NinjaTrader 生态，也可以先用 Go/Python adapter 原型，但 gateway 必须是独立进程。
- WebUI 可以展示 live readiness 和人工确认状态，但不能在 v1 直接提供 live order 控件。
- LLM 永远不能直接发 live order。LLM 最多生成结构化复核结论，真实执行只能来自已冻结策略、确定性 signal engine 和人工/风控批准。
- 策略优化不应以“反复调参直到回测好看”为主路径；默认应先让 LLM 对回测报告做诊断，明确失效区间、市场状态、特征缺口和假设风险，再决定是否改策略。
- LLM 评估输出必须是可审计的 review artifact，包括改进建议、反对意见、证据引用、置信度和候选辩题排序，而不是直接改仓位或覆盖风控。
- “全局记忆”是研究、回测、模拟盘、实盘报告和市场观察的知识库，不是交易权限；它用于让下一轮策略评估知道历史假设、失败案例、执行偏差和市场环境变化。
- 从 Stage 2 开始，每个阶段必须有量化通过标准；不能用“运行稳定”“效果不错”“长期稳定”这类主观描述作为进入下一阶段的依据。
- NT8 gateway 的核心难点不是能否发单，而是断线、重连、重复命令、OCO/bracket 不一致、partial fill、账户状态漂移和紧急平仓时的状态一致性。

## 3. 目标架构

推荐分层：

- `research-core`：当前 `src/tlm`，负责数据、策略、回测、滚动验证、leaderboard、paper replay 和离线导出。
- `llm-reviewer`：读取回测、paper shadow、NT8 sim、micro-live 和 live 报告，生成策略诊断、辩题排序和下一轮改进建议。
- `global-memory`：保存策略版本、特征假设、LLM 评审、市场观察、执行偏差、失败案例和人工决策备注。
- `api-contract`：OpenAPI 契约，定义 data、research、paper、execution、audit 和 gateway health API。
- `executor-api`：前端 typed client，按 OpenAPI 生成或手写最小封装。
- `nt8-gateway`：独立进程，负责 NinjaTrader 8 连接、账户/合约状态、命令接收、ACK/NACK、订单状态回报。
- `risk-gateway`：执行前风控闸门，可以先作为 research-core/API 内部模块，后续独立。
- `audit-store`：记录所有执行命令、批准、拒绝、回报、异常和人工备注。
- `report-store`：统一保存 backtest、walk-forward、paper shadow、NT8 sim、micro-live 和 live execution report，供 LLM 复核和人工审查。
- `position-reconciler`：周期性对比本地账本、gateway 状态、NT8 account/position/order 状态，发现漂移后阻断新 intent。
- `incident-controller`：统一处理 kill switch、cancel-all、flatten、read-only、降级 paper_shadow 和人工接管。

执行流：

1. 策略通过 hard gates、final holdout、tick replay 和人工冻结。
2. 运行期 signal engine 只对已冻结策略生成 deterministic signal。
3. risk-gateway 校验 session、max loss、position limit、event policy、data freshness 和 kill switch。
4. 人工确认或已批准 automation profile 放行。
5. execution API 发送命令到 nt8-gateway。
6. nt8-gateway 返回 accepted/rejected/filled/cancelled 状态。
7. audit-store 持久化完整链路。

研究优化流：

1. 从历史数据制造特征，形成候选策略。
2. 对候选策略运行 backtest、walk-forward 和 holdout，生成标准化报告。
3. LLM 只读取报告和策略说明，输出结构化评估，不直接调参。
4. 系统把 LLM 建议拆成若干可比较辩题，例如特征缺口、入场过滤、退出逻辑、市场 regime、成本模型和样本泄漏风险。
5. 辩题按证据强度、预期收益、实现成本、过拟合风险和实盘安全性排序。
6. 人工从排序后的辩题中选择下一轮策略修改方向。
7. 修改后的策略重新进入 backtest/holdout，而不是直接进入执行链路。
8. 模拟盘或实盘后的新报告回写 global-memory，形成下一轮评估上下文。

这个闭环的关键约束是：LLM 指导“下一步应该研究什么”，不能直接决定“现在应该下什么单”。

## 4. OpenAPI 契约优先

`docs/openapi/tradingllmagent.openapi.yaml` 是 HTTP API 契约的单一来源。本文列出目标能力；当 endpoint、request schema 或 response schema 与 OpenAPI 不一致时，必须先更新 OpenAPI，再同步本文。

应覆盖：

- `POST /api/research/reviews`
- `GET /api/research/reviews/{id}`
- `GET /api/research/debates`
- `POST /api/research/debates/{id}/select`
- `GET /api/research/global-memory`
- `POST /api/research/global-memory/events`
- `GET /api/execution/profiles`
- `POST /api/execution/intents`
- `POST /api/execution/intents/{id}/approve`
- `POST /api/execution/intents/{id}/reject`
- `POST /api/execution/intents/{id}/submit`
- `GET /api/execution/intents/{id}`
- `GET /api/execution/audit`
- `GET /api/gateways/nt8/health`
- `GET /api/gateways/nt8/accounts`
- `GET /api/gateways/nt8/instruments`
- `POST /api/gateways/nt8/commands`
- `POST /api/gateways/nt8/cancel-all`
- `POST /api/gateways/nt8/flatten`
- `GET /api/gateways/nt8/reconciliation`
- `POST /api/incidents`
- `POST /api/incidents/{id}/resolve`

关键对象：

- `StrategyReview`：LLM 对回测、模拟盘或实盘报告的结构化复核结果。
- `StrategyDebate`：可供人工选择的下一轮策略改进辩题。
- `GlobalMemoryEvent`：跨研究和运行阶段沉淀的事实、假设、结论和异常。
- `MarketObservation`：运行期市场状态观察，例如波动、点差、成交质量和 regime。
- `ExecutionIntent`：策略信号转换后的待执行意图，不等于订单。
- `RiskDecision`：风控闸门结果。
- `HumanApproval`：人工批准记录。
- `GatewayCommand`：发送给 NT8 gateway 的命令。
- `GatewayAck`：gateway 接收或拒绝命令。
- `OrderUpdate`：NT8 回报。
- `ExecutionAuditEvent`：审计事件。
- `ReconciliationReport`：本地、gateway、NT8 三方状态一致性报告。
- `IncidentEvent`：kill switch、断线、拒单、状态漂移、人工接管和恢复事件。

所有对象必须包含：

- `correlation_id`
- `idempotency_key`
- `strategy_spec_hash`
- `strategy_freeze_id`
- `data_version_hash`
- `signal_timestamp`
- `created_by`
- `created_at`
- `mode`
- `risk_profile`
- `operator_id`
- `schema_version`
- `protocol_version`

`StrategyReview` 还必须包含：

- `source_report_id`
- `review_scope`: `backtest`、`walk_forward`、`holdout`、`paper_shadow`、`nt8_sim`、`micro_live`、`live`
- `evidence_refs`
- `failure_modes`
- `overfit_risks`
- `recommended_changes`
- `debate_candidates`
- `confidence`
- `human_disposition`

`GlobalMemoryEvent` 必须区分事实、假设和建议，禁止把 LLM 推断写成不可质疑的事实。

## 5. ExecutionIntent 设计

`ExecutionIntent` 必须是实盘前的唯一入口，禁止 WebUI 或 LLM 直接创建 gateway order。

必要字段：

- `intent_id`
- `mode`: `offline_export`、`nt8_sim`、`paper_shadow`、`micro_live`、`live`
- `symbol`
- `instrument`
- `account`
- `action`: `buy`、`sell`、`sell_short`、`buy_to_cover`
- `quantity`
- `order_type`
- `limit_price`
- `stop_price`
- `time_in_force`
- `bracket`: stop-loss、take-profit、trailing-stop
- `max_position_after_fill`
- `max_loss_if_filled`
- `reason`
- `source_strategy`
- `risk_checks`
- `approval_status`
- `account_snapshot_id`
- `market_snapshot_id`
- `expected_max_slippage_ticks`
- `expected_spread_ticks`
- `broker_order_refs`

状态流：

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

状态机规则：

- `created` 只能由 deterministic signal engine 产生，不能由 LLM 或 WebUI 手写订单产生。
- `risk_rejected` 是终态，除非生成新的 intent；不能在原 intent 上覆盖风控结论。
- `approved` 必须绑定 human approval 或 automation profile 快照。
- `submitted` 后必须等待 gateway ACK/NACK，不能假设已被 NT8 接收。
- `accepted` 只表示 gateway 或 NT8 接受订单，不表示成交。
- `filled`、`partially_filled` 和 `cancelled` 必须来自 NT8 order update 或 reconcile 结果。
- `failed` 和 `reconciled` 必须保留失败原因、最终账户状态和人工处理记录。

## 6. 风控闸门

实盘前必须实现确定性风控，不能依赖 LLM 判断。

最小检查：

- 策略必须来自冻结版本。
- 当前时间必须在允许交易 session 内。
- 数据延迟不得超过阈值。
- macro event policy 不得处于禁止交易窗口。
- 当日 realized loss 不得超过限制。
- 当前持仓和下单后持仓不得超过限制。
- 同方向重复信号必须通过 idempotency 检查。
- spread、slippage estimate 和市场状态必须在允许范围内。
- kill switch 必须为 disabled。
- live profile 必须显式启用。
- micro-live 阶段只允许按账户风险预算计算出的最小仓位；对 NQ 优先使用 MNQ，不能机械地把 1 NQ contract 视为低风险。
- bracket 或 broker-side protective stop 必须在提交 entry 前完成校验；不允许裸单进入 live。
- gateway、broker 和本地 position-reconciler 必须三方一致；不一致时只能 reduce-only、cancel-all 或 flatten。
- 每日最大订单数、最大连续亏损次数、最大单笔亏损、最大日内回撤和最大滑点必须按 profile 配置。

LLM 可以参与解释风险，但不能覆盖任何 deterministic reject。
如果 LLM 认为 reject 规则过严，只能生成 `StrategyReview` 或人工复核建议，不能修改当次 `RiskDecision`。

仓位和风险预算：

- 每个 instrument profile 必须定义 tick size、tick value、保证金要求、常规点差范围和最大可接受滑点。
- 每个 strategy profile 必须定义单笔最大亏损、当日最大亏损、最大持仓、最大未平仓风险和 reduce-only 条件。
- `quantity` 必须由风险预算推导，不能由策略信号直接指定。
- micro-live 默认从 MNQ 或等价低风险合约开始；只有成本、滑点和状态一致性达标后才能考虑 NQ。
- 若 stop distance 或实时波动导致单笔风险超过预算，intent 必须被拒绝或降级为 paper_shadow。

## 7. NT8 Gateway 边界

NT8 gateway 是执行边界服务，只负责和 NinjaTrader 8 通信：

- 接收 `GatewayCommand`。
- 验证 command schema 和 idempotency。
- 将 command 转换为 NT8 支持的 OIF、AddOn、ATM 或 native adapter 操作。
- 返回 ACK/NACK。
- 订阅订单、成交、持仓和账户状态。
- 定期发布 heartbeat。
- 在断线、重连、重复命令、状态不一致时进入 safe mode。
- 支持 cancel-all、flatten、read-only 和 heartbeat degraded 状态。
- 将 NT8 order id、broker order id、OCO id、ATM strategy id 和本地 command id 绑定到同一 correlation_id。

gateway 不负责：

- 策略研究。
- LLM 调用。
- 参数优化。
- final holdout 或 leaderboard。
- 自行决定是否下单。

### 7.1 Gateway 协议规格

gateway 实现前必须先固化协议，而不是先写 adapter：

- 传输可以先用本地 HTTP/WebSocket 或 named pipe，但必须有 schema version、protocol version、idempotency key 和 monotonic sequence。
- `GatewayCommand` 必须只表达 submit、cancel、replace、cancel-all、flatten、read-only、heartbeat probe 等有限动作。
- ACK/NACK 只表示 gateway 是否接受命令，不代表 broker 已接收、订单已工作或已成交。
- `OrderUpdate` 必须是 append-only event，不允许覆盖历史状态。
- 重连后 gateway 必须先拉取 NT8 open orders、executions、positions 和 account snapshot，再恢复接收新命令。
- 如果本地账本、gateway cache 和 NT8 状态不一致，系统必须进入 safe mode，禁止新增开仓 intent。
- 对 bracket/OCO/ATM，必须定义子腿创建失败、部分成交、止损腿缺失、目标腿取消失败时的降级策略。

### 7.2 Source of Truth

状态权威必须明确：

- 策略信号的 source of truth 是 frozen strategy 和 signal engine。
- intent/risk/approval 的 source of truth 是 execution API 和 audit-store。
- live order、fill、position、account 的 source of truth 是 NT8/broker 回报。
- 本地账本只是可复盘镜像，不能在 reconcile 失败时自作主张覆盖 broker 状态。
- 人工操作如果直接发生在 NT8 内，也必须被 gateway 发现并记录为 external intervention。

## 8. LLM 复核与全局记忆

LLM 复核应作为研究和运行报告之间的“解释层”，不是执行层。它读取报告、策略说明、特征定义和 global-memory，然后产出下一轮研究方向。

### 8.1 回测后复核

回测结束后不默认进入参数搜索，而是生成 `StrategyReview`：

- 解释收益来源是否集中在少数交易日、少数 regime 或少数异常行情。
- 检查特征是否可能泄漏未来信息或依赖不可实盘获得的数据。
- 对比训练、walk-forward、holdout、tick replay 的表现差异。
- 识别固定点差、佣金、滑点、成交延迟和 partial fill 假设是否过于乐观。
- 输出候选辩题，例如“是否增加波动过滤”“是否重做出场逻辑”“是否减少交易频率”“是否替换成本模型”。

### 8.2 辩题排序

当 LLM 产生二十到三十个候选辩题时，系统不应全部进入开发。需要按以下维度排序：

- 证据强度：是否由报告数据支持，而不是单纯主观猜测。
- 过拟合风险：是否容易只改善历史样本。
- 实盘相关性：是否能解释模拟盘或实盘报告中的真实偏差。
- 实现成本：是否能以小变更验证。
- 安全影响：是否会增加仓位、频率、滑点或尾部风险。
- 可验证性：是否能通过下一轮 backtest、paper shadow 或 NT8 sim 明确证伪。

人工只选择排序靠前且可验证的辩题进入下一轮策略修改。未选择的辩题保留在 global-memory，避免后续重复提出相同低价值方向。

LLM 评估治理：

- 每次 review 必须记录 model、prompt version、tool/input report hash、temperature 和输出 schema version。
- `recommended_changes` 必须引用 report 中的证据，不能只给抽象建议。
- 每个被采纳辩题必须记录人工采纳理由、预期验证指标和最终实验结果。
- 定期统计 LLM 建议命中率：采纳后是否改善 holdout、paper shadow、NT8 sim 或 micro-live 指标。
- 如果 LLM 连续提出低证据、高过拟合或不可验证建议，应降低该类建议排序权重。
- global-memory 中的建议必须可过期；市场 regime 或策略版本变化后，旧建议不能无限期影响新评审。

### 8.3 市场观察反馈

策略冻结后，LLM 可进入“观察员”角色：

- 读取冻结策略、特征定义、当前市场摘要和执行报告。
- 观察实盘或模拟盘环境是否偏离回测假设，例如浮动点差、成交滑点、行情延迟、波动 regime、开盘跳空和新闻窗口。
- 生成市场观察和策略适配建议。
- 把观察写入 global-memory，供下一轮研究复核使用。

观察员不能直接改变运行中的策略参数，也不能绕过 frozen strategy、risk-gateway 或人工审批。

### 8.4 报告回流

每个运行阶段都必须生成可复核报告：

- `backtest_report`：历史回测、成本假设、样本拆分、关键交易分布。
- `walk_forward_report`：滚动验证表现和 regime 稳定性。
- `paper_shadow_report`：真实行情下如果执行会发生什么。
- `nt8_sim_report`：NT8 sim 下订单、成交、持仓和账户状态。
- `micro_live_report`：最小仓位真实执行的成交质量、点差、滑点和异常。
- `live_execution_report`：受控 live 的执行质量、风控触发、reconcile 和人工干预。

报告回流后，LLM 重点比较“回测假设”和“市场现实”的差异。典型差异包括固定点差 vs 浮动点差、理想成交 vs partial fill、无延迟信号 vs 真实行情延迟、历史手续费假设 vs 实际账户成本。

## 9. 上线阶段

### Stage 0：当前状态

- paper replay。
- CSV/OIF 离线导出。
- WebUI 明确无 live controls。

完成标准：

- 导出文件有风控字段。
- replay 有订单、成交、持仓和账户账本。
- 审计可追踪策略 hash 和数据 hash。
- backtest report 可被 LLM 复核，并能引用策略版本、特征版本和数据版本。

### Stage 1：OpenAPI 与 typed client

- 固化 execution/gateway API。
- 生成或封装 TS client。
- 添加契约测试。
- 固化 research review、debate 和 global-memory API。

完成标准：

- 前后端不手写分散 `fetch`。
- execution intent schema 可验证。
- StrategyReview 和 GlobalMemoryEvent schema 可验证。

范围边界：

- Stage 1 可以在本地 API 中提供 mock/sim endpoint 以支持契约测试。
- 这些 endpoint 不代表系统已经具备 NT8 实盘能力；只有 Stage 2 之后的独立 gateway 和 reconciliation 验收完成后，才允许进入 execution readiness。

### Stage 2：NT8 Sim Gateway

- 独立 `tools/nt8-gateway` 或等价独立进程。
- 连接 NT8 sim account。
- 支持 heartbeat、account snapshot、instrument discovery、submit/cancel sim order。

完成标准：

- 只允许 sim account。
- 所有命令都有 ACK/NACK 和审计记录。
- NT8 sim report 可回流到 report-store，并触发 LLM 复核。
- 连续 5 个交易日或至少 200 个 sim command 无未解释重复提交。
- 断线重连、重复 idempotency key、cancel-all、flatten 和 read-only 模式全部通过手工验证。
- reconcile 漂移率为 0；任何漂移必须有 incident event 和人工关闭记录。

实现边界：

- `src/tlm` 可以保留 mock gateway、sim state machine、contract tests 和 API adapter。
- 真实 NT8 AddOn/gateway 不应放入 `src/tlm` 研究核心模块，也不应和 LLM、backtest 或 research worker 共享进程。

### Stage 3：Paper Shadow

- 真实行情和策略信号进入 intent。
- 系统只记录“如果实盘会下什么单”，不提交 live。
- 对比 paper shadow 与 NT8 sim/market replay 行为。
- LLM 只分析 shadow report 与回测报告的偏差，不参与下单。

完成标准：

- signal、intent、risk decision 和 hypothetical fill 可复盘。
- 连续运行至少 10 个交易日，且覆盖开盘、午盘、收盘和高波动窗口。
- shadow intent 与实际 market data snapshot 可以重放，重放结果一致。
- 无未解释状态漂移；hypothetical fill 与 NT8 sim/market replay 差异必须形成 drift report。
- 明确记录浮动点差、滑点估计和成交假设相对回测的偏差。
- 平均和 p95 spread/slippage drift 不超过 strategy profile 阈值。

### Stage 4：Micro Live

- 只允许最小仓位。
- 每笔都需要人工确认。
- 启用日亏损、最大订单数、最大持仓和 kill switch。
- 每日生成 micro-live report，比较真实成交成本与回测成本模型。

完成标准：

- 手工批准链路可审计。
- 出现异常可一键停止。
- reconcile 能发现本地状态与 NT8 状态差异。
- LLM 复核只能提出下一轮策略或成本模型改进建议，不能影响当日 live profile。
- 至少 20 笔 micro-live 交易或 10 个交易日，无未解释订单状态漂移。
- 实际滑点、手续费、点差和 partial fill 分布必须回写成本模型。
- 任一 incident 发生后必须暂停新增 live intent，直到 runbook 复盘完成。
- 每笔交易必须有 broker-side protective stop 或等价保护机制。

### Stage 5：受控 Live

- 仅对长期稳定策略启用。
- 自动执行必须按 strategy/profile 白名单控制。
- 仍保留 kill switch、日亏损限制和人工旁路。
- live report 定期进入 global-memory，用于判断策略是否需要降级、暂停或重新研究。

完成标准：

- live profile 逐策略启用。
- 每日自动生成执行报告。
- 失败时自动降级为 paper_shadow。
- LLM 复核报告中出现高风险漂移时，只能建议降级或人工复核，不能自动放宽限制。
- 最近 30 个交易日或至少 100 笔 micro-live/sim-shadow 样本满足策略 profile 的成本、滑点、回撤和状态一致性阈值。
- gateway uptime、heartbeat 延迟、reconcile 成功率和 incident 关闭时间满足上线阈值。
- 自动执行初期必须有最大订单数和最大日内风险上限，且默认保留人工旁路。

## 10. WebUI 调整

后续 WebUI 应新增“Execution Readiness”，而不是直接增加 Buy/Sell 按钮：

- gateway health。
- active mode。
- account whitelist。
- frozen strategies。
- risk profile。
- kill switch。
- pending intents。
- approval queue。
- audit timeline。
- LLM review queue。
- debate ranking。
- global-memory timeline。
- backtest vs live drift summary。

实盘按钮规则：

- v1 不出现 live order 按钮。
- Stage 2 只显示 sim controls。
- Stage 4 只显示 approval controls，不显示自由下单。
- Stage 5 只显示已验证策略的 intent approval，不允许手写任意订单。

LLM 相关 UI 规则：

- 可以展示 LLM 建议、证据引用和辩题排序。
- 可以让人工选择下一轮研究辩题。
- 可以把人工结论写回 global-memory。
- 不能提供“一键应用到实盘策略”的入口。
- 不能让 LLM 输出直接变成 `ExecutionIntent`。

## 11. 人工验证与回滚

必须新增：

- `docs/operations/manual-validation.md`
- `docs/operations/backend-rollout.md`
- `docs/operations/nt8-gateway-runbook.md`

每次接入 NT8 相关变更必须验证：

- gateway 断线重连。
- 重复 command idempotency。
- 拒绝 live profile 未开启的命令。
- 拒绝超限仓位。
- 拒绝 stale data。
- 拒绝 event blackout。
- cancel 后本地和 NT8 状态一致。
- kill switch 后不再提交任何命令。
- broker-side stop 或 bracket 子腿创建失败时自动阻断或 flatten。
- NT8 内人工改仓、手工取消单、外部成交后系统能识别 external intervention。
- 网络分区、gateway 重启、NT8 重启后不会重复提交开仓命令。

回滚要求：

- API 可以禁用 execution routes。
- gateway 可以进入 read-only。
- live profile 可以统一关闭。
- WebUI 可以隐藏 approval controls。
- 所有未完成 intents 必须转为 blocked 或 manual_review。
- LLM review、debate 和 global-memory 写入可以暂停，不影响 execution kill switch。

事故响应要求：

- `kill switch`：阻断所有新增 intent submit，不自动平仓。
- `cancel-all`：取消所有 working orders，但不改变已有 position。
- `flatten`：取消 working orders 并按 runbook 平掉持仓，只允许人工确认或预授权 emergency profile。
- `read-only`：gateway 只同步账户、订单和持仓，不接收 submit/cancel。
- `reduce-only`：只允许降低风险的平仓或减仓 intent。
- 每个 incident 必须记录触发原因、操作者、账户快照、未完成订单、持仓、处理动作和恢复条件。

## 12. 对当前项目的立即影响

当前项目不需要马上接入 NT8 实盘代码，但应按以下方向演进：

- 保留 `paper` 和 `nt export-signal` 的安全边界。
- 新增 OpenAPI 契约和 typed API client 时，预留 execution/gateway schema。
- 不把 NT8 gateway 代码放入 `src/tlm` 核心研究模块。
- 所有实盘相关字段先落到 audit/report；LLM 可以读取报告做复核，但不能进入执行决策链。
- 任何 live execution 实现前必须先完成 Stage 1 和 Stage 2。
- 后续策略研究命令应优先产出标准化 report artifact，便于 LLM 复核，而不是只打印终端摘要。
- 新增 review/debate/global-memory 时，应先落文档和 schema，再接 UI 或 LLM provider。
- 参数优化应降级为受控实验工具，默认由 LLM review 先指出需要验证的策略假设，再决定是否运行有限参数搜索。

## 13. 不做事项

- 不让 LLM 直接下实盘单。
- 不让 WebUI 提供任意手写订单入口。
- 不绕过 frozen strategy 和 risk-gateway。
- 不在 macOS 本地研究进程里直接控制 NT8 实盘。
- 不把 sim 成功等同于 live 可用。
- 不把 LLM 建议当成已验证策略。
- 不用 LLM 自动调参替代 holdout、walk-forward、paper shadow 或 micro-live。
- 不把 global-memory 当成事实数据库；其中的假设和建议必须保留来源和置信度。
