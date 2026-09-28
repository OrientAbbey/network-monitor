/**
 * Client HTTP vers l'API FastAPI (../../app/api.py). Aucune génération de
 * données ici — uniquement des appels réseau vers le vrai backend.
 */

const API_BASE = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";

async function request(path, { method = "GET", params, body } = {}) {
  const url = new URL(API_BASE + path);
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value === undefined || value === null) continue;
      if (Array.isArray(value)) value.forEach((v) => url.searchParams.append(key, v));
      else url.searchParams.set(key, value);
    }
  }
  const res = await fetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(`${method} ${path} -> ${res.status} ${detail}`);
  }
  return res.status === 204 ? null : res.json();
}

export const api = {
  outputs: () => request("/outputs"),
  history: (outputId, since, until) => request(`/outputs/${outputId}/history`, { params: { since, until } }),
  anomalies: (outputId, since, until) => request(`/outputs/${outputId}/anomalies`, { params: { since, until } }),
  forecast: (outputId, since, until) => request(`/outputs/${outputId}/forecast`, { params: { since, until } }),
  forecastAhead: (outputId, horizonMinutes) =>
    request(`/outputs/${outputId}/forecast/ahead`, { params: { horizon_minutes: horizonMinutes } }),
  incidents: (status, limit = 100) => request("/incidents", { params: { status, limit } }),
  incident: (incidentId) => request(`/incidents/${incidentId}`),
  monitoringStatus: () => request("/monitoring/status"),
  startMonitoring: () => request("/monitoring/start", { method: "POST" }),
  stopMonitoring: () => request("/monitoring/stop", { method: "POST" }),
  getConfig: () => request("/config"),
  updateConfig: (partial) => request("/config", { method: "PUT", body: partial }),
};
