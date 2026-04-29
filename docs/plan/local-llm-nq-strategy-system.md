# 本地 LLM 驱动 NQ 策略发现系统实施方案

> 协调说明：当本文与 `agent-driven-trading-system-optimization.md`、`runtime-market-monitor-optimization.md` 或 `nt8-live-integration-roadmap.md` 冲突时，以更新的文档为准。本文仍是 `v1 research` 范围的权威文档，不定义 NT8 sim、micro-live 或 controlled live 的上线条件。

## 1. 背景与目标

目标是构建一套本地优先、面向 `NQmain` 日内策略研究的窄域验证系统，利用 LLM 辅助提出和迭代交易假设，并通过确定性本地回测快速证伪策略质量。

本项目不以“通用回测框架”作为 v1 目标。v1 的重点不是覆盖所有市场、所有订单类型、所有策略表达方式，而是围绕 NQ 日内交易最常见的少数策略 family，做足够真实、足够快、足够可复现的验证。

默认交易标的是 `NQmain`，同时保留对其他标的的扩展能力。策略筛选目标为：

- 年交易次数大于 1000 次。
- Sharpe Ratio 大于 2。
- 扣除成本后净盈利为正。
- 通过训练集、验证集、留出集和年度稳定性检查。

系统建设可以完整实施完成，但不能承诺一定找到满足上述条件的真实稳健策略。系统必须在没有合格策略时明确输出“未找到合格策略”，不能生成伪盈利结论。

### 1.1 简化原则

本方案采用“最小可信回测”原则：

- 不做通用多市场回测框架，默认只服务 `NQmain` 日内研究。
- 不做可编程通用 DSL，只做少数受控 Strategy Spec 模板。
- 不做大规模自动参数优化，只做小范围参数敏感性和稳定性检查。
- 不做复杂交易引擎，只实现 NQ 日内需要的最小成交顺序。
- 不做过早平台化的完整 WebUI，先保证研究闭环、报告和审计可用。
- 必须保留 `tick_size`、`point_value`、spread、slippage、fees、日内强平等真实性约束。

这里的“简单”不是忽略成本和成交细节，而是避免把研究系统做成大而全的平台。

## 2. 关键结论

- `NQmain` v1 使用 Dukascopy 的 `USATECHIDXUSD` 作为 Nasdaq 100 CFD 代理数据，不把它描述为真实 CME NQ 连续期货。
- Dukascopy tick `.bi5` 文件可以直接下载并用 Python 标准库 `lzma` 解压解析；v1 以 tick 下载为准，本地聚合 1m bar。
- Dukascopy 分钟 candle 路径不可作为核心依赖；所有分钟级数据都从 tick 数据派生。
- `USA100IDXUSD` 和 `NAS100IDXUSD` 探测不可用，默认符号锁定为 `USATECHIDXUSD`，其他符号通过发现命令扩展。
- Twelve Data 不作为主数据源，可作为 ETF、指数或辅助行情源；它不适合作为大规模 NQ tick replay 主数据源。
- AlpacaTradingAgent 的 WebUI 只借鉴交互设计，不直接复用 Dash、Alpaca 账户和下单业务代码。
- v1 不接 NinjaTrader 实盘；仅实现本地 paper/replay broker，并预留未来 OIF/CSV 信号导出。
- v1 不追求通用回测框架；所有抽象必须服务 NQ 日内策略验证。
- Strategy Spec 是窄域模板配置，不是通用策略编程语言。
- 参数尝试默认用于稳健性检查，不用于追逐历史最优参数。

## 3. 数据源方案

### 3.1 主数据源

默认数据源为 Dukascopy datafeed：

- 代理标的：`USATECHIDXUSD`。
- 系统标的别名：`NQmain`。
- 数据粒度：tick。
- 原始格式：hourly `.bi5`。
- 标准化输出：Parquet。
- 数据范围默认：`2018-01-01` 至当前日期。

`.bi5` tick 文件解析规则：

- 使用 `lzma.decompress` 解压。
- 解压后二进制数据每 20 字节一条记录。
- Dukascopy URL 的月份目录使用 0-based month，实际 1 月写作 `00`。
- 小时目录按 UTC 小时生成，文件名格式为 `{HH}h_ticks.bi5`。
- 价格字段必须按 instrument metadata 缩放；`USATECHIDXUSD` 的价格缩放因子必须写入 `symbols.yaml`，不能硬编码在解析器里。
- 输出字段统一为 `timestamp`、`bid`、`ask`、`bid_size`、`ask_size`、`mid`、`spread`。
- 所有时间统一为 UTC，并在回测配置中明确交易时段时区。
- HTTP 404 代表该小时无数据或无交易，记录为空小时；其他网络错误按指数退避重试并支持断点续传。

本地目录布局：

- `data/raw/dukascopy/{instrument}/{yyyy}/{mm0}/{dd}/{hh}h_ticks.bi5`
- `data/normalized/ticks/{symbol}/date={yyyy-mm-dd}/part-*.parquet`
- `data/bars/{timeframe}/{symbol}/date={yyyy-mm-dd}/part-*.parquet`
- `data/quality/{symbol}/{from}_{to}.json`
- `experiments/{experiment_id}/`

每次回测必须记录 `data_version_hash`，由原始文件清单、文件大小、mtime、解析配置、symbol metadata 和 bar 聚合配置生成。

### 3.2 本地聚合

本地数据管道必须支持：

- 从 tick 聚合 1m bar。
- 从 1m bar 聚合更高周期 bar。
- 计算 bid/ask spread、mid price、OHLCV 或 tick-volume。
- 对缺口、重复 tick、异常 spread、空小时和异常价格进行质量检查。

### 3.3 成本模型

虽然数据来自 CFD 代理，回测默认使用保守 NQ 期货近似成本：

- `tick_size = 0.25`
- `point_value = 20`
- `tick_value = 5`
- `slippage = 1 tick per side`
- `fees = 5 USD round-trip`

所有成本参数必须配置化，并在报告里显示，防止把 CFD 代理结果误解为真实 CME 可执行结果。

## 4. 系统架构

v1 技术栈：

- Python 3.12
- FastAPI
- Typer CLI
- DuckDB 查询 Parquet、指标和 leaderboard
- SQLite 记录任务、实验状态和 WebUI 会话状态
- Parquet 存储标准化行情和交易明细
- React + Vite 本地控制台
- Plotly 或 Lightweight Charts 展示行情和回放

建议模块边界保持清晰，但每个模块都按窄域实现，不为未来泛化提前设计复杂抽象：

- `data`：Dukascopy 下载、解析、标准化、bar 聚合、质量检查。
- `strategy`：NQ Strategy Spec 模板、schema 校验、有限参数展开、策略实例化。
- `backtest`：1m bar 筛选、tick bid/ask replay、NQ 成本、最小成交顺序、指标计算。
- `research`：LLM 生成假设和模板配置、失败反馈、实验编排、结果解释。
- `storage`：Parquet、DuckDB/SQLite、实验版本、fold 指标、排行榜。
- `api`：FastAPI HTTP 和实时进度接口。
- `web`：React 控制台。
- `paper`：本地模拟 broker、订单、成交、持仓和回放。
- `ninjatrader`：未来 OIF/CSV 信号导出，不作为 v1 实盘执行依赖。

状态与查询分工：

- DuckDB：读取 Parquet tick/bar/trades，计算指标、fold 汇总和 leaderboard。
- SQLite：保存任务状态、实验元数据、策略注册表、运行日志索引和 WebUI session。
- Parquet：保存标准化行情、bar、trades、equity curve、fold results。

长任务执行模型：

- v1 使用 FastAPI lifespan 启动本地 asyncio worker pool。
- 任务状态写入 SQLite，任务结果写入 Parquet 和 DuckDB 可查询目录。
- API 必须支持任务创建、取消、状态查询、日志分页和 Server-Sent Events 进度推送。
- 不在 v1 引入 Redis、Celery 或外部队列；后续多机并行再扩展。

## 5. NQ Strategy Spec 模板

LLM 不允许生成任意可执行代码。LLM 只能生成受控 YAML/JSON Strategy Spec，由本地引擎解析和执行。

Strategy Spec 在 v1 中不是通用 DSL，也不追求表达任意交易逻辑。它是 NQ 日内策略模板配置层，目的只有三个：

- 限制 LLM 输出，避免执行任意代码。
- 固定少数可解释策略 family，便于快速证伪。
- 保留必要风控、成本和成交约束，避免虚假回测。

Strategy Spec 至少包含：

- 策略名称和版本。
- 策略 family。
- 市场假设。
- 适用标的和时间框架。
- 市场状态过滤器。
- 交易时段过滤器。
- 指标定义。
- 入场规则。
- 出场规则。
- 止损、止盈、移动止损和最大持仓时间。
- 冷却时间和每日最大交易次数。
- 参数范围。
- 成本模型引用。
- 多空方向约束。
- 仓位 sizing 规则。
- 反马丁和反无限加仓约束。

DSL 校验必须拒绝：

- 任意 Python、JavaScript 或 shell 代码。
- 未来函数和 lookahead bias。
- 无界参数搜索。
- 明显不可执行的超高频规则。
- 缺少风控的策略。
- 无法确定时间框架或标的的数据引用。
- 无限 DCA。
- 马丁或亏损加倍。
- 无硬止损网格。
- 无最大库存限制的网格。
- 主要盈利来源依赖“越亏越加仓”的风险转移策略。

### 5.1 策略研究池

LLM 的策略研究必须从可解释的市场结构出发，而不是从加仓公式出发。每个候选策略必须声明 `market_hypothesis`，并归入一个允许的 `strategy_family`。

优先研究的策略 family：

- `opening_range_breakout`：开盘 5、15、30 分钟区间突破，结合波动率和 VWAP 过滤。
- `trend_pullback`：EMA、VWAP 或多周期趋势确认后，在顺势回撤中入场。
- `volatility_expansion`：低波动压缩后突破，使用 realized volatility、ATR 或 range compression 过滤。
- `intraday_momentum`：1m、5m、15m 多周期动量一致，限制持仓时间和回撤。
- `regime_filtered_mean_reversion`：只在震荡 regime 下做 VWAP、布林带或 z-score 回归。
- `time_of_day_edge`：针对开盘、午后、收盘前等 NQ 日内时段建模。
- `gap_fade_or_continuation`：隔夜 gap 的延续或回补，必须按波动率和趋势状态区分。

允许但严格限制的策略 family：

- `controlled_grid`：只允许在震荡 regime 下运行，必须设置有限层数、最大仓位、硬止损、最大日亏损、最大持仓时间和禁止亏损加倍。

禁止作为主策略的 family：

- `martingale`
- `infinite_dca`
- `unbounded_grid`
- `loss_doubling`
- `no_stop_inventory_accumulation`

如果策略的主要优势来自提高胜率但显著放大尾部亏损，系统必须标记为风险转移策略，并拒绝进入最终候选。

### 5.2 Strategy Spec v0 示例

Strategy Spec v0 使用 YAML，validator 必须把它解析为强类型对象后再交给回测器。

```yaml
schema_version: 0
name: nq_opening_range_breakout_v1
strategy_family: opening_range_breakout
market_hypothesis: "NQ 在美股开盘后出现高波动方向突破时，短期趋势更容易延续。"
symbol: NQmain
timeframe: 1m
direction: long_short
session:
  timezone: America/New_York
  trade: "09:35-15:45"
  flatten: "15:55"
regime_filter:
  all:
    - indicator: realized_volatility
      window: 30
      op: ">"
      value: percentile_60
indicators:
  atr_14:
    type: atr
    window: 14
  vwap_session:
    type: session_vwap
  opening_range_15:
    type: opening_range
    minutes: 15
entry:
  long:
    all:
      - left: close
        op: ">"
        right: opening_range_15.high
      - left: close
        op: ">"
        right: vwap_session
  short:
    all:
      - left: close
        op: "<"
        right: opening_range_15.low
      - left: close
        op: "<"
        right: vwap_session
exit:
  stop_loss:
    type: atr_multiple
    indicator: atr_14
    multiple: 1.2
  take_profit:
    type: atr_multiple
    indicator: atr_14
    multiple: 1.8
  max_holding_minutes: 45
risk:
  position_sizing:
    type: fixed_contracts
    contracts: 1
  max_position_contracts: 1
  max_trades_per_day: 20
  max_daily_loss_r: 3
anti_martingale_constraints:
  forbid_loss_doubling: true
  forbid_position_increase_when_unrealized_loss: true
  max_grid_levels: 0
parameters:
  opening_range_minutes:
    values: [5, 15, 30]
  atr_stop_multiple:
    min: 0.8
    max: 2.0
    step: 0.2
cost_model: nq_conservative_v1
```

允许的表达式能力：

- 逻辑节点只允许 `all`、`any`、`not`。
- 比较运算符只允许 `>`、`>=`、`<`、`<=`、`==`、`crosses_above`、`crosses_below`。
- 指标只允许 registry 中声明的指标类型；v0 registry 包含 EMA、SMA、ATR、RSI、Bollinger Bands、VWAP、opening range、realized volatility、z-score。
- 参数范围必须是显式 `values` 或有界 `min/max/step`。
- 禁止引用未来 bar、未来 tick、当前 bar 未完成 high/low 和同 bar close 后不可得信息。

### 5.3 成交模型规范

bar 回测和 tick replay 必须使用一致的事件顺序：

- bar 策略在 bar close 产生 signal，最早只能在下一根 bar open 或下一条 tick 成交。
- tick replay 中，买入使用 ask，卖出使用 bid。
- 滑点在 bid/ask 之后叠加，买入价格上调，卖出价格下调。
- 手续费在成交后计入 realized PnL。
- stop loss 和 take profit 如果同一 bar 都触发，默认按保守规则先触发 stop loss。
- 到达 `session.flatten` 必须强制平仓。
- 日内策略禁止跨 session 持仓。
- 交易价格必须按 `tick_size` 对齐。
- 任何无法确定先后顺序的同 bar 事件都按对策略不利的顺序处理。

## 6. 回测与验证流程

### 6.1 两阶段回测

第一阶段为 1m bar 快速筛选：

- 用于快速筛掉明显无效的策略模板和少量参数组合。
- 输出初步交易次数、净利润、Sharpe、回撤、胜率、盈亏比和年度分布。
- 不作为最终入榜依据。

第二阶段为 tick bid/ask replay：

- 只对第一阶段候选策略执行。
- 使用 bid/ask、spread、slippage 和 fees 计算更保守成交。
- 输出最终排行榜所需指标。

### 6.2 默认交易约束

- v1 默认只做日内策略。
- 默认不隔夜持仓。
- 默认单标的、单策略、单仓位；保留扩展到组合和多策略能力。
- 默认交易时段配置化，不硬编码。
- 强制记录每笔交易的入场理由、出场理由、价格、成本和持仓时间。

### 6.3 滚动训练、验证和测试

排行榜不能只看全样本结果。每个候选策略必须使用滚动 train、validation、test 数据集验证，避免只在固定历史区间上过拟合。

v1 不做自动寻找最优参数的优化器。train 集只允许生成少量候选参数和阈值，validation 用于选择是否继续验证，test 和 final holdout 用于拒绝不稳健策略。

默认滚动窗口：

- `train_window = 24 months`
- `validation_window = 6 months`
- `test_window = 6 months`
- `step = 3 months`
- `embargo = max(5 trading days, max_holding_period, label_horizon)`
- `indicator_warmup = max_indicator_lookback`
- `min_folds = 8`
- `final_holdout = latest 12 months or latest 15%-20% of data`

数据分区规则：

- final holdout 必须从滚动研究数据中剔除；候选研究阶段只能使用隐藏汇总门控，策略进入冻结确认状态后才允许暴露完整失败细节。
- rolling train、validation、test 只在 final holdout 之前的数据上生成。
- indicator warmup 数据只能用于计算指标状态，warmup 区间内产生的信号和交易不能计入回测结果。
- 如果 test 窗口重叠，报告必须标记为 overlapping folds，并额外输出 non-overlap test 汇总。

每个 fold 的职责：

- train 集只用于生成少量参数候选、拟合阈值和选择策略变体。
- validation 集用于策略晋级和参数选择，LLM 可以看到 validation 失败原因。
- test 集作为该 fold 的样本外门控结果，LLM 在同一轮优化中不能读取 test 细节。
- final holdout 是最终不可见样本外验证集，LLM 在策略冻结前不能读取结果或失败细节。
- embargo 区间必须从相邻集合之间剔除，降低标签泄漏和相邻样本污染。

晋级规则：

- 每个 fold 必须分别记录 train、validation、test 指标。
- 策略必须在多数 test folds 中净盈利为正。
- 汇总 test folds 的年化交易次数必须大于 1000。
- 汇总 test folds 的 Sharpe 必须大于 2。
- median test fold Sharpe 必须大于配置阈值，默认 `1.5`。
- final holdout 必须净盈利为正，且 Sharpe、回撤、交易次数不能明显低于 test folds 汇总表现。
- 最差年度、最差 fold、最大回撤和成本敏感性不能突破配置阈值。
- train 明显优秀但 validation 或 test 退化的策略必须标记为过拟合并拒绝入榜。
- validation、test 或 final holdout 任一阶段失败时，策略不能进入最终候选；报告必须写明失败阶段。

### 6.4 防过拟合

候选策略还必须经过：

- walk-forward 验证。
- 年度表现稳定性检查。
- 成本敏感性检查。
- 参数稳定性检查。
- trade count 分布检查。
- train、validation、test 指标退化率检查。
- Deflated Sharpe Ratio 或简化 trial-count-adjusted Sharpe 检查。
- Probability of Backtest Overfitting 后续补全，v1 至少记录试验次数、fold 数量和样本外 test 表现。

参数稳定性检查的目标不是寻找历史最佳参数，而是确认策略在相邻合理参数下不会从盈利突然退化为不可用。参数范围必须小而有交易含义。

#### 6.4.1 参数预算与超限规则

参数组合数默认上限为 50。这个数字是默认安全阈值，不是数学定律。它的作用是提醒系统优先验证市场假设，而不是通过扩大搜索空间挖历史噪声。

允许超过 50 组参数的情况：

- 参数维度虽然较多，但每个参数都有明确交易含义。
- 超限运行只用于敏感性测试，不用于直接选择最佳入榜参数。
- 实验报告明确标记 `parameter_budget_exceeded = true`。
- 报告记录 `trial_count`、`parameter_grid_hash` 和超限原因。
- leaderboard 评分必须应用 trial-count-adjusted Sharpe 或等价惩罚。

禁止超过 50 组参数的情况：

- 只是为了寻找历史收益最高的参数点。
- 参数步长没有交易含义，例如为了拟合历史而使用过细小数步长。
- 策略只有单一狭窄参数点有效，相邻参数表现明显崩溃。
- LLM 因 validation 失败而持续增加参数维度绕过失败原因。

默认处理规则：

- `<= 50`：正常研究预算。
- `51-200`：允许作为敏感性测试，但不能直接按最优参数入榜。
- `> 200`：默认拒绝，除非用户显式开启研究模式并接受过拟合风险标记。

### 6.5 指标、门槛和评分

所有排行榜指标必须基于扣除 spread、slippage 和 fees 后的结果。最终候选先通过硬性门槛，再按评分排序。

硬性门槛：

- `annual_trades_test > 1000`
- `sharpe_test_aggregate > 2`
- `median_sharpe_test_fold > 1.5`
- `net_pnl_test > 0`
- `net_pnl_final_holdout > 0`
- `max_drawdown_test <= configured_limit`
- `profit_factor_test > 1.1`
- `avg_trade_net_pnl > 1.5 * round_trip_cost`
- `positive_test_fold_ratio >= 0.6`
- `positive_year_ratio >= 0.6`
- `final_holdout_sharpe >= 0.7 * sharpe_test_aggregate`

退化检查：

- `validation_to_test_sharpe_decay = 1 - sharpe_test_aggregate / sharpe_validation_aggregate`
- `test_to_holdout_sharpe_decay = 1 - sharpe_final_holdout / sharpe_test_aggregate`
- 任一 decay 大于配置阈值时，策略标记为性能退化，默认阈值为 `0.5`。
- 如果 validation Sharpe 小于等于 0，而 test 或 holdout 表现异常优秀，必须标记为不稳定，不允许直接入榜。

排序评分：

- 只对通过硬性门槛的策略计算 `robustness_score`。
- `robustness_score = 0.35 * sharpe_score + 0.20 * pnl_score + 0.20 * stability_score + 0.15 * drawdown_score + 0.10 * trade_count_score`
- `sharpe_score` 基于 test 和 final holdout 的较小值。
- `stability_score` 基于 positive fold ratio、positive year ratio 和参数稳定性。
- `drawdown_score` 对高回撤策略惩罚。
- `trade_count_score` 对刚超过 1000 次但分布集中的策略惩罚。
- 如果 `parameter_budget_exceeded = true`，`robustness_score` 必须应用 trial count 惩罚，且报告不能只展示最优参数结果。

报告要求：

- 每个策略必须输出硬性门槛通过/失败表。
- 每个策略必须输出逐 fold、逐年、final holdout 和 non-overlap test 汇总。
- 每个被拒绝策略必须记录首个主要拒绝原因和所有次要风险。
- 每个策略必须输出 `trial_count`、参数组合数量、参数范围和是否超出默认预算。
- 参数预算超限时，必须展示参数稳定性热力图或相邻参数退化摘要。

指标公式：

- `trade_return = trade_net_pnl / starting_equity`
- `sharpe = mean(period_returns) / std(period_returns) * sqrt(252)`，默认使用日收益序列。
- 如果日收益标准差为 0，Sharpe 记为无效，策略不能入榜。
- `annual_trades = trade_count / calendar_days * 365`
- `profit_factor = gross_profit / abs(gross_loss)`，无亏损时不记为无限大，标记为样本不足风险。
- `max_drawdown` 基于逐笔或逐日 equity curve 的 peak-to-trough 计算。
- fold 聚合 Sharpe 默认使用拼接后的样本外日收益序列计算，同时报告 fold median。
- validation Sharpe 小于等于 0 时，decay 不按比值计算，直接标记为 validation failure。

### 6.6 可复现性

每次实验必须保存完整快照：

- `experiment_id`
- `strategy_spec_hash`
- `prompt_hash`
- `llm_model`
- `llm_parameters`
- `data_version_hash`
- `code_version`
- `config_snapshot`
- `random_seed`
- `fold_definition_hash`
- `cost_model_hash`

相同快照重新运行时，trades、equity curve、fold metrics 和 leaderboard score 必须一致。

## 7. LLM 策略研究流程

LLM 的职责：

- 先提出可验证的市场假设，再生成候选 Strategy Spec。
- 生成候选 Strategy Spec。
- 根据失败原因修改策略。
- 提出小范围、有交易含义的参数候选，而不是无限搜索空间或大规模优化网格。
- 总结策略逻辑和市场假设。
- 分析回测失败原因和过拟合风险。
- 避免把 DCA、马丁、无限网格或亏损加倍作为盈利核心。

研究预算和约束：

- 每次 `research run` 必须声明 `max_trials`。
- 每个 strategy family 必须设置试验配额，防止单一 family 占满研究预算。
- 每个 Strategy Spec 的参数组合数默认不得超过 50；超过该限制必须按参数预算与超限规则处理。
- 对 `strategy_spec_hash`、参数网格 hash 和交易信号相似度去重。
- 连续失败的 family 会降低后续采样权重，但不能完全归零。
- LLM 只能看到 train 和 validation 反馈；同轮 test、final holdout 和 rejected leaderboard 的隐藏字段不能进入 prompt。

LLM 研究循环的默认目标是快速证伪，而不是寻找能让历史收益最大的参数组合。任何因为增加参数维度而改善的策略，都必须优先标记为过拟合风险。

本地系统的职责：

- 校验 Strategy Spec。
- 执行回测。
- 计算指标。
- 管理实验版本。
- 拒绝不合格策略。
- 生成可复现报告。

一次研究循环：

1. 读取已有 leaderboard 和失败策略原因。
2. LLM 提出市场假设和策略 family。
3. LLM 生成或小幅变异 Strategy Spec 模板配置。
4. DSL validator 校验策略 family、风控和反马丁约束。
5. 1m bar 回测筛选。
6. tick replay 验证候选。
7. 防过拟合检查。
8. 写入实验数据库。
9. 更新 leaderboard。
10. 生成策略卡片和下一轮建议。

## 8. WebUI 方案

采用 FastAPI + React 实现本地控制台。AlpacaTradingAgent WebUI 仅作为交互参考，不复用其业务代码。

可借鉴的交互模式：

- 多标的分页。
- 分析状态表。
- LLM 调用和 tool 调用统计。
- 报告 tabs。
- Prompt 和 tool 输出弹窗。
- 运行配置面板。
- 图表和报告并列布局。

v1 页面：

- 数据源与质量：下载进度、覆盖率、缺口、异常 spread。
- 实验队列：运行中、已完成、失败、取消。
- Leaderboard：按 Sharpe、净利润、回撤、交易次数、稳定性筛选。
- 策略详情：Strategy Spec、参数、指标、年度表现。
- 滚动验证：train、validation、test fold 指标、退化率和拒绝原因。
- 交易分布：按年、月、小时、持仓时间、方向统计。
- Tick replay：K 线、入场、出场、止损、止盈和成本可视化。
- LLM 审计：prompt、response、模型、token、失败原因。
- Paper replay：本地订单、持仓、成交和 PnL。

WebUI 不直接管理实盘账户，不放置真实下单按钮，不连接 Alpaca 实盘或 NinjaTrader 实盘。

## 9. CLI 与 API

### 9.1 CLI

必须实现的 CLI：

- `tlm data discover --provider dukascopy --query nasdaq`
- `tlm data download --symbol NQmain --from 2018-01-01 --to today --granularity tick`
- `tlm data build-bars --symbol NQmain --timeframe 1m`
- `tlm data quality --symbol NQmain --from 2018-01-01 --to today`
- `tlm strategy validate --spec strategies/example.yaml`
- `tlm backtest bar --spec strategies/example.yaml --symbol NQmain`
- `tlm backtest tick --spec strategies/example.yaml --symbol NQmain`
- `tlm research run --symbol NQmain --trials N`
- `tlm report leaderboard --experiment EXP_ID`
- `tlm paper replay --strategy-id STRAT_ID`
- `tlm nt export-signal --strategy-id STRAT_ID --format oif|csv`

### 9.2 API

必须实现的 API 分组：

- `/api/data/*`
- `/api/strategies/*`
- `/api/backtests/*`
- `/api/experiments/*`
- `/api/reports/*`
- `/api/paper/*`

长任务必须支持：

- 创建任务。
- 查询状态。
- 实时进度推送。
- 取消任务。
- 获取日志。
- 获取结果。

最小 v1 API 必须优先实现：

- `POST /api/data/download`
- `POST /api/data/build-bars`
- `GET /api/data/quality`
- `POST /api/strategies/validate`
- `POST /api/backtests/bar`
- `POST /api/experiments/research-runs`
- `GET /api/experiments/{id}`
- `GET /api/reports/leaderboard`

## 10. Paper/Replay 与 NinjaTrader 边界

v1 只实现本地 paper/replay broker：

- 模拟账户。
- 模拟订单。
- 模拟成交。
- 仓位管理。
- 已实现策略的历史回放。
- 成本和滑点配置。

NinjaTrader 仅作为未来扩展：

- 不在 v1 中直接接入实盘。
- 不依赖 Windows 环境完成 v1。
- 预留 OIF/CSV 信号导出。
- 导出前必须明确账户、合约、方向、数量、订单类型和风控字段。

## 11. 实施阶段

### Phase 1：基础工程与数据闭环

- 初始化 Python 包、FastAPI、Typer CLI 和测试框架。
- 创建 `symbols.yaml`、`costs.yaml`、`llm.yaml`。
- 实现 Dukascopy tick 下载和 `.bi5` 解析。
- 实现 Parquet 标准化存储。
- 实现 1m bar 聚合。
- 实现数据质量报告。
- 实现 DuckDB 和 SQLite 的明确分工。

完成标准：

- 可以下载 `NQmain` 指定日期 tick 数据。
- 可以生成 1m bar。
- 可以输出质量报告。
- 每个数据产物有 `data_version_hash`。

### Phase 2：Strategy Spec 与基础回测

- 实现 NQ Strategy Spec 模板 schema。
- 实现 validator。
- 实现示例策略。
- 实现 NQ 日内最小成交模型规范。
- 实现 1m bar backtester。
- 实现交易明细和指标输出。

完成标准：

- 示例策略可重复回测。
- 非法 spec 被拒绝。
- 指标结果确定性一致。
- 固定 spec 的 trades 和 equity curve 可复现。

### Phase 3：Tick Replay 与成本模型

- 实现 bid/ask replay。
- 实现 slippage、fees 和 tick-size 对齐。
- 实现日内持仓约束。
- 实现候选策略二次验证。
- 实现滚动 train、validation、test fold 生成器。
- 实现 final holdout、动态 embargo 和 indicator warmup 处理。

完成标准：

- bar 筛选结果可进入 tick 验证。
- tick 验证输出最终交易明细和指标。
- 每个候选策略可输出逐 fold 的 train、validation、test 指标。
- 策略冻结后才能运行 final holdout，且结果单独记录。

### Phase 4：LLM 研究循环

- 实现 LLM Strategy Spec 模板配置生成。
- 实现失败反馈和小幅策略变异。
- 实现实验数据库。
- 实现 leaderboard。
- 实现审计日志。
- 实现只向 LLM 暴露 train 和 validation 反馈、隐藏同轮 test 明细的研究约束。
- 实现研究预算、family 配额和相似策略去重。

完成标准：

- 可以运行多轮窄域策略研究。
- 每个策略有可追溯 spec、prompt、参数和结果。
- leaderboard 只使用样本外 test folds、final holdout 和稳定性约束决定最终候选。
- leaderboard 分为 `candidate_leaderboard` 和 `freeze_confirmed_leaderboard`：前者只用于候选排序，后者必须在冻结确认后才可暴露 final holdout 细节。
- 重跑同一实验快照可得到一致结果。
- 参数组合数量受限，不能通过扩大网格追逐历史最优结果。

### Phase 5：WebUI

- 实现 React/Vite 控制台。
- 接入 FastAPI。
- 展示数据质量、实验队列、leaderboard 和策略详情。
- 展示 LLM 审计日志。
- 展示硬性门槛表、robustness score 和拒绝原因。

完成标准：

- 用户可在浏览器启动实验、查看进度、取消任务、筛选 leaderboard 和查看策略详情。

### Phase 6：Paper Replay 与导出预留

- 实现本地模拟 broker。
- 实现 paper replay。
- 实现 NinjaTrader OIF/CSV 导出草案。

完成标准：

- 策略可以在历史数据上按 paper broker 逻辑回放。
- 可以导出可审查的信号文件，但不自动实盘执行。

## 12. 验收标准

数据验收：

- 指定小时 `.bi5` 可下载、解压和解析。
- 2018、2020、2025 样本数据通过 schema 检查。
- 质量报告能发现缺口、重复 tick 和异常 spread。
- Dukascopy 0-based 月份、UTC 小时、空小时和价格缩放因子处理正确。
- 原始 tick、标准化 tick、bar 和质量报告目录符合约定。

策略验收：

- 合法 Strategy Spec 通过校验。
- 非法字段、未来函数、任意代码和无界参数被拒绝。
- 缺少 `strategy_family` 或 `market_hypothesis` 的策略被拒绝。
- DCA、马丁、亏损加倍、无限网格和无硬止损库存累积策略被拒绝。
- `controlled_grid` 如果缺少震荡 regime、有限层数、最大仓位、硬止损或最大日亏损，必须被拒绝。
- Strategy Spec v0 示例能通过 validator 并运行 bar 回测。

回测验收：

- 固定样例策略输出确定性 trades、PnL、Sharpe、drawdown 和年交易次数。
- 成本模型显示在每次报告中。
- bar 和 tick 两阶段结果可关联。
- 成交模型按 next bar 或 next tick、bid/ask、滑点、手续费、stop/take-profit 优先级和 session flatten 规则执行。
- 滚动 train、validation、test folds 可复现，且相邻集合之间应用 embargo。
- indicator warmup 不计入交易结果。
- overlapping test folds 和 non-overlap test 汇总必须分开展示。
- 硬性门槛、退化检查和 robustness score 计算可重复。
- 指标公式、fold 聚合和 Sharpe 无效场景处理可重复。

研究验收：

- LLM 生成的每个策略都有 spec、参数、数据版本、回测结果、拒绝原因或入榜理由。
- 未通过门槛的策略不能进入最终候选榜。
- train 表现优秀但 validation 或 test 明显退化的策略必须被拒绝，并标记为过拟合风险。
- 最终 leaderboard 必须显示样本外 test 汇总指标、final holdout 指标、逐 fold 指标和通过/拒绝原因。
- 最终 leaderboard 必须只排序已通过硬性门槛的策略，未通过策略只能进入 rejected 列表。
- 每次实验保存 spec hash、prompt hash、data hash、code version、config snapshot、random seed 和 fold hash。
- 研究预算、family 配额和策略去重生效。
- 参数预算超限的策略必须带有过拟合风险标记和 trial count 惩罚。

UI 验收：

- 可启动实验。
- 可查看进度。
- 可取消任务。
- 可筛选 leaderboard。
- 可打开策略详情。
- 可查看交易回放。
- 可查看 LLM 审计记录。

风险验收：

- 系统不能承诺发现盈利策略。
- 系统不能把 Dukascopy CFD 代理数据描述为真实 CME NQ。
- 系统不能直接执行 LLM 生成代码。
- 系统不能默认连接真实交易账户。

## 13. 风险与默认假设

主要风险：

- Dukascopy CFD 代理数据与真实 CME NQ 存在价格、交易时段、流动性和成交机制差异。
- 高频策略容易因 spread、slippage 和数据质量产生虚假优势。
- 大量 LLM 试验会显著提高过拟合风险。
- `Sharpe > 2` 和 `>1000 trades/year` 同时满足可能非常困难。
- v1 自研轻量回测引擎需要严格测试，避免成交和成本计算错误。

默认假设：

- 本地运行环境为 macOS。
- Python 版本使用 3.12。
- Node 版本满足 React/Vite 开发。
- v1 优先完成本地研究闭环，而不是实盘执行。
- 默认模型 fallback 为 `gpt-5.5 -> gpt-5.4 -> gpt-5.4-mini`，实际可用模型由 `llm.yaml` 覆盖。
- 默认日内交易，不隔夜持仓。

## 14. 不做事项

v1 不做：

- 不做通用回测框架。
- 不做通用可编程策略 DSL。
- 不做大规模自动参数优化器。
- 不做多资产组合优化。
- 不支持复杂订单类型、跨市场套利和多策略资金分配。
- 不接真实 NinjaTrader 实盘。
- 不接 Alpaca 实盘。
- 不直接复用 AlpacaTradingAgent WebUI 代码。
- 不执行 LLM 生成的任意代码。
- 不承诺一定找到满足条件的盈利策略。
- 不把代理数据结论直接视为真实 NQ 可执行结果。
