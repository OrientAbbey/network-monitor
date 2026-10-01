/** Fonctions pures du graphe (testées dans chart.test.ts). */

export interface Point {
  date: Date;
  traffic: number;
  loss: number;
}

/**
 * Réduit une série à ~`maxPoints` points en gardant, pour chaque tranche
 * de temps, le premier, le minimum, le maximum et le dernier point : les
 * pics et les creux — ce qu'on cherche à voir — ne sont jamais lissés, et le
 * tracé reste continu d'une tranche à l'autre.
 */
export function decimate(points: Point[], maxPoints: number): Point[] {
  if (points.length <= maxPoints || maxPoints < 8) return points;
  const buckets = Math.floor(maxPoints / 4);
  const size = points.length / buckets;
  const keep = new Set<number>();
  for (let b = 0; b < buckets; b++) {
    const from = Math.floor(b * size);
    const to = Math.min(points.length, Math.floor((b + 1) * size));
    if (from >= to) continue;
    let lo = from;
    let hi = from;
    for (let i = from; i < to; i++) {
      if (points[i].traffic < points[lo].traffic) lo = i;
      if (points[i].traffic > points[hi].traffic) hi = i;
    }
    [from, lo, hi, to - 1].forEach((i) => keep.add(i));
  }
  return [...keep].sort((a, b) => a - b).map((i) => points[i]);
}

/** Indice du point le plus proche de `t` (ms), points triés par date ; -1 si vide. */
export function nearestIndex(points: { date: Date }[], t: number): number {
  if (!points.length) return -1;
  let lo = 0;
  let hi = points.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (points[mid].date.getTime() < t) lo = mid + 1;
    else hi = mid;
  }
  if (lo > 0 && Math.abs(points[lo - 1].date.getTime() - t) <= Math.abs(points[lo].date.getTime() - t)) return lo - 1;
  return lo;
}

/**
 * Instant (ms) sous le pointeur, pour un graphe dont la zone de tracé est
 * [marginLeft, marginLeft + innerWidth] dans un repère de `chartWidth` unités
 * (le `viewBox` du SVG), affiché dans un rectangle de `rectWidth` pixels.
 * Les coordonnées hors de la zone de tracé sont ramenées à ses bords.
 */
export function pointerToTime(
  clientX: number,
  rect: { left: number; width: number },
  geometry: { chartWidth: number; marginLeft: number; innerWidth: number },
  tStart: number,
  span: number,
): number {
  if (rect.width <= 0) return tStart;
  const x = ((clientX - rect.left) / rect.width) * geometry.chartWidth;
  const ratio = Math.min(1, Math.max(0, (x - geometry.marginLeft) / geometry.innerWidth));
  return tStart + ratio * span;
}
