import { useCallback, useState } from "react";
import { api } from "./api";
import { formatGbpsBare, formatPercentBare } from "./format";
import { usePolling } from "./polling";
import { COLORS } from "./theme";
import type { TrafficSample } from "./types";

export type Thresholds = { traffic: number; loss: number };

export function TrendChip({ current, previous, formatValue, threshold }: {
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

export type CellPair = { key: string; cur: TrafficSample | null; prev: TrafficSample | null };

/** Fenêtres du tableau, en minutes avant « maintenant » ; chacune compare sa valeur à celle de 5 min plus tôt. */
export const SUMMARY_WINDOWS = [
  { key: "now", ago: 0 },
  { key: "prev", ago: 5 },
  { key: "day", ago: 24 * 60 },
  { key: "week", ago: 7 * 24 * 60 },
];

export function SummaryTable({ outputIds, thresholds }: { outputIds: string[]; thresholds: Thresholds }) {
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
