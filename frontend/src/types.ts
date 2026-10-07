/** Types des réponses de l'API (miroir de network_monitor/app/api.py et models.py). */

export type IncidentStatus = "suspect" | "open" | "stale" | "closed";

export interface TrafficSample {
  timestamp: string;
  output_id: string;
  traffic_mbps: number;
  loss_percent: number;
}

export interface ForecastPoint {
  timestamp: string;
  output_id: string;
  predicted_traffic_mbps: number;
  lower_bound_mbps: number;
  upper_bound_mbps: number;
  confidence: number;
  model_name: string;
}

export interface OutputSummary {
  output_id: string;
  latest_sample: TrafficSample | null;
  status: "normal" | IncidentStatus;
  active_incident_id: string | null;
}

export interface Incident {
  id: string;
  output_id: string;
  status: IncidentStatus;
  severity: string;
  incident_type: string;
  last_seen_at: string;
  started_at: string | null;
  ended_at: string | null;
  score: number;
  description: string;
}

export interface Alert {
  incident_id: string;
  output_id: string;
  severity: string;
  event: string;
  timestamp: string;
  message: string;
  incident_type: string;
  duration_seconds: number | null;
  traffic_mbps: number | null;
  expected_traffic_mbps: number | null;
  traffic_deviation_percent: number | null;
  loss_percent: number | null;
  expected_loss_percent: number | null;
}

export interface OutputAvailability {
  output_id: string;
  period_seconds: number;
  monitored_from: string | null;
  monitored_seconds: number;
  incidents: number;
  downtime_seconds: number;
  degraded_seconds: number;
  availability_percent: number | null;
  coverage_percent: number | null;
  mttd_seconds: number | null;
  mttr_seconds: number | null;
  longest_incident_seconds: number | null;
}

export interface AvailabilityReport {
  start: string;
  end: string;
  downtime_severity: string;
  outputs: OutputAvailability[];
}

export interface MonitoringStatus {
  running: boolean;
  calibrated: boolean;
  simulated_now: string;
  server_time: string;
  interval_seconds: number;
  seconds_per_tick: number;
  outputs: string[];
}

export interface AppConfigView {
  incident: { open_score: number; close_score: number; warning_score: number };
  alerts: { enabled: boolean; critical_score: number };
  source?: { type: string };
  [section: string]: unknown;
}

export interface ConfigUpdate {
  open_score?: number;
  close_score?: number;
  warning_score?: number;
  critical_score?: number;
}
