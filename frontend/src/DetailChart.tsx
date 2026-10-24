import { type KeyboardEvent, type MouseEvent, useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import { decimate, mergeSeries, nearestIndex, pointerToTime } from "./chart";
import { formatAxisLabel, formatMbps } from "./format";
import { usePolling } from "./polling";
import { COLORS } from "./theme";
import type { ForecastPoint, TrafficSample } from "./types";

export type Visibility = { traffic: boolean; loss: boolean; baseline: boolean; prediction: boolean };
export type Current = { traffic: number | null | undefined; loss: number | null | undefined };

export const RANGES = [
  { key: "12h", label: "12 h", spanMs: 12 * 3600_000 },
  { key: "1d", label: "1 j", spanMs: 24 * 3600_000 },
  { key: "1w", label: "1 sem", spanMs: 7 * 24 * 3600_000 },
  { key: "1m", label: "1 mois", spanMs: 30 * 24 * 3600_000 },
];

export const MAX_CHART_POINTS = 720; // ~1,3 point par pixel utile : au-delà, on décime (pics et creux conservés)

export const CHART_W = 640;

export const CHART_H = 230;

// `left` laisse la place au libellé le plus long de l'axe (« 1.22 Gbps » en 9 px) : à 50 il était tronqué.
export const MARGIN = { top: 12, right: 52, bottom: 26, left: 68 };

export const INNER_W = CHART_W - MARGIN.left - MARGIN.right;

export const INNER_H = CHART_H - MARGIN.top - MARGIN.bottom;

export function niceDomain(values: number[], paddingFrac = 0.12, floorAtZero = false): [number, number] {
  if (!values.length) return [0, 1];
  let min = Math.min(...values);
  let max = Math.max(...values);
  if (min === max) { min -= 1; max += 1; }
  const pad = (max - min) * paddingFrac;
  return [floorAtZero ? Math.max(0, min - pad) : min - pad, max + pad];
}

export function EmptyRange({ label }: { label: string }) {
  return (
    <div className="empty-range">
      Pas encore assez d'historique réel pour "{label}" — le système n'a
      commencé à mesurer qu'à son démarrage. Réessayez plus tard, ou choisissez
      une plage plus courte.
    </div>
  );
}

export interface DetailChartProps {
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

export function RangeSelector({ rangeKey, onRangeChange }: Pick<DetailChartProps, "rangeKey" | "onRangeChange">) {
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

export function Legend({ visibility, onToggle, current }: Pick<DetailChartProps, "visibility" | "onToggle" | "current">) {
  return (
    <div className="chart-legend">
      <LegendChip label="Trafic" color={COLORS.trace} active={visibility.traffic} onClick={() => onToggle("traffic")} value={formatMbps(current.traffic)} />
      <LegendChip label="Pertes" color={COLORS.lossTrace} active={visibility.loss} onClick={() => onToggle("loss")} value={current.loss != null ? `${current.loss.toFixed(2)}%` : "—"} />
      <LegendChip label="Baseline" color={COLORS.textDim} dashed active={visibility.baseline} onClick={() => onToggle("baseline")} />
      <LegendChip label="Prédiction" color={COLORS.textDim} dotted active={visibility.prediction} onClick={() => onToggle("prediction")} />
    </div>
  );
}

export function LegendChip({ label, color, active, onClick, dashed, dotted, value }: {
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
