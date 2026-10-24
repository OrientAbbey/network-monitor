export function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

export function formatMbps(v: number | null | undefined): string {
  if (v == null) return "—";
  if (v >= 1000) return (v / 1000).toFixed(2) + " Gbps";
  return v.toFixed(1) + " Mbps";
}

export function formatClock(date: Date): string {
  return date.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
}

export function formatAxisLabel(date: Date, rangeKey: string, now: Date): string {
  if (rangeKey === "12h" || rangeKey === "1d") return formatClock(date);
  const daysAgo = Math.round((now.getTime() - date.getTime()) / (24 * 3600_000));
  return daysAgo <= 0 ? formatClock(date) : `-${daysAgo}j ${formatClock(date)}`;
}

// --- Graphe détaillé --------------------------------------------------

export const formatGbpsBare = (v: number | null | undefined) => (v == null ? "—" : (v / 1000).toFixed(2));

export const formatPercentBare = (v: number | null | undefined) => (v == null ? "—" : v.toFixed(2));

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m ? `${h} h ${String(m).padStart(2, "0")}` : `${h} h`;
}

export function formatWhen(iso: string): string {
  return new Date(iso).toLocaleString("fr-FR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}
