# NQ 运行期市场监控与强信号复核优化方案

> 协调说明：当本文与更新的 `agent-driven-trading-system-optimization.md` 或 `nt8-live-integration-roadmap.md` 冲突时，以更新文档为准。本文的运行期监控默认属于 `v1 research`，输出观察、报告和 paper-only 复核，不授予 live execution 权限。

## 1. 背景与目标

本方案是在 `docs/plan/local-llm-nq-strategy-system.md` 和 `docs/plan/macro-event-aware-nq-strategy-optimization.md` 的基础上，补充一层运行期市场监控、播报和强信号复核能力。

原主方案解决的是“如何发现、验证和筛选可复现的 NQ 日内策略”。宏观事件方案解决的是“如何把 CPI、NFP、FOMC 等事件作为结构化风险输入”。本方案解决的是另一件事：当策略已经通过研究闭环进入 paper replay 或人工观察阶段后，系统如何每 5 分钟读取市场状态，提取特征，识别强弱信号，并在必要时调用 LLM 做结构化复核。

本方案不替代 Strategy Spec、回测、tick replay、防过拟合和 paper replay。运行期分析只能解释和复核已存在的策略或候选信号，不能绕过历史验证直接生成可交易结论。

核心目标：

- 每 5 分钟生成一次可审计的 NQ 市场状态快照。
- 使用确定性规则提取关键特征、关键价位、趋势结构、风险状态和事件状态。
- 对弱信号只做轻量提醒，对强信号才触发深度 LLM 复核。
- LLM 复核必须基于结构化输入和已验证策略上下文，不能自由编造行情、事件或交易理由。
- 将运行期观察结果写入日志，用于后续策略研究、失败归因和人工复盘。
- v1 只支持本地 paper/replay 和人工辅助决策，不直接接入真实实盘下单。

## 2. 关键结论

- `docs/优化1.md` 的价值主要在运行期监控和分析报告形态，不是完整策略研究方案。
- “找特征 -> 定策略 -> 回测验证 -> paper replay/人工观察”应作为系统主链路，“实时播报 -> 强信号复核”应作为上线后的监控链路。
- 5 分钟播报不等于 5 分钟 LLM 决策。规则引擎必须先筛选信号，LLM 只处理强信号、冲突信号、事件窗口信号和关键价位附近信号。
- 多空辩论有利用价值，但必须限定为风险复核机制，不能让 LLM 在没有 Strategy Spec 和回测证据时直接给出交易动作。
- 全局记忆可以使用，但只能包含 strategy cards、已关闭实验摘要、失败原因、事件 policy 和已批准的运行配置，不能泄露 test 或 final holdout 细节。
- 非强信号只记录和提醒，不生成交易建议，避免系统在噪声中频繁给出伪确定性判断。
- 运行期所有输出都必须保存输入快照、特征值、触发原因、LLM prompt hash、模型参数和最终决策，方便审计和复盘。

## 3. 与主方案的边界

### 3.1 主研究链路

主研究链路仍然由 `local-llm-nq-strategy-system.md` 定义：

1. 提出可验证的市场假设。
2. 生成受控 Strategy Spec。
3. 通过 validator 校验。
4. 执行 1m bar 快速筛选。
5. 执行 tick bid/ask replay。
6. 做 walk-forward、防过拟合和成本敏感性检查。
7. 写入 leaderboard 和策略卡片。
8. 合格策略进入 paper replay 或人工观察。

运行期监控不能替代上述步骤。任何未经过该链路的想法只能作为研究候选，不能作为可执行策略。

### 3.2 宏观事件链路

宏观事件仍由 `macro-event-aware-nq-strategy-optimization.md` 管理：

- 事件来自结构化日历或人工输入。
- 每个 bar 或 tick join 事件上下文。
- 事件窗口内的暂停、降权、二次确认和滑点扩展由 `event_policy` 控制。
- LLM 只接收结构化事件摘要，不能自行抓新闻或推断事件结果。

运行期监控读取事件上下文，但不修改事件日历，不直接解释新闻文本。

### 3.3 运行期监控链路

运行期监控负责：

- 读取最新行情、策略状态、事件状态和 paper/replay 持仓。
- 提取当前 5 分钟市场结构。
- 计算信号强度和风险等级。
- 对弱信号生成提醒。
- 对强信号生成 LLM 复核任务。
- 保存复核结论和人工可读报告。

运行期监控不负责：

- 发明未验证策略。
- 绕过 Strategy Spec 下单。
- 修改 final holdout 结果。
- 给未通过回测的策略包装成“临盘机会”。
- 连接真实经纪商或真实 NinjaTrader 实盘下单。

## 4. 总体流程

推荐流程：

```text
market data snapshot
  -> feature extraction
  -> event context join
  -> strategy state join
  -> signal strength scoring
  -> weak signal notice or strong signal review
  -> LLM bull/bear/risk debate
  -> paper/replay action review
  -> audit log and replay report
```

对应到 `docs/优化1.md` 的简化表达：

```text
找特征 -> 定策略 -> 回测验证 -> paper replay/人工观察
```

其中：

- `找特征`：由 deterministic scanner 执行。
- `定策略`：只能从 Strategy Spec 和已验证 strategy family 中选择或生成研究候选。
- `回测验证`：必须走主研究链路。
- `paper replay/人工观察`：由运行期监控提供状态、强信号复核和风险提示。

## 5. 数据输入

### 5.1 行情输入

v1 支持两类输入：

- 历史 replay：从本地 Parquet tick/bar 数据回放。
- 实时快照：从已接入的数据源或 NT8 快照读取最近行情。

运行期快照至少包含：

- `symbol`
- `snapshot_time`
- `last_price`
- `bid`
- `ask`
- `spread`
- `bar_1m`
- `bar_5m`
- `session_open`
- `session_high`
- `session_low`
- `vwap`
- `atr`
- `realized_volatility`
- `volume_or_tick_volume`

如果使用 NT8 快照，报告必须标记来源为 `nt8_snapshot`，不能把它混同为 Dukascopy 历史回测数据。

### 5.2 策略输入

运行期监控只能读取以下策略对象：

- 已通过 validator 的 Strategy Spec。
- 已完成回测并生成结果的 strategy card。
- paper/replay 中启用的策略实例。
- 已批准的策略 family 默认规则。

策略上下文至少包含：

- `strategy_id`
- `strategy_spec_hash`
- `family`
- `direction_mode`
- `timeframe`
- `enabled`
- `paper_only`
- `last_signal_time`
- `current_position`
- `risk_limits`
- `known_failure_modes`
- `event_policy_ref`

### 5.3 事件输入

运行期监控必须 join 事件上下文：

- `event_state`
- `active_event_ids`
- `max_importance`
- `policy_ref`
- `minutes_to_event`
- `minutes_since_event`
- `affected_strategy_families`

事件状态会影响信号强度、强信号门槛和 LLM 复核 prompt。

### 5.4 全局记忆输入

允许进入运行期 LLM prompt 的记忆：

- 已通过验证的 strategy cards 摘要。
- 已关闭失败实验的失败原因。
- strategy family 的风险注意事项。
- 事件 policy 和默认处理规则。
- 用户显式批准的运行期偏好。

禁止进入运行期 LLM prompt 的记忆：

- 同轮 test 细节。
- final holdout 细节。
- 未审计的新闻文本。
- 未经验证的实盘传闻或社交媒体结论。
- 可导致未来函数的信息。

## 6. 特征提取

### 6.1 市场结构特征

每 5 分钟计算一次：

- 当前价格相对 session high、session low、previous close 的位置。
- 当前价格相对 VWAP、rolling VWAP、opening range 的位置。
- 近 3、6、12 根 5m bar 的方向一致性。
- 高点/低点是否递增或递减。
- 是否接近关键整数位、前高、前低、日内极值。
- 当前 spread 是否异常。
- 当前 realized volatility 是否异常。
- 当前 bar range 是否显著高于最近均值。

### 6.2 策略相关特征

根据启用的 strategy family 额外计算：

- `opening_range_breakout`：开盘区间高低点、突破距离、突破后收盘确认。
- `volatility_expansion`：ATR/range 扩张倍数、扩张持续性、是否单根冲击。
- `intraday_momentum`：多周期动量一致性、VWAP 方向、回撤深度。
- `regime_filtered_mean_reversion`：偏离 VWAP 距离、波动状态、反转确认。
- `time_of_day_edge`：当前时段与历史优势窗口的匹配度。
- `gap_fade_or_continuation`：gap 来源、事件覆盖、延续或回补条件。

### 6.3 风险特征

每个快照必须计算：

- 是否在高影响事件 pre/release/post 窗口。
- 是否处于高 spread 或低流动性状态。
- 是否接近 session 极值但缺少二次确认。
- 是否出现单根大 bar 后追价风险。
- 是否与当前持仓方向冲突。
- 是否触发日内最大亏损、最大交易次数或冷却时间限制。

## 7. 信号强度评分

### 7.1 评分原则

信号强度由规则引擎计算，LLM 不直接决定是否为强信号。

默认输出：

- `none`
- `weak_notice`
- `medium_watch`
- `strong_review`
- `blocked`

只有 `strong_review` 触发深度 LLM 复核。`blocked` 直接记录阻断原因，除非用户显式要求审查。

### 7.2 推荐评分字段

```yaml
signal_score:
  total: 0.0
  trend_alignment: 0.0
  level_proximity: 0.0
  confirmation_quality: 0.0
  volatility_quality: 0.0
  event_penalty: 0.0
  spread_penalty: 0.0
  position_conflict_penalty: 0.0
  overextension_penalty: 0.0
  signal_class: weak_notice
  reasons: []
```

推荐默认门槛：

- `total < 0.35`：不提醒。
- `0.35 <= total < 0.60`：弱提醒。
- `0.60 <= total < 0.78`：观察列表。
- `total >= 0.78`：强信号复核。
- 高影响事件 release window 内默认不允许强信号直接通过，只能进入风险复核。

### 7.3 强信号触发条件

满足以下任一条件可进入 `strong_review`：

- 已启用 Strategy Spec 产生新信号，且 signal score 达到门槛。
- 价格接近关键高低点，且出现突破或失败突破结构。
- paper/replay 持仓接近失效条件或风险阈值。
- 事件窗口后出现策略要求的二次确认。
- 多个策略 family 给出同向信号。
- 行情结构与当前持仓方向出现明显冲突。

以下情况默认不进入强信号：

- 单根 5m 大阳线或大阴线，没有二次确认。
- 高影响事件发布窗口内的瞬时突破。
- spread 异常扩大时的 market order 信号。
- 已触发冷却时间或日内风险限制。
- 策略未通过 validator 或无回测结果。

## 8. LLM 强信号复核

### 8.1 复核目标

LLM 的职责是解释和复核，不是替代规则引擎或回测引擎。

LLM 可以输出：

- 多头论据。
- 空头论据。
- 中性或不交易理由。
- 关键失效条件。
- 风险点。
- 是否建议继续 paper/replay 观察。
- 是否需要创建新的研究候选。

LLM 不能输出：

- 未经 Strategy Spec 支持的直接下单指令。
- 基于新闻猜测的交易结论。
- 基于未来数据或 final holdout 泄露的判断。
- 加宽止损、亏损加仓、马丁或无限网格。
- 对真实账户的执行建议。

### 8.2 复核角色

推荐使用固定角色结构：

1. `bull_case`：只整理看多证据、前提和失效条件。
2. `bear_case`：只整理看空证据、前提和失效条件。
3. `risk_reviewer`：检查事件、spread、追价、持仓冲突、过拟合和执行风险。
4. `decision_summarizer`：综合输出 `observe`、`paper_allow`、`paper_block`、`research_candidate` 或 `no_action`。

这不是让多个模型自由争论，而是让同一个结构化 prompt 产出可审计的多视角分析。

### 8.3 LLM 输入

LLM prompt 必须使用结构化 JSON 或 YAML：

```yaml
review_request:
  symbol: NQmain
  snapshot_time: "2026-04-26T13:35:00Z"
  timeframe: 5m
  market_snapshot:
    last_price: 27440.25
    vwap: 27304.86
    session_high: 27462.50
    session_low: 27360.00
    spread_ticks: 1
  signal_score:
    signal_class: strong_review
    total: 0.81
    reasons:
      - near_session_high
      - trend_aligned
      - overextension_risk
  event_context:
    event_state: normal
    active_event_ids: []
  strategy_context:
    strategy_id: example_strategy
    family: intraday_momentum
    paper_only: true
    current_position: flat
  known_failure_modes:
    - false_breakout_near_high
    - post_event_single_bar_chase
```

### 8.4 LLM 输出

LLM 输出必须可解析：

```yaml
review_result:
  action: observe
  confidence: medium
  bull_case:
    evidence: []
    invalidation: []
  bear_case:
    evidence: []
    invalidation: []
  risk_review:
    key_risks: []
    blocked_reasons: []
  final_summary:
    market_structure: ""
    decision_reason: ""
    next_check: "next_5m_close"
  research_candidate:
    create: false
    hypothesis: null
```

允许的 `action`：

- `no_action`
- `observe`
- `paper_allow`
- `paper_block`
- `research_candidate`

`paper_allow` 只表示 paper/replay 层允许模拟执行，不能表示真实实盘下单。

## 9. 运行期报告

### 9.1 轻量播报

每 5 分钟默认生成轻量播报：

- 当前价格位置。
- VWAP/关键位关系。
- 趋势结构。
- 事件状态。
- spread/波动风险。
- 启用策略是否有信号。
- 弱提醒或无动作原因。

轻量播报不调用 LLM，优先由模板生成。

### 9.2 强信号报告

强信号报告包含：

- 行情缓存范围和数据来源。
- 市场结构摘要。
- 触发强信号的规则原因。
- 多头论据。
- 空头论据。
- 风险复核。
- 关键价位和失效条件。
- event policy 处理结果。
- paper/replay 是否允许继续。
- 是否建议创建研究候选。

### 9.3 审计记录

每次播报和复核都必须记录：

- `monitor_run_id`
- `snapshot_hash`
- `data_source`
- `strategy_spec_hash`
- `event_calendar_hash`
- `signal_score`
- `trigger_reasons`
- `llm_model`
- `prompt_hash`
- `response_hash`
- `action`
- `created_at`

## 10. 存储设计

推荐目录：

- `data/runtime/snapshots/{symbol}/date={yyyy-mm-dd}/part-*.parquet`
- `data/runtime/features/{symbol}/date={yyyy-mm-dd}/part-*.parquet`
- `data/runtime/signals/{symbol}/date={yyyy-mm-dd}/part-*.parquet`
- `data/runtime/reviews/{symbol}/date={yyyy-mm-dd}/part-*.jsonl`
- `data/runtime/reports/{monitor_run_id}.md`

SQLite 记录：

- monitor job 状态。
- active strategy 配置。
- 最近播报和复核索引。
- WebUI session 状态。

Parquet 记录：

- 快照。
- 特征。
- 信号评分。
- paper/replay 动作。

JSONL 记录：

- LLM 复核请求和响应。
- 人工备注。
- 异常和阻断原因。

## 11. CLI 与 API

### 11.1 CLI

新增 CLI：

- `tlm monitor run --symbol NQmain --interval 5m`
- `tlm monitor once --symbol NQmain`
- `tlm monitor replay --symbol NQmain --from YYYY-MM-DD --to YYYY-MM-DD`
- `tlm monitor report --run-id RUN_ID`
- `tlm monitor active-strategies`
- `tlm monitor enable-strategy --strategy-id STRAT_ID`
- `tlm monitor disable-strategy --strategy-id STRAT_ID`
- `tlm monitor review --snapshot-id SNAPSHOT_ID`

MVP 口径：

- `tlm monitor once` 可以先只生成 JSON/Markdown 报告，用于验证快照和关键位扫描。
- `run`、`replay`、active strategy 管理、review prompt 和 Parquet/SQLite 索引属于后续增强。
- 任何 monitor 输出进入 execution intent 前，必须经过 `agent-driven-trading-system-optimization.md` 定义的 Signal Engine 和 Risk Gateway。

### 11.2 API

新增 API：

- `POST /api/monitor/runs`
- `GET /api/monitor/runs/{id}`
- `POST /api/monitor/once`
- `GET /api/monitor/snapshots`
- `GET /api/monitor/signals`
- `GET /api/monitor/reviews/{id}`
- `POST /api/monitor/strategies/{id}/enable`
- `POST /api/monitor/strategies/{id}/disable`
- `GET /api/monitor/stream`

API 契约以 `docs/openapi/tradingllmagent.openapi.yaml` 为准；本文列表是目标能力清单，不是最终 schema。

实时进度使用 Server-Sent Events，避免为 v1 引入外部消息队列。

## 12. WebUI 优化

运行期监控页面应服务快速扫描和复盘：

- 顶部显示当前 NQ 状态、数据源、事件状态和 paper/replay 状态。
- 主图显示 5m K 线、VWAP、opening range、关键位、策略信号和持仓。
- 侧栏显示 signal score、触发原因、阻断原因和下一次检查时间。
- 强信号报告使用 tabs 展示多头、空头、风险复核和最终结论。
- 历史列表按 `strong_review`、`blocked`、`paper_allow`、`research_candidate` 筛选。
- 每条复核记录可跳转到对应 snapshot、Strategy Spec 和 event context。

WebUI 不显示真实下单按钮，不管理真实账户，不暗示系统已连接实盘执行。

## 13. 与研究循环的反馈

运行期监控可以向研究循环反馈候选问题，但必须经过明确边界。

可反馈内容：

- 多次出现但被阻断的信号模式。
- 强信号复核中反复出现的失败突破结构。
- paper/replay 中频繁触发的失效条件。
- 事件窗口导致策略失效的具体模式。
- 用户手动标记为值得研究的市场结构。

反馈方式：

- 生成 `research_candidate`，但不自动进入 leaderboard。
- 候选必须转换为 Strategy Spec 或 family 参数变体。
- 候选必须走 bar backtest、tick replay 和 walk-forward。
- LLM 只能看到 train/validation 反馈。

禁止反馈方式：

- 根据某几次运行期成功样例直接提高策略评分。
- 把人工盘感结论写入 final holdout。
- 用运行期 LLM 复核结果替代回测指标。
- 因为强信号报告看起来合理而跳过成本和滑点验证。

## 14. 风险控制

### 14.1 执行风险

v1 默认所有动作都是 paper/replay 或人工观察：

- `paper_allow` 只允许模拟执行。
- `paper_block` 阻止模拟执行或提示人工风险。
- 不接真实账户。
- 不发送真实 broker order。
- 不通过 NinjaTrader OIF 自动实盘下单。

### 14.2 LLM 风险

必须防止：

- LLM 过度自信。
- 单根 K 线叙事化。
- 对关键价位赋予伪确定性。
- 事件窗口追价。
- 把未验证策略包装成“临盘机会”。
- 从全局记忆中泄露样本外测试信息。

默认措施：

- 强制结构化输入输出。
- 强制输出 invalidation。
- 强制 risk reviewer。
- 保留 prompt/response hash。
- LLM 不能直接改变风险限制。

### 14.3 数据风险

必须标记：

- 数据来源。
- 数据延迟。
- bar 是否完整。
- spread 是否异常。
- 事件日历版本。
- 快照是否来自 NT8 而非历史回测源。

如果数据不完整或延迟超过阈值，系统应输出 `data_stale` 或 `data_incomplete`，并禁止 `paper_allow`。

## 15. 实施阶段

### Phase A：快照与特征

- 定义 runtime snapshot schema。
- 实现 5m 快照生成。
- 实现 VWAP、关键位、趋势结构和 spread 特征。
- 写入 Parquet 和 SQLite 索引。

验收标准：

- 可以对历史数据 replay 生成连续 5m 快照。
- 每个快照有稳定 hash。
- 数据来源和时间戳可追溯。

### Phase B：信号评分

- 实现 signal score schema。
- 实现弱信号、观察、强信号和阻断分类。
- 接入 Strategy Spec 状态。
- 接入 event context。

验收标准：

- 非强信号不触发 LLM。
- 高影响事件窗口内默认阻断或降权。
- 每个信号都有触发原因和阻断原因。

### Phase C：LLM 复核

- 实现强信号 review prompt。
- 实现 bull/bear/risk/summarizer 输出结构。
- 保存 prompt hash 和 response hash。
- 对不可解析输出进行拒绝和重试。

验收标准：

- LLM 输出可解析。
- 不允许输出真实实盘动作。
- 每个复核都有 invalidation 和 risk review。

### Phase D：报告与 WebUI

- 实现轻量播报报告。
- 实现强信号复核报告。
- WebUI 展示快照、信号、复核和历史记录。
- 支持从报告跳转到 Strategy Spec 和 event context。

验收标准：

- 用户可以在 WebUI 中快速看到当前市场状态。
- 强信号报告可复盘。
- 历史复核可按动作和原因筛选。

### Phase E：研究反馈

- 支持从复核结果创建 research candidate。
- 将候选转为 Strategy Spec 草案或策略 family 变体。
- 候选进入主研究链路。

验收标准：

- 运行期观察不会直接进入 leaderboard。
- 所有候选必须重新回测。
- 研究反馈不泄露 test 和 final holdout。

## 16. 默认配置

```yaml
runtime_monitor:
  symbol: NQmain
  interval: 5m
  mode: paper_only
  llm_review:
    enabled: true
    trigger_class: strong_review
    max_reviews_per_session: 20
    require_structured_output: true
  signal_thresholds:
    weak_notice: 0.35
    medium_watch: 0.60
    strong_review: 0.78
  event_handling:
    high_impact_release_window_action: block
    high_impact_post_event_requires_confirmation: true
  data_guards:
    max_snapshot_delay_seconds: 30
    block_on_stale_data: true
    block_on_incomplete_bar: true
  execution:
    allow_real_orders: false
    allow_paper_orders: true
```

## 17. 不做事项

v1 不做：

- 真实实盘自动下单。
- 通用新闻理解。
- 社交媒体情绪交易。
- LLM 自由生成交易代码。
- 每 5 分钟无条件调用 LLM。
- 未经回测的临盘策略执行。
- 用运行期成功样例覆盖样本外验证。
- 用多空辩论替代成本、滑点和成交验证。

## 18. 推荐默认方案

推荐把本方案作为主系统的第三层：

1. `local-llm-nq-strategy-system.md`：策略发现和验证主链路。
2. `macro-event-aware-nq-strategy-optimization.md`：事件上下文和事件风险控制。
3. `runtime-market-monitor-optimization.md`：运行期 5m 监控、弱提醒、强信号 LLM 复核和 paper/replay 观察。

这样可以保留 `docs/优化1.md` 中有价值的“多层验证、风险优先、结构化分析、强信号 deep review”思路，同时避免把实时分析报告误用为未经验证的交易策略。
