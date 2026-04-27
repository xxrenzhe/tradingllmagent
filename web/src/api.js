export function apiUrl(apiBase, path) {
  const base = apiBase.trim().replace(/\/$/, "");
  return `${base}${path}`;
}

export const API_PATHS = {
  backtestTick: "/api/backtests/tick",
  calibrationCosts: "/api/calibration/costs",
  dataBuildBars: "/api/data/build-bars",
  dataDownload: "/api/data/download",
  dataQuality: (query) => `/api/data/quality?${query}`,
  dataSymbols: "/api/data/symbols",
  executionApprovalQueue: "/api/execution/approval-queue",
  executionReadiness: "/api/execution/readiness",
  experiment: (id) => `/api/experiments/${encodeURIComponent(id)}`,
  experimentArtifacts: (id) => `/api/experiments/${encodeURIComponent(id)}/artifacts`,
  experimentAuditLogs: (id, limit = 50) => `/api/experiments/${encodeURIComponent(id)}/audit-logs?limit=${limit}`,
  gatewayHealth: "/api/gateways/nt8/health",
  gatewayIncidents: "/api/gateways/nt8/incidents",
  gatewayOrderUpdates: "/api/gateways/nt8/order-updates",
  gatewayReconciliation: "/api/gateways/nt8/reconciliation",
  modulesMemory: "/api/modules/memory",
  monitorReport: "/api/monitor/report",
  paperNtExportSignal: "/api/paper/nt-export-signal",
  paperReplay: "/api/paper/replay",
  readinessExternalValidation: "/api/readiness/external-validation",
  reportsLeaderboard: "/api/reports/leaderboard",
  researchIterations: "/api/experiments/iterations",
  researchProposals: "/api/experiments/proposals",
  researchRuns: "/api/experiments/research-runs",
  researchTargetDiscovery: "/api/experiments/target-discovery",
  taskCancel: (taskId) => `/api/tasks/${taskId}/cancel`,
  taskEvents: (taskId) => `/api/tasks/${taskId}/events`,
  taskRun: (taskId) => `/api/tasks/${taskId}/run`,
  tasks: (limit = 50) => `/api/tasks?limit=${limit}`,
  triggerGateMemory: "/api/trigger-gate/memory",
  triggerGateOutcomes: "/api/trigger-gate/outcomes",
  triggerGateReports: "/api/trigger-gate/reports",
  triggerGateSchedules: "/api/trigger-gate/schedules",
  triggerGateSimulations: "/api/trigger-gate/simulations"
};

export async function apiRequest(apiBase, path, options = {}) {
  const response = await fetch(apiUrl(apiBase, path), {
    headers: {
      "Content-Type": "application/json",
      ...(options.headers ?? {})
    },
    ...options
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.detail || payload.error || `Request failed: ${response.status}`);
  }
  return payload;
}

export function formatNumber(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "-";
  }
  return Number(value).toLocaleString(undefined, {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits
  });
}

export function formatCompact(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "-";
  }
  return Number(value).toLocaleString(undefined, { maximumFractionDigits: 0 });
}

export function formatPercent(value, digits = 1) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "-";
  }
  return `${(Number(value) * 100).toLocaleString(undefined, {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits
  })}%`;
}
