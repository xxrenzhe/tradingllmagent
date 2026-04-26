import { startTransition, useDeferredValue, useEffect, useState } from "react";
import { apiRequest, apiUrl, formatCompact, formatNumber } from "./api.js";

const DEFAULT_FORMS = {
  symbol: "NQmain",
  dateFrom: "2025-03-19",
  dateTo: "2025-03-19",
  timeframe: "1m",
  spec: "strategies/example_opening_range_breakout.yaml",
  experimentId: "nq_research_local",
  maxTrials: 3,
  executionMode: "bar",
  indicatorWarmupDays: "",
  llmModel: "local-deterministic-template",
  llmParameters: "{\"temperature\":0}"
};

function today() {
  return new Date().toISOString().slice(0, 10);
}

export default function App() {
  const [apiBase, setApiBase] = useState(() => localStorage.getItem("tlm-api-base") || "");
  const [forms, setForms] = useState(() => ({ ...DEFAULT_FORMS, dateTo: today() }));
  const [tasks, setTasks] = useState([]);
  const [symbols, setSymbols] = useState({});
  const [quality, setQuality] = useState(null);
  const [leaderboard, setLeaderboard] = useState({ leaderboard: [], rejected: [], rows: [], summary: {} });
  const [selectedTaskId, setSelectedTaskId] = useState("");
  const [taskEvents, setTaskEvents] = useState([]);
  const [experimentId, setExperimentId] = useState("");
  const [experiment, setExperiment] = useState(null);
  const [auditLogs, setAuditLogs] = useState([]);
  const [notice, setNotice] = useState({ tone: "neutral", text: "Connected UI shell. Start FastAPI on port 8000." });
  const [isPending, setIsPending] = useState(false);
  const [filters, setFilters] = useState({ minSharpe: "2", onlyPassed: false });
  const deferredFilter = useDeferredValue(filters);

  useEffect(() => {
    localStorage.setItem("tlm-api-base", apiBase);
  }, [apiBase]);

  useEffect(() => {
    let ignore = false;
    async function loadStaticData() {
      try {
        const [symbolPayload, reportPayload] = await Promise.all([
          apiRequest(apiBase, "/api/data/symbols"),
          apiRequest(apiBase, "/api/reports/leaderboard")
        ]);
        if (ignore) {
          return;
        }
        setSymbols(symbolPayload.symbols ?? {});
        setLeaderboard(reportPayload);
      } catch (error) {
        if (!ignore) {
          setNotice({ tone: "warn", text: error.message });
        }
      }
    }
    loadStaticData();
    return () => {
      ignore = true;
    };
  }, [apiBase]);

  useEffect(() => {
    let active = true;
    async function loadTasks() {
      try {
        const payload = await apiRequest(apiBase, "/api/tasks?limit=50");
        if (active) {
          setTasks(payload.tasks ?? []);
        }
      } catch (error) {
        if (active) {
          setNotice({ tone: "warn", text: error.message });
        }
      }
    }
    loadTasks();
    const interval = window.setInterval(loadTasks, 2500);
    return () => {
      active = false;
      window.clearInterval(interval);
    };
  }, [apiBase]);

  useEffect(() => {
    if (!selectedTaskId) {
      setTaskEvents([]);
      return undefined;
    }
    const source = new EventSource(apiUrl(apiBase, `/api/tasks/${selectedTaskId}/events`));
    setTaskEvents([]);
    source.addEventListener("task", (event) => {
      setTaskEvents((current) => [...current.slice(-80), { type: "task", payload: JSON.parse(event.data) }]);
    });
    source.addEventListener("log", (event) => {
      setTaskEvents((current) => [...current.slice(-80), { type: "log", payload: JSON.parse(event.data) }]);
    });
    source.onerror = () => source.close();
    return () => source.close();
  }, [apiBase, selectedTaskId]);

  function updateForm(key, value) {
    setForms((current) => ({ ...current, [key]: value }));
  }

  async function runAction(label, fn) {
    setIsPending(true);
    try {
      const payload = await fn();
      startTransition(() => {
        if (payload.task_id) {
          setSelectedTaskId(payload.task_id);
        }
      });
      setNotice({ tone: "success", text: `${label}: ${payload.task_id || "completed"}` });
    } catch (error) {
      setNotice({ tone: "danger", text: error.message });
    } finally {
      setIsPending(false);
    }
  }

  async function createTask(path, payload) {
    return apiRequest(apiBase, path, {
      method: "POST",
      body: JSON.stringify(payload)
    });
  }

  async function refreshQuality() {
    const params = new URLSearchParams({
      symbol: forms.symbol,
      date_from: forms.dateFrom,
      date_to: forms.dateTo
    });
    const payload = await apiRequest(apiBase, `/api/data/quality?${params.toString()}`);
    setQuality(payload);
    return { task_id: "quality refreshed" };
  }

  async function refreshLeaderboard() {
    const payload = await apiRequest(apiBase, "/api/reports/leaderboard");
    setLeaderboard(payload);
    return { task_id: "leaderboard refreshed" };
  }

  async function loadExperiment() {
    const id = experimentId.trim();
    if (!id) {
      throw new Error("experiment_id is required");
    }
    const [payload, auditPayload] = await Promise.all([
      apiRequest(apiBase, `/api/experiments/${encodeURIComponent(id)}`),
      apiRequest(apiBase, `/api/experiments/${encodeURIComponent(id)}/audit-logs?limit=50`)
    ]);
    setExperiment(payload);
    setAuditLogs(auditPayload.audit_logs ?? []);
    return { task_id: id };
  }

  const visibleRows = (deferredFilter.onlyPassed ? leaderboard.leaderboard : leaderboard.rows).filter((row) => {
    const minSharpe = Number(deferredFilter.minSharpe || 0);
    return (row.sharpe_test ?? -Infinity) >= minSharpe || !row.passed;
  });

  const runningTasks = tasks.filter((task) => ["queued", "running"].includes(task.status)).length;
  const failedTasks = tasks.filter((task) => task.status === "failed").length;
  const passedCount = leaderboard.summary?.passed ?? 0;
  const rejectedCount = leaderboard.summary?.rejected ?? 0;

  return (
    <main className="app-shell">
      <section className="hero-panel">
        <div>
          <p className="eyebrow">Local NQ strategy lab</p>
          <h1>Research console for LLM-generated strategies with strict out-of-sample gates.</h1>
          <p className="hero-copy">
            Data jobs, rolling validation results, rejected strategies, audit context, and paper replay stay local.
            This console deliberately avoids live brokerage controls.
          </p>
        </div>
        <div className="connection-card" aria-label="API connection">
          <label htmlFor="api-base">FastAPI base URL</label>
          <input
            id="api-base"
            value={apiBase}
            onChange={(event) => setApiBase(event.target.value)}
            placeholder="empty = same origin, or http://127.0.0.1:8000"
          />
          <p className={`notice ${notice.tone}`}>{notice.text}</p>
        </div>
      </section>

      <section className="metric-grid" aria-label="System summary">
        <Metric label="Tracked Symbols" value={Object.keys(symbols).length} detail={Object.keys(symbols).join(", ") || "No symbols loaded"} />
        <Metric label="Active Tasks" value={runningTasks} detail={`${tasks.length} recent tasks`} />
        <Metric label="Passed" value={passedCount} detail="Hard-gated leaderboard" />
        <Metric label="Rejected" value={rejectedCount} detail={`${failedTasks} task failures`} />
      </section>

      <section className="workbench-grid">
        <Panel title="Run Controls" kicker="Create queued jobs">
          <div className="form-grid">
            <TextField label="Symbol" value={forms.symbol} onChange={(value) => updateForm("symbol", value)} />
            <TextField label="From" type="date" value={forms.dateFrom} onChange={(value) => updateForm("dateFrom", value)} />
            <TextField label="To" type="date" value={forms.dateTo} onChange={(value) => updateForm("dateTo", value)} />
            <TextField label="Timeframe" value={forms.timeframe} onChange={(value) => updateForm("timeframe", value)} />
            <TextField label="Strategy Spec(s)" className="wide" value={forms.spec} onChange={(value) => updateForm("spec", value)} />
            <TextField label="Experiment ID" value={forms.experimentId} onChange={(value) => updateForm("experimentId", value)} />
            <TextField label="Max Trials" type="number" value={forms.maxTrials} onChange={(value) => updateForm("maxTrials", value)} />
            <TextField label="Warmup Days" type="number" value={forms.indicatorWarmupDays} onChange={(value) => updateForm("indicatorWarmupDays", value)} />
            <TextField label="LLM Model" value={forms.llmModel} onChange={(value) => updateForm("llmModel", value)} />
            <TextField label="LLM Parameters" value={forms.llmParameters} onChange={(value) => updateForm("llmParameters", value)} />
            <label className="field">
              <span>Execution Mode</span>
              <select value={forms.executionMode} onChange={(event) => updateForm("executionMode", event.target.value)}>
                <option value="bar">bar</option>
                <option value="tick">tick</option>
              </select>
            </label>
          </div>
          <div className="button-row">
            <ActionButton disabled={isPending} onClick={() => runAction("Download queued", () => createTask("/api/data/download", dataPayload(forms)))}>
              Download Tick
            </ActionButton>
            <ActionButton disabled={isPending} onClick={() => runAction("Build bars queued", () => createTask("/api/data/build-bars", dataPayload(forms)))}>
              Build Bars
            </ActionButton>
            <ActionButton disabled={isPending} onClick={() => runAction("Research queued", () => createTask("/api/experiments/research-runs", researchPayload(forms)))}>
              Run Research
            </ActionButton>
            <ActionButton disabled={isPending} onClick={() => runAction("Tick backtest queued", () => createTask("/api/backtests/tick", backtestPayload(forms)))}>
              Tick Backtest
            </ActionButton>
            <ActionButton variant="secondary" disabled={isPending} onClick={() => runAction("Quality", refreshQuality)}>
              Refresh Quality
            </ActionButton>
          </div>
        </Panel>

        <Panel title="Data Quality" kicker="Coverage and anomalies">
          {quality ? (
            <div className="quality-grid">
              <Metric label="Rows" value={formatCompact(quality.rows)} detail="normalized ticks" />
              <Metric label="Files" value={quality.files ?? 0} detail="existing parquet partitions" />
              <Metric label="Start" value={quality.start_timestamp ? quality.start_timestamp.slice(0, 10) : "-"} detail={quality.start_timestamp || "no rows"} />
              <Metric label="End" value={quality.end_timestamp ? quality.end_timestamp.slice(0, 10) : "-"} detail={quality.end_timestamp || "no rows"} />
              <Metric label="Missing" value={quality.missing_files?.length ?? 0} detail={(quality.missing_files ?? []).slice(0, 1).join(" | ") || "none"} />
              <Metric label="Empty" value={quality.zero_row_files?.length ?? 0} detail={(quality.zero_row_files ?? []).slice(0, 1).join(" | ") || "none"} />
              <Metric label="Duplicates" value={quality.duplicate_timestamps} detail="same timestamp rows" />
              <Metric label="Avg Spread" value={formatNumber(quality.avg_spread)} detail="mean bid/ask gap" />
              <Metric label="Max Spread" value={formatNumber(quality.max_spread)} detail="proxy CFD spread" />
              <Metric label="Large Spread" value={quality.large_spread_rows ?? 0} detail="above configured threshold" />
              <Metric label="Negative Spread" value={quality.negative_spread_rows} detail="invalid bid/ask rows" />
              <Metric label="Price Jumps" value={quality.price_jump_rows ?? 0} detail="large adjacent mid moves" />
            </div>
          ) : (
            <EmptyState title="No quality report loaded" text="Run Refresh Quality after building local tick parquet." />
          )}
        </Panel>
      </section>

      <section className="workbench-grid">
        <Panel title="Experiment Queue" kicker="Task status and SSE logs">
          <TaskTable
            tasks={tasks}
            selectedTaskId={selectedTaskId}
            onSelect={setSelectedTaskId}
            onCancel={(taskId) => runAction("Task cancel", () => apiRequest(apiBase, `/api/tasks/${taskId}/cancel`, { method: "POST" }))}
            onRun={(taskId) => runAction("Task run", () => apiRequest(apiBase, `/api/tasks/${taskId}/run`, { method: "POST" }))}
          />
        </Panel>
        <Panel title="Live Task Feed" kicker={selectedTaskId || "Select a task"}>
          {taskEvents.length ? (
            <div className="event-feed">
              {taskEvents.map((event, index) => (
                <pre key={`${event.type}-${index}`} className={event.type}>{JSON.stringify(event.payload, null, 2)}</pre>
              ))}
            </div>
          ) : (
            <EmptyState title="No task selected" text="Select a queued or completed task to stream status and logs." />
          )}
        </Panel>
      </section>

      <Panel title="Leaderboard" kicker="Out-of-sample only">
        <p className={`notice ${leaderboard.conclusion === "qualified_strategies_found" ? "success" : "warn"}`}>
          {leaderboard.message || "No leaderboard report loaded."}
        </p>
        <div className="toolbar">
          <label className="inline-control">
            <span>Min test Sharpe</span>
            <input value={filters.minSharpe} onChange={(event) => setFilters((current) => ({ ...current, minSharpe: event.target.value }))} />
          </label>
          <label className="checkbox-control">
            <input
              type="checkbox"
              checked={filters.onlyPassed}
              onChange={(event) => setFilters((current) => ({ ...current, onlyPassed: event.target.checked }))}
            />
            Passed only
          </label>
          <ActionButton variant="secondary" disabled={isPending} onClick={() => runAction("Leaderboard", refreshLeaderboard)}>
            Refresh
          </ActionButton>
        </div>
        <LeaderboardTable rows={visibleRows} />
      </Panel>

      <section className="workbench-grid">
        <Panel title="Experiment Detail" kicker="SQLite summary">
          <div className="inline-form">
            <input value={experimentId} onChange={(event) => setExperimentId(event.target.value)} placeholder="experiment_id" />
            <ActionButton variant="secondary" disabled={isPending} onClick={() => runAction("Experiment", loadExperiment)}>
              Load
            </ActionButton>
          </div>
          {experiment ? <JsonBlock payload={experiment} /> : <EmptyState title="No experiment loaded" text="Use an experiment id from a research task result." />}
        </Panel>
        <Panel title="LLM Audit" kicker="Prompt and tool trace">
          {auditLogs.length ? (
            <div className="event-feed">
              {auditLogs.map((entry) => (
                <pre key={entry.id} className="log">{JSON.stringify(entry, null, 2)}</pre>
              ))}
            </div>
          ) : (
            <EmptyState title="No audit logs loaded" text="Load an experiment to inspect prompt hashes, response hashes, and trial audit events." />
          )}
        </Panel>
      </section>

      <Panel title="Paper Replay Boundary" kicker="No live brokerage controls">
        <p className="body-copy">
          Paper replay and NinjaTrader export are API-only safety actions. The UI shows the boundary instead of placing real orders:
          use `/api/paper/replay` for simulated fills and `/api/paper/nt-export-signal` for offline CSV/OIF content.
        </p>
        <div className="safety-strip">
          <span>Local replay</span>
          <span>Offline export</span>
          <span>No Alpaca live account</span>
          <span>No NinjaTrader live bridge</span>
        </div>
      </Panel>
    </main>
  );
}

function backtestPayload(forms) {
  return {
    symbol: forms.symbol,
    spec: forms.spec,
    date_from: forms.dateFrom,
    date_to: forms.dateTo
  };
}

function dataPayload(forms) {
  return {
    symbol: forms.symbol,
    date_from: forms.dateFrom,
    date_to: forms.dateTo,
    timeframe: forms.timeframe,
    granularity: "tick"
  };
}

function researchPayload(forms) {
  const specs = forms.spec
    .split(/[,\n]/)
    .map((item) => item.trim())
    .filter(Boolean);
  const payload = {
    symbol: forms.symbol,
    date_from: forms.dateFrom,
    date_to: forms.dateTo,
    experiment_id: forms.experimentId,
    max_trials: Number(forms.maxTrials || 1),
    max_trials_per_family: Number(forms.maxTrials || 1),
    execution_mode: forms.executionMode,
    indicator_warmup_days: optionalNumber(forms.indicatorWarmupDays),
    llm_model: forms.llmModel,
    llm_parameters: parseJsonObject(forms.llmParameters, "LLM Parameters")
  };
  if (specs.length > 1) {
    payload.specs = specs;
  } else {
    payload.spec = specs[0] || forms.spec;
  }
  return payload;
}

function optionalNumber(value) {
  return value === "" || value === null || value === undefined ? null : Number(value);
}

function parseJsonObject(value, label) {
  const trimmed = value.trim();
  if (!trimmed) {
    return {};
  }
  const payload = JSON.parse(trimmed);
  if (!payload || Array.isArray(payload) || typeof payload !== "object") {
    throw new Error(`${label} must be a JSON object`);
  }
  return payload;
}

function Panel({ title, kicker, children }) {
  return (
    <section className="panel">
      <div className="panel-header">
        <div>
          <p className="kicker">{kicker}</p>
          <h2>{title}</h2>
        </div>
      </div>
      {children}
    </section>
  );
}

function Metric({ label, value, detail }) {
  return (
    <article className="metric-card">
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </article>
  );
}

function TextField({ label, value, onChange, type = "text", className = "" }) {
  return (
    <label className={`field ${className}`}>
      <span>{label}</span>
      <input type={type} value={value} onChange={(event) => onChange(event.target.value)} />
    </label>
  );
}

function ActionButton({ children, onClick, disabled, variant = "primary" }) {
  return (
    <button className={`action-button ${variant}`} type="button" onClick={onClick} disabled={disabled}>
      {children}
    </button>
  );
}

function TaskTable({ tasks, selectedTaskId, onSelect, onCancel, onRun }) {
  if (!tasks.length) {
    return <EmptyState title="No tasks yet" text="Create a data or research task to populate the queue." />;
  }
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Task</th>
            <th>Type</th>
            <th>Status</th>
            <th>Updated</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {tasks.map((task) => (
            <tr key={task.task_id} className={selectedTaskId === task.task_id ? "selected" : ""}>
              <td>
                <button className="link-button" type="button" onClick={() => onSelect(task.task_id)}>
                  {task.task_id}
                </button>
              </td>
              <td>{task.task_type}</td>
              <td>
                <span className={`status-pill ${task.status}`}>{task.status}</span>
              </td>
              <td>{task.updated_at}</td>
              <td className="table-actions">
                <button type="button" onClick={() => onRun(task.task_id)} disabled={["completed", "failed", "cancelled"].includes(task.status)}>
                  Run
                </button>
                <button type="button" onClick={() => onCancel(task.task_id)} disabled={["completed", "failed", "cancelled"].includes(task.status)}>
                  Cancel
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function LeaderboardTable({ rows }) {
  if (!rows.length) {
    return <EmptyState title="No leaderboard rows" text="Run research after data and bars exist, then refresh the report." />;
  }
  return (
    <div className="table-wrap leaderboard-table">
      <table>
        <thead>
          <tr>
            <th>Experiment</th>
            <th>Strategy</th>
            <th>Gate</th>
            <th>Overfit Risk</th>
            <th>Trials/Folds</th>
            <th>Score</th>
            <th>Test Sharpe</th>
            <th>Annual Trades</th>
            <th>Test PnL</th>
            <th>Holdout PnL</th>
            <th>Cost Stress</th>
            <th>Param Stability</th>
            <th>Snapshot</th>
            <th>Reject Reasons</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const overfitting = row.overfitting_report ?? {};
            const costSensitivity = row.cost_sensitivity_report ?? {};
            const parameterStability = row.parameter_stability_report ?? {};
            const costStressKnown = typeof costSensitivity.worst_case_survives === "boolean";
            return (
              <tr key={row.experiment_id}>
                <td>{row.experiment_id}</td>
                <td>{row.strategy_name}</td>
                <td>
                  <span className={`status-pill ${row.passed ? "completed" : "failed"}`}>{row.passed ? "passed" : "rejected"}</span>
                </td>
                <td>
                  <span className={`status-pill ${riskStatusClass(overfitting.risk_level)}`}>{overfitting.risk_level ?? "unknown"}</span>
                  <div className="table-detail">{(overfitting.reasons ?? []).slice(0, 2).join(", ") || "no risk flags"}</div>
                </td>
                <td>
                  {formatCompact(overfitting.trial_count ?? row.trial_count)} / {formatCompact(overfitting.fold_count)}
                  <div className="table-detail">{overfitting.pbo_status ?? "pbo unknown"}</div>
                </td>
                <td>{formatNumber(row.robustness_score, 3)}</td>
                <td>{formatNumber(row.sharpe_test)}</td>
                <td>{formatCompact(row.annual_trades_test)}</td>
                <td>{formatNumber(row.net_pnl_test)}</td>
                <td>{formatNumber(row.net_pnl_holdout)}</td>
                <td>
                  <span className={`status-pill ${costStressKnown ? (costSensitivity.worst_case_survives ? "completed" : "failed") : "running"}`}>
                    {costStressKnown ? (costSensitivity.worst_case_survives ? "survives" : "fails") : "unknown"}
                  </span>
                  <div className="table-detail">base rt {formatNumber(costSensitivity.baseline_round_trip_cost)}</div>
                </td>
                <td>
                  <span className={`status-pill ${parameterStabilityStatusClass(parameterStability.status)}`}>
                    {parameterStability.status ?? "unknown"}
                  </span>
                  <div className="table-detail">
                    {formatNumber(parameterStability.positive_neighbor_ratio, 2)} stable neighbors
                  </div>
                </td>
                <td>
                  {shortHash(row.data_version_hash)}
                  <div className="table-detail">{row.snapshot?.llm_model ?? "no llm model"}</div>
                </td>
                <td>{(row.reasons ?? []).join(", ") || "-"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function riskStatusClass(level) {
  if (level === "low") {
    return "completed";
  }
  if (level === "medium") {
    return "running";
  }
  return "failed";
}

function parameterStabilityStatusClass(status) {
  if (status === "stable" || status === "insufficient_variants") {
    return "completed";
  }
  if (status === "fragile") {
    return "failed";
  }
  return "running";
}

function shortHash(value) {
  return value ? String(value).slice(0, 10) : "-";
}

function JsonBlock({ payload }) {
  return <pre className="json-block">{JSON.stringify(payload, null, 2)}</pre>;
}

function EmptyState({ title, text }) {
  return (
    <div className="empty-state">
      <strong>{title}</strong>
      <span>{text}</span>
    </div>
  );
}
