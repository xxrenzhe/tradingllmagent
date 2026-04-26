export function apiUrl(apiBase, path) {
  const base = apiBase.trim().replace(/\/$/, "");
  return `${base}${path}`;
}

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
