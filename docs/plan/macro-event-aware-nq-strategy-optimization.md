# 宏观事件感知的 NQ 策略系统优化方案

> 协调说明：当本文与更新的 `runtime-market-monitor-optimization.md`、`agent-driven-trading-system-optimization.md` 或 `nt8-live-integration-roadmap.md` 冲突时，以更新文档为准。本文只定义结构化宏观事件能力，不定义实盘执行权限。

## 1. 背景与目标

本方案是在 `docs/plan/local-llm-nq-strategy-system.md` 的基础上增加宏观事件感知能力，使本地 LLM 驱动的 NQ 日内策略验证系统能够识别 CPI、PPI、NFP、FOMC、Fed 讲话、GDP、PCE 等重大事件窗口，并在策略生成、回测、报告和 paper replay 中显式处理事件风险。

核心目标不是让 LLM 自动阅读新闻并判断市场，而是把事件作为结构化输入交给系统。这样可以降低模型负担，减少误判，并保持回测和实验的可复现性。

本方案同样遵循窄域原则：事件层不是通用宏观研究平台，也不是新闻理解系统。v1 只解决 NQ 日内策略研究里最实际的问题：重大事件前后是否暂停、降权、等待二次确认，以及这些规则对回测结果的影响。

优化后的系统必须做到：

- 事件数据来自可追溯的数据源或人工输入。
- 每个事件都有确定的时间、影响标的、重要性和处理策略。
- NQ Strategy Spec 模板可以声明事件窗口内的交易约束。
- 回测能够区分事件窗口和非事件窗口表现。
- LLM 只能看到受控的事件上下文，不能自行编造事件。
- 最终报告必须说明策略是否依赖宏观事件冲击获利。

## 2. 关键结论

- v1 不应让 LLM 直接接入新闻 API 做事件识别。
- 事件日历应作为结构化配置输入，优先支持人工维护和官方源导入。
- 事件感知能力只服务 NQ 日内策略验证，不扩展为通用宏观事件交易框架。
- 免费官方数据源足以覆盖主要美国宏观发布时间，但通常不提供完整的预期值、重要性评分和全球资产映射。
- 商业或聚合 API 可作为增强源，用于补充 `consensus`、`previous`、`actual`、`importance` 等字段，但不能成为唯一依赖。
- 任何使用 `actual`、`surprise` 或事件结果的策略都必须通过 point-in-time 校验，防止未来函数。
- 对 NQ 日内策略，事件前后最重要的默认行为是降低突破信号可信度、禁止单根 K 线判断趋势、要求二次确认或直接暂停开仓。
- 事件感知逻辑必须进入 bar backtest 和 tick replay，不能只存在于 LLM prompt 或 WebUI 提示中。

## 3. 事件数据源方案

### 3.1 数据源分层

事件数据源分为三层：

- `manual`：人工维护的 YAML/JSON，优先级最高，适合盘前手动输入当天关键事件。
- `official`：官方免费日历或 API，用于构建可审计的基础事件日历。
- `aggregator`：第三方聚合 API，用于补充预期值、实际值、重要性、国家和资产影响范围。

系统应按优先级合并事件：

1. `manual`
2. `official`
3. `aggregator`

同一事件发生冲突时，人工输入覆盖其他来源；官方源覆盖聚合源的时间字段；聚合源只补缺失字段。

### 3.2 免费官方源

优先支持以下公开来源：

- BLS News Release Schedule：CPI、PPI、Employment Situation、JOLTS 等。
  - https://www.bls.gov/schedule/news_release/default.asp
- BEA Release Schedule：GDP、PCE、Personal Income and Outlays、Trade 等。
  - https://www.bea.gov/news/schedule/full
- Federal Reserve FOMC Calendars：FOMC 会议、声明、SEP、Minutes。
  - https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
- Federal Reserve feeds：Fed 新闻、讲话、testimony 的 RSS/feeds。
  - https://www.federalreserve.gov/feeds/feeds.htm
- FRED API release dates：通用经济数据发布时间索引。
  - https://fred.stlouisfed.org/docs/api/fred/releases_dates.html

官方源的定位是生成“什么时候有重要事件”，不保证直接给出交易系统所需的所有字段。

### 3.3 可选聚合源

可选接入以下聚合 API：

- Trading Economics Calendar API：覆盖经济日历、预期值、前值、实际值、重要性等字段。
  - https://docs.tradingeconomics.com/economic_calendar/schema/
- Finnhub Economic Calendar：适合作为轻量补充源。
- Financial Modeling Prep Economic Calendar：适合作为轻量补充源。

聚合源必须配置 API key、限流、缓存和来源标识。免费额度变化、授权限制和历史数据可用性不能影响核心回测链路。

### 3.4 数据源边界

v1 不做实时新闻理解，不做自然语言新闻分类，不做未经校验的社交媒体事件识别。

事件数据只回答三个问题：

- 什么时候有事件。
- 事件重要性大致如何。
- 事件可能影响哪些标的。

事件结果是否高于预期、低于预期，以及市场如何解释该结果，不作为 v1 策略实时决策的核心输入。

## 4. 事件数据模型

### 4.1 配置文件

新增配置文件：

- `configs/macro_events.yaml`
- `configs/event_sources.yaml`
- `configs/event_policies.yaml`

`macro_events.yaml` 保存归一化后的事件。`event_sources.yaml` 保存数据源启用状态、API key 引用、刷新频率和缓存策略。`event_policies.yaml` 保存不同事件重要性和策略 family 的默认交易处理规则。

实现口径：

- 文档示例使用 YAML 表达；代码可以先采用 JSON-compatible YAML，直到引入 YAML parser。
- CLI/API 入参可以接受 calendar 文件路径或 `calendar_id`。如果传入 `calendar_id`，必须通过 registry 解析到具体文件或 Parquet calendar。
- 字段别名必须归一化：`time_utc`、`timestamp_utc`、`time` 均归一到 UTC timestamp；`symbols` 和 `affected_symbols` 均归一到 `affected_symbols`；`pre_window_minutes` 和 `pre_event_minutes` 均归一到 `pre_event_minutes`。

### 4.2 事件 schema

标准事件结构如下：

```yaml
schema_version: 1
calendar_id: macro_events_v1
generated_at: "2026-04-25T00:00:00Z"
timezone: UTC
events:
  - event_id: "bls_cpi_2026_05_12_0830_us"
    name: CPI
    category: inflation
    source: bls
    source_url: "https://www.bls.gov/schedule/news_release/default.asp"
    country: US
    time: "2026-05-12T08:30:00-04:00"
    time_utc: "2026-05-12T12:30:00Z"
    importance: high
    symbols: [NQmain, ES, GC, DXY, ZN]
    affected_strategy_families:
      - opening_range_breakout
      - volatility_expansion
      - intraday_momentum
      - gap_fade_or_continuation
    pre_window_minutes: 30
    release_window_minutes: 5
    post_window_minutes: 30
    cooldown_minutes: 15
    policy_ref: high_impact_macro_v1
    fields:
      previous: null
      consensus: null
      actual: null
      unit: null
    note: "通胀数据，可能影响利率预期、股指、黄金和美元。"
```

### 4.3 字段规则

必填字段：

- `event_id`
- `name`
- `category`
- `source`
- `country`
- `time`
- `time_utc`
- `importance`
- `symbols`
- `pre_window_minutes`
- `post_window_minutes`

可选字段：

- `source_url`
- `affected_strategy_families`
- `release_window_minutes`
- `cooldown_minutes`
- `policy_ref`
- `previous`
- `consensus`
- `actual`
- `unit`
- `note`

时间必须统一转换为 UTC 存储。原始时区保留在 `time` 中用于审计和 UI 展示。

### 4.4 事件版本

每次回测必须记录 `event_calendar_hash`。该 hash 由以下内容生成：

- 事件清单。
- 事件时间。
- 事件重要性。
- 影响标的。
- 事件窗口参数。
- 数据源版本。
- 人工覆盖记录。

`event_calendar_hash` 必须进入实验快照，与 `data_version_hash`、`strategy_spec_hash`、`cost_model_hash` 一起保证可复现。

## 5. 事件上下文生成

### 5.1 事件状态

对每个 bar 或 tick 时间戳，系统生成事件状态：

- `normal`：不在事件窗口内。
- `pre_event`：事件发布前窗口。
- `release_window`：事件发布附近的极短窗口。
- `post_event`：事件发布后窗口。
- `cooldown`：事件后冷却期。

事件状态可以多事件重叠。重叠时按最高重要性和最严格 policy 处理。

### 5.2 标准化输出

事件上下文输出到 Parquet：

- `data/events/calendar/{calendar_id}.parquet`
- `data/events/context/{symbol}/date={yyyy-mm-dd}/part-*.parquet`

`event_context` 至少包含：

- `timestamp`
- `symbol`
- `event_state`
- `active_event_ids`
- `max_importance`
- `policy_ref`
- `minutes_to_event`
- `minutes_since_event`
- `affected_strategy_families`

### 5.3 与行情数据的关系

事件上下文不修改原始行情数据。bar backtest 和 tick replay 在运行时按时间戳 join 事件上下文。

这样可以保证：

- 同一行情数据可以用不同事件日历重跑。
- 事件策略变化不会污染价格数据。
- 报告可以明确区分行情版本和事件版本。

## 6. NQ Strategy Spec 事件扩展

### 6.1 新增字段

Strategy Spec 增加 `event_policy`：

```yaml
event_policy:
  calendar_ref: macro_events_v1
  default_action: allow
  high_impact:
    pre_window_minutes: 30
    post_window_minutes: 30
    release_window_minutes: 5
    new_entries: block
    existing_positions: tighten_or_flatten
    breakout_confirmation:
      mode: second_confirmation
      min_confirm_bars: 2
      require_post_event_close: true
      min_distance_atr: 0.25
    signal_weight_adjustments:
      opening_range_breakout: 0.4
      volatility_expansion: 0.5
      intraday_momentum: 0.6
      single_bar_momentum: 0.0
  medium_impact:
    pre_window_minutes: 15
    post_window_minutes: 15
    new_entries: require_extra_confirmation
    existing_positions: keep_with_tighter_stop
```

### 6.2 支持的动作

`new_entries` 支持：

- `allow`
- `block`
- `require_extra_confirmation`
- `reduce_signal_weight`
- `long_only`
- `short_only`
- `flatten_before_event`

`existing_positions` 支持：

- `keep`
- `tighten_stop`
- `tighten_or_flatten`
- `flatten_before_event`
- `block_adds`

`breakout_confirmation.mode` 支持：

- `none`
- `second_confirmation`
- `post_event_reclaim`
- `close_beyond_level`
- `vwap_and_range_confirm`

### 6.3 Validator 规则

Validator 必须拒绝：

- 事件规则引用不存在的 `calendar_ref`。
- 使用事件 `actual` 或 `surprise` 但没有 point-in-time 声明。
- 在事件发布前引用发布后的 `actual`、修正值或市场反应。
- 无边界的事件窗口。
- 高影响事件内允许无限加仓、亏损加倍或放宽止损。
- 声称“事件后必然趋势延续”的不可验证假设。
- 只依赖单根 K 线冲击入场、且无二次确认的高影响事件突破策略。

### 6.4 默认策略 family 规则

`opening_range_breakout`：

- 高影响事件前后默认禁止新开仓。
- 如果允许交易，必须要求事件后至少两根 bar 收盘确认。
- 禁止把事件瞬间扫高或扫低直接视为趋势突破。

`volatility_expansion`：

- 事件窗口内降低波动扩张信号权重。
- 事件发布后需要等待波动从极端值回落或重新形成方向。

`intraday_momentum`：

- 事件窗口内禁止单周期动量独立触发。
- 需要多周期一致或 VWAP 方向确认。

`regime_filtered_mean_reversion`：

- 高影响事件发布窗口默认禁止逆势接飞刀。
- 事件后只有在 spread、range 和 realized volatility 回到阈值内才允许恢复。

`time_of_day_edge`：

- 事件窗口覆盖原本时段优势时，必须输出事件覆盖标记。

`gap_fade_or_continuation`：

- 如果开盘前存在 CPI、NFP、FOMC 等事件，gap 逻辑必须显式区分事件驱动 gap 和普通隔夜 gap。

## 7. 回测与撮合优化

### 7.1 bar backtest

bar backtest 在每根 bar close 生成信号时必须读取事件上下文。

如果当前 bar 位于事件窗口：

- 按 `event_policy` 阻断、降权或延迟信号。
- 将入场理由记录为事件过滤后的结果。
- 如果信号被阻断，记录 `blocked_by_event_policy`。
- 如果要求二次确认，生成 pending signal，而不是立即下单。

### 7.2 tick replay

tick replay 必须在事件窗口使用更保守撮合假设：

- spread 异常扩大时按真实 bid/ask 执行。
- slippage 可以按事件重要性增加。
- 同一 bar 内无法确定顺序时继续按对策略不利顺序处理。
- 事件发布窗口内 stop 和 take profit 同时触发时优先按 stop 处理。
- 事件窗口内 market order 可配置为更高滑点或拒绝成交。

推荐默认滑点扩展：

```yaml
event_slippage_model:
  normal:
    slippage_ticks_per_side: 1
  medium_impact:
    pre_event: 1
    release_window: 2
    post_event: 1
  high_impact:
    pre_event: 2
    release_window: 4
    post_event: 2
```

### 7.3 持仓管理

事件前已有仓位时支持三种模式：

- 保持仓位，但禁止加仓。
- 收紧止损到更保守距离。
- 事件前强制平仓。

默认建议：

- 高影响事件前 5 分钟不新开仓。
- 高影响事件发布窗口内不加仓。
- 如果策略不是专门的事件策略，事件发布前可选择强平或收紧止损。

### 7.4 交易记录

每笔交易新增字段：

- `event_state_at_entry`
- `event_state_at_exit`
- `active_event_ids_at_entry`
- `active_event_ids_at_exit`
- `event_policy_action`
- `signal_weight_before_event_policy`
- `signal_weight_after_event_policy`
- `blocked_or_delayed_reason`

## 8. 研究循环优化

### 8.1 LLM 输入

LLM prompt 只接收结构化事件摘要：

```json
{
  "macro_context": {
    "calendar_id": "macro_events_v1",
    "events": [
      {
        "name": "CPI",
        "time": "2026-05-12T12:30:00Z",
        "importance": "high",
        "symbols": ["NQmain", "ES", "GC"],
        "policy_hint": "event window requires second confirmation for breakout signals"
      }
    ]
  }
}
```

LLM 不接收原始新闻文章，不抓取实时新闻，不自行推断当天是否有事件。

### 8.2 LLM 输出约束

LLM 可以提出：

- 事件窗口内暂停交易。
- 事件后等待二次确认。
- 事件窗口降低突破权重。
- 事件后只在 VWAP、ATR、range 条件重新稳定后恢复交易。
- 将事件日与非事件日分开评估。

LLM 不能提出：

- 直接基于未校验新闻文本交易。
- 使用事件发布后的实际值预测发布前交易。
- 将事件瞬间单根 K 线当作充分趋势证据。
- 用更宽止损和更大仓位吸收事件冲击。
- 通过增加大量事件条件和参数组合追逐历史事件日最优结果。

事件相关参数也必须遵守主方案的参数预算规则。事件前后窗口、确认 bar 数、信号降权系数和滑点压力参数不能组成大规模网格来挖历史事件日最优点；超出默认参数预算时，必须标记 `parameter_budget_exceeded = true` 并应用 trial count 惩罚。

### 8.3 失败反馈

回测反馈给 LLM 的失败原因应包含：

- `event_window_false_breakout`
- `event_window_slippage_sensitive`
- `event_dependency_risk`
- `non_event_performance_failure`
- `event_window_drawdown_excessive`
- `post_event_confirmation_too_late`
- `too_few_trades_after_event_filter`

LLM 只能看到 train 和 validation 的事件归因反馈；test 和 final holdout 的细节仍按主方案隐藏。

## 9. 指标与排行榜优化

### 9.1 新增指标

每个策略必须输出：

- `event_window_trade_count`
- `non_event_trade_count`
- `event_window_net_pnl`
- `non_event_net_pnl`
- `event_window_sharpe`
- `non_event_sharpe`
- `event_window_max_drawdown`
- `non_event_max_drawdown`
- `event_window_avg_slippage`
- `event_dependency_ratio`
- `blocked_signal_count`
- `delayed_signal_count`
- `second_confirmation_pass_rate`

`event_dependency_ratio` 定义为：

```text
event_dependency_ratio = abs(event_window_net_pnl) / max(abs(total_net_pnl), epsilon)
```

### 9.2 新增门槛

最终候选必须满足：

- 非事件窗口净盈利不能显著为负。
- 高影响事件窗口最大回撤不能突破配置阈值。
- 剔除事件窗口后，策略不能完全失效，除非该策略明确声明为事件策略。
- 事件窗口贡献超过阈值时，必须输出风险标记。
- 事件窗口内平均滑点超过配置阈值时，策略不能直接入榜。

推荐默认阈值：

```yaml
event_risk_thresholds:
  max_event_dependency_ratio: 0.4
  max_event_window_drawdown_ratio: 0.35
  min_non_event_profit_factor: 1.05
  max_event_avg_slippage_ticks: 4
  min_second_confirmation_pass_rate: 0.35
```

### 9.3 报告要求

每个策略报告必须包含：

- 事件窗口和非事件窗口表现对比。
- 逐事件类别表现。
- 高影响事件日前后交易分布。
- 被事件 policy 阻断或延迟的信号数量。
- 事件窗口中最大亏损交易明细。
- 是否存在事件依赖风险。

最终 leaderboard 增加列：

- `event_risk_label`
- `event_dependency_ratio`
- `non_event_sharpe`
- `event_window_drawdown`
- `event_policy_name`

## 10. WebUI 优化

新增页面或组件：

- 事件日历：显示已导入和人工维护的宏观事件。
- 事件源状态：显示 BLS、BEA、Fed、FRED、聚合 API 的刷新结果。
- 事件覆盖图层：在 K 线和 tick replay 图上标记事件前、发布、发布后、冷却窗口。
- 事件表现面板：展示事件窗口和非事件窗口指标。
- 事件规则面板：展示当前 Strategy Spec 的 `event_policy`。
- 事件审计面板：显示事件来源、导入时间、人工覆盖和 hash。

UI 不应该用自然语言解释“新闻利好/利空”。UI 只展示结构化事件、策略规则和回测结果。

## 11. CLI 与 API 优化

### 11.1 CLI

新增 CLI：

- `tlm events import --source bls --from YYYY-MM-DD --to YYYY-MM-DD`
- `tlm events import --source bea --from YYYY-MM-DD --to YYYY-MM-DD`
- `tlm events import --source fed --from YYYY-MM-DD --to YYYY-MM-DD`
- `tlm events import --source fred --from YYYY-MM-DD --to YYYY-MM-DD`
- `tlm events import --source tradingeconomics --from YYYY-MM-DD --to YYYY-MM-DD`
- `tlm events validate --calendar configs/macro_events.yaml`
- `tlm events build-context --symbol NQmain --calendar macro_events_v1`
- `tlm events list --symbol NQmain --from YYYY-MM-DD --to YYYY-MM-DD`
- `tlm events diff --left CALENDAR_A --right CALENDAR_B`

### 11.2 API

新增 API：

- `GET /api/events`
- `POST /api/events/import`
- `POST /api/events/validate`
- `POST /api/events/build-context`
- `GET /api/events/{event_id}`
- `GET /api/events/context`
- `GET /api/reports/{id}/event-attribution`

长任务仍使用现有任务模型，支持创建、取消、状态查询、日志和 SSE 进度推送。

## 12. 存储与审计

SQLite 新增表：

- `event_sources`
- `event_calendars`
- `event_import_runs`
- `event_overrides`
- `event_policy_registry`

Parquet 新增目录：

- `data/events/calendar/`
- `data/events/context/`
- `experiments/{experiment_id}/event_attribution/`

实验快照新增字段：

- `event_calendar_hash`
- `event_policy_hash`
- `event_context_hash`
- `event_source_snapshot`

每次人工修改事件必须记录：

- 修改人或执行来源。
- 修改时间。
- 修改前后内容。
- 修改原因。

## 13. 实施阶段

### Phase A：事件配置与校验

实现内容：

- 新增 `macro_events.yaml` schema。
- 实现事件时间 UTC 归一化。
- 实现事件重要性、标的映射和窗口参数校验。
- 生成 `event_calendar_hash`。

完成标准：

- 人工维护事件文件可以通过 validator。
- 无效时间、缺失标的、无边界窗口会被拒绝。
- 同一事件日历重复运行 hash 一致。

### Phase B：官方源导入

实现内容：

- BLS、BEA、Fed、FRED 的导入器。
- 原始响应缓存。
- 来源字段归一化。
- 人工覆盖优先级。

完成标准：

- 可以导入指定日期范围的官方事件。
- 可以把官方事件合并到 `macro_events.yaml` 或 Parquet calendar。
- 导入失败不会阻断手动事件文件使用。

### Phase C：事件上下文

实现内容：

- 根据事件日历生成 symbol-level event context。
- 支持事件窗口重叠。
- 支持最高重要性和最严格 policy 选择。

完成标准：

- 任意 1m bar 可以查询事件状态。
- tick replay 可以按 timestamp 读取事件状态。
- context hash 可复现。

### Phase D：Strategy Spec 与回测接入

实现内容：

- NQ Strategy Spec 增加 `event_policy`。
- Validator 拒绝未来函数和无边界事件规则。
- bar backtest 和 tick replay 执行事件 policy。
- trade 记录事件字段。

完成标准：

- 高影响事件窗口可阻断、延迟或降权信号。
- 二次确认逻辑可复现。
- tick replay 在事件窗口使用更保守滑点。

### Phase E：研究循环与报告

实现内容：

- LLM prompt 增加结构化事件上下文。
- 失败原因增加事件归因。
- 指标增加事件窗口和非事件窗口拆分。
- leaderboard 增加事件风险列。

完成标准：

- LLM 不需要新闻 API 即可生成事件感知策略。
- test 和 final holdout 的事件细节不会泄漏给同轮 LLM。
- 报告能明确说明策略是否依赖事件冲击。

### Phase F：WebUI

实现内容：

- 事件日历页面。
- 图表事件标记。
- 事件归因报告。
- 事件源和人工覆盖审计。

完成标准：

- 用户能查看事件窗口如何影响交易。
- 用户能发现策略是否在事件窗口承担过高风险。
- 用户能审计事件数据来源和版本。

## 14. 风险与限制

主要风险：

- 免费官方源格式可能变化，需要缓存和解析容错。
- 官方源通常不提供完整预期值和实际值。
- 聚合 API 免费层可能有额度、授权或历史数据限制。
- 事件时间修正和假期调整可能导致历史回测不一致。
- 使用事件实际值或 surprise 容易引入未来函数。
- 过度过滤事件窗口可能让年交易次数低于目标。

默认限制：

- v1 事件逻辑只作为交易约束和风险归因，不作为宏观预测模型。
- v1 不做通用宏观事件策略框架。
- v1 不基于新闻文本判断利多利空。
- v1 不承诺事件策略具有稳定盈利能力。
- v1 不把事件窗口内的极端成交视为真实可执行优势。

## 15. 推荐默认策略

对 `NQmain`，推荐默认事件处理为：

- 高影响事件前 30 分钟禁止新开突破仓。
- 高影响事件发布后 30 分钟要求二次确认。
- 发布窗口 5 分钟内禁止基于单根 K 线判断趋势。
- 对已有仓位，发布前 5 分钟禁止加仓，并按策略配置收紧止损或平仓。
- 事件窗口滑点按普通滑点的 2 到 4 倍压力测试。
- 报告中必须展示剔除事件窗口后的策略表现。

这套默认规则偏保守，适合作为研究系统的安全底座。后续如果专门研究事件策略，可以单独定义 `event_driven_macro` family，并要求更严格的 point-in-time 数据和样本外验证。
