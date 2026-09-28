import { useState, useEffect, useRef, useCallback } from "react";
import { api } from "./api.js";

/* ---------------------------------------------------------------------
   "Signal Room" — même identité visuelle que la maquette v2, mais toutes
   les données viennent maintenant du vrai backend (../../app/api.py) via
   des appels réseau. Différence structurante par rapport à la maquette :
   le backend réel n'a de données que depuis son démarrage — impossible de
   fabriquer un mois d'historique à la demande. Les plages longues peuvent
   donc légitimement être vides juste après le lancement (voir EmptyRange).
--------------------------------------------------------------------- */

const COLORS = {
  bg: "#060B0A",
  panel: "#0B1512",
  grid: "#12201C",
  border: "#1C2E28",
  borderBright: "#2A423A",
  text: "#DCEAE3",
  textDim: "#5E7C72",
  textFaint: "#3C544D",
  trace: "#7FE7C4",
  lossTrace: "#5FB3E8",
  amber: "#E8A33D",
  coral: "#E8604A",
  violet: "#9098B0",
};

const STATUS_STYLE = {
  normal: { color: COLORS.textDim, label: "normal" },
  suspect: { color: COLORS.amber, label: "SUSPECT" },
  open: { color: COLORS.coral, label: "OPEN" },
  stale: { color: COLORS.violet, label: "STALE" },
  closed: { color: COLORS.textDim, label: "closed" },
};

const RANGES = [
  { key: "12h", label: "12 h", spanMs: 12 * 3600_000 },
  { key: "1d", label: "1 j", spanMs: 24 * 3600_000 },
  { key: "1w", label: "1 sem", spanMs: 7 * 24 * 3600_000 },
  { key: "1m", label: "1 mois", spanMs: 30 * 24 * 3600_000 },
];

const DEFAULT_CONFIG_UPDATE_DELTA_THRESHOLDS = { traffic: 0.05, loss: 0.1 };

function formatMbps(v) {
  if (v == null) return "—";
  if (v >= 1000) return (v / 1000).toFixed(2) + " Gbps";
  return v.toFixed(1) + " Mbps";
}

function formatClock(date) {
  return date.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
}

function formatAxisLabel(date, rangeKey, now) {
  if (rangeKey === "12h" || rangeKey === "1d") return formatClock(date);
  const daysAgo = Math.round((now - date) / (24 * 3600_000));
  return daysAgo <= 0 ? formatClock(date) : `-${daysAgo}j ${formatClock(date)}`;
}

// --- Graphe détaillé --------------------------------------------------

const CHART_W = 640;
const CHART_H = 230;
const MARGIN = { top: 12, right: 52, bottom: 26, left: 50 };
const INNER_W = CHART_W - MARGIN.left - MARGIN.right;
const INNER_H = CHART_H - MARGIN.top - MARGIN.bottom;

function niceDomain(values, paddingFrac = 0.12, floorAtZero = false) {
  if (!values.length) return [0, 1];
  let min = Math.min(...values);
  let max = Math.max(...values);
  if (min === max) { min -= 1; max += 1; }
  const pad = (max - min) * paddingFrac;
  return [floorAtZero ? Math.max(0, min - pad) : min - pad, max + pad];
}

function EmptyRange({ label }) {
  return (
    <div className="empty-range">
      Pas encore assez d'historique réel pour "{label}" — le système n'a
      commencé à mesurer qu'à son démarrage. Réessayez plus tard, ou choisissez
      une plage plus courte.
    </div>
  );
}

function DetailChart({ outputId, rangeKey, onRangeChange, visibility, onToggle, current }) {
  const [history, setHistory] = useState([]);
  const [baseline, setBaseline] = useState([]);
  const [prediction, setPrediction] = useState([]);
  const [loading, setLoading] = useState(true);
  const range = RANGES.find((r) => r.key === rangeKey) ?? RANGES[0];

  const refresh = useCallback(async () => {
    const until = new Date();
    const since = new Date(until.getTime() - range.spanMs);
    try {
      const [hist, fc, ahead] = await Promise.all([
        api.history(outputId, since.toISOString(), until.toISOString()),
        visibility.baseline ? api.forecast(outputId, since.toISOString(), until.toISOString()) : Promise.resolve([]),
        visibility.prediction ? api.forecastAhead(outputId, 35) : Promise.resolve([]),
      ]);
      setHistory(hist);
      setBaseline(fc);
      setPrediction(ahead);
    } catch (err) {
      console.error("Échec du rafraîchissement du graphe", outputId, err);
    } finally {
      setLoading(false);
    }
  }, [outputId, rangeKey, visibility.baseline, visibility.prediction]);

  useEffect(() => {
    setLoading(true);
    refresh();
    const id = setInterval(refresh, 5000);
    return () => clearInterval(id);
  }, [refresh]);

  if (loading && history.length === 0) {
    return <div className="chart-loading">Chargement…</div>;
  }
  if (history.length < 2) {
    return (
      <div>
        <RangeSelector rangeKey={rangeKey} onRangeChange={onRangeChange} />
        <EmptyRange label={range.label} />
        <Legend visibility={visibility} onToggle={onToggle} current={current} />
      </div>
    );
  }

  const now = new Date();
  const points = history.map((h) => ({ date: new Date(h.timestamp), traffic: h.traffic_mbps, loss: h.loss_percent }));
  const baselinePoints = baseline.map((f) => ({ date: new Date(f.timestamp), traffic: f.predicted_traffic_mbps }));
  const predictionPoints = prediction.map((f) => ({ date: new Date(f.timestamp), traffic: f.predicted_traffic_mbps, loss: null }));

  const tStart = points[0].date.getTime();
  const tEnd = (predictionPoints.length ? predictionPoints[predictionPoints.length - 1].date : points[points.length - 1].date).getTime();
  const span = Math.max(1, tEnd - tStart);
  const xAt = (date) => MARGIN.left + ((date.getTime() - tStart) / span) * INNER_W;
  const nowX = xAt(points[points.length - 1].date);

  const trafficValues = [
    ...(visibility.traffic ? points.map((p) => p.traffic) : []),
    ...(visibility.baseline ? baselinePoints.map((p) => p.traffic) : []),
    ...(visibility.prediction ? predictionPoints.map((p) => p.traffic) : []),
  ];
  const lossValues = visibility.loss ? points.map((p) => p.loss) : [];
  const [tMin, tMax] = niceDomain(trafficValues.length ? trafficValues : [current.traffic || 1]);
  const [lMin, lMax] = niceDomain(lossValues.length ? lossValues : [current.loss || 0.1], 0.25, true);

  const yTraffic = (v) => MARGIN.top + INNER_H - ((v - tMin) / (tMax - tMin || 1)) * INNER_H;
  const yLoss = (v) => MARGIN.top + INNER_H - ((v - lMin) / (lMax - lMin || 1)) * INNER_H;
  const pathFor = (pts, yFn) => pts.map((p, i) => `${i === 0 ? "M" : "L"} ${xAt(p.date).toFixed(1)} ${yFn(p.v).toFixed(1)}`).join(" ");

  const actualTrafficPath = pathFor(points.map((p) => ({ date: p.date, v: p.traffic })), yTraffic);
  const actualLossPath = pathFor(points.map((p) => ({ date: p.date, v: p.loss })), yLoss);
  const baselinePath = pathFor(baselinePoints.map((p) => ({ date: p.date, v: p.traffic })), yTraffic);
  const predictionPath = pathFor(predictionPoints.map((p) => ({ date: p.date, v: p.traffic })), yTraffic);

  const rows = [0, 1 / 3, 2 / 3, 1];
  const nTimeLabels = 5;
  const timeIdx = Array.from({ length: nTimeLabels }, (_, i) => Math.round((points.length - 1) * (i / (nTimeLabels - 1))));

  return (
    <div>
      <RangeSelector rangeKey={rangeKey} onRangeChange={onRangeChange} />
      <svg viewBox={`0 0 ${CHART_W} ${CHART_H}`} style={{ width: "100%", height: "auto", display: "block" }}>
        {rows.map((f) => {
          const py = MARGIN.top + f * INNER_H;
          const tVal = tMax - f * (tMax - tMin);
          const lVal = lMax - f * (lMax - lMin);
          return (
            <g key={f}>
              <line x1={MARGIN.left} x2={CHART_W - MARGIN.right} y1={py} y2={py} stroke={COLORS.grid} strokeWidth="1" />
              {(visibility.traffic || visibility.baseline || visibility.prediction) && (
                <text x={MARGIN.left - 8} y={py + 3} textAnchor="end" fontSize="9" fill={COLORS.textFaint} fontFamily="JetBrains Mono, monospace">
                  {formatMbps(Math.max(0, tVal))}
                </text>
              )}
              {visibility.loss && (
                <text x={CHART_W - MARGIN.right + 8} y={py + 3} textAnchor="start" fontSize="9" fill={COLORS.textFaint} fontFamily="JetBrains Mono, monospace">
                  {Math.max(0, lVal).toFixed(2)}%
                </text>
              )}
            </g>
          );
        })}

        {timeIdx.map((i) => (
          <text key={i} x={xAt(points[i].date)} y={CHART_H - 6} textAnchor="middle" fontSize="8.5" fill={COLORS.textFaint} fontFamily="JetBrains Mono, monospace">
            {formatAxisLabel(points[i].date, rangeKey, now)}
          </text>
        ))}

        <line x1={nowX} x2={nowX} y1={MARGIN.top} y2={MARGIN.top + INNER_H} stroke={COLORS.borderBright} strokeWidth="1" strokeDasharray="2 3" />
        <text x={nowX} y={MARGIN.top - 2} textAnchor="middle" fontSize="8.5" fill={COLORS.textFaint}>maintenant</text>

        {visibility.baseline && baselinePoints.length > 1 && (
          <path d={baselinePath} fill="none" stroke={COLORS.trace} strokeWidth="1" strokeDasharray="4 3" opacity="0.55" />
        )}
        {visibility.prediction && predictionPoints.length > 1 && (
          <path d={predictionPath} fill="none" stroke={COLORS.trace} strokeWidth="1.3" strokeDasharray="1 3" strokeLinecap="round" opacity="0.85" />
        )}
        {visibility.loss && <path d={actualLossPath} fill="none" stroke={COLORS.lossTrace} strokeWidth="1.3" strokeLinejoin="round" />}
        {visibility.traffic && <path d={actualTrafficPath} fill="none" stroke={COLORS.trace} strokeWidth="1.6" strokeLinejoin="round" />}
      </svg>
      <Legend visibility={visibility} onToggle={onToggle} current={current} />
    </div>
  );
}

function RangeSelector({ rangeKey, onRangeChange }) {
  return (
    <div className="range-selector">
      {RANGES.map((r) => (
        <button
          key={r.key}
          type="button"
          className={`range-btn${r.key === rangeKey ? " active" : ""}`}
          aria-pressed={r.key === rangeKey}
          onClick={() => onRangeChange(r.key)}
        >
          {r.label}
        </button>
      ))}
    </div>
  );
}

function Legend({ visibility, onToggle, current }) {
  return (
    <div className="chart-legend">
      <LegendChip label="Trafic" color={COLORS.trace} active={visibility.traffic} onClick={() => onToggle("traffic")} value={formatMbps(current.traffic)} />
      <LegendChip label="Pertes" color={COLORS.lossTrace} active={visibility.loss} onClick={() => onToggle("loss")} value={current.loss != null ? `${current.loss.toFixed(2)}%` : "—"} />
      <LegendChip label="Baseline" color={COLORS.textDim} dashed active={visibility.baseline} onClick={() => onToggle("baseline")} />
      <LegendChip label="Prédiction" color={COLORS.textDim} dotted active={visibility.prediction} onClick={() => onToggle("prediction")} />
    </div>
  );
}

function LegendChip({ label, color, active, onClick, dashed, dotted, value }) {
  return (
    <button type="button" className={`legend-chip${active ? " active" : ""}`} onClick={onClick} aria-pressed={active}>
      <svg width="14" height="8" aria-hidden="true">
        <line x1="0" y1="4" x2="14" y2="4" stroke={active ? color : COLORS.textFaint} strokeWidth="2" strokeDasharray={dashed ? "3 2" : dotted ? "0.5 2.5" : undefined} strokeLinecap="round" />
      </svg>
      {label}
      {value && <span className="legend-chip-value mono">{value}</span>}
    </button>
  );
}

// --- Tableau comparatif -------------------------------------------------

const formatGbpsBare = (v) => (v == null ? "—" : (v / 1000).toFixed(2));
const formatPercentBare = (v) => (v == null ? "—" : v.toFixed(2));

function TrendChip({ current, previous, formatValue, threshold }) {
  if (current == null) return <div className="trend-cell"><span className="trend-value mono">—</span></div>;
  if (previous == null) return <div className="trend-cell"><span className="trend-value mono">{formatValue(current)}</span></div>;
  const relative = previous !== 0 ? (current - previous) / Math.abs(previous) : current === 0 ? 0 : 1;
  const significant = Math.abs(relative) >= threshold;
  const dir = relative > 1e-4 ? "up" : relative < -1e-4 ? "down" : "flat";
  const arrow = dir === "up" ? "▲" : dir === "down" ? "▼" : "–";
  const color = !significant ? COLORS.textFaint : dir === "up" ? COLORS.trace : COLORS.coral;
  return (
    <div className="trend-cell">
      <span className="trend-value mono">{formatValue(current)}</span>
      <span className="trend-delta mono" style={{ color }}>{arrow} {(Math.abs(relative) * 100).toFixed(1)}%</span>
    </div>
  );
}

async function fetchNearest(outputId, targetDate) {
  const since = new Date(targetDate.getTime() - 35 * 60_000);
  const until = new Date(targetDate.getTime() + 10 * 60_000);
  try {
    const points = await api.history(outputId, since.toISOString(), until.toISOString());
    if (!points.length) return null;
    let best = null;
    let bestDist = Infinity;
    for (const p of points) {
      const d = Math.abs(new Date(p.timestamp).getTime() - targetDate.getTime());
      if (d < bestDist) { bestDist = d; best = p; }
    }
    return best;
  } catch {
    return null;
  }
}

function SummaryTable({ outputIds, thresholds }) {
  const [rows, setRows] = useState({});

  const refresh = useCallback(async () => {
    const now = new Date();
    const windows = [
      { key: "now", offset: 0 },
      { key: "prev", offset: 5 * 60_000 },
      { key: "day", offset: 24 * 3600_000 },
      { key: "week", offset: 7 * 24 * 3600_000 },
    ];
    const next = {};
    await Promise.all(
      outputIds.map(async (id) => {
        const perWindow = await Promise.all(
          windows.map(async (w) => {
            const target = new Date(now.getTime() - w.offset);
            const prevTarget = new Date(target.getTime() - 5 * 60_000);
            const [cur, prev] = await Promise.all([fetchNearest(id, target), fetchNearest(id, prevTarget)]);
            return { key: w.key, cur, prev };
          })
        );
        next[id] = perWindow;
      })
    );
    setRows(next);
  }, [outputIds.join(",")]);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 15000);
    return () => clearInterval(id);
  }, [refresh]);

  const windowMeta = [
    { key: "now", label: "Act", full: "Actuelle" },
    { key: "prev", label: "Préc", full: "Précédente" },
    { key: "day", label: "-1j", full: "Il y a un jour" },
    { key: "week", label: "-1s", full: "Il y a une semaine" },
  ];

  return (
    <table className="nm-table">
      <thead>
        <tr>
          <th rowSpan={2} className="nm-table-id-head">Sortie</th>
          <th colSpan={4} className="nm-table-group">Trafic (Gbps)</th>
          <th colSpan={4} className="nm-table-group">Pertes (%)</th>
        </tr>
        <tr>
          {windowMeta.map((w) => <th key={"t" + w.key} title={w.full}>{w.label}</th>)}
          {windowMeta.map((w) => <th key={"l" + w.key} title={w.full}>{w.label}</th>)}
        </tr>
      </thead>
      <tbody>
        {outputIds.map((id) => {
          const perWindow = rows[id];
          return (
            <tr key={id}>
              <td className="nm-table-id">{id}</td>
              {windowMeta.map((w) => {
                const cell = perWindow?.find((r) => r.key === w.key);
                return (
                  <td key={"t" + w.key}>
                    <TrendChip current={cell?.cur?.traffic_mbps} previous={cell?.prev?.traffic_mbps} formatValue={formatGbpsBare} threshold={thresholds.traffic} />
                  </td>
                );
              })}
              {windowMeta.map((w) => {
                const cell = perWindow?.find((r) => r.key === w.key);
                return (
                  <td key={"l" + w.key}>
                    <TrendChip current={cell?.cur?.loss_percent} previous={cell?.prev?.loss_percent} formatValue={formatPercentBare} threshold={thresholds.loss} />
                  </td>
                );
              })}
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

// --- Panneau de configuration --------------------------------------------

function NumberField({ label, hint, value, onChange, min, max, step }) {
  return (
    <label className="settings-field">
      <span className="settings-field-label">{label}</span>
      <input
        type="number"
        className="settings-input mono"
        value={value}
        min={min} max={max} step={step}
        onChange={(e) => { const v = parseFloat(e.target.value); if (!Number.isNaN(v)) onChange(v); }}
      />
      {hint && <span className="settings-field-hint">{hint}</span>}
    </label>
  );
}

function SettingsPanel({ config, deltaThresholds, onDeltaThresholdsChange, onSave, saving, saveError }) {
  const [draft, setDraft] = useState(config.incident);
  useEffect(() => setDraft(config.incident), [config]);

  const orderIssue = !(draft.close_score < draft.warning_score && draft.warning_score < draft.open_score);

  return (
    <div className="settings-grid">
      <div className="settings-group">
        <h3>Moteur d'incidents (backend réel)</h3>
        <NumberField label="Seuil SUSPECT (warning_score)" value={draft.warning_score} min={0} max={1} step={0.01}
          onChange={(v) => setDraft({ ...draft, warning_score: v })} />
        <NumberField label="Seuil OPEN (open_score)" value={draft.open_score} min={0} max={1} step={0.01}
          onChange={(v) => setDraft({ ...draft, open_score: v })} />
        <NumberField label="Seuil CLOSED (close_score)" value={draft.close_score} min={0} max={1} step={0.01}
          onChange={(v) => setDraft({ ...draft, close_score: v })} />
        {orderIssue && <p className="settings-warning">Incohérent : il faut close_score &lt; warning_score &lt; open_score. L'enregistrement sera refusé par l'API (§21).</p>}
        {saveError && <p className="settings-warning">{saveError}</p>}
        <button type="button" className="settings-reset" disabled={saving} onClick={() => onSave(draft)}>
          {saving ? "Envoi à l'API…" : "Envoyer à l'API"}
        </button>
      </div>

      <div className="settings-group">
        <h3>Tableau comparatif (affichage local)</h3>
        <NumberField label="Seuil de coloration — Trafic" value={deltaThresholds.traffic * 100} min={0} max={100} step={1}
          onChange={(v) => onDeltaThresholdsChange({ ...deltaThresholds, traffic: v / 100 })} />
        <NumberField label="Seuil de coloration — Pertes" value={deltaThresholds.loss * 100} min={0} max={100} step={1}
          onChange={(v) => onDeltaThresholdsChange({ ...deltaThresholds, loss: v / 100 })} />
        <p className="settings-field-hint">Ces deux seuils ne concernent que l'affichage du tableau dans ce navigateur ; ils ne sont pas envoyés à l'API.</p>
      </div>
    </div>
  );
}

// --- Composant racine -----------------------------------------------------

export default function Dashboard() {
  const [outputs, setOutputs] = useState([]);
  const [incidents, setIncidents] = useState([]);
  const [config, setConfig] = useState(null);
  const [monitoringStatus, setMonitoringStatus] = useState(null);
  const [connectionError, setConnectionError] = useState(null);
  const [expandedCharts, setExpandedCharts] = useState({});
  const [visibility, setVisibility] = useState({});
  const [chartRange, setChartRange] = useState({});
  const [tableExpanded, setTableExpanded] = useState(true);
  const [settingsExpanded, setSettingsExpanded] = useState(false);
  const [deltaThresholds, setDeltaThresholds] = useState(DEFAULT_CONFIG_UPDATE_DELTA_THRESHOLDS);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(null);

  const ensureOutputDefaults = (ids) => {
    setExpandedCharts((prev) => { const next = { ...prev }; ids.forEach((id) => { if (!(id in next)) next[id] = true; }); return next; });
    setVisibility((prev) => { const next = { ...prev }; ids.forEach((id) => { if (!(id in next)) next[id] = { traffic: true, loss: true, baseline: false, prediction: false }; }); return next; });
    setChartRange((prev) => { const next = { ...prev }; ids.forEach((id) => { if (!(id in next)) next[id] = "12h"; }); return next; });
  };

  const refreshTopLevel = useCallback(async () => {
    try {
      const [outs, incs, status] = await Promise.all([api.outputs(), api.incidents(undefined, 100), api.monitoringStatus()]);
      setOutputs(outs);
      setIncidents(incs);
      setMonitoringStatus(status);
      setConnectionError(null);
      ensureOutputDefaults(outs.map((o) => o.output_id));
    } catch (err) {
      setConnectionError(`Impossible de joindre l'API (${err.message}). Vérifiez qu'elle tourne sur l'URL configurée (VITE_API_BASE_URL).`);
    }
  }, []);

  useEffect(() => {
    api.getConfig().then(setConfig).catch(() => {});
    refreshTopLevel();
    const id = setInterval(refreshTopLevel, 4000);
    return () => clearInterval(id);
  }, [refreshTopLevel]);

  const saveIncidentConfig = async (draft) => {
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
      setSaveError("Rejeté par l'API : " + err.message);
    } finally {
      setSaving(false);
    }
  };

  const toggleChart = (id) => setExpandedCharts((prev) => ({ ...prev, [id]: !prev[id] }));
  const toggleSeries = (id, key) => setVisibility((prev) => ({ ...prev, [id]: { ...prev[id], [key]: !prev[id][key] } }));

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

      <div className="nm-header">
        <div className="nm-title">Signal Room <span>· réseau, branché sur l'API réelle</span></div>
        <div className="nm-header-meta">
          <div className="nm-live"><span className="nm-live-dot" />{monitoringStatus?.running ? "live" : "arrêté"}</div>
          <div>{outputs.length} sorties · <span className={activeIncidents ? "nm-incident-count" : ""}>{activeIncidents} incident{activeIncidents !== 1 ? "s" : ""} actif{activeIncidents !== 1 ? "s" : ""}</span></div>
          <div className="nm-clock mono">{monitoringStatus ? formatClock(new Date(monitoringStatus.simulated_now)) : "—"}</div>
        </div>
      </div>

      <div className="nm-channels">
        {outputs.map((output) => {
          const id = output.output_id;
          const style = STATUS_STYLE[output.status] ?? STATUS_STYLE.normal;
          const isActive = output.status === "open" || output.status === "stale";
          const expanded = expandedCharts[id];
          return (
            <div key={id} className={`nm-channel${isActive ? " is-active" : ""}`} style={{ "--stripe": style.color, borderLeftColor: isActive ? style.color : COLORS.border }}>
              <div className="nm-channel-head" onClick={() => toggleChart(id)} role="button" tabIndex={0} onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") toggleChart(id); }}>
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
      </div>

      <div className="nm-panel nm-table-panel">
        <div className="nm-panel-head" onClick={() => setTableExpanded((v) => !v)} role="button" tabIndex={0} onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") setTableExpanded((v) => !v); }}>
          <h2>Comparatif temporel</h2>
          <button type="button" className="nm-collapse-btn" aria-expanded={tableExpanded} aria-label={tableExpanded ? "Replier le tableau" : "Déplier le tableau"} onClick={(e) => { e.stopPropagation(); setTableExpanded((v) => !v); }}>
            {tableExpanded ? "−" : "+"}
          </button>
        </div>
        {tableExpanded && <SummaryTable outputIds={outputs.map((o) => o.output_id)} thresholds={deltaThresholds} />}
      </div>

      <div className="nm-panel nm-table-panel">
        <div className="nm-panel-head" onClick={() => setSettingsExpanded((v) => !v)} role="button" tabIndex={0} onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") setSettingsExpanded((v) => !v); }}>
          <h2>Configuration</h2>
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
      </div>

      <div className="nm-panel">
        <h2>Incidents (récents et actifs)</h2>
        <div className="nm-log">
          {incidents.length === 0 && <div className="nm-log-empty">Aucun incident pour l'instant.</div>}
          {incidents.map((inc) => (
            <div className="nm-log-row" key={inc.id}>
              <span className="nm-log-time mono">{formatClock(new Date(inc.last_seen_at))}</span>
              <span className="nm-log-dot" style={{ background: (STATUS_STYLE[inc.status] ?? STATUS_STYLE.normal).color, marginTop: 4 }} />
              <span className="nm-log-msg">{inc.output_id} — {inc.incident_type} ({(STATUS_STYLE[inc.status] ?? STATUS_STYLE.normal).label}, score {inc.score.toFixed(2)})</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

const BASE_STYLE = `
  @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');

  .nm-root {
    background: ${COLORS.bg};
    background-image: repeating-linear-gradient(0deg, rgba(127,231,196,0.012) 0px, rgba(127,231,196,0.012) 1px, transparent 1px, transparent 3px);
    color: ${COLORS.text}; font-family: 'Space Grotesk', sans-serif; min-height: 100vh; padding: 28px 32px 48px; box-sizing: border-box;
  }
  .nm-root * { box-sizing: border-box; }
  .mono { font-family: 'JetBrains Mono', monospace; font-variant-numeric: tabular-nums; }

  .connection-error { max-width: 560px; margin: 60px auto; text-align: center; color: ${COLORS.textDim}; }
  .connection-error h1 { color: ${COLORS.text}; }
  .chart-loading { color: ${COLORS.textFaint}; font-size: 12px; padding: 20px 0; }
  .empty-range { color: ${COLORS.textFaint}; font-size: 12px; padding: 30px 10px; text-align: center; border: 1px dashed ${COLORS.border}; margin: 10px 0; }

  .nm-header { display: flex; align-items: baseline; justify-content: space-between; border-bottom: 1px solid ${COLORS.border}; padding-bottom: 18px; margin-bottom: 24px; flex-wrap: wrap; gap: 12px; }
  .nm-title { font-size: 22px; font-weight: 600; letter-spacing: -0.01em; }
  .nm-title span { color: ${COLORS.textDim}; font-weight: 400; }
  .nm-header-meta { display: flex; align-items: center; gap: 22px; font-size: 13px; color: ${COLORS.textDim}; }
  .nm-live { display: flex; align-items: center; gap: 7px; }
  .nm-live-dot { width: 7px; height: 7px; border-radius: 50%; background: ${COLORS.trace}; }
  @media (prefers-reduced-motion: no-preference) { .nm-live-dot { animation: pulse 2.2s ease-in-out infinite; } }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.35; } }
  .nm-incident-count { color: ${COLORS.coral}; font-weight: 600; }
  .nm-clock { font-size: 15px; color: ${COLORS.text}; }

  .nm-channels { display: flex; flex-direction: column; gap: 14px; margin-bottom: 28px; }
  .nm-channel { background: ${COLORS.panel}; border: 1px solid ${COLORS.border}; border-left: 3px solid ${COLORS.border}; }
  .nm-channel.is-active { box-shadow: 0 0 0 1px var(--stripe) inset, 0 0 28px -10px var(--stripe); }
  .nm-channel-head { display: flex; align-items: center; gap: 24px; padding: 14px 18px; cursor: pointer; flex-wrap: wrap; }
  .nm-channel-head:focus-visible { outline: 2px solid ${COLORS.trace}; outline-offset: -2px; }
  .nm-channel-id-block { display: flex; flex-direction: column; min-width: 130px; }
  .nm-card-id { font-size: 14px; font-weight: 600; }
  .nm-channel-readout { display: flex; flex-direction: column; align-items: flex-end; font-size: 15px; min-width: 100px; }
  .nm-readout-loss { font-size: 11px; color: ${COLORS.textDim}; }
  .nm-status-row { display: flex; align-items: center; gap: 8px; font-size: 12px; flex: 1; min-width: 140px; }
  .nm-status-dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
  .nm-collapse-btn { background: transparent; border: 1px solid ${COLORS.border}; color: ${COLORS.textDim}; width: 26px; height: 26px; font-size: 15px; line-height: 1; cursor: pointer; flex-shrink: 0; display: flex; align-items: center; justify-content: center; }
  .nm-collapse-btn:hover { border-color: ${COLORS.borderBright}; color: ${COLORS.text}; }
  .nm-collapse-btn:focus-visible { outline: 2px solid ${COLORS.trace}; outline-offset: 1px; }
  .nm-channel-body { padding: 4px 18px 18px; border-top: 1px solid ${COLORS.grid}; }

  .range-selector { display: flex; gap: 6px; margin: 12px 0 10px; }
  .range-btn { background: transparent; border: 1px solid ${COLORS.border}; color: ${COLORS.textFaint}; font-family: 'JetBrains Mono', monospace; font-size: 11px; padding: 4px 10px; cursor: pointer; }
  .range-btn.active { color: ${COLORS.text}; border-color: ${COLORS.trace}; }
  .range-btn:hover { border-color: ${COLORS.borderBright}; }

  .chart-legend { display: flex; gap: 8px; margin-top: 6px; flex-wrap: wrap; }
  .legend-chip { display: flex; align-items: center; gap: 6px; background: transparent; border: 1px solid ${COLORS.border}; color: ${COLORS.textFaint}; font-family: 'Space Grotesk', sans-serif; font-size: 11px; padding: 4px 9px; cursor: pointer; }
  .legend-chip.active { color: ${COLORS.text}; border-color: ${COLORS.borderBright}; }
  .legend-chip-value { color: ${COLORS.textDim}; margin-left: 2px; }

  .nm-panel { background: ${COLORS.panel}; border: 1px solid ${COLORS.border}; padding: 18px 20px; margin-bottom: 20px; }
  .nm-panel h2 { font-size: 13px; font-weight: 500; color: ${COLORS.textDim}; margin: 0; }
  .nm-panel-head { display: flex; align-items: center; justify-content: space-between; cursor: pointer; margin-bottom: 14px; }
  .nm-table-panel { overflow-x: auto; }

  .nm-table { border-collapse: collapse; width: 100%; margin-top: 14px; font-size: 12.5px; min-width: 760px; }
  .nm-table th, .nm-table td { padding: 7px 10px; text-align: right; border-bottom: 1px solid ${COLORS.grid}; }
  .nm-table thead tr:first-child th { padding-bottom: 4px; font-weight: 500; color: ${COLORS.textDim}; }
  .nm-table thead tr:last-child th { font-size: 10.5px; color: ${COLORS.textFaint}; font-weight: 400; padding-top: 0; }
  .nm-table-group { text-align: center; border-bottom: 1px solid ${COLORS.border}; }
  .nm-table-group:first-of-type { border-right: 1px solid ${COLORS.border}; }
  .nm-table tbody td:nth-child(5) { border-right: 1px solid ${COLORS.border}; }
  .nm-table-id-head { text-align: left; vertical-align: bottom; }
  .nm-table-id { text-align: left; font-weight: 600; }
  .trend-cell { display: flex; flex-direction: column; align-items: flex-end; gap: 1px; }
  .trend-value { font-size: 12.5px; }
  .trend-delta { font-size: 10.5px; }

  .settings-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; margin-top: 4px; }
  @media (max-width: 700px) { .settings-grid { grid-template-columns: 1fr; } }
  .settings-group h3 { font-size: 12px; font-weight: 500; color: ${COLORS.textDim}; margin: 0 0 12px; }
  .settings-field { display: flex; flex-direction: column; gap: 2px; margin-bottom: 14px; }
  .settings-field-label { font-size: 12px; color: ${COLORS.text}; }
  .settings-field-hint { font-size: 10.5px; color: ${COLORS.textFaint}; }
  .settings-input { background: ${COLORS.bg}; border: 1px solid ${COLORS.border}; color: ${COLORS.text}; padding: 5px 8px; font-size: 12.5px; width: 100px; margin-top: 3px; }
  .settings-warning { font-size: 11.5px; color: ${COLORS.amber}; border: 1px solid ${COLORS.amber}; padding: 8px 10px; margin: 4px 0; }
  .settings-reset { background: transparent; border: 1px solid ${COLORS.border}; color: ${COLORS.textDim}; font-family: 'Space Grotesk', sans-serif; font-size: 12px; padding: 7px 14px; cursor: pointer; }
  .settings-reset:hover { border-color: ${COLORS.borderBright}; color: ${COLORS.text}; }
  .settings-reset:disabled { opacity: 0.5; cursor: default; }

  .nm-log { display: flex; flex-direction: column; gap: 9px; max-height: 340px; overflow-y: auto; }
  .nm-log-row { display: flex; gap: 10px; align-items: baseline; font-size: 12.5px; }
  .nm-log-time { color: ${COLORS.textFaint}; flex-shrink: 0; width: 40px; }
  .nm-log-dot { width: 5px; height: 5px; border-radius: 50%; flex-shrink: 0; margin-top: 4px; }
  .nm-log-empty { color: ${COLORS.textFaint}; font-size: 12.5px; }
`;
