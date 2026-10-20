import { useState, useEffect, useRef, useCallback, type KeyboardEvent, type MouseEvent } from "react";
import { api, currentSession, hasApiToken, login, logout, type Session } from "./api";
import { decimate, mergeSeries, nearestIndex, pointerToTime, type Point } from "./chart";
import { usePolling } from "./polling";
import type {
  Alert, AppConfigView, AvailabilityReport, ForecastPoint, Incident, MaintenanceWindow, MonitoringStatus, OutputSummary, PrecisionReport,
  TrafficSample, Verdict,
} from "./types";

type Visibility = { traffic: boolean; loss: boolean; baseline: boolean; prediction: boolean };
type Current = { traffic: number | null | undefined; loss: number | null | undefined };
type Thresholds = { traffic: number; loss: number };
type IncidentDraft = AppConfigView["incident"];

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
  // Contrastes WCAG AA (≥ 4,5:1) sur les fonds de panneau : #5E7C72 (4,07) et #3C544D (2,27) échouaient (axe-core).
  textDim: "#7C9A90",
  textFaint: "#718C81",
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

const MAX_CHART_POINTS = 720; // ~1,3 point par pixel utile : au-delà, on décime (pics et creux conservés)

const DEFAULT_CONFIG_UPDATE_DELTA_THRESHOLDS = { traffic: 0.05, loss: 0.1 };

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

function formatMbps(v: number | null | undefined): string {
  if (v == null) return "—";
  if (v >= 1000) return (v / 1000).toFixed(2) + " Gbps";
  return v.toFixed(1) + " Mbps";
}

function formatClock(date: Date): string {
  return date.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
}

function formatAxisLabel(date: Date, rangeKey: string, now: Date): string {
  if (rangeKey === "12h" || rangeKey === "1d") return formatClock(date);
  const daysAgo = Math.round((now.getTime() - date.getTime()) / (24 * 3600_000));
  return daysAgo <= 0 ? formatClock(date) : `-${daysAgo}j ${formatClock(date)}`;
}

// --- Graphe détaillé --------------------------------------------------

const CHART_W = 640;
const CHART_H = 230;
// `left` laisse la place au libellé le plus long de l'axe (« 1.22 Gbps » en 9 px) : à 50 il était tronqué.
const MARGIN = { top: 12, right: 52, bottom: 26, left: 68 };
const INNER_W = CHART_W - MARGIN.left - MARGIN.right;
const INNER_H = CHART_H - MARGIN.top - MARGIN.bottom;

function niceDomain(values: number[], paddingFrac = 0.12, floorAtZero = false): [number, number] {
  if (!values.length) return [0, 1];
  let min = Math.min(...values);
  let max = Math.max(...values);
  if (min === max) { min -= 1; max += 1; }
  const pad = (max - min) * paddingFrac;
  return [floorAtZero ? Math.max(0, min - pad) : min - pad, max + pad];
}

function EmptyRange({ label }: { label: string }) {
  return (
    <div className="empty-range">
      Pas encore assez d'historique réel pour "{label}" — le système n'a
      commencé à mesurer qu'à son démarrage. Réessayez plus tard, ou choisissez
      une plage plus courte.
    </div>
  );
}

interface DetailChartProps {
  outputId: string;
  rangeKey: string;
  onRangeChange: (key: string) => void;
  visibility: Visibility;
  onToggle: (key: keyof Visibility) => void;
  current: Current;
}

export function DetailChart({ outputId, rangeKey, onRangeChange, visibility, onToggle, current }: DetailChartProps) {
  const [history, setHistory] = useState<TrafficSample[]>([]);
  const [baseline, setBaseline] = useState<ForecastPoint[]>([]);
  const [prediction, setPrediction] = useState<ForecastPoint[]>([]);
  const [loading, setLoading] = useState(true);
  const [hover, setHover] = useState<number | null>(null); // indice du point survolé / focalisé (dans la série affichée)
  const range = RANGES.find((r) => r.key === rangeKey) ?? RANGES[0];

  // Séries affichées, aussi tenues dans des refs : un rafraîchissement ne demande à l'API que les
  // points postérieurs au dernier connu (quelques octets) au lieu de re-télécharger toute la fenêtre.
  const historyRef = useRef<TrafficSample[]>([]);
  const baselineRef = useRef<ForecastPoint[]>([]);
  const generation = useRef(0); // invalide les réponses en vol d'une fenêtre ou d'une sortie abandonnée

  useEffect(() => {
    generation.current += 1;
    historyRef.current = [];
    baselineRef.current = [];
    setHistory([]);
    setBaseline([]);
    setLoading(true);
  }, [outputId, rangeKey]);

  useEffect(() => {
    if (!visibility.baseline) {
      baselineRef.current = [];
      setBaseline([]);
    }
  }, [visibility.baseline]);

  const refresh = useCallback(async () => {
    // Fenêtres relatives à l'horloge du SERVEUR (`span_hours`, ou `since` = dernier point connu) :
    // l'horloge simulée du backend n'est pas celle du navigateur, construire since/until avec
    // `new Date()` ne retrouvait presque aucun échantillon.
    const gen = generation.current;
    const spanHours = range.spanMs / 3600_000;
    const lastHistory = historyRef.current.at(-1)?.timestamp;
    const lastBaseline = baselineRef.current.at(-1)?.timestamp;
    try {
      const [hist, fc, ahead] = await Promise.all([
        lastHistory ? api.history(outputId, lastHistory) : api.history(outputId, undefined, undefined, spanHours),
        !visibility.baseline
          ? Promise.resolve([])
          : lastBaseline ? api.forecast(outputId, lastBaseline) : api.forecast(outputId, undefined, undefined, spanHours),
        visibility.prediction ? api.forecastAhead(outputId, 35) : Promise.resolve([]),
      ]);
      if (gen !== generation.current) return; // la fenêtre a changé pendant la requête
      historyRef.current = mergeSeries(historyRef.current, hist, range.spanMs);
      baselineRef.current = mergeSeries(baselineRef.current, fc, range.spanMs);
      setHistory(historyRef.current);
      setBaseline(baselineRef.current);
      setPrediction(ahead);
    } catch (err) {
      console.error("Échec du rafraîchissement du graphe", outputId, err);
    } finally {
      if (gen === generation.current) setLoading(false);
    }
  }, [outputId, rangeKey, visibility.baseline, visibility.prediction]);

  usePolling(refresh, 5000, [refresh]);

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

  const points = decimate(
    history.map((h) => ({ date: new Date(h.timestamp), traffic: h.traffic_mbps, loss: h.loss_percent })),
    MAX_CHART_POINTS,
  );
  const baselinePoints = decimate(baseline.map((f) => ({ date: new Date(f.timestamp), traffic: f.predicted_traffic_mbps, loss: 0 })), MAX_CHART_POINTS);
  const predictionPoints = prediction.map((f) => ({ date: new Date(f.timestamp), traffic: f.predicted_traffic_mbps, loss: null }));

  const tStart = points[0].date.getTime();
  const tEnd = (predictionPoints.length ? predictionPoints[predictionPoints.length - 1].date : points[points.length - 1].date).getTime();
  const span = Math.max(1, tEnd - tStart);
  const xAt = (date: Date) => MARGIN.left + ((date.getTime() - tStart) / span) * INNER_W;
  const nowX = xAt(points[points.length - 1].date);

  const trafficValues = [
    ...(visibility.traffic ? points.map((p) => p.traffic) : []),
    ...(visibility.baseline ? baselinePoints.map((p) => p.traffic) : []),
    ...(visibility.prediction ? predictionPoints.map((p) => p.traffic) : []),
  ];
  const lossValues = visibility.loss ? points.map((p) => p.loss) : [];
  const [tMin, tMax] = niceDomain(trafficValues.length ? trafficValues : [current.traffic || 1]);
  const [lMin, lMax] = niceDomain(lossValues.length ? lossValues : [current.loss || 0.1], 0.25, true);

  const yTraffic = (v: number) => MARGIN.top + INNER_H - ((v - tMin) / (tMax - tMin || 1)) * INNER_H;
  const yLoss = (v: number) => MARGIN.top + INNER_H - ((v - lMin) / (lMax - lMin || 1)) * INNER_H;
  const pathFor = (pts: { date: Date; v: number }[], yFn: (v: number) => number) => pts.map((p, i) => `${i === 0 ? "M" : "L"} ${xAt(p.date).toFixed(1)} ${yFn(p.v).toFixed(1)}`).join(" ");

  const actualTrafficPath = pathFor(points.map((p) => ({ date: p.date, v: p.traffic })), yTraffic);
  const actualLossPath = pathFor(points.map((p) => ({ date: p.date, v: p.loss })), yLoss);
  const baselinePath = pathFor(baselinePoints.map((p) => ({ date: p.date, v: p.traffic })), yTraffic);
  const predictionPath = pathFor(predictionPoints.map((p) => ({ date: p.date, v: p.traffic })), yTraffic);

  const hoverIdx = hover === null ? null : Math.min(hover, points.length - 1);
  const hoverPoint = hoverIdx === null ? null : points[hoverIdx];
  const hoverBaseline = hoverPoint && visibility.baseline ? baselinePoints[nearestIndex(baselinePoints, hoverPoint.date.getTime())] : null;
  const onPointerMove = (e: MouseEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const t = pointerToTime(e.clientX, rect, { chartWidth: CHART_W, marginLeft: MARGIN.left, innerWidth: INNER_W }, tStart, span);
    setHover(nearestIndex(points, t));
  };
  const onChartKeyDown = (e: KeyboardEvent<SVGSVGElement>) => {
    const last = points.length - 1;
    const at = hoverIdx ?? last;
    const next = ({ ArrowLeft: at - 1, ArrowRight: at + 1, Home: 0, End: last } as Record<string, number>)[e.key];
    if (e.key === "Escape") { setHover(null); return; }
    if (next === undefined) return;
    e.preventDefault();
    setHover(Math.max(0, Math.min(last, next)));
  };

  const rows = [0, 1 / 3, 2 / 3, 1];
  const nTimeLabels = 5;
  const timeIdx = Array.from({ length: nTimeLabels }, (_, i) => Math.round((points.length - 1) * (i / (nTimeLabels - 1))));

  return (
    <div>
      <RangeSelector rangeKey={rangeKey} onRangeChange={onRangeChange} />
      <svg
        className="chart-svg"
        viewBox={`0 0 ${CHART_W} ${CHART_H}`}
        role="group"
        tabIndex={0}
        aria-label={`Trafic et pertes de ${outputId} sur ${range.label}. Flèches gauche et droite : lire les valeurs point par point.`}
        aria-describedby={`readout-${outputId}`}
        style={{ width: "100%", height: "auto", display: "block" }}
        onMouseMove={onPointerMove}
        onMouseLeave={() => setHover(null)}
        onFocus={() => setHover((h) => h ?? points.length - 1)}
        onBlur={() => setHover(null)}
        onKeyDown={onChartKeyDown}
      >
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
            {formatAxisLabel(points[i].date, rangeKey, points[points.length - 1].date)}
          </text>
        ))}

        <line x1={nowX} x2={nowX} y1={MARGIN.top} y2={MARGIN.top + INNER_H} stroke={COLORS.borderBright} strokeWidth="1" strokeDasharray="2 3" />
        {/* À gauche du trait (textAnchor="end") : centré, il recouvrait le premier libellé de l'axe des pertes. */}
        <text x={nowX - 5} y={MARGIN.top - 2} textAnchor="end" fontSize="8.5" fill={COLORS.textFaint}>maintenant</text>

        {visibility.baseline && baselinePoints.length > 1 && (
          <path d={baselinePath} fill="none" stroke={COLORS.trace} strokeWidth="1" strokeDasharray="4 3" opacity="0.55" />
        )}
        {visibility.prediction && predictionPoints.length > 1 && (
          <path d={predictionPath} fill="none" stroke={COLORS.trace} strokeWidth="1.3" strokeDasharray="1 3" strokeLinecap="round" opacity="0.85" />
        )}
        {visibility.loss && <path d={actualLossPath} fill="none" stroke={COLORS.lossTrace} strokeWidth="1.3" strokeLinejoin="round" />}
        {visibility.traffic && <path d={actualTrafficPath} fill="none" stroke={COLORS.trace} strokeWidth="1.6" strokeLinejoin="round" />}
        {hoverPoint && (
          <g pointerEvents="none">
            <line x1={xAt(hoverPoint.date)} x2={xAt(hoverPoint.date)} y1={MARGIN.top} y2={MARGIN.top + INNER_H} stroke={COLORS.text} strokeWidth="1" opacity="0.5" />
            {visibility.traffic && <circle cx={xAt(hoverPoint.date)} cy={yTraffic(hoverPoint.traffic)} r="3.5" fill={COLORS.trace} />}
            {visibility.loss && <circle cx={xAt(hoverPoint.date)} cy={yLoss(hoverPoint.loss)} r="3.5" fill={COLORS.lossTrace} />}
          </g>
        )}
      </svg>
      <div id={`readout-${outputId}`} className="chart-readout mono" role="status" aria-live="polite">
        {hoverPoint
          ? `${hoverPoint.date.toLocaleString("fr-FR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })} — trafic ${formatMbps(hoverPoint.traffic)} · pertes ${hoverPoint.loss.toFixed(2)} %${hoverBaseline ? ` · attendu ${formatMbps(hoverBaseline.traffic)}` : ""}`
          : "Survolez le graphe, ou focalisez-le et utilisez les flèches, pour lire les valeurs."}
      </div>
      <Legend visibility={visibility} onToggle={onToggle} current={current} />
    </div>
  );
}

function RangeSelector({ rangeKey, onRangeChange }: Pick<DetailChartProps, "rangeKey" | "onRangeChange">) {
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

function Legend({ visibility, onToggle, current }: Pick<DetailChartProps, "visibility" | "onToggle" | "current">) {
  return (
    <div className="chart-legend">
      <LegendChip label="Trafic" color={COLORS.trace} active={visibility.traffic} onClick={() => onToggle("traffic")} value={formatMbps(current.traffic)} />
      <LegendChip label="Pertes" color={COLORS.lossTrace} active={visibility.loss} onClick={() => onToggle("loss")} value={current.loss != null ? `${current.loss.toFixed(2)}%` : "—"} />
      <LegendChip label="Baseline" color={COLORS.textDim} dashed active={visibility.baseline} onClick={() => onToggle("baseline")} />
      <LegendChip label="Prédiction" color={COLORS.textDim} dotted active={visibility.prediction} onClick={() => onToggle("prediction")} />
    </div>
  );
}

function LegendChip({ label, color, active, onClick, dashed, dotted, value }: {
  label: string; color: string; active: boolean; onClick: () => void; dashed?: boolean; dotted?: boolean; value?: string;
}) {
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

const formatGbpsBare = (v: number | null | undefined) => (v == null ? "—" : (v / 1000).toFixed(2));
const formatPercentBare = (v: number | null | undefined) => (v == null ? "—" : v.toFixed(2));

function TrendChip({ current, previous, formatValue, threshold }: {
  current: number | null | undefined; previous: number | null | undefined; formatValue: (v: number) => string; threshold: number;
}) {
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

type CellPair = { key: string; cur: TrafficSample | null; prev: TrafficSample | null };

/** Fenêtres du tableau, en minutes avant « maintenant » ; chacune compare sa valeur à celle de 5 min plus tôt. */
const SUMMARY_WINDOWS = [
  { key: "now", ago: 0 },
  { key: "prev", ago: 5 },
  { key: "day", ago: 24 * 60 },
  { key: "week", ago: 7 * 24 * 60 },
];

function SummaryTable({ outputIds, thresholds }: { outputIds: string[]; thresholds: Thresholds }) {
  const [rows, setRows] = useState<Record<string, CellPair[]>>({});

  const refresh = useCallback(async () => {
    if (outputIds.length === 0) return;
    // Sept instants distincts, résolus d'un coup par le serveur sur SON horloge (jamais celle du navigateur).
    const agos = [...new Set(SUMMARY_WINDOWS.flatMap((w) => [w.ago, w.ago + 5]))];
    try {
      const nearest = await api.nearest(agos);
      setRows(Object.fromEntries(outputIds.map((id) => [id, SUMMARY_WINDOWS.map((w) => ({
        key: w.key, cur: nearest[id]?.[String(w.ago)] ?? null, prev: nearest[id]?.[String(w.ago + 5)] ?? null,
      }))])));
    } catch {
      setRows({}); // « — » partout plutôt que des valeurs périmées
    }
  }, [outputIds.join(",")]);

  usePolling(refresh, 15000, [refresh]);

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

function NumberField({ label, hint, value, onChange, min, max, step }: {
  label: string; hint?: string; value: number; onChange: (v: number) => void; min: number; max: number; step: number;
}) {
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

const PERIODS = [7, 30, 90];

function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m ? `${h} h ${String(m).padStart(2, "0")}` : `${h} h`;
}

function AvailabilityPanel() {
  const [days, setDays] = useState(30);
  const [report, setReport] = useState<AvailabilityReport | null>(null);
  const [failed, setFailed] = useState(false);
  const refresh = useCallback(async () => {
    try {
      setReport(await api.availability(days));
      setFailed(false);
    } catch {
      setFailed(true);
    }
  }, [days]);
  usePolling(refresh, 60_000, [refresh]);

  return (
    <section className="nm-panel nm-table-panel" aria-labelledby="h-availability">
      <h2 id="h-availability">Disponibilité</h2>
      <div className="range-selector" role="group" aria-label="Période du rapport">
        {PERIODS.map((d) => (
          <button key={d} type="button" className={`range-btn${d === days ? " active" : ""}`} aria-pressed={d === days} onClick={() => setDays(d)}>
            {d} j
          </button>
        ))}
        <a className="range-btn" href={api.availabilityCsvUrl(days)} download>Exporter en CSV</a>
      </div>
      {failed && <p className="settings-warning" role="alert">Rapport indisponible.</p>}
      {report && (
        <div style={{ overflowX: "auto" }}>
          <table className="nm-table">
            <caption style={{ position: "absolute", left: -9999 }}>Disponibilité par sortie sur les {days} derniers jours</caption>
            <thead>
              <tr>
                <th scope="col">Sortie</th><th scope="col">Observé</th><th scope="col">Disponibilité</th><th scope="col">Indisponible</th>
                <th scope="col">Dégradé</th><th scope="col">Maintenance</th><th scope="col">Incidents</th><th scope="col">Détection</th>
                <th scope="col">Durée moy.</th><th scope="col">Couverture</th>
              </tr>
            </thead>
            <tbody>
              {report.outputs.map((o) => (
                <tr key={o.output_id}>
                  <th scope="row">{o.output_id}</th>
                  <td className="mono">{formatDuration(o.monitored_seconds)}</td>
                  <td className="mono">{o.availability_percent === null ? "—" : `${o.availability_percent.toFixed(3)} %`}</td>
                  <td className="mono">{formatDuration(o.downtime_seconds)}</td>
                  <td className="mono">{formatDuration(o.degraded_seconds)}</td>
                  <td className="mono">{formatDuration(o.maintenance_seconds)}</td>
                  <td className="mono">
                    {o.incidents}
                    {o.false_positives_excluded > 0 && <span className="nm-note"> (+{o.false_positives_excluded} faux positif{o.false_positives_excluded > 1 ? "s" : ""} exclu{o.false_positives_excluded > 1 ? "s" : ""})</span>}
                  </td>
                  <td className="mono">{formatDuration(o.mttd_seconds)}</td>
                  <td className="mono">{formatDuration(o.mttr_seconds)}</td>
                  <td className="mono">{o.coverage_percent === null ? "—" : `${o.coverage_percent.toFixed(1)} %`}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="settings-field-hint">
            Calculé sur la période <strong>observée</strong> (depuis la première mesure conservée, au plus tôt le début de la
            période choisie), pas sur la période demandée. Indisponibilité = incidents critiques, du début réel à la fin réelle ; le temps de maintenance planifiée est retiré de l'indisponibilité et du temps dû, et les incidents jugés faux positifs ne sont pas comptés.
            Couverture = part des mesures attendues effectivement reçues (une couverture basse rend la disponibilité moins fiable).
          </p>
        </div>
      )}
    </section>
  );
}

const NOT_LOGGED_IN = "Connectez-vous (panneau Configuration) pour effectuer cette action.";

/** Message lisible pour un échec d'action protégée : session absente ou expirée (401), authentification non configurée (503). */
function actionError(err: unknown): string {
  const msg = errorMessage(err);
  if (msg.includes(" 401 ")) return NOT_LOGGED_IN;
  if (msg.includes(" 503 ")) return "Authentification non configurée sur le serveur : action impossible.";
  return "Rejeté par l'API : " + msg;
}

const VERDICT_LABEL: Record<Verdict, string> = { true_positive: "vrai incident", false_positive: "faux positif" };

function IncidentsPanel({ incidents }: { incidents: Incident[] }) {
  const [precision, setPrecision] = useState<PrecisionReport | null>(null);
  const [verdicts, setVerdicts] = useState<Record<string, Verdict | null>>({}); // jugements posés ici, avant le prochain rafraîchissement
  const [owners, setOwners] = useState<Record<string, string | null>>({}); // prises en charge posées ici (nom, ou null si retirée)
  const [error, setError] = useState<string | null>(null);
  const refreshPrecision = useCallback(async () => {
    try {
      setPrecision(await api.precision(30));
    } catch {
      setPrecision(null);
    }
  }, []);
  usePolling(refreshPrecision, 30_000, [refreshPrecision]);

  const judge = async (inc: Incident, verdict: Verdict | null) => {
    if (!hasApiToken()) return setError(NOT_LOGGED_IN);
    setError(null);
    try {
      const { verdict: saved } = await api.setFeedback(inc.id, verdict); // lu ici : une réponse inattendue est attrapée par le catch
      setVerdicts((prev) => ({ ...prev, [inc.id]: saved }));
      void refreshPrecision();
    } catch (err) {
      setError(actionError(err));
    }
  };

  const takeCharge = async (inc: Incident, release: boolean) => {
    if (!hasApiToken()) return setError(NOT_LOGGED_IN);
    setError(null);
    try {
      const { acknowledged_by: by } = await (release ? api.release(inc.id) : api.acknowledge(inc.id));
      setOwners((prev) => ({ ...prev, [inc.id]: by }));
    } catch (err) {
      setError(actionError(err));
    }
  };

  const outputOf = (id: string) => incidents.find((i) => i.id === id)?.output_id;
  return (
    <section className="nm-panel" aria-labelledby="h-incidents">
      <h2 id="h-incidents">Incidents (récents et actifs)</h2>
      <p className="settings-field-hint" role="status">
        {precision === null ? "Précision indisponible."
          : `Précision mesurée sur ${precision.days} j : ${precision.precision_percent === null ? "—" : `${precision.precision_percent.toFixed(1)} %`} `
            + `(${precision.true_positives} vrai${precision.true_positives > 1 ? "s" : ""}, ${precision.false_positives} faux ; ${precision.unjudged} sans verdict`
            + `${precision.judged_share_percent === null ? "" : ` — ${precision.judged_share_percent.toFixed(0)} % jugés`})`}
      </p>
      {error && <p className="settings-warning" role="alert">{error}</p>}
      <div className="nm-log" role="log" aria-live="polite" aria-relevant="additions">
        {incidents.length === 0 && <div className="nm-log-empty">Aucun incident pour l'instant.</div>}
        {incidents.map((inc) => {
          const verdict = inc.id in verdicts ? verdicts[inc.id] : inc.verdict;
          const cause = inc.root_cause_incident_id;
          const owner = inc.id in owners ? owners[inc.id] : inc.acknowledged_by;
          const live = inc.opened_at !== null && (inc.status === "open" || inc.status === "stale"); // seuls ceux-là se prennent en charge
          return (
            <div className="nm-log-row" id={`incident-${inc.id}`} key={inc.id}>
              <span className="nm-log-time mono">{formatClock(new Date(inc.last_seen_at))}</span>
              <span className="nm-log-dot" style={{ background: (STATUS_STYLE[inc.status] ?? STATUS_STYLE.normal).color, marginTop: 4 }} />
              <span className="nm-log-msg">
                {inc.output_id} — {inc.incident_type} ({(STATUS_STYLE[inc.status] ?? STATUS_STYLE.normal).label}, score {inc.score.toFixed(2)})
                {inc.maintenance && <span className="nm-tag">pendant une maintenance</span>}
                {cause && (outputOf(cause)
                  ? <a className="nm-tag" href={`#incident-${cause}`}>cause : {outputOf(cause)}</a>
                  : <span className="nm-tag">cause : un incident amont</span>)}
                {verdict && <span className="nm-tag">{VERDICT_LABEL[verdict]}</span>}
                {inc.escalated_at && <span className="nm-tag">escaladé</span>}
                {owner && <span className="nm-tag">pris en charge par {owner}</span>}
                {live && (
                  <span className="nm-verdicts">
                    <button type="button" className="range-btn" onClick={() => void takeCharge(inc, Boolean(owner))}
                      aria-label={owner ? `Retirer la prise en charge de l'incident de ${inc.output_id}` : `Prendre en charge l'incident de ${inc.output_id}`}>
                      {owner ? "Retirer la prise en charge" : "Prendre en charge"}
                    </button>
                  </span>
                )}
                {inc.opened_at && (
                  <span className="nm-verdicts">
                    {(["false_positive", "true_positive"] as Verdict[]).map((v) => (
                      <button key={v} type="button" className={`range-btn${verdict === v ? " active" : ""}`} aria-pressed={verdict === v}
                        aria-label={`Juger l'incident de ${inc.output_id} : ${VERDICT_LABEL[v]}`} onClick={() => void judge(inc, v)}>
                        {v === "false_positive" ? "Faux positif" : "Vrai incident"}
                      </button>
                    ))}
                    {verdict && (
                      <button type="button" className="range-btn" aria-label={`Retirer le verdict de l'incident de ${inc.output_id}`} onClick={() => void judge(inc, null)}>
                        Retirer le verdict
                      </button>
                    )}
                  </span>
                )}
              </span>
            </div>
          );
        })}
      </div>
    </section>
  );
}

const MAINTENANCE_STATE: Record<MaintenanceWindow["state"], string> = { upcoming: "à venir", active: "en cours", past: "terminée", cancelled: "annulée" };

function formatWhen(iso: string): string {
  return new Date(iso).toLocaleString("fr-FR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function MaintenancePanel({ outputIds }: { outputIds: string[] }) {
  const [windows, setWindows] = useState<MaintenanceWindow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [output, setOutput] = useState("");
  const [startsIn, setStartsIn] = useState(0);
  const [duration, setDuration] = useState(60);
  const [reason, setReason] = useState("");
  const refresh = useCallback(async () => {
    try {
      setWindows(await api.maintenance(7));
    } catch {
      setWindows([]);
    }
  }, []);
  usePolling(refresh, 15_000, [refresh]);

  const act = async (action: () => Promise<unknown>) => {
    if (!hasApiToken()) return setError(NOT_LOGGED_IN);
    setError(null);
    try {
      await action();
      await refresh();
    } catch (err) {
      setError(actionError(err));
    }
  };
  const create = () => act(async () => {
    await api.createMaintenance({ output_id: output || null, reason: reason.trim(), starts_in_minutes: startsIn, duration_minutes: duration });
    setReason("");
  });

  return (
    <section className="nm-panel" aria-labelledby="h-maintenance">
      <h2 id="h-maintenance">Maintenance planifiée</h2>
      <p className="settings-field-hint">
        Pendant une fenêtre, les incidents de la sortie sont enregistrés mais n'envoient aucune alerte ; son temps est retiré de la disponibilité.
        Les horaires sont relatifs à l'horloge du serveur.
      </p>
      {error && <p className="settings-warning" role="alert">{error}</p>}
      <div className="nm-log" role="log" aria-live="polite" aria-relevant="additions">
        {windows.length === 0 && <div className="nm-log-empty">Aucune maintenance planifiée.</div>}
        {windows.map((w) => (
          <div className="nm-log-row" key={w.id}>
            <span className="nm-log-msg">
              {w.output_id ?? "Toutes les sorties"} — {formatWhen(w.start)} → {formatWhen(w.end)} — {w.reason}
              <span className="nm-tag">{MAINTENANCE_STATE[w.state]}</span>
              {(w.state === "upcoming" || w.state === "active") && (
                <button type="button" className="range-btn" aria-label={`Annuler la maintenance « ${w.reason} »`} onClick={() => void act(() => api.cancelMaintenance(w.id))}>
                  Annuler
                </button>
              )}
            </span>
          </div>
        ))}
      </div>
      <fieldset className="nm-form">
        <legend>Planifier une fenêtre</legend>
        <label>Sortie
          <select className="settings-input" style={{ width: 150 }} value={output} onChange={(e) => setOutput(e.target.value)}>
            <option value="">Toutes les sorties</option>
            {outputIds.map((id) => <option key={id} value={id}>{id}</option>)}
          </select>
        </label>
        <label>Début dans (min)
          <input className="settings-input mono" type="number" min={0} value={startsIn} onChange={(e) => setStartsIn(Number(e.target.value))} />
        </label>
        <label>Durée (min)
          <input className="settings-input mono" type="number" min={1} value={duration} onChange={(e) => setDuration(Number(e.target.value))} />
        </label>
        <label>Motif
          <input className="settings-input" style={{ width: 220 }} maxLength={200} value={reason} onChange={(e) => setReason(e.target.value)} />
        </label>
        <button type="button" className="settings-reset" disabled={!reason.trim() || duration <= 0} onClick={() => void create()}>Planifier</button>
        {!hasApiToken() && <span className="settings-field-hint">{NOT_LOGGED_IN}</span>}
      </fieldset>
    </section>
  );
}

function SettingsPanel({ config, deltaThresholds, onDeltaThresholdsChange, onSave, saving, saveError }: {
  config: AppConfigView; deltaThresholds: Thresholds; onDeltaThresholdsChange: (t: Thresholds) => void;
  onSave: (draft: IncidentDraft) => void; saving: boolean; saveError: string | null;
}) {
  const [draft, setDraft] = useState<IncidentDraft>(config.incident);
  useEffect(() => setDraft(config.incident), [config]);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [session, setSession] = useState<Session | null>(currentSession());
  const [loginError, setLoginError] = useState<string | null>(null);
  const [loggingIn, setLoggingIn] = useState(false);
  const submitLogin = async () => {
    setLoggingIn(true);
    setLoginError(null);
    try {
      setSession(await login(username, password));
      setPassword(""); // le mot de passe ne reste pas dans l'état
    } catch (err) {
      setLoginError(errorMessage(err).includes("429") ? "Trop d'essais : patientez quelques minutes." : "Identifiant ou mot de passe incorrect.");
    } finally {
      setLoggingIn(false);
    }
  };

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
        {saveError && <p className="settings-warning" role="alert">{saveError}</p>}
        {session ? (
          <p className="settings-field-hint" role="status">
            Connecté en tant que <strong>{session.username}</strong> (session valable jusqu'à{" "}
            {session.expiresAt.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" })}).{" "}
            <button type="button" className="settings-reset" onClick={() => { logout(); setSession(null); }}>Se déconnecter</button>
          </p>
        ) : (
          <fieldset className="settings-field" aria-describedby="login-hint">
            <legend className="settings-field-label">Connexion (requise pour enregistrer)</legend>
            <input aria-label="Identifiant" autoComplete="username" className="settings-input mono" style={{ width: 130 }} value={username} onChange={(e) => setUsername(e.target.value)} />
            <input aria-label="Mot de passe" type="password" autoComplete="current-password" className="settings-input mono" style={{ width: 150 }} value={password} onChange={(e) => setPassword(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && username && password) void submitLogin(); }} />
            <button type="button" className="settings-reset" disabled={loggingIn || !username || !password} onClick={() => void submitLogin()}>Se connecter</button>
            {loginError && <span className="settings-warning" role="alert">{loginError}</span>}
            <span id="login-hint" className="settings-field-hint">Session à durée limitée, gardée en mémoire : effacée au rechargement de la page, jamais écrite dans le navigateur.</span>
          </fieldset>
        )}
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
      setSaveError("Rejeté par l'API : " + errorMessage(err));
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
  .nm-title { margin: 0; font-size: 22px; font-weight: 600; letter-spacing: -0.01em; }
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
  .range-btn:focus-visible, .legend-chip:focus-visible, .settings-reset:focus-visible, .settings-input:focus-visible, .chart-svg:focus-visible, .skip-link:focus-visible { outline: 2px solid ${COLORS.trace}; outline-offset: 2px; }
  .nm-sim-banner { margin: 0 0 16px; padding: 8px 12px; border: 1px solid ${COLORS.amber}; border-radius: 4px; color: ${COLORS.amber}; font-size: 12.5px; }
  .skip-link { position: absolute; left: -9999px; top: 8px; background: ${COLORS.panel}; color: ${COLORS.text}; padding: 8px 12px; z-index: 10; }
  .skip-link:focus { left: 8px; }
  #nm-main:focus { outline: none; }
  .chart-readout { min-height: 18px; margin-top: 6px; font-size: 11.5px; color: ${COLORS.textDim}; }
  @media (max-width: 720px) {
    .nm-root { padding: 16px 12px; }
    .nm-header-meta { flex-wrap: wrap; gap: 8px 16px; }
    .nm-channel-head { gap: 12px; padding: 12px; }
    .nm-channel-body { padding: 4px 10px 14px; }
  }

  .chart-legend { display: flex; gap: 8px; margin-top: 6px; flex-wrap: wrap; }
  .legend-chip { display: flex; align-items: center; gap: 6px; background: transparent; border: 1px solid ${COLORS.border}; color: ${COLORS.textFaint}; font-family: 'Space Grotesk', sans-serif; font-size: 11px; padding: 4px 9px; cursor: pointer; }
  .legend-chip.active { color: ${COLORS.text}; border-color: ${COLORS.borderBright}; }
  .legend-chip-value { color: ${COLORS.textDim}; margin-left: 2px; }

  .nm-panel { background: ${COLORS.panel}; border: 1px solid ${COLORS.border}; padding: 18px 20px; margin-bottom: 20px; }
  .nm-panel h2 { font-size: 13px; font-weight: 500; color: ${COLORS.textDim}; margin: 0; }
  .nm-panel-head:focus-visible { outline: 2px solid ${COLORS.trace}; outline-offset: 2px; }
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
  .nm-tag { margin-left: 8px; padding: 1px 6px; border: 1px solid ${COLORS.border}; color: ${COLORS.textDim}; font-size: 11px; white-space: nowrap; }
  a.nm-tag { color: ${COLORS.trace}; }
  .nm-note { color: ${COLORS.textDim}; font-size: 11px; }
  .nm-form { display: flex; flex-wrap: wrap; gap: 12px 16px; align-items: flex-end; margin: 14px 0 0; padding: 10px 14px 14px; border: 1px solid ${COLORS.border}; }
  .nm-form legend { font-size: 12px; color: ${COLORS.text}; padding: 0 6px; }
  .nm-form label { display: flex; flex-direction: column; gap: 3px; font-size: 11.5px; color: ${COLORS.textDim}; }
  .nm-form .settings-input { margin-top: 0; }
  .nm-verdicts { display: inline-flex; gap: 6px; margin-left: 10px; }
  .nm-log-empty { color: ${COLORS.textFaint}; font-size: 12.5px; }
`;
