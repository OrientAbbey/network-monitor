import { useCallback, useEffect, useState } from "react";
import { AvailabilityPanel } from "./AvailabilityPanel";
import { DetailChart, type Visibility } from "./DetailChart";
import { IncidentsPanel } from "./IncidentsPanel";
import { MaintenancePanel } from "./MaintenancePanel";
import { type IncidentDraft, SettingsPanel } from "./SettingsPanel";
import { SummaryTable } from "./SummaryTable";
import { api } from "./api";
import { errorMessage, formatClock, formatMbps } from "./format";
import { actionError } from "./operatorActions";
import { usePolling } from "./polling";
import { BASE_STYLE } from "./styles";
import { COLORS, STATUS_STYLE } from "./theme";
import type { Alert, AppConfigView, Incident, MonitoringStatus, OutputSummary } from "./types";

/* ---------------------------------------------------------------------
   "Signal Room" — même identité visuelle que la maquette v2, mais toutes
   les données viennent maintenant du vrai backend (../../app/api.py) via
   des appels réseau. Différence structurante par rapport à la maquette :
   le backend réel n'a de données que depuis son démarrage — impossible de
   fabriquer un mois d'historique à la demande. Les plages longues peuvent
   donc légitimement être vides juste après le lancement (voir EmptyRange).
--------------------------------------------------------------------- */

export const DEFAULT_CONFIG_UPDATE_DELTA_THRESHOLDS = { traffic: 0.05, loss: 0.1 };

export { DetailChart } from "./DetailChart";

export default function Dashboard() {
  const [outputs, setOutputs] = useState<OutputSummary[]>([]);
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [config, setConfig] = useState<AppConfigView | null>(null);
  const [monitoringStatus, setMonitoringStatus] = useState<MonitoringStatus | null>(null);
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [expandedCharts, setExpandedCharts] = useState<Record<string, boolean>>({});
  const [visibility, setVisibility] = useState<Record<string, Visibility>>({});
  const [chartRange, setChartRange] = useState<Record<string, string>>({});
  const [tableExpanded, setTableExpanded] = useState(true);
  const [settingsExpanded, setSettingsExpanded] = useState(false);
  const [deltaThresholds, setDeltaThresholds] = useState(DEFAULT_CONFIG_UPDATE_DELTA_THRESHOLDS);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const ensureOutputDefaults = (ids: string[]) => {
    setExpandedCharts((prev) => { const next = { ...prev }; ids.forEach((id) => { if (!(id in next)) next[id] = true; }); return next; });
    setVisibility((prev) => { const next = { ...prev }; ids.forEach((id) => { if (!(id in next)) next[id] = { traffic: true, loss: true, baseline: false, prediction: false }; }); return next; });
    setChartRange((prev) => { const next = { ...prev }; ids.forEach((id) => { if (!(id in next)) next[id] = "12h"; }); return next; });
  };

  const refreshTopLevel = useCallback(async () => {
    try {
      const [outs, incs, status, recentAlerts] = await Promise.all([
        api.outputs(), api.incidents(undefined, 100), api.monitoringStatus(), api.alerts(undefined, 20),
      ]);
      setOutputs(outs);
      setIncidents(incs);
      setAlerts(recentAlerts);
      setMonitoringStatus(status);
      setConnectionError(null);
      ensureOutputDefaults(outs.map((o) => o.output_id));
    } catch (err) {
      setConnectionError(`Impossible de joindre l'API (${errorMessage(err)}). Vérifiez qu'elle tourne sur l'URL configurée (VITE_API_BASE_URL).`);
    }
  }, []);

  useEffect(() => {
    api.getConfig().then(setConfig).catch(() => {});
  }, []);
  usePolling(refreshTopLevel, 4000, [refreshTopLevel]);

  const saveIncidentConfig = async (draft: IncidentDraft) => {
    setSaving(true);
    setSaveError(null);
    try {
      const updated = await api.updateConfig({
        open_score: draft.open_score,
        close_score: draft.close_score,
        warning_score: draft.warning_score,
      });
      setConfig(updated);
    } catch (err) {
      setSaveError(errorMessage(err).includes(" 403 ") ? actionError(err) : "Rejeté par l'API : " + errorMessage(err));
    } finally {
      setSaving(false);
    }
  };

  const toggleChart = (id: string) => setExpandedCharts((prev) => ({ ...prev, [id]: !prev[id] }));
  const toggleSeries = (id: string, key: keyof Visibility) => setVisibility((prev) => ({ ...prev, [id]: { ...prev[id], [key]: !prev[id][key] } }));

  if (connectionError) {
    return (
      <div className="nm-root">
        <style>{BASE_STYLE}</style>
        <div className="connection-error">
          <h1>Signal Room</h1>
          <p>{connectionError}</p>
        </div>
      </div>
    );
  }

  if (!config || outputs.length === 0) {
    return (
      <div className="nm-root">
        <style>{BASE_STYLE}</style>
        <p className="chart-loading">Connexion à l'API…</p>
      </div>
    );
  }

  const activeIncidents = outputs.filter((o) => o.status === "open").length;

  return (
    <div className="nm-root">
      <style>{BASE_STYLE}</style>
      <a className="skip-link" href="#nm-main">Aller au contenu</a>

      <header className="nm-header">
        <h1 className="nm-title">Signal Room <span>· réseau, branché sur l'API réelle</span></h1>
        <div className="nm-header-meta">
          <div className="nm-live"><span className="nm-live-dot" />{monitoringStatus?.running ? "live" : "arrêté"}</div>
          <div>{outputs.length} sorties · <span className={activeIncidents ? "nm-incident-count" : ""}>{activeIncidents} incident{activeIncidents !== 1 ? "s" : ""} actif{activeIncidents !== 1 ? "s" : ""}</span></div>
          <div className="nm-clock mono">{monitoringStatus ? formatClock(new Date(monitoringStatus.simulated_now)) : "—"}</div>
        </div>
      </header>

      {config?.source?.type === "mock" && (
        <div role="status" className="nm-sim-banner">
          Données simulées (source « mock ») : ce que vous voyez n'est pas votre réseau. Choisissez une vraie source dans la
          configuration du serveur (<code>source.type</code>).
        </div>
      )}

      <main id="nm-main" tabIndex={-1}>
      <section className="nm-channels" aria-label="Sorties">
        {outputs.map((output) => {
          const id = output.output_id;
          const style = STATUS_STYLE[output.status] ?? STATUS_STYLE.normal;
          const isActive = output.status === "open" || output.status === "stale";
          const expanded = expandedCharts[id];
          return (
            <div key={id} className={`nm-channel${isActive ? " is-active" : ""}`} style={{ ["--stripe" as string]: style.color, borderLeftColor: isActive ? style.color : COLORS.border }}>
              <div className="nm-channel-head" onClick={() => toggleChart(id)}>
                <div className="nm-channel-id-block">
                  <span className="nm-card-id">{id}</span>
                </div>
                <div className="nm-channel-readout">
                  <span className="mono">{formatMbps(output.latest_sample?.traffic_mbps)}</span>
                  <span className="mono nm-readout-loss">{output.latest_sample ? `${output.latest_sample.loss_percent.toFixed(2)}% perte` : "—"}</span>
                </div>
                <div className="nm-status-row">
                  <span className="nm-status-dot" style={{ background: style.color }} />
                  <span style={{ color: style.color, fontWeight: 600 }}>{style.label}</span>
                </div>
                <button type="button" className="nm-collapse-btn" aria-expanded={expanded} aria-label={expanded ? `Replier ${id}` : `Déplier ${id}`}
                  onClick={(e) => { e.stopPropagation(); toggleChart(id); }}>
                  {expanded ? "−" : "+"}
                </button>
              </div>
              {expanded && visibility[id] && (
                <div className="nm-channel-body">
                  <DetailChart
                    outputId={id}
                    rangeKey={chartRange[id] || "12h"}
                    onRangeChange={(key) => setChartRange((prev) => ({ ...prev, [id]: key }))}
                    visibility={visibility[id]}
                    onToggle={(key) => toggleSeries(id, key)}
                    current={{ traffic: output.latest_sample?.traffic_mbps, loss: output.latest_sample?.loss_percent }}
                  />
                </div>
              )}
            </div>
          );
        })}
      </section>

      <section className="nm-panel nm-table-panel" aria-labelledby="h-compare">
        <div className="nm-panel-head" onClick={() => setTableExpanded((v) => !v)}>
          <h2 id="h-compare">Comparatif temporel</h2>
          <button type="button" className="nm-collapse-btn" aria-expanded={tableExpanded} aria-label={tableExpanded ? "Replier le tableau" : "Déplier le tableau"} onClick={(e) => { e.stopPropagation(); setTableExpanded((v) => !v); }}>
            {tableExpanded ? "−" : "+"}
          </button>
        </div>
        {tableExpanded && <SummaryTable outputIds={outputs.map((o) => o.output_id)} thresholds={deltaThresholds} />}
      </section>

      <section className="nm-panel nm-table-panel" aria-labelledby="h-config">
        <div className="nm-panel-head" onClick={() => setSettingsExpanded((v) => !v)}>
          <h2 id="h-config">Configuration</h2>
          <button type="button" className="nm-collapse-btn" aria-expanded={settingsExpanded} aria-label={settingsExpanded ? "Replier la configuration" : "Déplier la configuration"} onClick={(e) => { e.stopPropagation(); setSettingsExpanded((v) => !v); }}>
            {settingsExpanded ? "−" : "+"}
          </button>
        </div>
        {settingsExpanded && (
          <SettingsPanel
            config={config}
            deltaThresholds={deltaThresholds}
            onDeltaThresholdsChange={setDeltaThresholds}
            onSave={saveIncidentConfig}
            saving={saving}
            saveError={saveError}
          />
        )}
      </section>

      <AvailabilityPanel />

      <section className="nm-panel" aria-labelledby="h-alerts">
        <h2 id="h-alerts">Alertes récentes</h2>
        <div className="nm-log" role="log" aria-live="polite" aria-relevant="additions">
          {alerts.length === 0 && <div className="nm-log-empty">Aucune alerte pour l'instant.</div>}
          {alerts.map((a) => (
            <div className="nm-log-row" key={`${a.incident_id}-${a.timestamp}-${a.event}`}>
              <span className="nm-log-time mono">{formatClock(new Date(a.timestamp))}</span>
              <span className="nm-log-dot" style={{ background: a.severity === "critical" ? COLORS.coral : a.severity === "warning" ? COLORS.amber : COLORS.textDim }} />
              <span className="nm-log-msg">
                {a.output_id} — {a.event} : {a.message}
                {a.suppressed && <span className="nm-tag">retenue : {a.suppressed === "maintenance" ? "maintenance" : "cause amont"}</span>}
                {a.impacted_outputs.length > 0 && <span className="nm-note"> Explique aussi : {a.impacted_outputs.join(", ")}</span>}
              </span>
            </div>
          ))}
        </div>
      </section>

      <IncidentsPanel incidents={incidents} />

      <MaintenancePanel outputIds={outputs.map((o) => o.output_id)} />
      </main>
    </div>
  );
}
