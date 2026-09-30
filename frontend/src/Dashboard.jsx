import { useState, useEffect, useRef, useCallback, useMemo } from "react";
import { api, isAbortError } from "./api.js";
import {
  backoffDelay,
  downsampleMinMax,
  formatAge,
  formatAxisLabel,
  formatClock,
  formatMbps,
  isPageHidden,
  nearestIndexByTime,
  niceDomain,
} from "./lib.js";

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

// --- Graphe détaillé --------------------------------------------------

const CHART_W = 640;
const CHART_H = 230;
// left: 66 pour que l'étiquette « 32.00 Gbps » tienne dans le SVG.
const MARGIN = { top: 16, right: 52, bottom: 26, left: 66 };
const INNER_W = CHART_W - MARGIN.left - MARGIN.right;
const INNER_H = CHART_H - MARGIN.top - MARGIN.bottom;

function EmptyRange({ label }) {
  return (
    <div className="empty-range">
      Pas encore assez d'historique réel pour "{label}" — le système n'a
      commencé à mesurer qu'à son démarrage. Réessayez plus tard, ou choisissez
      une plage plus courte.
    </div>
  );
}

function DetailChart({ outputId, rangeKey, onRangeChange, visibility, onToggle, current, simNowRef }) {
  const [history, setHistory] = useState([]);
  const [baseline, setBaseline] = useState([]);
  const [prediction, setPrediction] = useState([]);
  const [loading, setLoading] = useState(true);
  const [fetchError, setFetchError] = useState(null);
  const [lastOk, setLastOk] = useState(null);
  const [cursor, setCursor] = useState(null);
  const controllerRef = useRef(null);
  const range = RANGES.find((r) => r.key === rangeKey) ?? RANGES[0];

  const refresh = useCallback(async () => {
    if (isPageHidden()) return;
    controllerRef.current?.abort(); // une requête en cours devient inutile
    const controller = new AbortController();
    controllerRef.current = controller;
    const { signal } = controller;

    // Fenêtre en horloge simulée du serveur (simNowRef), pas en `new Date()` : la simulation avance
    // 150× plus vite que le temps réel. `until` est omis : le serveur prend son horloge.
    const simNow = simNowRef.current ?? new Date();
    const since = new Date(simNow.getTime() - range.spanMs).toISOString();
    try {
      const [hist, fc, ahead] = await Promise.all([
        api.history(outputId, since, undefined, signal),
        visibility.baseline ? api.forecast(outputId, since, undefined, signal) : Promise.resolve([]),
        visibility.prediction ? api.forecastAhead(outputId, 35, signal) : Promise.resolve([]),
      ]);
      setHistory(hist);
      setBaseline(fc);
      setPrediction(ahead);
      setFetchError(null);
      setLastOk(new Date());
    } catch (err) {
      if (isAbortError(err)) return;
      setFetchError(err.message ?? String(err));
    } finally {
      if (!signal.aborted) setLoading(false);
    }
  }, [outputId, rangeKey, visibility.baseline, visibility.prediction, simNowRef]);

  useEffect(() => {
    setLoading(true);
    refresh();
    const id = setInterval(refresh, 5000);
    return () => {
      clearInterval(id);
      controllerRef.current?.abort();
    };
  }, [refresh]);

  // Échelles et chemins SVG calculés une fois par jeu de données : bouger le curseur ne les recalcule pas.
  const geometry = useMemo(() => {
    if (history.length < 2) return null;
    const points = history.map((h) => ({ date: new Date(h.timestamp), traffic: h.traffic_mbps, loss: h.loss_percent }));
    const baselinePoints = baseline.map((f) => ({ date: new Date(f.timestamp), traffic: f.predicted_traffic_mbps }));
    const predictionPoints = prediction.map((f) => ({ date: new Date(f.timestamp), traffic: f.predicted_traffic_mbps }));

    const tStart = points[0].date.getTime();
    const last = points[points.length - 1];
    const tEnd = (predictionPoints.length ? predictionPoints[predictionPoints.length - 1].date : last.date).getTime();
    const span = Math.max(1, tEnd - tStart);
    const xAt = (date) => MARGIN.left + ((date.getTime() - tStart) / span) * INNER_W;

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

    // Min/max par tranche : un mois fait 8 640 points pour ~520 unités de large, et les pics sont conservés.
    const shownTraffic = downsampleMinMax(points, (p) => p.traffic, INNER_W);
    const shownLoss = downsampleMinMax(points, (p) => p.loss, INNER_W);
    const shownBaseline = downsampleMinMax(baselinePoints, (p) => p.traffic, INNER_W);
    const pathFor = (pts, valueOf, yFn) =>
      pts.map((p, i) => `${i === 0 ? "M" : "L"} ${xAt(p.date).toFixed(1)} ${yFn(valueOf(p)).toFixed(1)}`).join(" ");

    const nTimeLabels = 5;
    const timeIdx = Array.from({ length: nTimeLabels }, (_, i) => Math.round((points.length - 1) * (i / (nTimeLabels - 1))));

    let minT = Infinity;
    let maxT = -Infinity;
    for (const p of points) {
      if (p.traffic < minT) minT = p.traffic;
      if (p.traffic > maxT) maxT = p.traffic;
    }

    return {
      points, last, tStart, span, xAt, yTraffic, yLoss, tMin, tMax, lMin, lMax, shownTraffic, timeIdx, minT, maxT,
      nowX: xAt(last.date),
      trafficPath: pathFor(shownTraffic, (p) => p.traffic, yTraffic),
      lossPath: pathFor(shownLoss, (p) => p.loss, yLoss),
      baselinePath: pathFor(shownBaseline, (p) => p.traffic, yTraffic),
      predictionPath: pathFor(predictionPoints, (p) => p.traffic, yTraffic),
      baselineCount: baselinePoints.length,
      predictionCount: predictionPoints.length,
    };
  }, [history, baseline, prediction, visibility.traffic, visibility.loss, visibility.baseline, visibility.prediction, current.traffic, current.loss]);

  if (loading && history.length === 0) {
    return <div className="chart-loading" role="status">Chargement…</div>;
  }
  if (!geometry) {
    return (
      <div>
        <RangeSelector rangeKey={rangeKey} onRangeChange={onRangeChange} />
        <EmptyRange label={range.label} />
        <StaleNote error={fetchError} lastOk={lastOk} />
        <Legend visibility={visibility} onToggle={onToggle} current={current} />
      </div>
    );
  }

  const g = geometry;
  const rows = [0, 1 / 3, 2 / 3, 1];
  const cursorIndex = cursor == null ? null : Math.min(cursor, g.shownTraffic.length - 1);
  const cursorPoint = cursorIndex == null ? null : g.shownTraffic[cursorIndex];
  const readout = cursorPoint
    ? `${formatAxisLabel(cursorPoint.date, rangeKey, g.last.date)} · ${formatMbps(cursorPoint.traffic)} · ${cursorPoint.loss.toFixed(2)} % de pertes`
    : "Survolez le graphe, ou utilisez le curseur (flèches du clavier), pour lire une valeur exacte.";
  const summary =
    `Trafic et pertes de ${outputId} sur ${range.label} : ${g.points.length} mesures, ` +
    `trafic de ${formatMbps(g.minT)} à ${formatMbps(g.maxT)}, dernière valeur ${formatMbps(g.last.traffic)}, ` +
    `pertes ${g.last.loss.toFixed(2)} %.`;

  const onMove = (e) => {
    const rect = e.currentTarget.getBoundingClientRect();
    if (!rect.width) return;
    const x = ((e.clientX - rect.left) / rect.width) * CHART_W;
    const t = g.tStart + ((x - MARGIN.left) / INNER_W) * g.span;
    setCursor(nearestIndexByTime(g.shownTraffic, t));
  };

  return (
    <div>
      <RangeSelector rangeKey={rangeKey} onRangeChange={onRangeChange} />
      <div className="chart-wrap">
        <svg
          viewBox={`0 0 ${CHART_W} ${CHART_H}`}
          style={{ width: "100%", height: "auto", display: "block" }}
          role="img"
          aria-label={summary}
          onMouseMove={onMove}
          onMouseLeave={() => setCursor(null)}
        >
          {rows.map((f) => {
            const py = MARGIN.top + f * INNER_H;
            const tVal = g.tMax - f * (g.tMax - g.tMin);
            const lVal = g.lMax - f * (g.lMax - g.lMin);
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

          {g.timeIdx.map((i) => (
            <text key={i} x={g.xAt(g.points[i].date)} y={CHART_H - 6} textAnchor="middle" fontSize="8.5" fill={COLORS.textFaint} fontFamily="JetBrains Mono, monospace">
              {formatAxisLabel(g.points[i].date, rangeKey, g.last.date)}
            </text>
          ))}

          <line x1={g.nowX} x2={g.nowX} y1={MARGIN.top} y2={MARGIN.top + INNER_H} stroke={COLORS.borderBright} strokeWidth="1" strokeDasharray="2 3" />
          <text x={g.nowX} y={MARGIN.top - 5} textAnchor={g.nowX > CHART_W - MARGIN.right - 30 ? "end" : "middle"} fontSize="8.5" fill={COLORS.textFaint}>
            dernière mesure
          </text>

          {visibility.baseline && g.baselineCount > 1 && (
            <path d={g.baselinePath} fill="none" stroke={COLORS.trace} strokeWidth="1" strokeDasharray="4 3" opacity="0.55" />
          )}
          {visibility.prediction && g.predictionCount > 1 && (
            <path d={g.predictionPath} fill="none" stroke={COLORS.trace} strokeWidth="1.3" strokeDasharray="1 3" strokeLinecap="round" opacity="0.85" />
          )}
          {visibility.loss && <path d={g.lossPath} fill="none" stroke={COLORS.lossTrace} strokeWidth="1.3" strokeLinejoin="round" />}
          {visibility.traffic && <path d={g.trafficPath} fill="none" stroke={COLORS.trace} strokeWidth="1.6" strokeLinejoin="round" />}

          {cursorPoint && (
            <g data-testid="chart-cursor" pointerEvents="none">
              <line x1={g.xAt(cursorPoint.date)} x2={g.xAt(cursorPoint.date)} y1={MARGIN.top} y2={MARGIN.top + INNER_H} stroke={COLORS.text} strokeWidth="1" opacity="0.5" />
              {visibility.traffic && <circle cx={g.xAt(cursorPoint.date)} cy={g.yTraffic(cursorPoint.traffic)} r="3" fill={COLORS.trace} />}
              {visibility.loss && <circle cx={g.xAt(cursorPoint.date)} cy={g.yLoss(cursorPoint.loss)} r="3" fill={COLORS.lossTrace} />}
            </g>
          )}
        </svg>
        {/* Curseur clavier : un <input type="range"> natif (flèches, Début/Fin, aria-valuetext). */}
        <input
          type="range"
          className="chart-cursor-input"
          min={0}
          max={g.shownTraffic.length - 1}
          value={cursorIndex ?? g.shownTraffic.length - 1}
          onChange={(e) => setCursor(Number(e.target.value))}
          aria-label={`Parcourir les valeurs du graphe de ${outputId}`}
          aria-valuetext={readout}
        />
      </div>
      <div className="chart-readout mono" data-testid="chart-readout">{readout}</div>
      <StaleNote error={fetchError} lastOk={lastOk} />
      <Legend visibility={visibility} onToggle={onToggle} current={current} />
    </div>
  );
}

function StaleNote({ error, lastOk }) {
  if (!error) return null;
  return (
    <div role="status" className="chart-stale">
      Rafraîchissement impossible ({error}){lastOk ? ` — données affichées de ${formatClock(lastOk)}` : ""}.
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

// Décalages (secondes avant la dernière mesure) des quatre colonnes.
const SNAPSHOT_OFFSETS = { now: 0, prev: 300, day: 86400, week: 604800 };

function SummaryTable({ outputIds, thresholds }) {
  const [rows, setRows] = useState({});
  const [error, setError] = useState(null);
  const controllerRef = useRef(null);
  const idsKey = outputIds.join(",");

  // Une requête par sortie : le serveur calcule les mesures voisines, dans son horloge.
  const refresh = useCallback(async () => {
    if (isPageHidden()) return;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    try {
      const entries = await Promise.all(
        idsKey.split(",").map(async (id) => [id, await api.snapshots(id, Object.values(SNAPSHOT_OFFSETS), controller.signal)])
      );
      setRows(Object.fromEntries(entries));
      setError(null);
    } catch (err) {
      if (isAbortError(err)) return;
      setError(err.message ?? String(err));
    }
  }, [idsKey]);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 15000);
    return () => {
      clearInterval(id);
      controllerRef.current?.abort();
    };
  }, [refresh]);

  const windowMeta = [
    { key: "now", label: "Act", full: "Actuelle" },
    { key: "prev", label: "Préc", full: "Précédente" },
    { key: "day", label: "-1j", full: "Il y a un jour" },
    { key: "week", label: "-1s", full: "Il y a une semaine" },
  ];
  const cellFor = (perOutput, w) => perOutput?.find((snap) => snap.offset_seconds === SNAPSHOT_OFFSETS[w.key]);

  return (
    <>
      {error && <div role="status" className="chart-stale">Mise à jour du tableau impossible ({error}).</div>}
      <table className="nm-table">
        <caption className="sr-only">Comparatif temporel du trafic et des pertes, par sortie</caption>
        <thead>
          <tr>
            <th rowSpan={2} className="nm-table-id-head" scope="col">Sortie</th>
            <th colSpan={4} className="nm-table-group" scope="colgroup">Trafic (Gbps)</th>
            <th colSpan={4} className="nm-table-group" scope="colgroup">Pertes (%)</th>
          </tr>
          <tr>
            {windowMeta.map((w) => <th key={"t" + w.key} title={w.full} scope="col"><abbr title={w.full}>{w.label}</abbr></th>)}
            {windowMeta.map((w) => <th key={"l" + w.key} title={w.full} scope="col"><abbr title={w.full}>{w.label}</abbr></th>)}
          </tr>
        </thead>
        <tbody>
          {outputIds.map((id) => {
            const perOutput = rows[id];
            return (
              <tr key={id}>
                <th scope="row" className="nm-table-id">{id}</th>
                {windowMeta.map((w) => {
                  const cell = cellFor(perOutput, w);
                  return (
                    <td key={"t" + w.key}>
                      <TrendChip current={cell?.current?.traffic_mbps} previous={cell?.previous?.traffic_mbps} formatValue={formatGbpsBare} threshold={thresholds.traffic} />
                    </td>
                  );
                })}
                {windowMeta.map((w) => {
                  const cell = cellFor(perOutput, w);
                  return (
                    <td key={"l" + w.key}>
                      <TrendChip current={cell?.current?.loss_percent} previous={cell?.previous?.loss_percent} formatValue={formatPercentBare} threshold={thresholds.loss} />
                    </td>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
    </>
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

// Ajoute les ids manquants sans changer l'identité de l'objet quand rien ne change (évite un re-rendu).
function withDefaults(prev, ids, makeDefault) {
  let next = prev;
  for (const id of ids) {
    if (id in prev) continue;
    if (next === prev) next = { ...prev };
    next[id] = makeDefault();
  }
  return next;
}

/** Âge de la dernière mise à jour réussie. */
function FreshnessBadge({ lastOk }) {
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, []);
  if (!lastOk) return null;
  const age = (Date.now() - lastOk.getTime()) / 1000;
  return (
    <div className={`nm-fresh${age > 15 ? " is-stale" : ""}`} title="Âge de la dernière mise à jour réussie">
      màj il y a {formatAge(age)}
    </div>
  );
}

export default function Dashboard() {
  const [outputs, setOutputs] = useState([]);
  const [incidents, setIncidents] = useState([]);
  const [config, setConfig] = useState(null);
  const [monitoringStatus, setMonitoringStatus] = useState(null);
  const [connectionError, setConnectionError] = useState(null);
  const [staleError, setStaleError] = useState(null);
  const [lastOk, setLastOk] = useState(null);
  const [expandedCharts, setExpandedCharts] = useState({});
  const [visibility, setVisibility] = useState({});
  const [chartRange, setChartRange] = useState({});
  const [tableExpanded, setTableExpanded] = useState(true);
  const [settingsExpanded, setSettingsExpanded] = useState(false);
  const [deltaThresholds, setDeltaThresholds] = useState(DEFAULT_CONFIG_UPDATE_DELTA_THRESHOLDS);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(null);

  // Horloge simulée du serveur, lue par les graphes pour calculer leurs fenêtres.
  const simNowRef = useRef(null);
  const hasDataRef = useRef(false);
  const configLoadedRef = useRef(false);

  const ensureOutputDefaults = (ids) => {
    setExpandedCharts((prev) => withDefaults(prev, ids, () => true));
    setVisibility((prev) => withDefaults(prev, ids, () => ({ traffic: true, loss: true, baseline: false, prediction: false })));
    setChartRange((prev) => withDefaults(prev, ids, () => "12h"));
  };

  // Renvoie true (succès), false (échec) ou null (annulé : composant démonté).
  const refreshTopLevel = useCallback(async (signal) => {
    if (isPageHidden()) return true;
    try {
      const [outs, incs, status, cfg] = await Promise.all([
        api.outputs(signal),
        api.incidents(undefined, 100, signal),
        api.monitoringStatus(signal),
        configLoadedRef.current ? Promise.resolve(null) : api.getConfig(signal),
      ]);
      simNowRef.current = new Date(status.simulated_now);
      if (cfg) {
        setConfig(cfg);
        configLoadedRef.current = true;
      }
      setOutputs(outs);
      setIncidents(incs);
      setMonitoringStatus(status);
      ensureOutputDefaults(outs.map((o) => o.output_id));
      hasDataRef.current = true;
      setConnectionError(null);
      setStaleError(null);
      setLastOk(new Date());
      return true;
    } catch (err) {
      if (isAbortError(err)) return null;
      const message = err.message ?? String(err);
      if (hasDataRef.current) {
        // Des données sont déjà affichées : on les garde, avec un avertissement.
        setStaleError(message);
      } else {
        setConnectionError(`Impossible de joindre l'API (${message}). Vérifiez qu'elle tourne sur l'URL configurée (VITE_API_BASE_URL).`);
      }
      return false;
    }
  }, []);

  // Rafraîchissement périodique, avec recul exponentiel (4 s, 8 s, … 30 s) quand l'API échoue.
  useEffect(() => {
    let cancelled = false;
    let timer = null;
    let failures = 0;
    const controller = new AbortController();
    const tick = async () => {
      const ok = await refreshTopLevel(controller.signal);
      if (cancelled) return;
      failures = ok === false ? failures + 1 : 0;
      timer = setTimeout(tick, backoffDelay(failures));
    };
    tick();
    return () => {
      cancelled = true;
      clearTimeout(timer);
      controller.abort();
    };
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

  if (connectionError && !hasDataRef.current) {
    return (
      <div className="nm-root">
        <style>{BASE_STYLE}</style>
        <div className="connection-error" role="alert">
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
        <p className="chart-loading" role="status">Connexion à l'API…</p>
      </div>
    );
  }

  const activeIncidents = outputs.filter((o) => o.status === "open").length;
  const scoreLabel = (inc) =>
    inc.current_score != null && Math.abs(inc.current_score - inc.score) > 0.005
      ? `score max ${inc.score.toFixed(2)}, actuel ${inc.current_score.toFixed(2)}`
      : `score ${inc.score.toFixed(2)}`;

  return (
    <div className="nm-root">
      <style>{BASE_STYLE}</style>
      <a className="skip-link" href="#main">Aller au contenu</a>

      <header className="nm-header">
        <h1 className="nm-title">Signal Room <span>· réseau, branché sur l'API réelle</span></h1>
        <div className="nm-header-meta">
          <div className="nm-live"><span className="nm-live-dot" aria-hidden="true" />{monitoringStatus?.running ? "live" : "arrêté"}</div>
          <div>{outputs.length} sorties · <span className={activeIncidents ? "nm-incident-count" : ""}>{activeIncidents} incident{activeIncidents !== 1 ? "s" : ""} actif{activeIncidents !== 1 ? "s" : ""}</span></div>
          <div className="nm-clock mono" title="Horloge simulée du serveur">{monitoringStatus ? formatClock(new Date(monitoringStatus.simulated_now)) : "—"}</div>
          <FreshnessBadge lastOk={lastOk} />
        </div>
      </header>

      {staleError && (
        <div role="alert" className="nm-banner">
          Connexion à l'API perdue — les données affichées peuvent être périmées ({staleError}).
        </div>
      )}

      <main id="main" tabIndex={-1}>
        <section className="nm-channels" aria-labelledby="h-outputs">
          <h2 id="h-outputs" className="sr-only">Sorties</h2>
          {outputs.map((output) => {
            const id = output.output_id;
            const style = STATUS_STYLE[output.status] ?? STATUS_STYLE.normal;
            const isActive = output.status === "open" || output.status === "stale";
            const expanded = expandedCharts[id];
            return (
              <div key={id} className={`nm-channel${isActive ? " is-active" : ""}`} style={{ "--stripe": style.color, borderLeftColor: isActive ? style.color : COLORS.border }}>
                {/* Souris : toute la barre est cliquable. Clavier et lecteurs d'écran : le <button> seul. */}
                <div className="nm-channel-head" onClick={() => toggleChart(id)}>
                  <div className="nm-channel-id-block">
                    <span className="nm-card-id">{id}</span>
                  </div>
                  <div className="nm-channel-readout">
                    <span className="mono">{formatMbps(output.latest_sample?.traffic_mbps)}</span>
                    <span className="mono nm-readout-loss">{output.latest_sample ? `${output.latest_sample.loss_percent.toFixed(2)}% perte` : "—"}</span>
                  </div>
                  <div className="nm-status-row">
                    <span className="nm-status-dot" style={{ background: style.color }} aria-hidden="true" />
                    <span style={{ color: style.color, fontWeight: 600 }}>{style.label}</span>
                  </div>
                  <button type="button" className="nm-collapse-btn" aria-expanded={!!expanded} aria-label={expanded ? `Replier ${id}` : `Déplier ${id}`}
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
                      simNowRef={simNowRef}
                    />
                  </div>
                )}
              </div>
            );
          })}
        </section>

        <section className="nm-panel" aria-labelledby="h-compare">
          <div className="nm-panel-head" onClick={() => setTableExpanded((v) => !v)}>
            <h2 id="h-compare">Comparatif temporel</h2>
            <button type="button" className="nm-collapse-btn" aria-expanded={tableExpanded} aria-label={tableExpanded ? "Replier le tableau" : "Déplier le tableau"} onClick={(e) => { e.stopPropagation(); setTableExpanded((v) => !v); }}>
              {tableExpanded ? "−" : "+"}
            </button>
          </div>
          {/* overflow-x ici plutôt que sur le panneau : le titre ne défile pas avec le tableau. */}
          {tableExpanded && (
            <div className="nm-table-scroll">
              <SummaryTable outputIds={outputs.map((o) => o.output_id)} thresholds={deltaThresholds} />
            </div>
          )}
        </section>

        <section className="nm-panel" aria-labelledby="h-config">
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

        <section className="nm-panel" aria-labelledby="h-incidents">
          <h2 id="h-incidents">Incidents (récents et actifs)</h2>
          {/* role="log" : les nouveaux incidents sont annoncés aux lecteurs d'écran. */}
          <div className="nm-log" role="log" aria-live="polite" aria-relevant="additions" aria-label="Journal des incidents">
            {incidents.length === 0 && <div className="nm-log-empty">Aucun incident pour l'instant.</div>}
            {incidents.map((inc) => {
              const st = STATUS_STYLE[inc.status] ?? STATUS_STYLE.normal;
              return (
                <div className="nm-log-row" key={inc.id}>
                  <span className="nm-log-time mono">{formatClock(new Date(inc.last_seen_at))}</span>
                  <span className="nm-log-dot" style={{ background: st.color }} aria-hidden="true" />
                  <span className="nm-log-msg">{inc.output_id} — {inc.incident_type} ({st.label}, {scoreLabel(inc)})</span>
                </div>
              );
            })}
          </div>
        </section>
      </main>
    </div>
  );
}

const BASE_STYLE = `
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
  .nm-title { font-size: 22px; font-weight: 600; letter-spacing: -0.01em; margin: 0; }
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
  .nm-table-scroll { overflow-x: auto; }

  .nm-table { border-collapse: collapse; width: 100%; margin-top: 14px; font-size: 12.5px; min-width: 760px; }
  .nm-table th, .nm-table td { padding: 7px 10px; text-align: right; border-bottom: 1px solid ${COLORS.grid}; }
  .nm-table thead tr:first-child th { padding-bottom: 4px; font-weight: 500; color: ${COLORS.textDim}; }
  .nm-table thead tr:last-child th { font-size: 10.5px; color: ${COLORS.textFaint}; font-weight: 400; padding-top: 0; }
  .nm-table-group { text-align: center; border-bottom: 1px solid ${COLORS.border}; }
  .nm-table-group:first-of-type { border-right: 1px solid ${COLORS.border}; }
  .nm-table tbody td:nth-child(5) { border-right: 1px solid ${COLORS.border}; }
  .nm-table-id-head { text-align: left; vertical-align: bottom; }
  .nm-table-id { text-align: left; font-weight: 600; }
  .nm-table caption.sr-only { caption-side: top; }
  .nm-table abbr { text-decoration: none; }
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

  /* --- Accessibilité --- */
  .sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
  .skip-link { position: absolute; left: 12px; top: -48px; background: ${COLORS.trace}; color: ${COLORS.bg}; padding: 8px 14px; z-index: 10; font-weight: 600; text-decoration: none; }
  .skip-link:focus { top: 12px; }
  main:focus { outline: none; }
  /* Focus visible sur tous les contrôles. */
  .nm-root :is(button, a, input, select, textarea, [tabindex]):focus-visible { outline: 2px solid ${COLORS.trace}; outline-offset: 2px; }
  .chart-wrap { position: relative; }
  .chart-wrap:focus-within { outline: 1px solid ${COLORS.borderBright}; outline-offset: 2px; }
  .chart-cursor-input { position: absolute; width: 1px; height: 1px; opacity: 0; margin: 0; pointer-events: none; }
  .chart-readout { min-height: 18px; margin: 6px 0 2px; font-size: 12px; color: ${COLORS.textDim}; }

  /* --- Fraîcheur et erreurs de rafraîchissement --- */
  .nm-fresh { font-size: 12px; color: ${COLORS.textFaint}; }
  .nm-fresh.is-stale { color: ${COLORS.amber}; font-weight: 600; }
  .nm-banner { border: 1px solid ${COLORS.amber}; color: ${COLORS.amber}; padding: 10px 14px; margin-bottom: 20px; font-size: 13px; }
  .chart-stale { color: ${COLORS.amber}; font-size: 12px; margin: 6px 0; }

  /* --- Petits écrans --- */
  @media (max-width: 720px) {
    .nm-root { padding: 18px 14px 36px; }
    .nm-header-meta { gap: 12px; flex-wrap: wrap; }
    .nm-channel-head { gap: 12px; padding: 12px; }
    .nm-channel-id-block, .nm-channel-readout, .nm-status-row { min-width: 0; }
    .nm-panel { padding: 14px 12px; }
  }
`;
