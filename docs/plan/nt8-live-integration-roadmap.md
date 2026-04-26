# NinjaTrader 8 实盘集成演进方案

## 1. 背景与目标

本方案补充 `local-llm-nq-strategy-system.md` 和 `runtime-market-monitor-optimization.md`：当前 v1 保持 paper/replay 和离线 NinjaTrader CSV/OIF 导出边界不变，但后续确定需要接入 NinjaTrader 8 实盘。因此系统必须提前按“可安全演进到实盘”的方式设计接口、审计、权限和回滚，而不是在研究系统里直接增加真实下单按钮。

核心目标：

- 保持研究系统和实盘执行系统隔离。
- 将 NT8 实盘能力做成独立 gateway，而不是嵌入回测或 LLM 模块。
- 所有 live action 默认关闭，必须通过显式 profile、人工确认和风险闸门启用。
- 实盘前先经过 offline export、NT8 sim、paper shadow、micro-live 四个阶段。
- 所有命令、响应、行情快照、策略版本、人工确认和风控检查必须可审计。

## 2. 关键结论

- 截图中可借鉴的重点是 contract-first、executor API、独立 NT8 gateway、backend rollout 和 manual validation 文档。
- 当前 Python 本地研究闭环不应重构成多语言系统；Python 继续负责研究、回测、leaderboard 和策略冻结。
- 未来 NT8 gateway 可以使用 C#/.NET 更贴近 NinjaTrader 生态，也可以先用 Go/Python adapter 原型，但 gateway 必须是独立进程。
- WebUI 可以展示 live readiness 和人工确认状态，但不能在 v1 直接提供 live order 控件。
- LLM 永远不能直接发 live order。LLM 最多生成结构化复核结论，真实执行只能来自已冻结策略、确定性 signal engine 和人工/风控批准。

## 3. 目标架构

推荐分层：

- `research-core`：当前 `src/tlm`，负责数据、策略、回测、滚动验证、leaderboard、paper replay 和离线导出。
- `api-contract`：OpenAPI 契约，定义 data、research、paper、execution、audit 和 gateway health API。
- `executor-api`：前端 typed client，按 OpenAPI 生成或手写最小封装。
- `nt8-gateway`：独立进程，负责 NinjaTrader 8 连接、账户/合约状态、命令接收、ACK/NACK、订单状态回报。
- `risk-gateway`：执行前风控闸门，可以先作为 research-core/API 内部模块，后续独立。
- `audit-store`：记录所有执行命令、批准、拒绝、回报、异常和人工备注。

执行流：

1. 策略通过 hard gates、final holdout、tick replay 和人工冻结。
2. 运行期 signal engine 只对已冻结策略生成 deterministic signal。
3. risk-gateway 校验 session、max loss、position limit、event policy、data freshness 和 kill switch。
4. 人工确认或已批准 automation profile 放行。
5. execution API 发送命令到 nt8-gateway。
6. nt8-gateway 返回 accepted/rejected/filled/cancelled 状态。
7. audit-store 持久化完整链路。

## 4. OpenAPI 契约优先

应新增 `docs/openapi/tradingllmagent.openapi.yaml`，至少覆盖：

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

关键对象：

- `ExecutionIntent`：策略信号转换后的待执行意图，不等于订单。
- `RiskDecision`：风控闸门结果。
- `HumanApproval`：人工批准记录。
- `GatewayCommand`：发送给 NT8 gateway 的命令。
- `GatewayAck`：gateway 接收或拒绝命令。
- `OrderUpdate`：NT8 回报。
- `ExecutionAuditEvent`：审计事件。

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
- micro-live 阶段只允许 1 contract 或更低风险配置。

LLM 可以参与解释风险，但不能覆盖任何 deterministic reject。

## 7. NT8 Gateway 边界

NT8 gateway 是执行边界服务，只负责和 NinjaTrader 8 通信：

- 接收 `GatewayCommand`。
- 验证 command schema 和 idempotency。
- 将 command 转换为 NT8 支持的 OIF、AddOn、ATM 或 native adapter 操作。
- 返回 ACK/NACK。
- 订阅订单、成交、持仓和账户状态。
- 定期发布 heartbeat。
- 在断线、重连、重复命令、状态不一致时进入 safe mode。

gateway 不负责：

- 策略研究。
- LLM 调用。
- 参数优化。
- final holdout 或 leaderboard。
- 自行决定是否下单。

## 8. 上线阶段

### Stage 0：当前状态

- paper replay。
- CSV/OIF 离线导出。
- WebUI 明确无 live controls。

完成标准：

- 导出文件有风控字段。
- replay 有订单、成交、持仓和账户账本。
- 审计可追踪策略 hash 和数据 hash。

### Stage 1：OpenAPI 与 typed client

- 固化 execution/gateway API。
- 生成或封装 TS client。
- 添加契约测试。

完成标准：

- 前后端不手写分散 `fetch`。
- execution intent schema 可验证。

### Stage 2：NT8 Sim Gateway

- 独立 `tools/nt8-gateway`。
- 连接 NT8 sim account。
- 支持 heartbeat、account snapshot、instrument discovery、submit/cancel sim order。

完成标准：

- 只允许 sim account。
- 所有命令都有 ACK/NACK 和审计记录。

### Stage 3：Paper Shadow

- 真实行情和策略信号进入 intent。
- 系统只记录“如果实盘会下什么单”，不提交 live。
- 对比 paper shadow 与 NT8 sim/market replay 行为。

完成标准：

- signal、intent、risk decision 和 hypothetical fill 可复盘。
- 连续运行若干交易日无状态漂移。

### Stage 4：Micro Live

- 只允许最小仓位。
- 每笔都需要人工确认。
- 启用日亏损、最大订单数、最大持仓和 kill switch。

完成标准：

- 手工批准链路可审计。
- 出现异常可一键停止。
- reconcile 能发现本地状态与 NT8 状态差异。

### Stage 5：受控 Live

- 仅对长期稳定策略启用。
- 自动执行必须按 strategy/profile 白名单控制。
- 仍保留 kill switch、日亏损限制和人工旁路。

完成标准：

- live profile 逐策略启用。
- 每日自动生成执行报告。
- 失败时自动降级为 paper_shadow。

## 9. WebUI 调整

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

实盘按钮规则：

- v1 不出现 live order 按钮。
- Stage 2 只显示 sim controls。
- Stage 4 只显示 approval controls，不显示自由下单。
- Stage 5 只显示已验证策略的 intent approval，不允许手写任意订单。

## 10. 人工验证与回滚

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

回滚要求：

- API 可以禁用 execution routes。
- gateway 可以进入 read-only。
- live profile 可以统一关闭。
- WebUI 可以隐藏 approval controls。
- 所有未完成 intents 必须转为 blocked 或 manual_review。

## 11. 对当前项目的立即影响

当前项目不需要马上接入 NT8 实盘代码，但应按以下方向演进：

- 保留 `paper` 和 `nt export-signal` 的安全边界。
- 新增 OpenAPI 契约和 typed API client 时，预留 execution/gateway schema。
- 不把 NT8 gateway 代码放入 `src/tlm` 核心研究模块。
- 所有实盘相关字段先落到 audit/report，不进入 LLM prompt 决策链。
- 任何 live execution 实现前必须先完成 Stage 1 和 Stage 2。

## 12. 不做事项

- 不让 LLM 直接下实盘单。
- 不让 WebUI 提供任意手写订单入口。
- 不绕过 frozen strategy 和 risk-gateway。
- 不在 macOS 本地研究进程里直接控制 NT8 实盘。
- 不把 sim 成功等同于 live 可用。
