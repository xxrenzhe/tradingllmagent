# NQ 1m 扩展高边际策略搜索结论（2026-05-01）

## 结论

本轮按最新约束重跑：

- 不使用低边际信号凑交易数；组合只能从单信号低 R 回放后仍为正边际的候选里构建。
- 完整年度 2019-2025 实际交易数必须全部 >1000。
- 2026 未完成，只用年化交易数检查频率。
- 并发不预设单仓，回测扫描 `1/2/3/6/12/24/99`。
- 新增联网启发的 ORB、VWAP reclaim/bounce、高量 impulse、吸收反转、前日高低点突破/拒绝等实现。

输出文件：`experiments/profit_mining/expanded_high_edge_strategy_search_2019_2026.json`

脚本：`scripts/search_expanded_high_edge_strategy.py`

## 最强稳定候选

如果按“2019-2026 每年净利为正 + 净利润最大”定义稳定，当前最强候选是：

- 策略标签：`positive_expanded_edge_top_32`
- 净利润：**$5,804,050.00 / 1 NQ 合约**
- 交易数：**46,851**
- 完整年度最低实际交易数：**5,498**
- 2026 年化交易数：**8,004.45**
- Profit factor：**1.270**
- 最大回撤：**$480,126.25**
- 净利/最大回撤：**12.09**
- 平均持仓：**81.35 分钟**
- 中位持仓：**98 分钟**
- 回测选择并发：**99**
- edge 数：**32**

年度结果：

| 年份 | 交易数 | 净利润 | PF | 最大回撤 |
|---:|---:|---:|---:|---:|
| 2019 | 5,498 | $89,935.00 | 1.088 | $172,061.25 |
| 2020 | 6,134 | $718,960.00 | 1.289 | $315,828.75 |
| 2021 | 6,150 | $825,376.25 | 1.352 | $185,562.50 |
| 2022 | 7,210 | $1,570,143.75 | 1.414 | $290,920.00 |
| 2023 | 6,410 | $730,503.75 | 1.276 | $310,460.00 |
| 2024 | 6,563 | $882,780.00 | 1.271 | $259,670.00 |
| 2025 | 6,693 | $968,727.50 | 1.221 | $339,425.00 |
| 2026 | 2,193 | $17,623.75 | 1.011 | $475,640.00 |

策略构成：

- `opening_range_breakout`: 13 条
- `prior_day_breakout`: 12 条
- `vwap_pullback_bounce`: 2 条
- `vwap_reclaim_continuation`: 1 条
- `range_expansion_continuation`: 1 条
- `selling_absorption_reversal`: 1 条
- `session_extreme_reversion`: 1 条
- `trend_pullback_reclaim`: 1 条

关键参数：

- `max_concurrent_positions`: 99
- `max_hold_minutes`: 120
- `stop_range_multiple`: 6.0
- `min_stop_points`: 8.0
- `max_stop_points`: 90.0
- `flatten_on_date_change`: true

## 更稳的风险调整版本

如果不想接受 99 并发，当前更合理的风险调整版本是同类 `robust_expanded_positive_edge_top_32`，但限制 `max_concurrent_positions=24`：

- 净利润：**$5,371,751.25**
- 交易数：**33,238**
- Profit factor：**1.292**
- 最大回撤：**$397,986.25**
- 净利/最大回撤：**13.50**
- 最差年度净利：**$72,637.50**
- 平均持仓：**131.67 分钟**

这比 99 并发少赚约 **7.45%**，但最大回撤低约 **17.1%**，最差年度利润更厚，净利/回撤更高。若目标是“稳定优先但仍追求高收益”，我更倾向把 24 并发作为默认实盘候选，把 99 并发作为理论上限。

## 最高净利候选

如果只要求完整年度 2019-2025 为正，不强制 2026 YTD 为正，最高净利候选是：

- 策略标签：`positive_expanded_edge_top_32`
- 净利润：**$8,947,040.00**
- 交易数：**50,004**
- Profit factor：**1.328**
- 最大回撤：**$1,021,192.50**
- 平均持仓：**181.44 分钟**
- 回测选择并发：**99**
- 2019-2025 全部盈利，2026 YTD：**-$375,277.50**

我不建议把它作为稳定版首选。原因不是收益不够，而是 300 分钟持仓和 99 并发把 2026 当前 regime 的亏损暴露出来了。

## 联网实现来源

本轮新增实现参考了这些方向：

- ORB/开盘区间突破：Finance Research Letters 的 ORB 研究支持把 opening range 作为日内动量/扩张信号来源。链接：https://www.sciencedirect.com/science/article/pii/S1544612312000438
- 日内动量：Gao、Han、Li、Zhou 的 intraday momentum 研究支持开盘后走势与日内后续延续之间的关系。链接：https://www.sciencedirect.com/science/article/abs/pii/S0304405X18301351
- VWAP：Boyd 的 VWAP optimal execution 论文支持 VWAP 作为成交量加权的日内基准；本轮用它做 reclaim/bounce 信号，不把它单独当万能 alpha。链接：https://web.stanford.edu/~boyd/papers/vwap_opt_exec.html
- Order flow / absorption：Cont、Kukanov、Stoikov 的 OFI 研究说明短周期价格变化更接近订单流不平衡；本地只有 OHLCV/tick_count，所以实现成高量弱跟随后的 absorption proxy。链接：https://arxiv.org/abs/1011.6402

## 判断

旧的 `$1.03M` 高边际低 R 策略已经被扩展搜索支配。当前结论是：

- 绝对盈利上限：`$8.947M`，但 2026 YTD 为负，不作为稳定首选。
- 最强全年度为正：`$5.804M`，99 并发，平均持仓 81 分钟。
- 更稳默认候选：`$5.372M`，24 并发，回撤和最差年度更好。

下一步优化不应该继续加低边际信号，而应该做两件事：

- 对 2026 的亏损/薄利候选做 edge-level attribution，剔除当前 regime 失效的 ORB/prior-day 子组。
- 对 24 并发版本做 walk-forward 和分年度参数冻结，确认不是 2019-2025 后验过拟合。
