# WebUI UX Redesign Notes

## Research References

- Freqtrade FreqUI: responsive bot monitoring and interaction surface for local trading operations.
  https://github.com/freqtrade/freqtrade/blob/develop/docs/freq-ui.md
- QuantConnect LEAN: open-source backtesting and live-trading engine with professional algorithm workflow concepts.
  https://github.com/QuantConnect/Lean
- BacktestJS: browser-visible backtesting results and CLI/UI driven workflow.
  https://backtestjs.com/
- HedgeVision discussion: React dashboard patterns for live PnL, position tracking, equity curve, Sharpe, Calmar, drawdown, win rate, and trade logs.
  https://www.reddit.com/r/algotrading/comments/1sad7ik/opensourced_my_stat_arb_dashboard_hedgevision/
- Vercel Web Interface Guidelines: focus states, semantic controls, tabular numbers, reduced motion, dense content handling, and dark-mode theming.
  https://raw.githubusercontent.com/vercel-labs/web-interface-guidelines/main/command.md

## Design Direction

This console should read as an operations workbench, not a marketing page. The primary user is evaluating strategy candidates and execution readiness, so the visual system prioritizes:

- Dense status visibility: API state, queue state, gate state, task failures, and strategy counts are available in the first viewport.
- Low decoration: no hero illustration, no decorative gradients, no oversized editorial type, and no ornamental cards.
- Trading semantics: green/teal for passed and active edges, amber for warnings/running work, red for failed gates, blue for neutral inspection and selection.
- Numeric stability: monospaced tabular figures for metrics, tables, charts, and replay values.
- Fast scanning: compact panels, sticky table headers, tighter controls, sharper radii, and consistent section anchors.
- Local safety: paper replay, NT export, readiness, gateway state, and trigger gate remain visibly separated from research controls.

## Implemented System

- App shell: sticky operations header with workspace anchors and compact API connection state.
- Layout: dense dashboard grids, two-column workbench sections, responsive single-column mobile fallback.
- Components: unified panel, metric card, status pill, table, chart card, empty state, action button, and safety strip styles.
- Interaction: visible focus rings, hover feedback, `touch-action: manipulation`, reduced-motion handling, and scroll-margin for anchor navigation.
- Data display: tabular numbers, tighter table rows, sticky headers, high-contrast empty/error states, and overflow handling for long experiment IDs and logs.

## Known Runtime Constraint

The Vite dev server proxies `/api` to `http://127.0.0.1:8000`. If the FastAPI process is not running, the UI correctly renders but browser console/resource logs show proxy `500` errors from refused API connections.
