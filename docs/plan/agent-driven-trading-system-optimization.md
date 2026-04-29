# Agent 驱动交易系统优化方案

> 协调说明：本文晚于主研究方案和运行期监控方案生成。当本文与更早文档冲突时，策略模块、5m/15m cadence、Signal Engine、Risk Gateway 和 Trade Intent 边界以本文为准；若后续文档再次更新，则以后续文档为准。

## 1. 背景与目标

本方案基于近期关于 NT8 插件、执行网关、LLM 交易延迟、关键位触发、全局记忆和策略模块沉淀的讨论，补充现有方案：

- `docs/plan/local-llm-nq-strategy-system.md`
- `docs/plan/runtime-market-monitor-optimization.md`
- `docs/plan/nt8-live-integration-roadmap.md`

核心目标不是让 LLM 直接获得实盘下单权限，而是把当前系统从“研究和离线导出工具”演进为“研究沉淀、信号生成、风控审批、执行网关分层清晰”的交易系统。

本方案坚持以下原则：

- LLM 负责研究、提炼、复盘和结构化复核，不直接发 live order。
- 运行期执行链路必须确定性、低延迟、可审计。
- 1m 周期不做 LLM 临场主动判定，优先支持 5m、15m 和关键位触发。
- 所有交易经验必须沉淀为可回测、可赛马、可淘汰、可晋级的策略模块。
- 实盘执行必须经过独立 gateway、风控闸门、审计日志和 kill switch。

## 2. 对当前系统的启发

### 2.1 Agent 不能放在最快链路里

LLM 跑一次完整报告可能需要分钟级时间。如果用于 1m 主动判定，信号生成完成时行情可能已经变化，滑点和状态漂移会吞掉优势。因此：

- 1m 数据适合用于特征计算、触发监控和回测精度，不适合每根 K 线调用 LLM 决策。
- 5m 和 15m 更适合作为 LLM 复核周期，因为有足够时间读取上下文、比较策略卡片和输出结构化判断。
- 关键位触发优于固定频率全量分析，只有接近关键价位、策略触发点或风险状态变化时才调用深度复核。

### 2.2 简单策略模块比复杂临场判断更重要

交易经验里真正可复用的部分不是“泛泛的技术分析语言”，而是可测试的模块：

- pinbar、吞没、突破、回踩、均线、RSI、VWAP、开盘区间、流动性清扫。
- 每个模块要有明确输入、触发条件、出场规则、参数范围、适用时段和失效条件。
- 胜率、样本数、1R 期望、profit factor、最大回撤、分年份稳定性必须一起看。
- 低复杂度模块更容易积累样本，也更容易判断是否真实有效。

### 2.3 全局记忆应变成系统资产

聊天上下文不是可靠记忆。系统需要把经验沉淀为长期资产：

- `strategy_cards`：通过验证的策略摘要、适用市场、风险特征。
- `module_library`：指标和形态模块定义。
- `experiment_memory`：成功和失败实验的原因。
- `parameter_memory`：参数区间表现、稳定区间、过拟合风险。
- `runtime_memory`：运行期触发、复核、人工拒绝和异常案例。

这些记忆必须可追踪来源和版本，不能让未验证经验直接进入实盘链路。

### 2.4 NT8 插件能力应封装为执行边界

NT8 AddOn 可以实现市价单、批量下单、撤单、flatten、closeQty、bracket/OCO 和账户状态订阅。对本系统来说，正确做法是把 NT8 放在执行边界，而不是让研究系统直接操作账户。

推荐边界：

```text
Research Agent
  -> Strategy Module Library
  -> Signal Engine
  -> Risk Gateway
  -> Execution Intent
  -> NT8 Gateway
  -> Account / Order / Position Updates
```

## 3. 推荐目标架构

### 3.1 Research Agent

职责：

- 把交易经验转成结构化 Strategy Spec 或策略模块。
- 对模块做历史回测、walk-forward、tick replay、成本敏感性测试。
- 生成 strategy card、失败原因和参数稳定性报告。
- 提出下一轮研究候选。

禁止职责：

- 直接创建 gateway order。
- 根据临场文本判断绕过回测。
- 覆盖 deterministic 风控拒绝。

### 3.2 Strategy Module Library

新增策略模块库，作为当前 Strategy Spec 之上的沉淀层。

每个模块建议包含：

- `module_id`
- `family`
- `description`
- `inputs`
- `features`
- `entry_rules`
- `exit_rules`
- `parameter_space`
- `supported_timeframes`
- `minimum_sample_size`
- `promotion_gates`
- `known_failure_modes`
- `source_notes`
- `version`

模块类型优先级：

- `price_action`: pinbar、吞没、突破失败、结构突破。
- `level_reaction`: 前高前低、VWAP、开盘区间、整数位、流动性清扫。
- `momentum`: 多周期动量、均线方向、波动扩张。
- `mean_reversion`: VWAP 偏离、RSI 极值、z-score。
- `time_window`: 开盘后、午盘、收盘前、事件前后。

### 3.3 Signal Engine

职责：

- 只运行已批准的策略模块。
- 按 5m、15m 或关键位事件生成 `trade_intent_candidate`。
- 输出确定性特征、触发原因、信号强度和建议风控参数。
- 不调用经纪商，不直接下单。

推荐触发方式：

- `bar_close`: 5m 或 15m K 线收盘后评估。
- `level_touch`: 价格接近关键位后评估。
- `breakout_confirmed`: 突破并收盘确认后评估。
- `risk_event`: spread、波动、事件窗口、日亏损状态变化后评估。

### 3.4 Risk Gateway

职责：

- 对 `trade_intent_candidate` 做确定性审批。
- 生成 `risk_approved` 或 `risk_rejected`。
- 所有拒绝原因必须结构化记录。

最小风控规则：

- 策略必须来自已冻结版本。
- 当前时间必须在允许 session 内。
- 数据延迟和 spread 必须在阈值内。
- 下单后持仓不得超过限制。
- 当日亏损不得超过限制。
- 同一信号不得重复提交。
- bracket/OCO 必须完整。
- macro event policy 不得处于禁止交易窗口。
- kill switch 必须关闭。
- live profile 必须显式启用。

### 3.5 Execution Gateway

职责：

- 接收经过风控批准的执行命令。
- 和 NT8 AddOn 或 OIF/ATM/native adapter 通信。
- 返回 ACK/NACK、订单状态、成交、持仓和账户快照。
- 管理幂等、重连、异常和 safe mode。

必须支持的命令：

- `marketOrder`: 单账号市价下单，参数为 `account`、`instrument`、`action`、`qty`。
- `marketBatch`: 多账号批量市价下单，参数为 `accounts`、`items`。
- `cancelOrders`: 按 `orderId` 或 `namePrefix` 撤单。
- `flatten`: 单账号按品种全平并撤单，参数为 `account`、`instrument`。
- `flattenBatch`: 多账号批量全平，参数为 `accounts`。
- `closeQty`: 按品种指定数量平仓，参数为 `account`、`instrument`、`qty`。
- `bracket`: 对持仓挂止损止盈 OCO，参数为 `stop`、`limit`。

所有命令必须包含：

- `command_id`
- `correlation_id`
- `idempotency_key`
- `mode`
- `strategy_freeze_id`
- `strategy_spec_hash`
- `risk_decision_id`
- `created_at`
- `expires_at`

## 4. 数据和记忆设计

### 4.1 Strategy Card

用于表示一个已验证策略的摘要：

- 策略名称、family、timeframe、方向。
- 核心假设。
- 参数区间和稳定区间。
- 样本数、胜率、平均 R、profit factor、最大回撤。
- 分年份、分月份、分时段表现。
- 失败模式和禁用条件。
- 是否允许 runtime monitor 使用。
- 是否允许进入 paper shadow、NT8 sim 或 micro live。

### 4.2 Module Performance Memory

记录模块表现，而不是只记录单次实验：

- `module_id`
- `dataset_hash`
- `timeframe`
- `parameter_set`
- `trade_count`
- `win_rate`
- `expectancy_r`
- `profit_factor`
- `max_drawdown_r`
- `yearly_stability`
- `promotion_status`
- `rejection_reasons`

这个表用于回答：“pinbar 在哪些上下文、哪些参数、哪些时间段真的有效？”

### 4.3 Runtime Decision Log

运行期每次触发都要保存：

- 市场快照。
- 策略模块版本。
- 特征值。
- 信号强度。
- LLM 复核输入和输出 hash。
- 风控结果。
- 人工批准或拒绝。
- gateway 命令和回报。
- 后续实际结果。

这会形成真正可学习的闭环，而不是只保留最终交易盈亏。

## 5. 策略晋级标准

不要只用 53% 胜率作为唯一标准。若以 1R 风险回报为目标，可以把 53% 作为初筛线，但晋级必须看更完整的统计。

建议晋级门槛：

- 样本数达到模块最低要求。
- 扣除手续费、滑点、spread 后仍为正 expectancy。
- profit factor 大于最低阈值。
- 最大回撤在账户风险预算内。
- 训练、验证、测试和 final holdout 不出现明显断崖。
- 分年份表现不过度集中。
- 参数相邻区间稳定，不能只靠单点最优。
- tick replay 与 bar replay 偏差可解释。
- 运行期 paper shadow 不出现状态漂移。

机会少但质量高的模块可以保留，但必须明确资金利用率和信号稀疏性。

## 6. 实施阶段

### Stage 1：策略模块库

新增模块定义格式和存储目录，先沉淀最小模块集：

- opening range breakout。
- trend pullback。
- VWAP reaction。
- RSI / z-score mean reversion。
- liquidity sweep reversal。
- pinbar at level。

完成标准：

- `module -> Strategy Spec` 是默认主链路；LLM 只能提出模块候选或模块参数变体，不能绕过模块库直接生成 live-bound Strategy Spec。
- 模块能生成 Strategy Spec 或被 Strategy Spec 引用。
- 每个模块有参数空间、适用周期和失败模式。
- 现有回测和 leaderboard 能记录 `module_id`。

### Stage 2：5m/15m 研究优先级

把研究默认周期从 1m 临场思维转向 5m/15m 模块赛马。

完成标准：

- Web 和 CLI 的默认研究模板支持 5m、15m。
- leaderboard 可按 timeframe 和 module_id 筛选。
- 1m 主要用于数据构建、tick replay 和触发校验。

### Stage 3：关键位触发监控

在 runtime monitor 中新增关键位 scanner。

完成标准：

- 系统能识别 session high/low、opening range、VWAP、前高前低、整数位。
- 只有接近关键位或策略触发时才生成强复核任务。
- 弱信号只记录，不调用深度 LLM。

### Stage 4：Trade Intent 和风控闸门

新增 execution intent schema，但先只做 paper shadow。

完成标准：

- `trade_intent_candidate` 和 `risk_decision` 可持久化。
- deterministic 风控拒绝优先级高于 LLM 复核。
- 每个 intent 都能追溯到 strategy hash、module_id 和数据版本。

### Stage 5：NT8 Sim Gateway

实现或接入 NT8 sim gateway，只连接模拟账户。真实 NT8 gateway 必须按 `nt8-live-integration-roadmap.md` 放在独立进程或独立工具边界内；`src/tlm` 中只能保留契约、mock、sim 或测试辅助代码。

完成标准：

- 支持 market、cancel、flatten、closeQty、bracket。
- 所有命令有幂等键和 ACK/NACK。
- 所有订单、成交、持仓状态写入审计日志。

### Stage 6：Micro Live 准入

实盘前必须经过连续 paper shadow 和 NT8 sim 验证。

完成标准：

- 只允许最小仓位。
- 每笔人工确认。
- bracket/OCO 必填。
- kill switch 可用。
- 连续运行若干交易日无状态漂移和审计缺口。

## 7. 当前系统的优先改动

优先级从高到低：

- 新增 `module_id` 概念，把策略家族、指标、参数和实验结果连接起来。
- 扩展 leaderboard，使其按 module、timeframe、样本数、expectancy、稳定性排序。
- 新增 module performance memory，记录模块级表现。
- 在 runtime monitor 中实现关键位 scanner 和强信号阈值。
- 引入 `trade_intent_candidate`，先只做 paper shadow，不接真实账户。
- 设计 execution command schema，为 NT8 gateway 做契约准备。
- 后续再实现 NT8 sim gateway，而不是直接 live。

## 8. 不建议做的事

- 不建议让 LLM 每 1m 主动判定入场。
- 不建议把 WebUI 直接加真实下单按钮。
- 不建议让 LLM 直接调用 `marketOrder` 或 `flatten`。
- 不建议只按胜率晋级策略。
- 不建议把未验证交易经验写进 live prompt。
- 不建议用复杂组合条件堆出低样本高胜率。
- 不建议在没有 bracket/OCO 的情况下允许自动进场。

## 9. 成功标准

系统优化完成后，应具备以下能力：

- 交易经验可以沉淀为模块，而不是停留在文字描述。
- 每个模块能被历史数据检验、参数赛马和淘汰。
- 运行期只对强信号或关键位触发做深度复核。
- LLM 输出可解释但不直接越过风控。
- paper shadow 可以完整记录“如果执行会发生什么”。
- NT8 gateway 可以安全承接有限命令集。
- 每个信号、风险判断、人工决定和执行回报都可审计。
