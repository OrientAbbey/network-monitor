/**
 * Fonctions pures du dashboard (aucune dépendance à React ni au DOM), extraites
 * pour être testées isolément.
 */

export function formatMbps(v) {
  if (v == null) return "—";
  if (v >= 1000) return (v / 1000).toFixed(2) + " Gbps";
  return v.toFixed(1) + " Mbps";
}

export function formatClock(date) {
  return date.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
}

export function formatAxisLabel(date, rangeKey, now) {
  if (rangeKey === "12h" || rangeKey === "1d") return formatClock(date);
  const daysAgo = Math.round((now - date) / (24 * 3600_000));
  return daysAgo <= 0 ? formatClock(date) : `-${daysAgo}j ${formatClock(date)}`;
}

export function niceDomain(values, paddingFrac = 0.12, floorAtZero = false) {
  if (!values.length) return [0, 1];
  let min = Infinity;
  let max = -Infinity;
  for (const v of values) {
    if (v < min) min = v;
    if (v > max) max = v;
  }
  if (min === max) { min -= 1; max += 1; }
  const pad = (max - min) * paddingFrac;
  return [floorAtZero ? Math.max(0, min - pad) : min - pad, max + pad];
}

/** « 12 s », « 3 min », « 2 h ». */
export function formatAge(seconds) {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min`;
  return `${Math.floor(s / 3600)} h`;
}

/** Délai avant la prochaine tentative : double à chaque échec consécutif, plafonné. */
export function backoffDelay(failures, baseMs = 4000, maxMs = 30000) {
  return Math.min(maxMs, baseMs * 2 ** Math.max(0, failures));
}

/**
 * Sous-échantillonne une série pour le tracé SVG en conservant les PICS : pour
 * chaque tranche de temps on garde le minimum ET le maximum (dans l'ordre
 * chronologique), plus le premier et le dernier point. Une moyenne ou un
 * pas fixe lisserait justement les pointes qu'une console de supervision doit
 * montrer. Renvoie la série d'origine si elle tient déjà dans le budget.
 *
 * @param points       tableau ordonné par temps
 * @param valueOf      (point) => nombre (ou null : le point n'influence pas min/max)
 * @param maxBuckets   nombre maximal de tranches (≈ largeur utile du graphe)
 */
export function downsampleMinMax(points, valueOf, maxBuckets) {
  const n = points.length;
  if (maxBuckets < 1 || n <= maxBuckets * 2) return points;
  const out = [points[0]];
  const size = (n - 2) / maxBuckets;
  for (let b = 0; b < maxBuckets; b++) {
    const start = 1 + Math.floor(b * size);
    const end = Math.min(n - 1, 1 + Math.floor((b + 1) * size));
    if (start >= end) continue;
    let minI = -1;
    let maxI = -1;
    for (let i = start; i < end; i++) {
      const v = valueOf(points[i]);
      if (v == null || Number.isNaN(v)) continue;
      if (minI < 0 || v < valueOf(points[minI])) minI = i;
      if (maxI < 0 || v > valueOf(points[maxI])) maxI = i;
    }
    if (minI < 0) continue;
    if (minI === maxI) out.push(points[minI]);
    else if (minI < maxI) out.push(points[minI], points[maxI]);
    else out.push(points[maxI], points[minI]);
  }
  out.push(points[n - 1]);
  return out;
}

/** Indice du point (série triée par `date`) le plus proche de l'instant `timeMs`. */
export function nearestIndexByTime(points, timeMs) {
  if (!points.length) return -1;
  let lo = 0;
  let hi = points.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (points[mid].date.getTime() < timeMs) lo = mid + 1;
    else hi = mid;
  }
  if (lo > 0 && Math.abs(points[lo - 1].date.getTime() - timeMs) <= Math.abs(points[lo].date.getTime() - timeMs)) {
    return lo - 1;
  }
  return lo;
}

export function isPageHidden() {
  return typeof document !== "undefined" && document.hidden === true;
}
