import { startTransition, useDeferredValue, useEffect, useMemo, useState } from "react";
import { API_PATHS, apiRequest, apiUrl, formatCompact, formatNumber, formatPercent } from "./api.js";

const DEFAULT_FORMS = {
  symbol: "NQmain",
  dateFrom: "2025-03-19",
  dateTo: "2025-03-19",
  timeframe: "1m",
  spec: "strategies/example_opening_range_breakout.yaml",
  experimentId: "nq_research_local",
  maxTrials: 3,
  maxRounds: 10,
  trialsPerRound: 1,
  targetCount: 1,
  maxSeedStrategies: 6,
  minAnnualTrades: 1000,
  minSharpe: 2,
  minWinProbability: 0.53,
  targetSeedMode: "auto",
  strategiesRoot: "strategies",
  executionMode: "bar",
  indicatorWarmupDays: "",
  llmModel: "local-deterministic-template",
  llmParameters: "{\"temperature\":0}"
};

const DEFAULT_HISTORY_FILTERS = {
  query: "",
  status: "all",
  executionMode: "all",
  moduleId: "all",
  minSharpe: "2",
  minAnnualTrades: "1000",
  minWinProbability: "0.53",
  minNetPnl: "",
  sortBy: "sharpe"
};

const COMPARISON_COLORS = ["#0b6f5b", "#1d4f73", "#9d5b12", "#a9362f", "#5b5f97", "#006d77", "#7f4f24", "#5a3e85"];

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
  const [selectedArtifactId, setSelectedArtifactId] = useState("");
  const [artifactDetail, setArtifactDetail] = useState(null);
  const [auditLogs, setAuditLogs] = useState([]);
  const [paperForm, setPaperForm] = useState({
    strategyId: "",
    account: "Sim101",
    instrument: "NQ 06-26",
    format: "csv"
  });
  const [paperReplay, setPaperReplay] = useState(null);
  const [ntExport, setNtExport] = useState(null);
  const [monitorForm, setMonitorForm] = useState({
    symbol: "NQmain",
    date: today(),
    timeframe: "5m",
    calendar: "configs/macro_events.yaml"
  });
  const [monitorReport, setMonitorReport] = useState(null);
  const [readinessForm, setReadinessForm] = useState({
    stage: "paper_shadow",
    evidence: "{\"trading_days\":0,\"replay_consistent\":false,\"max_allowed_drift\":1}"
  });
  const [readinessDecision, setReadinessDecision] = useState(null);
  const [externalValidation, setExternalValidation] = useState(null);
  const [gatewayOverview, setGatewayOverview] = useState(null);
  const [approvalQueue, setApprovalQueue] = useState(null);
  const [costCalibration, setCostCalibration] = useState(null);
  const [costSampleJson, setCostSampleJson] = useState("[]");
  const [moduleMemory, setModuleMemory] = useState(null);
  const [triggerGateForm, setTriggerGateForm] = useState({
    dateFrom: "2026-04-25",
    dateTo: "2026-04-26",
    outputDir: "experiments/trigger_gate/latest",
    enableLlm: true,
    dailyTokenBudget: "30000",
    memoryStrategyHash: "",
    memoryDecision: ""
  });
  const [triggerGateSimulation, setTriggerGateSimulation] = useState(null);
  const [triggerGateReport, setTriggerGateReport] = useState(null);
  const [triggerGateSchedule, setTriggerGateSchedule] = useState(null);
  const [triggerGateMemory, setTriggerGateMemory] = useState(null);
  const [notice, setNotice] = useState({ tone: "neutral", text: "Connected UI shell. Start FastAPI on port 8000." });
  const [isPending, setIsPending] = useState(false);
  const [historyFilters, setHistoryFilters] = useState(DEFAULT_HISTORY_FILTERS);
  const [selectedStrategyIds, setSelectedStrategyIds] = useState([]);
  const [comparisonArtifacts, setComparisonArtifacts] = useState({});
  const [comparisonLoading, setComparisonLoading] = useState(false);
  const deferredHistoryFilters = useDeferredValue(historyFilters);

  useEffect(() => {
    localStorage.setItem("tlm-api-base", apiBase);
  }, [apiBase]);

  useEffect(() => {
    let ignore = false;
    async function loadStaticData() {
      try {
        const [symbolPayload, reportPayload] = await Promise.all([
          apiRequest(apiBase, API_PATHS.dataSymbols),
          apiRequest(apiBase, API_PATHS.reportsLeaderboard)
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
        const payload = await apiRequest(apiBase, API_PATHS.tasks(50));
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
    const source = new EventSource(apiUrl(apiBase, API_PATHS.taskEvents(selectedTaskId)));
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

  function updatePaperForm(key, value) {
    setPaperForm((current) => ({ ...current, [key]: value }));
  }

  function updateMonitorForm(key, value) {
    setMonitorForm((current) => ({ ...current, [key]: value }));
  }

  function updateReadinessForm(key, value) {
    setReadinessForm((current) => ({ ...current, [key]: value }));
  }

  function updateTriggerGateForm(key, value) {
    setTriggerGateForm((current) => ({ ...current, [key]: value }));
  }

  async function runAction(label, fn) {
    setIsPending(true);
    try {
      const payload = await fn();
      startTransition(() => {
        if (payload.task_id && payload.task_type) {
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

  function selectedStrategyResultPath() {
    return paperForm.strategyId || (selectedArtifactId ? `experiments/${selectedArtifactId}/leaderboard.json` : "");
  }

  async function runPaperReplay() {
    const strategyId = selectedStrategyResultPath();
    if (!strategyId) {
      throw new Error("Select a leaderboard row or enter a strategy result path.");
    }
    const payload = await apiRequest(apiBase, API_PATHS.paperReplay, {
      method: "POST",
      body: JSON.stringify({ strategy_id: strategyId })
    });
    setPaperReplay(payload);
    return { task_id: "paper replay completed" };
  }

  async function runNtExport() {
    const strategyId = selectedStrategyResultPath();
    if (!strategyId) {
      throw new Error("Select a leaderboard row or enter a strategy result path.");
    }
    const payload = await apiRequest(apiBase, API_PATHS.paperNtExportSignal, {
      method: "POST",
      body: JSON.stringify({
        strategy_id: strategyId,
        format: paperForm.format,
        account: paperForm.account,
        instrument: paperForm.instrument
      })
    });
    setNtExport(payload);
    return { task_id: "nt export generated" };
  }

  async function runMonitorReport() {
    const payload = await apiRequest(apiBase, API_PATHS.monitorReport, {
      method: "POST",
      body: JSON.stringify(monitorForm)
    });
    setMonitorReport(payload);
    return { task_id: "monitor report loaded" };
  }

  async function evaluateReadiness() {
    const payload = await apiRequest(apiBase, API_PATHS.executionReadiness, {
      method: "POST",
      body: JSON.stringify({
        stage: readinessForm.stage,
        evidence: parseJsonObject(readinessForm.evidence, "Readiness Evidence")
      })
    });
    setReadinessDecision(payload);
    return { task_id: `readiness ${payload.decision}` };
  }

  async function evaluateExternalValidation() {
    const payload = await apiRequest(apiBase, API_PATHS.readinessExternalValidation, {
      method: "POST",
      body: JSON.stringify({
        stage: readinessForm.stage,
        evidence: parseJsonObject(readinessForm.evidence, "Readiness Evidence")
      })
    });
    setExternalValidation(payload);
    return { task_id: `external validation ${payload.decision?.decision ?? "checked"}` };
  }

  async function refreshGatewayOverview() {
    const [health, approval, reconciliation, incidents, orderUpdates] = await Promise.all([
      apiRequest(apiBase, API_PATHS.gatewayHealth),
      apiRequest(apiBase, API_PATHS.executionApprovalQueue),
      apiRequest(apiBase, API_PATHS.gatewayReconciliation),
      apiRequest(apiBase, API_PATHS.gatewayIncidents),
      apiRequest(apiBase, API_PATHS.gatewayOrderUpdates)
    ]);
    setGatewayOverview({ health, reconciliation, incidents, orderUpdates });
    setApprovalQueue(approval);
    return { task_id: "gateway overview refreshed" };
  }

  async function buildCostCalibration() {
    const samples = parseJsonArray(costSampleJson, "Cost Samples");
    const payload = await apiRequest(apiBase, API_PATHS.calibrationCosts, {
      method: "POST",
      body: JSON.stringify({
        cost_model: "nq_conservative_v1",
        samples,
        data_quality_report: quality ?? {},
        proxy_instrument: "USATECHIDXUSD",
        executable_instrument: "CME_NQ"
      })
    });
    setCostCalibration(payload);
    return { task_id: "cost calibration built" };
  }

  async function refreshModuleMemory() {
    const payload = await apiRequest(apiBase, API_PATHS.modulesMemory);
    setModuleMemory(payload);
    return { task_id: "module memory refreshed" };
  }

  async function runTriggerGateSimulation() {
    const payload = await apiRequest(apiBase, API_PATHS.triggerGateSimulations, {
      method: "POST",
      body: JSON.stringify({
        target_frequency_pool: moduleMemory?.target_frequency_pool,
        from: triggerGateForm.dateFrom,
        to: triggerGateForm.dateTo,
        output_dir: triggerGateForm.outputDir,
        enable_llm: triggerGateForm.enableLlm,
        daily_token_budget: optionalNumber(triggerGateForm.dailyTokenBudget)
      })
    });
    setTriggerGateSimulation(payload);
    return { task_id: "trigger gate simulation completed" };
  }

  async function loadTriggerGateReport() {
    const payload = await apiRequest(apiBase, API_PATHS.triggerGateReports, {
      method: "POST",
      body: JSON.stringify({ output_dir: triggerGateForm.outputDir })
    });
    setTriggerGateReport(payload);
    return { task_id: "trigger gate report loaded" };
  }

  async function buildTriggerGateSchedule() {
    const payload = await apiRequest(apiBase, API_PATHS.triggerGateSchedules, {
      method: "POST",
      body: JSON.stringify({
        target_frequency_pool: moduleMemory?.target_frequency_pool,
        as_of: triggerGateForm.dateTo,
        output_root: triggerGateForm.outputDir,
        enable_llm: triggerGateForm.enableLlm,
        daily_token_budget: optionalNumber(triggerGateForm.dailyTokenBudget)
      })
    });
    setTriggerGateSchedule(payload);
    return { task_id: "trigger gate schedule built" };
  }

  async function loadTriggerGateMemory() {
    const payload = await apiRequest(apiBase, API_PATHS.triggerGateMemory, {
      method: "POST",
      body: JSON.stringify({
        output_dir: triggerGateForm.outputDir,
        strategy_spec_hash: triggerGateForm.memoryStrategyHash || null,
        decision: triggerGateForm.memoryDecision || null,
        limit: 25
      })
    });
    setTriggerGateMemory(payload);
    return { task_id: "trigger gate memory loaded" };
  }

  async function refreshQuality() {
    const params = new URLSearchParams({
      symbol: forms.symbol,
      date_from: forms.dateFrom,
      date_to: forms.dateTo
    });
    const payload = await apiRequest(apiBase, API_PATHS.dataQuality(params.toString()));
    setQuality(payload);
    return { task_id: "quality refreshed" };
  }

  async function refreshLeaderboard() {
    const payload = await apiRequest(apiBase, API_PATHS.reportsLeaderboard);
    setLeaderboard(payload);
    return { task_id: "leaderboard refreshed" };
  }

  async function loadExperiment() {
    const id = experimentId.trim();
    if (!id) {
      throw new Error("experiment_id is required");
    }
    const [payload, auditPayload] = await Promise.all([
      apiRequest(apiBase, API_PATHS.experiment(id)),
      apiRequest(apiBase, API_PATHS.experimentAuditLogs(id))
    ]);
    setExperiment(payload);
    setAuditLogs(auditPayload.audit_logs ?? []);
    return { task_id: id };
  }

  async function loadResearchArtifacts(id) {
    const experimentKey = id.trim();
    if (!experimentKey) {
      throw new Error("experiment_id is required");
    }
    const payload = await apiRequest(
      apiBase,
      API_PATHS.experimentArtifacts(experimentKey)
    );
    setSelectedArtifactId(experimentKey);
    setArtifactDetail(payload);
    return { task_id: `artifacts loaded: ${experimentKey}` };
  }

  function updateHistoryFilter(key, value) {
    setHistoryFilters((current) => ({ ...current, [key]: value }));
  }

  function toggleStrategySelection(id) {
    setSelectedStrategyIds((current) => (
      current.includes(id) ? current.filter((strategyId) => strategyId !== id) : [...current, id]
    ));
  }

  function toggleVisibleStrategySelection() {
    const visibleIds = visibleRows.map((row) => row.experiment_id);
    const visibleSet = new Set(visibleIds);
    const selectedVisibleCount = selectedStrategyIds.filter((id) => visibleSet.has(id)).length;
    if (visibleIds.length && selectedVisibleCount === visibleIds.length) {
      setSelectedStrategyIds((current) => current.filter((id) => !visibleSet.has(id)));
      return;
    }
    setSelectedStrategyIds((current) => Array.from(new Set([...current, ...visibleIds])));
  }

  async function loadSelectedComparisonArtifacts() {
    if (!selectedRows.length) {
      throw new Error("Select at least one strategy to compare");
    }
    setComparisonLoading(true);
    setComparisonArtifacts((current) => {
      const next = { ...current };
      for (const row of selectedRows) {
        next[row.experiment_id] = { row, loading: true, detail: current[row.experiment_id]?.detail ?? null, error: "" };
      }
      return next;
    });
    const results = await Promise.all(selectedRows.map(async (row) => {
      try {
        const detail = await apiRequest(apiBase, API_PATHS.experimentArtifacts(row.experiment_id));
        return { row, detail, error: "" };
      } catch (error) {
        return { row, detail: null, error: error.message };
      }
    }));
    setComparisonArtifacts((current) => {
      const next = { ...current };
      for (const result of results) {
        next[result.row.experiment_id] = {
          row: result.row,
          detail: result.detail,
          loading: false,
          error: result.error
        };
      }
      return next;
    });
    setComparisonLoading(false);
    return { task_id: `comparison loaded: ${results.length} strategies` };
  }

  const historyOptions = useMemo(() => buildHistoryOptions(leaderboard.rows ?? []), [leaderboard.rows]);
  const visibleRows = useMemo(
    () => filterHistoryRows(leaderboard.rows ?? [], deferredHistoryFilters),
    [leaderboard.rows, deferredHistoryFilters]
  );
  const selectedIdSet = useMemo(() => new Set(selectedStrategyIds), [selectedStrategyIds]);
  const selectedRows = useMemo(() => {
    const rowsById = new Map((leaderboard.rows ?? []).map((row) => [row.experiment_id, row]));
    return selectedStrategyIds.map((id) => rowsById.get(id)).filter(Boolean);
  }, [leaderboard.rows, selectedStrategyIds]);
  const visibleSelectedCount = visibleRows.filter((row) => selectedIdSet.has(row.experiment_id)).length;
  const allVisibleSelected = visibleRows.length > 0 && visibleSelectedCount === visibleRows.length;
  const comparisonSeries = useMemo(
    () => buildComparisonSeries(selectedRows, comparisonArtifacts),
    [selectedRows, comparisonArtifacts]
  );
  const selectedLeaderboardRow = (leaderboard.rows ?? []).find((row) => row.experiment_id === selectedArtifactId);

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
            <TextField label="Max Rounds" type="number" value={forms.maxRounds} onChange={(value) => updateForm("maxRounds", value)} />
            <TextField label="Trials/Round" type="number" value={forms.trialsPerRound} onChange={(value) => updateForm("trialsPerRound", value)} />
            <TextField label="Target Count" type="number" value={forms.targetCount} onChange={(value) => updateForm("targetCount", value)} />
            <TextField label="Max Seeds" type="number" value={forms.maxSeedStrategies} onChange={(value) => updateForm("maxSeedStrategies", value)} />
            <TextField label="Min Annual Trades" type="number" value={forms.minAnnualTrades} onChange={(value) => updateForm("minAnnualTrades", value)} />
            <TextField label="Min Sharpe" type="number" value={forms.minSharpe} onChange={(value) => updateForm("minSharpe", value)} />
            <TextField label="Min Win Probability" type="number" value={forms.minWinProbability} onChange={(value) => updateForm("minWinProbability", value)} />
            <TextField label="Strategies Root" value={forms.strategiesRoot} onChange={(value) => updateForm("strategiesRoot", value)} />
            <TextField label="Warmup Days" type="number" value={forms.indicatorWarmupDays} onChange={(value) => updateForm("indicatorWarmupDays", value)} />
            <TextField label="LLM Model" value={forms.llmModel} onChange={(value) => updateForm("llmModel", value)} />
            <TextField label="LLM Parameters" value={forms.llmParameters} onChange={(value) => updateForm("llmParameters", value)} />
            <label className="field">
              <span>Seed Mode</span>
              <select value={forms.targetSeedMode} onChange={(event) => updateForm("targetSeedMode", event.target.value)}>
                <option value="auto">auto scan</option>
                <option value="specs">listed specs</option>
              </select>
            </label>
            <label className="field">
              <span>Execution Mode</span>
              <select value={forms.executionMode} onChange={(event) => updateForm("executionMode", event.target.value)}>
                <option value="bar">bar</option>
                <option value="tick">tick</option>
                <option value="bar_then_tick">bar then tick</option>
              </select>
            </label>
          </div>
          <div className="button-row">
            <ActionButton disabled={isPending} onClick={() => runAction("Download queued", () => createTask(API_PATHS.dataDownload, dataPayload(forms)))}>
              Download Tick
            </ActionButton>
            <ActionButton disabled={isPending} onClick={() => runAction("Build bars queued", () => createTask(API_PATHS.dataBuildBars, dataPayload(forms)))}>
              Build Bars
            </ActionButton>
            <ActionButton disabled={isPending} onClick={() => runAction("Research queued", () => createTask(API_PATHS.researchRuns, researchPayload(forms)))}>
              Run Research
            </ActionButton>
            <ActionButton disabled={isPending} onClick={() => runAction("Target discovery queued", () => createTask(API_PATHS.researchTargetDiscovery, targetDiscoveryPayload(forms)))}>
              Target Discovery
            </ActionButton>
            <ActionButton disabled={isPending} onClick={() => runAction("Proposal queued", () => createTask(API_PATHS.researchProposals, proposalPayload(forms)))}>
              Generate Proposal
            </ActionButton>
            <ActionButton disabled={isPending} onClick={() => runAction("Tick backtest queued", () => createTask(API_PATHS.backtestTick, backtestPayload(forms)))}>
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
              <Metric label="Coverage" value={formatPercent(quality.coverage_ratio)} detail={quality.status ?? "unknown"} />
              <Metric label="Files" value={`${quality.files ?? 0}/${quality.expected_files ?? "?"}`} detail="existing / expected partitions" />
              <Metric label="Start" value={quality.start_timestamp ? quality.start_timestamp.slice(0, 10) : "-"} detail={quality.start_timestamp || "no rows"} />
              <Metric label="End" value={quality.end_timestamp ? quality.end_timestamp.slice(0, 10) : "-"} detail={quality.end_timestamp || "no rows"} />
              <Metric label="Missing" value={quality.missing_files?.length ?? 0} detail={(quality.missing_files ?? []).slice(0, 1).join(" | ") || "none"} />
              <Metric label="Flags" value={quality.quality_flags?.length ?? 0} detail={(quality.quality_flags ?? []).join(", ") || "none"} />
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
            onCancel={(taskId) => runAction("Task cancel", () => apiRequest(apiBase, API_PATHS.taskCancel(taskId), { method: "POST" }))}
            onRun={(taskId) => runAction("Task run", () => apiRequest(apiBase, API_PATHS.taskRun(taskId), { method: "POST" }))}
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

      <Panel title="Historical Strategy Performance" kicker="Filter, select, compare">
        <p className={`notice ${leaderboard.conclusion === "qualified_strategies_found" ? "success" : "warn"}`}>
          {leaderboard.message || "No leaderboard report loaded."}
        </p>
        <div className="history-filter-grid">
          <TextField label="Search" value={historyFilters.query} onChange={(value) => updateHistoryFilter("query", value)} />
          <label className="field">
            <span>Gate Status</span>
            <select value={historyFilters.status} onChange={(event) => updateHistoryFilter("status", event.target.value)}>
              <option value="all">all</option>
              <option value="passed">passed</option>
              <option value="rejected">rejected</option>
            </select>
          </label>
          <label className="field">
            <span>Execution Mode</span>
            <select value={historyFilters.executionMode} onChange={(event) => updateHistoryFilter("executionMode", event.target.value)}>
              <option value="all">all</option>
              {historyOptions.executionModes.map((mode) => <option key={mode} value={mode}>{mode}</option>)}
            </select>
          </label>
          <label className="field">
            <span>Module</span>
            <select value={historyFilters.moduleId} onChange={(event) => updateHistoryFilter("moduleId", event.target.value)}>
              <option value="all">all</option>
              {historyOptions.moduleIds.map((moduleId) => <option key={moduleId} value={moduleId}>{moduleId}</option>)}
            </select>
          </label>
          <TextField label="Min Sharpe" type="number" value={historyFilters.minSharpe} onChange={(value) => updateHistoryFilter("minSharpe", value)} />
          <TextField label="Min Annual Trades" type="number" value={historyFilters.minAnnualTrades} onChange={(value) => updateHistoryFilter("minAnnualTrades", value)} />
          <TextField label="Min Win Probability" type="number" value={historyFilters.minWinProbability} onChange={(value) => updateHistoryFilter("minWinProbability", value)} />
          <TextField label="Min Test PnL" type="number" value={historyFilters.minNetPnl} onChange={(value) => updateHistoryFilter("minNetPnl", value)} />
          <label className="field">
            <span>Sort By</span>
            <select value={historyFilters.sortBy} onChange={(event) => updateHistoryFilter("sortBy", event.target.value)}>
              <option value="sharpe">test sharpe</option>
              <option value="annual_trades">annual trades</option>
              <option value="win_probability">win probability</option>
              <option value="net_pnl">test pnl</option>
              <option value="robustness">robustness</option>
            </select>
          </label>
        </div>
        <div className="history-summary-strip" aria-label="Filtered strategy summary">
          <Metric label="Visible" value={formatCompact(visibleRows.length)} detail={`${formatCompact(leaderboard.rows?.length ?? 0)} total rows`} />
          <Metric label="Selected" value={formatCompact(selectedRows.length)} detail="ready for comparison" />
          <Metric label="Median Sharpe" value={formatNumber(median(visibleRows.map((row) => row.sharpe_test)))} detail="filtered rows" />
          <Metric label="Max Trades" value={formatCompact(maxNumber(visibleRows.map((row) => row.annual_trades_test)))} detail="annualized test trades" />
        </div>
        <div className="toolbar history-toolbar">
          <ActionButton variant="secondary" disabled={isPending} onClick={() => runAction("Leaderboard", refreshLeaderboard)}>
            Refresh
          </ActionButton>
          <ActionButton
            disabled={isPending || comparisonLoading || !selectedRows.length}
            onClick={() => runAction("Comparison", loadSelectedComparisonArtifacts)}
          >
            Load Comparison
          </ActionButton>
          <ActionButton variant="secondary" disabled={!selectedRows.length} onClick={() => setSelectedStrategyIds([])}>
            Clear Selection
          </ActionButton>
        </div>
        <LeaderboardTable
          rows={visibleRows}
          selectedExperimentId={selectedArtifactId}
          selectedStrategyIds={selectedIdSet}
          allVisibleSelected={allVisibleSelected}
          onToggleSelect={toggleStrategySelection}
          onToggleAll={toggleVisibleStrategySelection}
          onInspect={(id) => runAction("Artifacts", () => loadResearchArtifacts(id))}
        />
      </Panel>

      <Panel title="Selected Strategy Comparison" kicker={`${selectedRows.length} selected`}>
        <ComparisonEquityChart series={comparisonSeries} loading={comparisonLoading} />
      </Panel>

      <Panel title="Strategy Cards" kicker="Promotion, holdout, next round">
        <StrategyCards
          rows={visibleRows.slice(0, 8)}
          selectedExperimentId={selectedArtifactId}
          onInspect={(id) => runAction("Artifacts", () => loadResearchArtifacts(id))}
        />
      </Panel>

      <Panel title="Strategy Replay Detail" kicker={selectedArtifactId || "Select a leaderboard row"}>
        {artifactDetail ? (
          <ArtifactDashboard detail={artifactDetail} row={selectedLeaderboardRow} />
        ) : (
          <EmptyState
            title="No strategy artifacts loaded"
            text="Inspect a leaderboard row to load trades, equity, fold metrics, and distribution charts."
          />
        )}
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
          Paper replay and NinjaTrader export stay offline. These controls call local simulation/export APIs only and never place live orders.
        </p>
        <div className="form-grid compact-form">
          <TextField
            label="Strategy Result Path"
            className="wide"
            value={paperForm.strategyId}
            onChange={(value) => updatePaperForm("strategyId", value)}
          />
          <TextField label="Account" value={paperForm.account} onChange={(value) => updatePaperForm("account", value)} />
          <TextField label="Instrument" value={paperForm.instrument} onChange={(value) => updatePaperForm("instrument", value)} />
          <label className="field">
            <span>Export Format</span>
            <select value={paperForm.format} onChange={(event) => updatePaperForm("format", event.target.value)}>
              <option value="csv">csv</option>
              <option value="oif">oif</option>
            </select>
          </label>
        </div>
        <div className="button-row">
          <ActionButton variant="secondary" disabled={isPending} onClick={() => runAction("Paper replay", runPaperReplay)}>
            Run Paper Replay
          </ActionButton>
          <ActionButton variant="secondary" disabled={isPending} onClick={() => runAction("NT export", runNtExport)}>
            Generate NT Export
          </ActionButton>
        </div>
        <div className="safety-strip">
          <span>Local replay</span>
          <span>Offline export</span>
          <span>No Alpaca live account</span>
          <span>No NinjaTrader live bridge</span>
        </div>
        {paperReplay ? <JsonBlock payload={paperReplay} /> : null}
        {ntExport ? <JsonBlock payload={ntExport} /> : null}
      </Panel>

      <section className="workbench-grid">
        <Panel title="Runtime Monitor" kicker="Snapshot, events, signal class">
          <div className="form-grid compact-form">
            <TextField label="Symbol" value={monitorForm.symbol} onChange={(value) => updateMonitorForm("symbol", value)} />
            <TextField label="Date" type="date" value={monitorForm.date} onChange={(value) => updateMonitorForm("date", value)} />
            <TextField label="Timeframe" value={monitorForm.timeframe} onChange={(value) => updateMonitorForm("timeframe", value)} />
            <TextField label="Event Calendar" className="wide" value={monitorForm.calendar} onChange={(value) => updateMonitorForm("calendar", value)} />
          </div>
          <div className="button-row">
            <ActionButton variant="secondary" disabled={isPending} onClick={() => runAction("Monitor", runMonitorReport)}>
              Load Monitor Report
            </ActionButton>
          </div>
          {monitorReport ? <RuntimeMonitorSummary report={monitorReport} /> : <EmptyState title="No runtime report" text="Load a monitor report from local bar data and event context." />}
        </Panel>

        <Panel title="Execution Readiness" kicker="Paper, sim, live gates">
          <div className="form-grid compact-form two-column-form">
            <label className="field">
              <span>Stage</span>
              <select value={readinessForm.stage} onChange={(event) => updateReadinessForm("stage", event.target.value)}>
                <option value="paper_shadow">paper shadow</option>
                <option value="nt8_sim">nt8 sim</option>
                <option value="micro_live">micro live</option>
                <option value="controlled_live">controlled live</option>
              </select>
            </label>
            <TextField
              label="Evidence JSON"
              className="wide"
              value={readinessForm.evidence}
              onChange={(value) => updateReadinessForm("evidence", value)}
            />
          </div>
          <div className="button-row">
            <ActionButton variant="secondary" disabled={isPending} onClick={() => runAction("Readiness", evaluateReadiness)}>
              Evaluate Readiness
            </ActionButton>
            <ActionButton variant="secondary" disabled={isPending} onClick={() => runAction("External validation", evaluateExternalValidation)}>
              Build Validation Artifact
            </ActionButton>
            <ActionButton variant="secondary" disabled={isPending} onClick={() => runAction("Gateway", refreshGatewayOverview)}>
              Refresh Gateway State
            </ActionButton>
          </div>
          {readinessDecision ? <ReadinessSummary decision={readinessDecision} /> : <EmptyState title="No readiness decision" text="Evaluate evidence before enabling any execution stage." />}
          {externalValidation ? <JsonBlock payload={externalValidation} /> : null}
          {gatewayOverview ? <GatewayReadinessSummary overview={gatewayOverview} approvalQueue={approvalQueue} /> : null}
        </Panel>
      </section>

      <Panel title="Cost Calibration" kicker="Proxy data and runtime drift">
        <div className="form-grid compact-form">
          <TextField
            label="Calibration Samples JSON"
            className="wide"
            value={costSampleJson}
            onChange={setCostSampleJson}
          />
        </div>
        <div className="button-row">
          <ActionButton variant="secondary" disabled={isPending} onClick={() => runAction("Cost calibration", buildCostCalibration)}>
            Build Cost Calibration
          </ActionButton>
        </div>
        {costCalibration ? <JsonBlock payload={costCalibration} /> : <EmptyState title="No cost calibration artifact" text="Paste spread/slippage samples from paper shadow, NT8 sim, or micro-live." />}
      </Panel>

      <Panel title="Module Memory" kicker="Promotion, retirement, retest">
        <div className="button-row">
          <ActionButton variant="secondary" disabled={isPending} onClick={() => runAction("Module memory", refreshModuleMemory)}>
            Refresh Module Memory
          </ActionButton>
        </div>
        {moduleMemory ? <ModuleMemorySummary summary={moduleMemory} /> : <EmptyState title="No module memory loaded" text="Refresh after research writes module performance records." />}
      </Panel>

      <Panel title="Trigger Gate" kicker="Target pool, token budget, forward test">
        <p className="body-copy">
          Trigger gate simulations use selected module-memory pool rows and write local evidence, decision, token, and forward-test artifacts. No live commands are emitted.
        </p>
        <div className="form-grid compact-form">
          <TextField label="From" value={triggerGateForm.dateFrom} onChange={(value) => updateTriggerGateForm("dateFrom", value)} />
          <TextField label="To" value={triggerGateForm.dateTo} onChange={(value) => updateTriggerGateForm("dateTo", value)} />
          <TextField
            label="Output Dir"
            className="wide"
            value={triggerGateForm.outputDir}
            onChange={(value) => updateTriggerGateForm("outputDir", value)}
          />
          <TextField
            label="Daily Token Budget"
            value={triggerGateForm.dailyTokenBudget}
            onChange={(value) => updateTriggerGateForm("dailyTokenBudget", value)}
          />
          <TextField
            label="Memory Strategy Hash"
            value={triggerGateForm.memoryStrategyHash}
            onChange={(value) => updateTriggerGateForm("memoryStrategyHash", value)}
          />
          <TextField
            label="Memory Decision"
            value={triggerGateForm.memoryDecision}
            onChange={(value) => updateTriggerGateForm("memoryDecision", value)}
          />
          <label className="field checkbox-field">
            <input
              type="checkbox"
              checked={triggerGateForm.enableLlm}
              onChange={(event) => updateTriggerGateForm("enableLlm", event.target.checked)}
            />
            <span>Enable LLM gate simulation</span>
          </label>
        </div>
        <div className="button-row">
          <ActionButton variant="secondary" disabled={isPending || !moduleMemory?.target_frequency_pool} onClick={() => runAction("Trigger gate simulation", runTriggerGateSimulation)}>
            Run Trigger Gate Simulation
          </ActionButton>
          <ActionButton variant="secondary" disabled={isPending} onClick={() => runAction("Trigger gate report", loadTriggerGateReport)}>
            Load Forward Report
          </ActionButton>
          <ActionButton variant="secondary" disabled={isPending || !moduleMemory?.target_frequency_pool} onClick={() => runAction("Trigger gate schedule", buildTriggerGateSchedule)}>
            Build 48h/7d/30d/90d Schedule
          </ActionButton>
          <ActionButton variant="secondary" disabled={isPending} onClick={() => runAction("Trigger gate memory", loadTriggerGateMemory)}>
            Load Decision Memory
          </ActionButton>
        </div>
        <TriggerGateSummary pool={moduleMemory?.target_frequency_pool} simulation={triggerGateSimulation} report={triggerGateReport} schedule={triggerGateSchedule} memory={triggerGateMemory} />
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

function targetDiscoveryPayload(forms) {
  const payload = {
    symbol: forms.symbol,
    timeframe: forms.timeframe,
    strategies_root: forms.strategiesRoot,
    date_from: forms.dateFrom,
    date_to: forms.dateTo,
    experiment_id: forms.experimentId,
    max_rounds: Number(forms.maxRounds || 1),
    trials_per_round: Number(forms.trialsPerRound || 1),
    target_count: Number(forms.targetCount || 1),
    max_seed_strategies: optionalNumber(forms.maxSeedStrategies),
    min_annual_trades: Number(forms.minAnnualTrades || 1000),
    min_sharpe: Number(forms.minSharpe || 2),
    min_win_probability: Number(forms.minWinProbability || 0.53),
    execution_mode: forms.executionMode,
    indicator_warmup_days: optionalNumber(forms.indicatorWarmupDays),
    llm_model: forms.llmModel,
    llm_parameters: parseJsonObject(forms.llmParameters, "LLM Parameters")
  };
  if (forms.targetSeedMode === "specs") {
    const specs = forms.spec
      .split(/[,\n]/)
      .map((item) => item.trim())
      .filter(Boolean);
    if (specs.length > 1) {
      payload.specs = specs;
    } else if (specs[0]) {
      payload.spec = specs[0];
    }
  }
  return payload;
}

function proposalPayload(forms) {
  const specs = forms.spec
    .split(/[,\n]/)
    .map((item) => item.trim())
    .filter(Boolean);
  return {
    spec: specs[0] || forms.spec,
    experiment_id: forms.experimentId ? `${forms.experimentId}_proposal` : "",
    model: forms.llmModel,
    llm_parameters: parseJsonObject(forms.llmParameters, "LLM Parameters")
  };
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

function parseJsonArray(value, label) {
  const payload = JSON.parse(value.trim() || "[]");
  if (!Array.isArray(payload)) {
    throw new Error(`${label} must be a JSON array`);
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

function buildHistoryOptions(rows) {
  return {
    executionModes: uniqueSorted(rows.map((row) => row.execution_mode).filter(Boolean)),
    moduleIds: uniqueSorted(rows.map((row) => row.module_id).filter(Boolean))
  };
}

function filterHistoryRows(rows, filters) {
  const query = filters.query.trim().toLowerCase();
  const minSharpe = parseOptionalNumber(filters.minSharpe);
  const minAnnualTrades = parseOptionalNumber(filters.minAnnualTrades);
  const minWinProbability = parseProbabilityFilter(filters.minWinProbability);
  const minNetPnl = parseOptionalNumber(filters.minNetPnl);
  const filtered = rows.filter((row) => {
    if (filters.status === "passed" && !row.passed) {
      return false;
    }
    if (filters.status === "rejected" && row.passed) {
      return false;
    }
    if (filters.executionMode !== "all" && row.execution_mode !== filters.executionMode) {
      return false;
    }
    if (filters.moduleId !== "all" && row.module_id !== filters.moduleId) {
      return false;
    }
    if (!passesMinimum(row.sharpe_test, minSharpe)) {
      return false;
    }
    if (!passesMinimum(row.annual_trades_test, minAnnualTrades)) {
      return false;
    }
    if (!passesMinimum(row.win_probability_test, minWinProbability)) {
      return false;
    }
    if (!passesMinimum(row.net_pnl_test, minNetPnl)) {
      return false;
    }
    if (!query) {
      return true;
    }
    const haystack = [
      row.experiment_id,
      row.strategy_name,
      row.module_id,
      row.execution_mode,
      row.strategy_card?.name,
      ...(row.reasons ?? [])
    ].join(" ").toLowerCase();
    return haystack.includes(query);
  });
  return filtered.sort((left, right) => sortHistoryRow(left, right, filters.sortBy));
}

function sortHistoryRow(left, right, sortBy) {
  const fields = {
    annual_trades: "annual_trades_test",
    net_pnl: "net_pnl_test",
    robustness: "robustness_score",
    sharpe: "sharpe_test",
    win_probability: "win_probability_test"
  };
  const field = fields[sortBy] ?? fields.sharpe;
  return numericValue(right[field]) - numericValue(left[field]);
}

function buildComparisonSeries(selectedRows, comparisonArtifacts) {
  return selectedRows.map((row, index) => {
    const artifact = comparisonArtifacts[row.experiment_id] ?? {};
    const points = (artifact.detail?.equity ?? [])
      .filter((entry) => typeof entry.equity === "number")
      .map((entry) => ({
        value: Number(entry.equity),
        label: entry.exit_time ?? entry.timestamp ?? entry.trade_index ?? ""
      }));
    const first = points.length ? points[0].value : 0;
    const last = points.length ? points[points.length - 1].value : 0;
    return {
      id: row.experiment_id,
      label: row.strategy_name || row.experiment_id,
      color: COMPARISON_COLORS[index % COMPARISON_COLORS.length],
      points,
      netChange: last - first,
      row,
      loading: artifact.loading,
      error: artifact.error
    };
  });
}

function uniqueSorted(values) {
  return Array.from(new Set(values)).sort((left, right) => String(left).localeCompare(String(right)));
}

function parseOptionalNumber(value) {
  if (value === "" || value === null || value === undefined) {
    return null;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function parseProbabilityFilter(value) {
  const parsed = parseOptionalNumber(value);
  if (parsed === null) {
    return null;
  }
  return parsed > 1 ? parsed / 100 : parsed;
}

function numericValue(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : Number.NEGATIVE_INFINITY;
}

function passesMinimum(value, minimum) {
  if (minimum === null) {
    return true;
  }
  return numericValue(value) >= minimum;
}

function median(values) {
  const numericValues = values.map(Number).filter(Number.isFinite).sort((left, right) => left - right);
  if (!numericValues.length) {
    return null;
  }
  const middle = Math.floor(numericValues.length / 2);
  return numericValues.length % 2 ? numericValues[middle] : (numericValues[middle - 1] + numericValues[middle]) / 2;
}

function maxNumber(values) {
  const numericValues = values.map(Number).filter(Number.isFinite);
  return numericValues.length ? Math.max(...numericValues) : null;
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

function LeaderboardTable({
  rows,
  selectedExperimentId,
  selectedStrategyIds,
  allVisibleSelected,
  onToggleSelect,
  onToggleAll,
  onInspect
}) {
  if (!rows.length) {
    return <EmptyState title="No leaderboard rows" text="Run research after data and bars exist, then refresh the report." />;
  }
  return (
    <div className="table-wrap leaderboard-table">
      <table>
        <thead>
          <tr>
            <th className="select-column">
              <input
                type="checkbox"
                aria-label="Select all visible strategies"
                checked={allVisibleSelected}
                onChange={onToggleAll}
              />
            </th>
            <th>Experiment</th>
            <th>Strategy</th>
            <th>Gate</th>
            <th>Mode</th>
            <th>Overfit Risk</th>
            <th>Trials/Folds</th>
            <th>Score</th>
            <th>Test Sharpe</th>
            <th>Annual Trades</th>
            <th>Win Prob</th>
            <th>Tick Replay</th>
            <th>Non-Overlap</th>
            <th>Trade Spread</th>
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
            const tradeDistribution = row.trade_count_distribution_report ?? {};
            const tickReplay = row.tick_replay_report ?? {};
            const costStressKnown = typeof costSensitivity.worst_case_survives === "boolean";
            const selectedForComparison = selectedStrategyIds.has(row.experiment_id);
            return (
              <tr
                key={row.experiment_id}
                className={`${selectedExperimentId === row.experiment_id ? "selected" : ""} ${selectedForComparison ? "compare-selected" : ""}`}
              >
                <td className="select-column">
                  <input
                    type="checkbox"
                    aria-label={`Select ${row.experiment_id} for comparison`}
                    checked={selectedForComparison}
                    onChange={() => onToggleSelect(row.experiment_id)}
                  />
                </td>
                <td>
                  <button className="link-button" type="button" onClick={() => onInspect(row.experiment_id)}>
                    {row.experiment_id}
                  </button>
                  <div className="table-detail">inspect artifacts</div>
                </td>
                <td>{row.strategy_name}</td>
                <td>
                  <span className={`status-pill ${row.passed ? "completed" : "failed"}`}>{row.passed ? "passed" : "rejected"}</span>
                </td>
                <td>
                  {row.execution_mode ?? "-"}
                  <div className="table-detail">{row.module_id ?? "module unknown"}</div>
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
                <td>{formatPercent(row.win_probability_test)}</td>
                <td>
                  <span className={`status-pill ${tickReplayStatusClass(tickReplay)}`}>
                    {tickReplay.status ?? "unknown"}
                  </span>
                  <div className="table-detail">{tickReplay.method ?? "method unknown"}</div>
                </td>
                <td>
                  {formatNumber(row.sharpe_non_overlap_test)}
                  <div className="table-detail">
                    {formatCompact((row.non_overlap_test_fold_indexes ?? []).length)} folds · {row.overlapping_test_folds ? "overlap flagged" : "no overlap"}
                  </div>
                </td>
                <td>
                  <span className={`status-pill ${tradeDistribution.status === "balanced" ? "completed" : "failed"}`}>
                    {tradeDistribution.status ?? "unknown"}
                  </span>
                  <div className="table-detail">
                    max fold {formatNumber(tradeDistribution.max_fold_trade_share, 2)}
                  </div>
                </td>
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

function RuntimeMonitorSummary({ report }) {
  const signal = report.signal ?? {};
  const eventContext = report.event_context ?? {};
  const snapshot = report.snapshot ?? {};
  return (
    <div className="readiness-grid">
      <Metric label="Signal" value={signal.bucket ?? "none"} detail={(signal.reasons ?? []).join(", ") || "no reasons"} />
      <Metric label="Last Price" value={formatNumber(snapshot.last_price)} detail={snapshot.snapshot_time ?? "no timestamp"} />
      <Metric label="Event State" value={eventContext.event_state ?? "normal"} detail={(eventContext.active_event_ids ?? []).join(", ") || "no active events"} />
      <Metric label="Key Levels" value={(report.key_levels ?? []).length} detail={(report.key_levels ?? []).slice(0, 2).map((level) => level.level).join(", ") || "none"} />
      <JsonBlock payload={report} />
    </div>
  );
}

function ReadinessSummary({ decision }) {
  const statusClass = decision.passed ? "completed" : "failed";
  return (
    <div className="readiness-grid">
      <div className="readiness-banner">
        <span className={`status-pill ${statusClass}`}>{decision.decision}</span>
        <strong>{decision.stage}</strong>
        <small>{(decision.reasons ?? []).join(", ") || "all gates passed"}</small>
      </div>
      <JsonBlock payload={decision} />
    </div>
  );
}

function GatewayReadinessSummary({ overview, approvalQueue }) {
  const health = overview.health ?? {};
  const reconciliation = overview.reconciliation ?? {};
  const incidents = overview.incidents ?? {};
  const orderUpdates = overview.orderUpdates ?? {};
  return (
    <div className="readiness-grid">
      <div className="strict-validation-grid">
        <Metric label="Gateway Mode" value={health.mode ?? "-"} detail={health.safe_mode ? "safe mode active" : "normal"} />
        <Metric label="Read Only" value={health.read_only ? "yes" : "no"} detail={`${health.order_count ?? 0} orders tracked`} />
        <Metric label="Approval Queue" value={approvalQueue?.pending_count ?? 0} detail="pending human approval" />
        <Metric label="Reconciliation" value={reconciliation.status ?? "-"} detail={`${(reconciliation.drift ?? []).length} drift rows`} />
        <Metric label="Incidents" value={incidents.count ?? 0} detail="gateway incident timeline" />
        <Metric label="Order Updates" value={orderUpdates.event_count ?? 0} detail="append-only gateway stream" />
      </div>
      <JsonBlock payload={{ overview, approvalQueue }} />
    </div>
  );
}

function ModuleMemorySummary({ summary }) {
  const modules = summary.modules ?? [];
  return (
    <div>
      <TriggerPoolSummary pool={summary.target_frequency_pool} />
      <div className="module-memory-grid">
        {modules.slice(0, 12).map((module) => (
          <article key={module.module_id} className="module-memory-card">
            <div className="strategy-card-topline">
              <span className={`status-pill ${moduleStatusClass(module.status)}`}>{module.status ?? "summary"}</span>
              <span>{formatPercent(module.pass_rate ?? 0)}</span>
            </div>
            <h3>{module.module_id}</h3>
            <p>{module.catalog?.description ?? "No catalog description."}</p>
            <div className="module-memory-stats">
              <Metric label="Records" value={formatCompact(module.evaluated_records)} detail={`${formatCompact(module.passed_records)} passed`} />
              <Metric label="Trades" value={formatCompact(module.total_trade_count)} detail={module.best_experiment_id ?? "no best experiment"} />
            </div>
            <div className="gate-list">
              {Object.entries(module.rejection_reasons ?? {}).slice(0, 4).map(([reason, count]) => (
                <span key={reason} className="failed">{reason}: {count}</span>
              ))}
            </div>
          </article>
        ))}
      </div>
    </div>
  );
}

function TriggerPoolSummary({ pool }) {
  if (!pool) {
    return null;
  }
  const diversity = pool.diversity_report ?? {};
  return (
    <div className="module-memory-stats">
      <Metric label="Pool Status" value={pool.status ?? "-"} detail={`${formatCompact(pool.selected_count)} selected`} />
      <Metric label="Signals / Day" value={formatNumber(pool.selected_trades_per_day ?? 0)} detail={`${formatNumber(pool.target_min_per_day ?? 0)}-${formatNumber(pool.target_max_per_day ?? 0)} target`} />
      <Metric label="Proxy Win" value={formatPercent(pool.weighted_proxy_win_rate ?? 0)} detail={`min ${formatPercent(pool.min_proxy_win_rate ?? 0)}`} />
      <Metric label="Correlation Groups" value={formatCompact(diversity.selected_correlation_group_count ?? 0)} detail={`${formatCompact(diversity.selected_near_duplicate_pair_count ?? 0)} selected near-dupes`} />
    </div>
  );
}

function TriggerGateSummary({ pool, simulation, report, schedule, memory }) {
  const drift = report?.proxy_outcome_drift;
  const opportunityCost = report?.block_opportunity_cost;
  const recommendations = report?.adaptive_recommendations?.recommendations ?? [];
  return (
    <div className="artifact-dashboard">
      {pool ? <TriggerPoolSummary pool={pool} /> : <EmptyState title="No target pool loaded" text="Refresh module memory before running a trigger gate simulation." />}
      {simulation ? (
        <div className="artifact-summary">
          <Metric label="Mode" value={simulation.mode ?? "-"} detail={simulation.simulation_id ? shortHash(simulation.simulation_id) : "no id"} />
          <Metric label="Triggers" value={formatCompact(simulation.trigger_count)} detail={`${formatNumber(simulation.trigger_per_day)} / day`} />
          <Metric label="LLM Calls" value={formatCompact(simulation.llm_call_count)} detail={`${formatCompact(simulation.token_budget?.token_total)} tokens`} />
        </div>
      ) : null}
      {report ? (
        <div className="artifact-summary">
          <Metric label="Allow" value={formatPercent(report.allow_rate ?? 0)} detail="decision rate" />
          <Metric label="Block" value={formatPercent(report.block_rate ?? 0)} detail="decision rate" />
          <Metric label="Observe" value={formatPercent(report.observe_rate ?? 0)} detail={`${formatCompact(report.token_total)} tokens`} />
          <Metric label="Live Commands" value={formatCompact(report.live_gateway_command_count ?? 0)} detail="must remain zero" />
          <Metric label="Proxy Drift" value={drift?.drift === null || drift?.drift === undefined ? "-" : formatPercent(drift.drift)} detail={drift?.status ?? "no drift report"} />
          <Metric label="Block Cost" value={formatNumber(opportunityCost?.opportunity_cost ?? 0)} detail={`${formatCompact(opportunityCost?.missed_winner_count ?? 0)} missed winners`} />
        </div>
      ) : null}
      {recommendations.length ? (
        <div className="gate-list">
          {recommendations.slice(0, 5).map((item) => (
            <span key={item.action} className={item.severity === "high" ? "failed" : "passed"}>{item.action}: {item.reason}</span>
          ))}
        </div>
      ) : null}
      {memory ? <TriggerGateMemoryTimeline memory={memory} /> : null}
      {schedule ? (
        <div className="gate-list">
          {(schedule.runs ?? []).map((run) => (
            <span key={run.label} className="passed">{run.label}: {run.payload?.from} to {run.payload?.to}</span>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function TriggerGateMemoryTimeline({ memory }) {
  const rows = memory.rows ?? [];
  return (
    <div className="table-wrap">
      <div className="section-heading">
        <h3>Decision Memory</h3>
        <span>{formatCompact(memory.row_count)} rows, {formatCompact(memory.outcome_count)} outcomes</span>
      </div>
      <table>
        <thead>
          <tr>
            <th>Signal</th>
            <th>Strategy</th>
            <th>Decision</th>
            <th>Risk</th>
            <th>Outcome</th>
            <th>Tokens</th>
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 12).map((row) => (
            <tr key={row.decision_id ?? row.evidence_id}>
              <td>{row.signal_time ?? "-"}</td>
              <td>{row.strategy_name ?? shortHash(row.strategy_spec_hash)}</td>
              <td>{row.decision ?? "pending"}</td>
              <td>{row.risk_level ?? "-"}</td>
              <td>{row.final_label ?? "pending"} {row.net_pnl === null || row.net_pnl === undefined ? "" : `(${formatNumber(row.net_pnl)})`}</td>
              <td>{formatCompact(row.total_tokens ?? 0)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ComparisonEquityChart({ series, loading }) {
  if (!series.length) {
    return <EmptyState title="No strategies selected" text="Select rows in the historical strategy table to build a comparison set." />;
  }
  const readySeries = series.filter((item) => item.points.length);
  const width = 860;
  const height = 280;
  const values = readySeries.flatMap((item) => item.points.map((point) => point.value));
  const min = values.length ? Math.min(...values) : 0;
  const max = values.length ? Math.max(...values) : 0;
  const span = max - min || 1;
  const polylines = readySeries.map((item) => ({
    ...item,
    polyline: item.points
      .map((point, index) => {
        const x = item.points.length === 1 ? 0 : (index / (item.points.length - 1)) * width;
        const y = height - ((point.value - min) / span) * height;
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(" ")
  }));

  return (
    <div className="comparison-panel">
      <div className="comparison-legend" aria-label="Selected strategy legend">
        {series.map((item) => (
          <article key={item.id} className={item.error ? "error" : ""}>
            <i style={{ background: item.color }} />
            <div>
              <strong>{item.label}</strong>
              <span>
                {item.loading ? "loading" : item.error || `${formatCompact(item.points.length)} points · ${formatNumber(item.netChange)} net`}
              </span>
            </div>
          </article>
        ))}
      </div>
      {readySeries.length ? (
        <div className="equity-chart comparison-equity-chart" aria-label="Selected strategy equity comparison">
          <svg viewBox={`0 0 ${width} ${height}`} role="img">
            <title>Selected strategy equity comparison</title>
            <path d={`M0 ${height} H${width}`} />
            {polylines.map((item) => (
              <polyline key={item.id} points={item.polyline} style={{ stroke: item.color }} />
            ))}
          </svg>
          <div className="chart-axis">
            <span>{formatNumber(min)}</span>
            <span>{formatNumber(max)}</span>
          </div>
        </div>
      ) : (
        <EmptyState
          title={loading ? "Loading comparison data" : "No comparison curves loaded"}
          text="Selected strategies need persisted artifact equity rows before they can be charted together."
        />
      )}
    </div>
  );
}

function ArtifactDashboard({ detail, row }) {
  const distributions = detail.distributions ?? {};
  const trades = detail.trades ?? [];
  const foldMetrics = detail.fold_metrics ?? [];
  const strictValidation = row ? {
    tick_replay_report: row.tick_replay_report,
    final_holdout_policy: row.final_holdout_policy,
    overlapping_test_folds: row.overlapping_test_folds,
    non_overlap_test_fold_indexes: row.non_overlap_test_fold_indexes,
    non_overlap_test_metrics: {
      sharpe: row.sharpe_non_overlap_test,
      net_pnl: row.net_pnl_non_overlap_test,
      annual_trades: row.annual_trades_non_overlap_test
    }
  } : detail.manifest?.strict_validation;
  return (
    <div className="artifact-dashboard">
      <div className="artifact-summary">
        <Metric label="Artifact Trades" value={formatCompact(trades.length)} detail={detail.manifest?.execution_mode ?? "mode unknown"} />
        <Metric label="Equity Points" value={formatCompact((detail.equity ?? []).length)} detail="trade-level curve" />
        <Metric label="Metric Splits" value={formatCompact(foldMetrics.length)} detail="train / validation / test / holdout" />
        <Metric label="Schema" value={`v${detail.manifest?.schema_version ?? "-"}`} detail={shortHash(detail.manifest?.data_version_hash)} />
      </div>
      <StrictValidationSummary strictValidation={strictValidation} />
      <div className="chart-grid">
        <section className="chart-card wide-chart">
          <div className="chart-heading">
            <h3>Equity Curve</h3>
            <span>All split curves, trade-level replay</span>
          </div>
          <EquityChart rows={detail.equity ?? []} />
        </section>
        <section className="chart-card">
          <div className="chart-heading">
            <h3>Replay Tape</h3>
            <span>Recent trade outcomes</span>
          </div>
          <ReplayTimeline trades={trades} />
        </section>
      </div>
      <TradeDistributionGrid distributions={distributions} />
      <SplitMetricsTable rows={foldMetrics} />
    </div>
  );
}

function StrictValidationSummary({ strictValidation }) {
  if (!strictValidation) {
    return <EmptyState title="No strict validation summary" text="Run research again to write strict validation metadata." />;
  }
  const tickReplay = strictValidation.tick_replay_report ?? {};
  const holdoutPolicy = strictValidation.final_holdout_policy ?? {};
  const nonOverlap = strictValidation.non_overlap_test_metrics ?? {};
  return (
    <section className="strict-validation-grid" aria-label="Strict validation summary">
      <Metric label="Tick Replay" value={tickReplay.status ?? "unknown"} detail={tickReplay.method ?? "method unknown"} />
      <Metric
        label="Replay Gap"
        value={tickReplay.strict_tick_replay_gap ? "flagged" : "clear"}
        detail={tickReplay.native_tick_replay ? "native bid/ask replay" : "fallback or not applicable"}
      />
      <Metric
        label="Non-Overlap Sharpe"
        value={formatNumber(nonOverlap.sharpe)}
        detail={`${formatCompact((strictValidation.non_overlap_test_fold_indexes ?? []).length)} non-overlap folds`}
      />
      <Metric
        label="Holdout Isolation"
        value={holdoutPolicy.isolation_status ?? holdoutPolicy.status ?? "unknown"}
        detail={(holdoutPolicy.llm_hidden_splits ?? []).join(" / ") || "hidden split policy unknown"}
      />
    </section>
  );
}

function StrategyCards({ rows, selectedExperimentId, onInspect }) {
  if (!rows.length) {
    return <EmptyState title="No strategy cards" text="Run research to generate promotion reports and strategy cards." />;
  }
  return (
    <div className="strategy-card-grid">
      {rows.map((row) => {
        const card = row.strategy_card ?? {};
        const promotion = row.promotion_report ?? {};
        const similarity = row.signal_similarity_report ?? {};
        const test = card.key_metrics?.test ?? {};
        const holdout = card.key_metrics?.final_holdout ?? {};
        return (
          <article key={row.experiment_id} className={`strategy-card ${selectedExperimentId === row.experiment_id ? "selected" : ""}`}>
            <div className="strategy-card-topline">
              <span className={`status-pill ${row.passed ? "completed" : "failed"}`}>{card.status ?? (row.passed ? "qualified" : "rejected")}</span>
              <span>{promotion.stage ?? "direct"} · {similarity.status ?? "signals unknown"}</span>
            </div>
            <h3>{card.name ?? row.strategy_name}</h3>
            <p>{card.market_hypothesis ?? "No market hypothesis recorded."}</p>
            <div className="strategy-card-metrics">
              <Metric label="Test Sharpe" value={formatNumber(test.sharpe ?? row.sharpe_test)} detail="sample-out aggregate" />
              <Metric label="Holdout PnL" value={formatNumber(holdout.net_pnl ?? row.net_pnl_holdout)} detail="frozen final check" />
            </div>
            <div className="suggestion-list">
              {(row.next_round_suggestions ?? card.next_round_suggestions ?? []).slice(0, 2).map((item) => (
                <span key={item}>{item}</span>
              ))}
            </div>
            <div className="gate-list" aria-label="Hard gate report">
              {(row.hard_gate_report ?? card.hard_gate_report ?? []).slice(0, 5).map((gate) => (
                <span key={gate.name} className={gate.passed ? "passed" : "failed"}>
                  {gate.name}: {gate.passed ? "pass" : "fail"}
                </span>
              ))}
            </div>
            <ActionButton variant="secondary" onClick={() => onInspect(row.experiment_id)}>
              Inspect Replay
            </ActionButton>
          </article>
        );
      })}
    </div>
  );
}

function EquityChart({ rows }) {
  const points = rows.filter((row) => typeof row.equity === "number");
  if (!points.length) {
    return <EmptyState title="No equity rows" text="Run research again to write equity.parquet." />;
  }
  const values = points.map((row) => Number(row.equity));
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const width = 720;
  const height = 220;
  const polyline = points
    .map((row, index) => {
      const x = points.length === 1 ? 0 : (index / (points.length - 1)) * width;
      const y = height - ((Number(row.equity) - min) / span) * height;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <div className="equity-chart" aria-label="Equity curve">
      <svg viewBox={`0 0 ${width} ${height}`} role="img">
        <title>Equity curve</title>
        <path d={`M0 ${height} H${width}`} />
        <polyline points={polyline} />
      </svg>
      <div className="chart-axis">
        <span>{formatNumber(min)}</span>
        <span>{formatNumber(max)}</span>
      </div>
    </div>
  );
}

function ReplayTimeline({ trades }) {
  if (!trades.length) {
    return <EmptyState title="No trades" text="The selected strategy produced no persisted trade rows." />;
  }
  const recentTrades = trades.slice(-80);
  const maxAbsPnl = Math.max(...recentTrades.map((trade) => Math.abs(Number(trade.net_pnl) || 0)), 1);
  return (
    <div className="replay-tape" aria-label="Trade replay timeline">
      {recentTrades.map((trade, index) => {
        const pnl = Number(trade.net_pnl) || 0;
        const width = `${Math.max(6, (Math.abs(pnl) / maxAbsPnl) * 100)}%`;
        return (
          <article key={`${trade.split}-${trade.fold_index}-${trade.trade_index}-${index}`} className={`replay-row ${pnl >= 0 ? "gain" : "loss"}`}>
            <span>{trade.split}</span>
            <div className="replay-bar">
              <i style={{ width }} />
            </div>
            <strong>{formatNumber(pnl)}</strong>
            <small>{trade.side} · {String(trade.exit_time ?? "").slice(0, 16)}</small>
          </article>
        );
      })}
    </div>
  );
}

function TradeDistributionGrid({ distributions }) {
  const groups = [
    ["Year", distributions.by_year ?? []],
    ["Month", distributions.by_month ?? []],
    ["Hour", distributions.by_hour ?? []],
    ["Direction", distributions.by_direction ?? []],
    ["Holding", distributions.by_holding_minutes ?? []]
  ];
  return (
    <div className="distribution-grid">
      {groups.map(([title, rows]) => (
        <section className="chart-card" key={title}>
          <div className="chart-heading">
            <h3>{title}</h3>
            <span>count and net PnL</span>
          </div>
          <DistributionBars rows={rows} />
        </section>
      ))}
    </div>
  );
}

function DistributionBars({ rows }) {
  if (!rows.length) {
    return <EmptyState title="No distribution" text="No trades are available for this bucket." />;
  }
  const maxCount = Math.max(...rows.map((row) => Number(row.trade_count) || 0), 1);
  return (
    <div className="distribution-bars">
      {rows.map((row) => (
        <article key={row.bucket}>
          <span>{row.bucket}</span>
          <div>
            <i style={{ width: `${Math.max(4, (Number(row.trade_count) / maxCount) * 100)}%` }} />
          </div>
          <strong>{formatCompact(row.trade_count)}</strong>
          <small className={Number(row.net_pnl) >= 0 ? "positive" : "negative"}>{formatNumber(row.net_pnl)}</small>
        </article>
      ))}
    </div>
  );
}

function SplitMetricsTable({ rows }) {
  if (!rows.length) {
    return <EmptyState title="No fold metrics" text="fold_metrics.parquet was not found for this experiment." />;
  }
  return (
    <div className="table-wrap split-table">
      <table>
        <thead>
          <tr>
            <th>Split</th>
            <th>Fold</th>
            <th>Trades</th>
            <th>Sharpe</th>
            <th>Net PnL</th>
            <th>Max DD</th>
            <th>Annual Trades</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={`${row.split}-${row.fold_index}-${index}`}>
              <td>{row.split}</td>
              <td>{row.fold_index ?? "final"}</td>
              <td>{formatCompact(row.trade_count)}</td>
              <td>{formatNumber(row.sharpe)}</td>
              <td>{formatNumber(row.net_pnl)}</td>
              <td>{formatNumber(row.max_drawdown)}</td>
              <td>{formatCompact(row.annual_trades)}</td>
            </tr>
          ))}
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

function tickReplayStatusClass(report) {
  if (report?.strict_tick_replay_gap) {
    return "failed";
  }
  if (report?.native_tick_replay || report?.status === "not_applicable") {
    return "completed";
  }
  return "running";
}

function moduleStatusClass(status) {
  if (["candidate", "freeze_confirmed", "paper_shadow"].includes(status)) {
    return "completed";
  }
  if (status === "retired") {
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
