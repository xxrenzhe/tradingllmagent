# LLM 触发门控与目标频率策略池优化方案

> 协调说明：本文晚于 `current-trading-system-optimization-roadmap.md` 生成。本文是“触发式 LLM 门控、目标交易频率策略池、token 成本控制、记忆回灌和前测迭代”的专项权威文档；若与更早文档在该主题上冲突，以本文为准。本文不授予 live execution 权限，所有执行仍受 `nt8-live-integration-roadmap.md` 和 readiness gates 约束。

## 1. 背景

交易者分享的实跑结果给出了一条明确优化方向：LLM 不应该按行情事件或每根 bar 高频调用，而应该只在“已验证策略信号出现后”作为二级门控介入。

已观察到的关键事实：

- 48h 回放、15m 步长、2 条主锚策略时，事件 364 次，但只触发 LLM 2 次。
- 首轮 LLM 调用 2 次，token 合计约 10,888，最新门控结果为两条均 `block/high`。
- 记忆已回灌到最新：`memory_evidence_records.csv` 约 785 条，`memory_decisions.csv` 约 399 条。
- 最近 90 天统计下，6 条策略组合可达到约 2.29 次/天，代理胜率约 58%，高于 53% 目标。
- 48h 最新 6 策略池实跑触发 5 次，约 2.5 次/天，token 合计约 27,002。

这些结果说明系统优化重点不是“让 LLM 更频繁判断行情”，而是建立可复用的目标频率策略池和触发式门控链路。

## 2. 目标

核心目标：

- 将候选策略池控制在每天 2-3 次可交易信号附近。
- 只保留最近样本代理胜率不低于 53% 的策略或策略组合。
- LLM 只在策略信号已出现、确定性风险门控已通过后调用。
- 每次 LLM 调用必须有输入证据、输出决策、token 成本和最终结果回灌。
- 前测持续产出新报告，用于修正策略池、门控阈值和风险条件。

非目标：

- 不让 LLM 直接扫描所有事件并自由寻找交易机会。
- 不让未通过研究验证的策略进入 runtime 或 execution intent。
- 不用当前 90 天样本结果替代长期样本外验证。
- 不绕过 paper shadow、sim、micro-live 和 controlled-live 阶段。

## 3. 设计原则

- 先用确定性策略产生信号，再让 LLM 做二级复核。
- 先满足频率和胜率目标，再考虑扩大策略池。
- 先记录完整 evidence 和 decision，再谈自动化执行。
- 先做禁用 LLM 的频率验证，再做开启 LLM 的成本验证。
- 先前测持续校准，再将策略池进入 paper shadow。
- 所有频率、胜率和 token 统计都必须落地到 artifact，不能只存在对话上下文。

## 4. 当前已落地能力

当前代码层已具备的直接支撑：

- `module_performance.jsonl` 可记录模块级研究表现。
- module summary 已能聚合模块表现、通过率、样本数和失败原因。
- `target_frequency_pool` 已加入模块摘要，用于筛选满足目标频率和代理胜率的候选池。
- 新研究产物会写入 `annual_trades`、`trades_per_day`、`win_rate`、`proxy_win_rate`。
- CLI `modules summary` 已支持目标频率参数：
  - `--target-min-per-day`
  - `--target-max-per-day`
  - `--min-proxy-win-rate`
  - `--lookback-days`
  - `--include-rejected`
- API `/api/modules/memory` 已返回 `target_frequency_pool`，可供 WebUI 或运行期服务读取。

## 5. 目标架构

推荐链路：

```text
strategy research artifacts
  -> module performance memory
  -> target frequency pool selection
  -> runtime strategy signal detector
  -> deterministic risk pre-gates
  -> LLM trigger gate
  -> paper intent or block decision
  -> evidence/decision/result memory backfill
  -> rolling forward-test report
```

分层职责：

- Research Layer：生成可验证策略、回测结果、胜率、频率和模块记忆。
- Pool Selection Layer：按目标频率和代理胜率自动选择策略池。
- Signal Layer：只检测策略自身信号，不让 LLM 扫描行情寻找机会。
- Risk Pre-Gate Layer：处理事件窗口、spread、数据 stale、冷却时间、日内风险预算。
- LLM Gate Layer：对已触发信号进行结构化 allow/block/observe 判断。
- Memory Layer：保存 evidence、decision、token usage、outcome 和下一轮阈值建议。
- Report Layer：持续输出 48h、7d、30d、90d 前测报告。

## 6. 策略池选择

### 6.1 输入字段

每条候选策略至少需要：

- `strategy_name`
- `strategy_spec_hash`
- `module_id`
- `timeframe`
- `passed`
- `trades_per_day` 或 `annual_trades`
- `proxy_win_rate`
- `expectancy`
- `robustness_score`
- `rejection_reasons`

如果缺少 `trades_per_day`，可从 `annual_trades / 365` 推导。如果缺少 `annual_trades`，可在指定 `lookback_days` 下用 `trade_count / lookback_days` 作为降级估算。

### 6.2 筛选规则

默认筛选规则：

- 只选 `passed=true` 的策略。
- `proxy_win_rate >= 0.53`。
- `trades_per_day > 0`。
- 总触发频率目标为 `2.0 <= pool_trades_per_day <= 3.0`。
- 优先级排序为：代理胜率、robustness、expectancy、频率。
- 同类高相关信号应后续加入去重或相关性惩罚。

当前已实现的 `target_frequency_pool` 是贪心选择：按质量排序后逐个纳入，只要不超过目标上限；如果低于目标下限，则补入一个最接近目标的候选。

### 6.3 输出字段

策略池报告至少输出：

- `status`: `target_met`、`below_target`、`above_target`
- `target_min_per_day`
- `target_max_per_day`
- `min_proxy_win_rate`
- `lookback_days`
- `candidate_count`
- `selected_count`
- `selected_trades_per_day`
- `weighted_proxy_win_rate`
- `selected`
- `rejected`

该报告是运行期策略池配置的输入，不是自动实盘许可。

## 7. 触发式 LLM 门控

### 7.1 触发条件

LLM 调用必须同时满足：

- 策略属于当前 `target_frequency_pool.selected`。
- 策略本身产生了明确 entry signal。
- 当前不在强阻断事件窗口。
- market data 未 stale。
- spread、slippage、波动、冷却时间和日内风险预算通过。
- 同一策略没有处于未完成 intent 或冷却状态。

### 7.2 LLM 输入

LLM prompt 只能包含结构化证据：

- strategy card 摘要。
- module memory 摘要。
- 当前 signal features。
- 当前 market snapshot。
- event context。
- risk pre-gate 结果。
- 最近同策略 decision/outcome 摘要。
- 当前 token budget 和调用原因。

禁止输入：

- final holdout 细节。
- 未结构化新闻臆测。
- 未验证策略的自由交易想法。
- 可直接下单的自由文本命令。

### 7.3 LLM 输出

LLM 输出必须是结构化 JSON：

```json
{
  "decision": "allow | block | observe",
  "risk_level": "low | medium | high",
  "confidence": 0.0,
  "reasons": [],
  "invalidation": [],
  "required_follow_up": [],
  "token_budget_note": ""
}
```

运行期只接受枚举动作：

- `allow`: 进入 paper intent 或 paper shadow。
- `block`: 记录阻断，不生成 intent。
- `observe`: 记录观察，不生成可执行 intent。

LLM 不允许输出 live gateway command。

## 8. 记忆回灌

### 8.1 Evidence 记录

每次触发前写入 evidence：

- `evidence_id`
- `strategy_spec_hash`
- `module_id`
- `snapshot_hash`
- `signal_time`
- `signal_features`
- `market_snapshot`
- `event_context`
- `risk_pre_gate`
- `pool_version`
- `trigger_reason`

### 8.2 Decision 记录

每次 LLM 后写入 decision：

- `decision_id`
- `evidence_id`
- `model`
- `prompt_hash`
- `response_hash`
- `decision`
- `risk_level`
- `confidence`
- `reasons`
- `input_tokens`
- `output_tokens`
- `total_tokens`
- `created_at`

### 8.3 Outcome 记录

每个 allow 或 block 后续都应补 outcome：

- `decision_id`
- `paper_fill_id`
- `outcome_window`
- `mfe`
- `mae`
- `net_pnl`
- `would_have_hit_target`
- `would_have_hit_stop`
- `final_label`
- `notes`

blocked 决策也需要 outcome，用于判断 LLM 是否过度保守。

## 9. Token 成本治理

成本控制目标：

- 每天 LLM 调用接近策略信号次数，而不是行情事件次数。
- 默认日均调用约 2-3 次。
- 每次调用记录 input/output/total token。
- 48h、7d、30d 报告统计 token 与 allow/block/observe 比例。
- 当日 token 超预算时，LLM gate 降级为 deterministic block 或 observe。

建议新增字段：

- `daily_token_budget`
- `remaining_token_budget`
- `trigger_count_today`
- `llm_call_count_today`
- `token_per_decision`
- `cost_per_allowed_intent`

## 10. 前测与防拟合

90 天统计只能作为阶段性依据，必须继续做前测。

前测报告应固定输出：

- `lookback_days`
- `forward_days`
- `selected_strategy_pool`
- `trigger_per_day`
- `proxy_win_rate`
- `actual_paper_win_rate`
- `allow_rate`
- `block_rate`
- `observe_rate`
- `token_total`
- `token_per_trigger`
- `decision_outcome_confusion`
- `strategy_pool_changes`

验收口径：

- 频率连续多个窗口维持 2-3 次/天附近。
- 代理胜率和 paper outcome 不明显背离。
- LLM block 的机会成本可量化。
- 策略池调整必须有报告支持，不能手工凭感觉替换。

## 11. 风险条件

LLM 门控前必须保留 deterministic hard blocks：

- 高影响事件 release window。
- 数据 stale。
- spread 超限。
- slippage stress 超限。
- 日内亏损或交易次数超限。
- strategy/module retired。
- strategy hash 不在当前池。
- paper shadow drift 超限。
- gateway/readiness 未通过。

这些条件不需要 LLM 判断。LLM 只处理通过 deterministic gates 后仍需要语义复核的边界情况。

## 12. WebUI 与报告

WebUI 应新增或扩展模块：

- Target Frequency Pool：展示当前选池、目标频率、加权胜率和 rejected 原因。
- Trigger Gate Timeline：展示每次 signal -> pre-gate -> LLM decision -> outcome。
- Token Budget：展示 48h、7d、30d token 用量和单次成本。
- Forward Test Report：展示阶段性触发频率、胜率和策略池变化。
- Decision Memory：按策略、decision、risk_level、outcome 查询。

报告 artifact 应存入实验或运行目录，不能只在控制台输出。

## 13. 实施阶段

### Phase 1：模块记忆补齐

已完成或进行中：

- 在 module memory 写入 `trades_per_day` 和 `proxy_win_rate`。
- 增加 `target_frequency_pool` 摘要。
- CLI/API 暴露目标频率选池结果。

验收：

- `modules summary` 可直接输出候选池。
- 空记忆、缺字段、低胜率、rejected 策略都有稳定输出。

### Phase 2：离线触发模拟

新增触发模拟器或 task：

- 输入 selected pool。
- 回放 48h、7d、30d、90d。
- 禁用 LLM 时只验证触发频率。
- 开启 LLM 时记录 token 和 allow/block。

验收：

- 可复现实跑中的 `trigger_per_day`、`llm_call_count`、token 统计。
- artifact 包含 manifest、evidence、decision 和 summary。

### Phase 3：运行期集成

将 selected pool 接入 runtime monitor：

- monitor 只监听池内策略信号。
- deterministic risk pre-gates 先执行。
- strong signal 才调用 LLM gate。
- 输出 paper intent 或 block/observe。

验收：

- 非池内策略不会触发 LLM。
- 普通行情事件不会触发 LLM。
- 每次 LLM 调用都有 evidence 和 decision。

### Phase 4：前测自动迭代

建立定期前测：

- 每日生成 48h 和 7d 报告。
- 每周生成 30d 和 90d 报告。
- 自动标记低胜率、低频率、过高 token 成本策略。
- 给出下一轮 pool 建议，但不自动实盘启用。

验收：

- 策略池变更有审计记录。
- 新策略加入前必须通过禁用 LLM 的频率验证。
- 前测报告能解释本轮参数变化。

## 14. 验收标准

完整方案落地后应满足：

- 最近 90 天禁用 LLM 回放显示 pool 频率在 2-3 次/天附近。
- pool 加权代理胜率不低于 53%。
- 48h 开启 LLM 回放中，LLM 调用次数约等于策略触发次数，明显低于行情事件数。
- 每次 LLM 调用都有 token usage、prompt hash、response hash 和结构化 decision。
- memory evidence、decision、outcome 可持续增长并可用于下一轮 pool 修正。
- WebUI/API 可查看当前 pool、触发历史、token 成本和前测报告。
- 所有 allow 只进入 paper shadow 或 paper intent，不产生 live gateway command。

## 15. 推荐命令

查看当前模块记忆与目标池：

```bash
PYTHONPATH=src python3 -c 'from tlm.cli import main; raise SystemExit(main(["modules", "summary", "--experiments-root", "experiments"]))'
```

按 2-3 次/天和 53% 胜率筛选：

```bash
PYTHONPATH=src python3 -c 'from tlm.cli import main; raise SystemExit(main(["modules", "summary", "--target-min-per-day", "2", "--target-max-per-day", "3", "--min-proxy-win-rate", "0.53"]))'
```

扩大前测观察窗口：

```bash
PYTHONPATH=src python3 -c 'from tlm.cli import main; raise SystemExit(main(["modules", "summary", "--lookback-days", "90"]))'
```

## 16. 后续问题

需要继续解决的问题：

- 策略池中高相关信号如何去重。
- `proxy_win_rate` 与真实 paper outcome 背离时如何降权。
- LLM block 的机会成本如何纳入下一轮阈值。
- 低频高胜率策略与高频中胜率策略如何组合。
- 事件窗口策略是否允许单独策略池。
- token 超预算时是否允许 deterministic allow，还是统一 observe/block。

这些问题应通过前测 artifact 和 memory outcome 解决，不应通过手工固定参数解决。
