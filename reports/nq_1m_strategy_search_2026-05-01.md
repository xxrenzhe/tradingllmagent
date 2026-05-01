# NQ 1m 策略搜索结论（2026-05-01）

## 结论

如果把“每年盈利”和“短持仓”作为硬约束，我不建议采用纯最高净利组合。全历史 17/17 年盈利的最高净利组合约为 **$210,415/NQ 合约**，但平均持仓 **106.1 分钟**，不符合“持仓时间最短”的目标。

综合收益、持仓、回撤、规则复杂度后，推荐候选是：

- **策略文件**：`experiments/profit_mining/best_nq_1m_strategy_search_h15_e12.json`
- **净利**：$104,575
- **年份**：2010-2026 全部为正（2010、2026 为数据内半年度/部分年度）
- **交易数**：2,802
- **平均持仓**：10.46 分钟
- **Profit factor**：1.309
- **最大回撤**：$11,680
- **净利/回撤**：8.95
- **最弱年份**：2011，+$980

## 推荐策略定义

执行规则：

- 每 1 分钟收盘后评估信号，下一根 1m open 入场。
- 固定时间退出，持仓只允许 5/10/15 分钟。
- 同一时间多信号时按组合顺序取第一条；已有持仓未退出时跳过新信号。
- 成本按 NQ 每回合 $15 计入，近似包含手续费和双边各 1 tick 滑点。

12 条入场边：

| # | family | side | hold | session NY | DOW | trend | vol | range |
|---|---|---:|---:|---|---:|---:|---:|---:|
| 1 | failed_breakout_reversion | long | 10m | 12:00-15:59 | Fri | -1 | 1 | -1 |
| 2 | failed_breakout_reversion | long | 15m | 06:00-09:29 | Fri | -1 | 1 | 1 |
| 3 | failed_breakout_reversion | long | 15m | 09:30-11:59 | Tue | -1 | 0 | -1 |
| 4 | failed_breakout_reversion | long | 15m | 09:30-11:59 | Tue | -1 | 2 | 1 |
| 5 | failed_breakout_reversion | long | 5m | 06:00-09:29 | Fri | -1 | 3 | 1 |
| 6 | range_expansion_continuation | short | 15m | 09:30-11:59 | Thu | 1 | 3 | 2 |
| 7 | range_expansion_continuation | long | 5m | 09:30-11:59 | Mon | 1 | 3 | 3 |
| 8 | session_extreme_reversion | long | 15m | 09:30-11:59 | Tue | -1 | 1 | 0 |
| 9 | trend_pullback_reclaim | long | 15m | 00:00-05:59 | Mon | 1 | 3 | 3 |
| 10 | trend_pullback_reclaim | short | 15m | 06:00-09:29 | Fri | -1 | 3 | 1 |
| 11 | trend_pullback_reclaim | short | 15m | 09:30-11:59 | Thu | -1 | 1 | 1 |
| 12 | trend_pullback_reclaim | short | 15m | 09:30-11:59 | Fri | -1 | 0 | -1 |

Feature bins：

- `trend=1/-1`：close 高于/低于 50-bar MA。
- `vol=-1/0/1/2/3`：当前 tick_count 相对 50-bar 均量的低/正常/高/极高分桶。
- `range=-1/0/1/2/3`：当前 high-low 相对 20-bar 平均 range 的低/正常/高/极高分桶。

## 候选对比

| 约束 | 文件 | 净利 | 平均持仓 | 边数 | PF | 最大回撤 | 最弱年 |
|---|---|---:|---:|---:|---:|---:|---:|
| 最高净利、全胜 | `best_nq_1m_strategy_search.json` | $210,415 | 106.13m | 10 | 1.236 | $26,165 | $805 |
| <=60m | `best_nq_1m_strategy_search_h60.json` | $153,165 | 52.47m | 12 | 1.178 | $22,715 | $750 |
| <=30m | `best_nq_1m_strategy_search_h30.json` | $141,675 | 28.77m | 14 | 1.195 | $18,060 | $110 |
| <=30m, <=8 edges | `best_nq_1m_strategy_search_h30_e8.json` | $95,075 | 22.48m | 8 | 1.262 | $12,030 | $10 |
| <=15m, <=12 edges | `best_nq_1m_strategy_search_h15_e12.json` | $104,575 | 10.46m | 12 | 1.309 | $11,680 | $980 |
| <=10m | `best_nq_1m_strategy_search_h10.json` | $124,545 | 8.62m | 18 | 1.348 | $11,420 | $160 |
| 5m only | `best_nq_1m_strategy_search_h5.json` | $84,525 | 5.00m | 20 | 1.234 | $11,185 | $85 |

## 外部研究参考

- ORB：[Zarattini/Aziz 的 SSRN 研究](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4416622)显示 opening range breakout 可作为系统化日内候选，但本地 NQ 搜索中单一 ORB 不能满足每年盈利，需要和失败突破、趋势回撤组合。
- Intraday momentum：[Gao/Han/Li/Zhou 的研究](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2440866)发现早盘收益可预测尾盘收益，支持日内动量/时段条件作为候选，但本地最优结果不是纯尾盘动量。
- VWAP：[SIAM 的 VWAP 执行研究](https://epubs.siam.org/doi/10.1137/16M1058406)和 [Stanford/Busseti-Boyd VWAP optimal execution](https://web.stanford.edu/~boyd/papers/pdf/vwap_opt_exec.pdf)支持 VWAP 作为常用日内成交量基准；本地搜索中 VWAP reclaim 是有效补充，但不是单独足够稳定的策略。

## 风险判断

该结果是全历史数据内搜索，不等于未来保证。真正上线前应做三件事：

- 用 2024-2026 作为完全留出或滚动 walk-forward 重新选择规则。
- 用 tick/盘口回放替换 1m OHLC 回放，确认 next-open 可成交和滑点。
- 做规则压缩：优先保留 `failed_breakout_reversion`、`range_expansion_continuation`、`trend_pullback_reclaim` 三类，减少 weekday/bin 参数数量。
