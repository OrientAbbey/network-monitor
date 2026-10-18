/**
 * Client HTTP vers l'API FastAPI (../../network_monitor/app/api.py). Aucune
 * génération de données ici — uniquement des appels réseau vers le vrai backend.
 */
import type {
  Alert, AppConfigView, AvailabilityReport, ConfigUpdate, ForecastPoint, Incident, MaintenanceRequest, MaintenanceWindow,
  MonitoringStatus, OutputSummary, PrecisionReport, TrafficSample, Verdict,
} from "./types";

// Chaîne vide (image Docker : l'API sert elle-même l'interface) = même origine que la page.
const API_BASE: string = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";
const TIMEOUT_MS = 10_000;

// Jeton des endpoints de contrôle. Obtenu en se connectant (identifiant et mot de passe,
// POST /auth/login) : jeton de session signé, à durée limitée, gardé en mémoire seulement —
// jamais écrit dans le bundle ni dans le stockage du navigateur. `setApiToken` accepte
// aussi directement le jeton machine (NM_API_TOKEN) ; VITE_API_TOKEN l'injecte au build,
// mais il est alors visible de quiconque charge la page (démo locale uniquement).
let apiToken: string | undefined = import.meta.env.VITE_API_TOKEN;
let session: Session | null = null;

export interface Session {
  username: string;
  expiresAt: Date;
}

export function setApiToken(token: string | undefined): void {
  apiToken = token?.trim() || undefined;
  session = null;
}

export function hasApiToken(): boolean {
  if (session && session.expiresAt.getTime() <= Date.now()) logout(); // session expirée
  return apiToken !== undefined;
}

/** Session en cours (null si déconnecté, jeton machine ou session expirée). */
export function currentSession(): Session | null {
  return hasApiToken() ? session : null;
}

export function logout(): void {
  apiToken = undefined;
  session = null;
}

type Param = string | number | undefined | null | string[];

interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "DELETE";
  params?: Record<string, Param>;
  body?: unknown;
}

async function request<T>(path: string, { method = "GET", params, body }: RequestOptions = {}): Promise<T> {
  const url = new URL(path, API_BASE || window.location.origin);
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value === undefined || value === null) continue;
    if (Array.isArray(value)) value.forEach((v) => url.searchParams.append(key, v));
    else url.searchParams.set(key, String(value));
  }
  const headers: Record<string, string> = {};
  if (body) headers["Content-Type"] = "application/json";
  if (apiToken && method !== "GET") headers.Authorization = `Bearer ${apiToken}`;
  // Délai maximal : une API qui ne répond plus ne doit pas empiler des requêtes
  // pendantes à chaque rafraîchissement automatique.
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  let res: Response;
  try {
    res = await fetch(url, { method, headers, body: body ? JSON.stringify(body) : undefined, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(`${method} ${path} -> ${res.status} ${detail}`);
  }
  return (res.status === 204 ? null : await res.json()) as T;
}

interface LoginResponse {
  access_token: string;
  expires_in: number;
  username: string;
}

/** Ouvre une session. Le mot de passe n'est ni conservé ni réutilisé après l'appel. */
export async function login(username: string, password: string): Promise<Session> {
  const res = await request<LoginResponse>("/auth/login", { method: "POST", body: { username, password } });
  apiToken = res.access_token;
  session = { username: res.username, expiresAt: new Date(Date.now() + res.expires_in * 1000) };
  return session;
}

const out = (id: string) => `/outputs/${encodeURIComponent(id)}`;

export const api = {
  outputs: () => request<OutputSummary[]>("/outputs"),
  history: (id: string, since?: string, until?: string, spanHours?: number) =>
    request<TrafficSample[]>(`${out(id)}/history`, { params: { since, until, span_hours: spanHours } }),
  forecast: (id: string, since?: string, until?: string, spanHours?: number) =>
    request<ForecastPoint[]>(`${out(id)}/forecast`, { params: { since, until, span_hours: spanHours } }),
  forecastAhead: (id: string, horizonMinutes: number) =>
    request<ForecastPoint[]>(`${out(id)}/forecast/ahead`, { params: { horizon_minutes: horizonMinutes } }),
  incidents: (status?: string[], limit = 100) => request<Incident[]>("/incidents", { params: { status, limit } }),
  // `days` : fenêtre calculée par le SERVEUR (son horloge, pas celle du navigateur).
  availability: (days: number) => request<AvailabilityReport>("/reports/availability", { params: { days } }),
  availabilityCsvUrl: (days: number) => new URL(`/reports/availability?format=csv&days=${days}`, API_BASE || window.location.origin).toString(),
  alerts: (outputId?: string, limit = 20) => request<Alert[]>("/alerts", { params: { output_id: outputId, limit } }),
  // Verdict d'un opérateur (connecté) ; `null` le retire. Seul un incident confirmé peut être jugé (409 sinon).
  setFeedback: (id: string, verdict: Verdict | null) =>
    request<Incident>(`/incidents/${encodeURIComponent(id)}/feedback`, { method: "PUT", body: { verdict } }),
  precision: (days: number) => request<PrecisionReport>("/reports/precision", { params: { days } }),
  // Fenêtres relatives à l'horloge du SERVEUR (`days`, `starts_in_minutes`) : jamais de dates fabriquées ici.
  maintenance: (days = 7) => request<MaintenanceWindow[]>("/maintenance", { params: { days } }),
  createMaintenance: (body: MaintenanceRequest) => request<MaintenanceWindow>("/maintenance", { method: "POST", body }),
  cancelMaintenance: (id: string) => request<MaintenanceWindow>(`/maintenance/${encodeURIComponent(id)}`, { method: "DELETE" }),
  incident: (id: string) => request<Incident>(`/incidents/${encodeURIComponent(id)}`),
  monitoringStatus: () => request<MonitoringStatus>("/monitoring/status"),
  startMonitoring: () => request<MonitoringStatus>("/monitoring/start", { method: "POST" }),
  stopMonitoring: () => request<MonitoringStatus>("/monitoring/stop", { method: "POST" }),
  getConfig: () => request<AppConfigView>("/config"),
  updateConfig: (partial: ConfigUpdate) => request<AppConfigView>("/config", { method: "PUT", body: partial }),
};
