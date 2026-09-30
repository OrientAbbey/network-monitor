/**
 * Client HTTP vers l'API FastAPI (network_monitor/app/api.py).
 * Chaque requête a un délai maximal et peut être annulée par l'appelant via un `AbortSignal`.
 */

const API_BASE = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";
export const REQUEST_TIMEOUT_MS = 8000;

/** Vrai pour une annulation volontaire (ni échec réseau, ni délai dépassé). */
export function isAbortError(err) {
  return err?.name === "AbortError";
}

async function request(path, { method = "GET", params, body, signal } = {}) {
  const url = new URL(API_BASE + path);
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value === undefined || value === null) continue;
      if (Array.isArray(value)) value.forEach((v) => url.searchParams.append(key, v));
      else url.searchParams.set(key, value);
    }
  }

  const controller = new AbortController();
  const forwardAbort = () => controller.abort(signal.reason);
  if (signal) {
    if (signal.aborted) controller.abort(signal.reason);
    else signal.addEventListener("abort", forwardAbort, { once: true });
  }
  const timer = setTimeout(
    () => controller.abort(new DOMException(`Délai dépassé (${REQUEST_TIMEOUT_MS / 1000} s)`, "TimeoutError")),
    REQUEST_TIMEOUT_MS
  );

  try {
    const res = await fetch(url, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    });
    if (!res.ok) {
      const detail = await res.text();
      const err = new Error(`${method} ${path} -> ${res.status} ${detail}`);
      err.status = res.status;
      throw err;
    }
    return res.status === 204 ? null : await res.json();
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", forwardAbort);
  }
}

export const api = {
  outputs: (signal) => request("/outputs", { signal }),
  history: (outputId, since, until, signal) =>
    request(`/outputs/${outputId}/history`, { params: { since, until }, signal }),
  anomalies: (outputId, since, until, signal) =>
    request(`/outputs/${outputId}/anomalies`, { params: { since, until }, signal }),
  forecast: (outputId, since, until, signal) =>
    request(`/outputs/${outputId}/forecast`, { params: { since, until }, signal }),
  forecastAhead: (outputId, horizonMinutes, signal) =>
    request(`/outputs/${outputId}/forecast/ahead`, { params: { horizon_minutes: horizonMinutes }, signal }),
  snapshots: (outputId, offsets, signal) =>
    request(`/outputs/${outputId}/snapshots`, { params: { offsets }, signal }),
  incidents: (status, limit = 100, signal) => request("/incidents", { params: { status, limit }, signal }),
  incident: (incidentId, signal) => request(`/incidents/${incidentId}`, { signal }),
  monitoringStatus: (signal) => request("/monitoring/status", { signal }),
  startMonitoring: () => request("/monitoring/start", { method: "POST" }),
  stopMonitoring: () => request("/monitoring/stop", { method: "POST" }),
  getConfig: (signal) => request("/config", { signal }),
  updateConfig: (partial) => request("/config", { method: "PUT", body: partial }),
};
