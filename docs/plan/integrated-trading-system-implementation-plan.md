# 交易系统方案整合落地计划

> 协调说明：本文晚于当前 `docs/plan` 下其他方案生成。本文只裁定跨文档的落地顺序、阶段门槛和冲突归并；具体领域细节仍以对应专项文档为准。若未来生成更新的整合文档，则以后续文档为准。

## 1. 目标

当前方案已经形成五条主线：

- `local-llm-nq-strategy-system.md`：研究、回测、验证和 paper replay。
- `macro-event-aware-nq-strategy-optimization.md`：结构化宏观事件和事件风险。
- `runtime-market-monitor-optimization.md`：5m 运行期快照、关键位扫描和强信号复核。
- `agent-driven-trading-system-optimization.md`：策略模块沉淀、Signal Engine、Risk Gateway 和 Trade Intent。
- `nt8-live-integration-roadmap.md`：NT8 sim、paper shadow、micro-live、controlled live 和事故响应。

本文把这些方案压成一条可落地路径，避免把研究能力、运行期观察和真实执行权限混在一起。

## 2. 总体原则

- LLM 是研究、复核和报告解释层，不是执行层。
- 运行期低延迟链路必须由确定性规则、冻结策略、风控闸门和审计事件组成。
- 1m 数据用于特征、回测精度和触发校验；LLM 主动复核默认只服务 5m、15m 或关键位触发。
- 交易经验必须沉淀为可回测的 module、Strategy Spec、strategy card 和 performance memory。
- OpenAPI 是 HTTP contract source of truth；文档中的 endpoint 列表只是目标能力清单。
- NT8 能力必须放在独立 gateway 边界内；`src/tlm` 不承载真实 AddOn 或 live order 进程。
- 未通过 final holdout、paper shadow、sim gateway、reconciliation 和 runbook 的策略不能进入 live。

## 3. 阶段归并

### Phase 1：研究闭环加固

目标是让系统能稳定地产生、验证、拒绝和冻结策略，而不是追求实盘。

应落地：

- Strategy Spec schema、validator 和受控策略 family。
- Dukascopy tick 数据、1m bar 聚合、tick bid/ask replay 和成本模型。
- rolling train、validation、test、hidden final holdout 和 embargo。
- candidate leaderboard 与 freeze-confirmed leaderboard 分离。
- report artifact、experiment snapshot、data hash、cost model hash 和 fold definition hash。

阶段门槛：

- 策略不能绕过 validator。
- LLM 不能看到同轮 test 和 final holdout 细节。
- 未通过硬门槛的策略只能进入 rejected 列表。
- 系统能明确输出“未找到合格策略”。

### Phase 2：模块记忆与策略沉淀

目标是把交易员经验从文本变成可验证资产。

应落地：

- `module_id`、module registry 和 module -> Strategy Spec 主链路。
- opening range、trend pullback、VWAP reaction、RSI/z-score mean reversion、liquidity sweep、pinbar at level 等最小模块集。
- module performance memory、strategy card、parameter memory 和 failure mode 记录。
- leaderboard 按 module、timeframe、样本数、expectancy、profit factor、drawdown 和稳定性筛选。

阶段门槛：

- live-bound Strategy Spec 必须来自模块库或可追溯模块变体。
- 53% 胜率只能作为 1R 初筛参考，不能替代 expectancy、成本、样本数和回撤。
- 高胜率低样本模块必须标记信号稀疏性和资金利用率。

### Phase 3：宏观事件与运行期监控

目标是让系统在 paper-only 状态下识别事件风险、关键位和强信号，而不是临盘自由交易。

应落地：

- macro event calendar、event policy、event context hash 和事件窗口归因。
- runtime snapshot、VWAP、opening range、session high/low、整数位和前高前低扫描。
- signal score、`weak_notice`、`medium_watch`、`strong_review`、`blocked` 分类。
- 强信号 LLM 复核，输出 `observe`、`paper_allow`、`paper_block`、`research_candidate` 或 `no_action`。
- runtime audit log、prompt hash、response hash 和 replay report。

阶段门槛：

- `paper_allow` 只允许模拟执行，不代表真实下单。
- 高影响事件 release window 默认不能直接放行强信号。
- 弱信号不调用深度 LLM。
- 运行期观察生成的候选必须回到 Phase 1 重新验证。

### Phase 4：执行契约与风控闸门

目标是先固化执行语义，再考虑连接真实平台。

应落地：

- `ExecutionIntent`、`RiskDecision`、`HumanApproval`、`GatewayCommand`、`GatewayAck`、`OrderUpdate` 和 `ExecutionAuditEvent`。
- OpenAPI execution/gateway schema 和 typed client。
- idempotency key、correlation id、strategy freeze id、risk profile 和 audit trail。
- deterministic Risk Gateway：session、data freshness、spread、event blackout、position limit、daily loss、kill switch、bracket/OCO 完整性。
- mock/sim gateway contract tests。

阶段门槛：

- WebUI 和 LLM 都不能直接创建 gateway order。
- `risk_rejected` 不能在原 intent 上被覆盖。
- bracket 或 broker-side protective stop 未通过校验时不能进入 live submit。
- mock/sim endpoint 不代表 NT8 live readiness。

## 4. NT8 命令集落地边界

用户需要的交易命令可以支持，但必须落在 gateway contract 内，而不是暴露给 LLM 直接调用。

| 命令 | 用途 | 最小参数 | live 前置条件 |
| --- | --- | --- | --- |
| `marketOrder` | 单账号市价下单 | `account`、`instrument`、`action`、`qty` | 已冻结策略、风控通过、保护单通过、live profile 启用 |
| `marketBatch` | 多账号批量市价下单 | `accounts`、`items` | 所有账号分别通过风险预算和白名单 |
| `cancelOrders` | 按订单或前缀撤单 | `orderId` 或 `namePrefix` | 幂等检查、订单归属校验、审计记录 |
| `flatten` | 单账号按品种全平并撤单 | `account`、`instrument` | 人工确认或 emergency profile |
| `flattenBatch` | 多账号批量全平 | `accounts` | incident/runbook 触发，逐账号审计 |
| `closeQty` | 指定数量平仓 | `account`、`instrument`、`qty` | reduce-only 校验，不能增加风险 |
| `bracket` | 对持仓挂止损止盈 OCO | `stop`、`limit` | OCO id 绑定、子腿失败降级策略 |

所有命令都必须附带：

- `command_id`
- `correlation_id`
- `idempotency_key`
- `schema_version`
- `protocol_version`
- `mode`
- `strategy_freeze_id`
- `strategy_spec_hash`
- `risk_decision_id`
- `created_at`
- `expires_at`

## 5. NT8 Sim 到 Live 阶段

### Phase 5：NT8 Sim Gateway

目标是在独立进程中验证平台通信、状态一致性和异常处理。

应落地：

- `tools/nt8-gateway` 或等价独立进程。
- heartbeat、account snapshot、instrument discovery、submit/cancel/flatten/bracket sim command。
- ACK/NACK、append-only order update、reconciliation report 和 incident event。
- 断线重连、重复 idempotency key、cancel-all、flatten、read-only 和 safe mode 验证。

阶段门槛：

- 只允许 sim account。
- 连续 5 个交易日或至少 200 个 sim command 无未解释重复提交。
- reconcile 漂移率为 0；任何漂移必须形成 incident event 并人工关闭。
- 真实 gateway 不放入 `src/tlm`，也不和 LLM 或 backtest worker 共享进程。

### Phase 6：Paper Shadow

目标是用真实行情和冻结策略记录“如果执行会发生什么”，但不提交 live。

应落地：

- frozen strategy -> signal -> intent -> risk decision -> hypothetical fill 全链路。
- shadow report、drift report、spread/slippage drift 和 market snapshot replay。
- LLM 只读取报告做偏差复核。

阶段门槛：

- 连续至少 10 个交易日，覆盖开盘、午盘、收盘和高波动窗口。
- shadow intent 可重放，重放结果一致。
- 平均和 p95 spread/slippage drift 不超过 strategy profile 阈值。

### Phase 7：Micro Live

目标是在最小风险下验证真实成交质量和事故处理。

应落地：

- 最小仓位，优先 MNQ 或等价低风险合约。
- 每笔人工确认。
- broker-side protective stop 或等价保护机制。
- daily micro-live report、cost model feedback 和 incident runbook。

阶段门槛：

- 至少 20 笔 micro-live 交易或 10 个交易日无未解释订单状态漂移。
- 任一 incident 发生后暂停新增 live intent，直到 runbook 复盘完成。
- LLM 只能建议降级、复核或下一轮研究，不能放宽 live profile。

### Phase 8：Controlled Live

目标是对少数长期稳定策略启用受控自动执行。

应落地：

- strategy/profile 白名单。
- 自动执行上限、最大订单数、最大日内风险和人工旁路。
- live execution report、global-memory 回流、drift detection 和自动降级。

阶段门槛：

- 最近 30 个交易日或至少 100 笔 micro-live/sim-shadow 样本满足 profile 阈值。
- gateway uptime、heartbeat 延迟、reconcile 成功率和 incident 关闭时间满足上线阈值。
- 任意高风险漂移默认降级为 paper_shadow。

## 6. 冲突裁决

- 如果研究方案说 v1 不接 NT8，而 NT8 路线定义 sim/live 阶段，两者不冲突：v1 不接 live，post-v1 才能按 NT8 路线推进。
- 如果运行期监控说 `paper_allow`，它只表示 paper/replay 模拟允许，不表示真实下单允许。
- 如果文档列出 API endpoint，但 OpenAPI schema 未定义，以 OpenAPI 缺失为准，需要先补 contract。
- 如果模块文档允许 LLM 生成 Strategy Spec，live-bound 场景必须走 module -> Strategy Spec，不能让 LLM 绕过模块库。
- 如果当前代码出现 mock/sim gateway，不代表系统具备 live execution readiness。
- 如果胜率达到 53% 但 expectancy、成本后净值、样本数、回撤或稳定性不达标，不能晋级。

## 7. 当前最小可落地路径

当前最小路径应按以下顺序推进：

1. 固化 Strategy Spec、module registry、leaderboard 和 report artifact。
2. 建立 strategy card、module performance memory 和 LLM report review。
3. 接入 macro event context 和 runtime key-level scanner。
4. 定义 OpenAPI execution/gateway schemas。
5. 实现 mock/sim execution intent 和 Risk Gateway。
6. 将 NT8 gateway 放到独立进程后只接 sim。
7. paper shadow 足够稳定后再进入 micro-live。

这条路径保留了交易员分享里最有价值的部分：NT8 插件能力、5m/15m 节奏、关键位触发、全局记忆、策略模块沉淀和样本驱动的粗暴筛选；同时避免把最危险的部分提前落地为“Agent 直接下单”。
