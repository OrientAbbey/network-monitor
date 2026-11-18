import { describe, expect, it } from "vitest";
import { decimate, mergeSeries, nearestIndex, pointerToTime, type Point } from "./chart";

const T0 = Date.UTC(2026, 8, 1);
const series = (n: number, f: (i: number) => number): Point[] =>
  Array.from({ length: n }, (_, i) => ({ date: new Date(T0 + i * 300_000), traffic: f(i), loss: 0 }));

describe("decimate", () => {
  it("laisse intacte une série déjà assez courte", () => {
    const pts = series(100, (i) => i);
    expect(decimate(pts, 720)).toBe(pts);
  });

  it("borne la taille et garde l'ordre chronologique", () => {
    const out = decimate(series(8640, (i) => Math.sin(i / 50) * 100), 720);
    expect(out.length).toBeLessThanOrEqual(720);
    expect(out.length).toBeGreaterThan(300);
    expect(out.map((p) => p.date.getTime())).toEqual([...out.map((p) => p.date.getTime())].sort((a, b) => a - b));
  });

  it("ne lisse jamais un pic ni un creux isolés", () => {
    const pts = series(8640, () => 100);
    pts[4321].traffic = 900; // pic
    pts[6543].traffic = -50; // creux
    const out = decimate(pts, 720).map((p) => p.traffic);
    expect(Math.max(...out)).toBe(900);
    expect(Math.min(...out)).toBe(-50);
  });

  it("garde le premier et le dernier point", () => {
    const pts = series(5000, (i) => i % 17);
    const out = decimate(pts, 400);
    expect(out[0]).toBe(pts[0]);
    expect(out[out.length - 1]).toBe(pts[pts.length - 1]);
  });
});

describe("nearestIndex", () => {
  const pts = series(10, (i) => i);
  it("trouve le point le plus proche", () => {
    expect(nearestIndex(pts, T0 + 3 * 300_000 + 100_000)).toBe(3);
    expect(nearestIndex(pts, T0 + 3 * 300_000 + 200_000)).toBe(4);
  });
  it("borne aux extrémités et gère le vide", () => {
    expect(nearestIndex(pts, T0 - 1e9)).toBe(0);
    expect(nearestIndex(pts, T0 + 1e9)).toBe(9);
    expect(nearestIndex([], T0)).toBe(-1);
  });
});

describe("pointerToTime", () => {
  const geometry = { chartWidth: 640, marginLeft: 50, innerWidth: 538 };
  const rect = { left: 100, width: 1280 }; // le SVG est affiché à 2 px par unité
  it("convertit la position du pointeur en instant", () => {
    // x = 50 unités (bord gauche de la zone de tracé) ; x = 588 (bord droit)
    expect(pointerToTime(100 + 2 * 50, rect, geometry, 1000, 2000)).toBe(1000);
    expect(pointerToTime(100 + 2 * 588, rect, geometry, 1000, 2000)).toBe(3000);
    expect(pointerToTime(100 + 2 * 319, rect, geometry, 1000, 2000)).toBeCloseTo(2000, 0);
  });
  it("ramène les marges aux bords de la zone de tracé", () => {
    expect(pointerToTime(100, rect, geometry, 1000, 2000)).toBe(1000);
    expect(pointerToTime(100 + 1280, rect, geometry, 1000, 2000)).toBe(3000);
  });
  it("ne plante pas si le SVG n'a pas de largeur", () => {
    expect(pointerToTime(10, { left: 0, width: 0 }, geometry, 1000, 2000)).toBe(1000);
  });
});

describe("mergeSeries", () => {
  const at = (min: number, v = 0) => ({ timestamp: new Date(T0 + min * 60_000).toISOString(), v });
  it("ajoute les nouveaux points, dédoublonne par horodatage et garde l'ordre", () => {
    const prev = [at(0, 1), at(5, 2), at(10, 3)];
    const out = mergeSeries(prev, [at(10, 99), at(15, 4)], 3600_000);
    expect(out.map((p) => [Date.parse(p.timestamp) - T0, p.v])).toEqual([[0, 1], [300_000, 2], [600_000, 99], [900_000, 4]]); // le nouveau 10 min l'emporte
  });
  it("abandonne ce qui sort de la fenêtre, mesurée depuis le point le plus récent", () => {
    const out = mergeSeries([at(0), at(30), at(60)], [at(90)], 60 * 60_000);
    expect(out.map((p) => Date.parse(p.timestamp) - T0)).toEqual([30 * 60_000, 60 * 60_000, 90 * 60_000]);
  });
  it("trie des points reçus en désordre et tolère le vide", () => {
    expect(mergeSeries([], [at(10), at(0)], 3600_000).map((p) => Date.parse(p.timestamp) - T0)).toEqual([0, 600_000]);
    expect(mergeSeries([at(0)], [], 3600_000)).toHaveLength(1);
    expect(mergeSeries([], [], 3600_000)).toEqual([]);
  });
});
