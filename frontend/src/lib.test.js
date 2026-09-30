import { describe, expect, it } from "vitest";
import {
  backoffDelay,
  downsampleMinMax,
  formatAge,
  formatAxisLabel,
  formatMbps,
  nearestIndexByTime,
  niceDomain,
} from "./lib.js";

const series = (values, stepMs = 300_000) =>
  values.map((v, i) => ({ date: new Date(Date.UTC(2026, 8, 1) + i * stepMs), v }));

describe("downsampleMinMax", () => {
  it("renvoie la série d'origine (même référence) quand elle tient dans le budget", () => {
    const pts = series([1, 2, 3, 4, 5]);
    expect(downsampleMinMax(pts, (p) => p.v, 10)).toBe(pts);
  });

  it("borne le nombre de points à 2 par tranche + 2 extrémités", () => {
    const pts = series(Array.from({ length: 8640 }, (_, i) => Math.sin(i / 50) * 100));
    const out = downsampleMinMax(pts, (p) => p.v, 500);
    expect(out.length).toBeLessThanOrEqual(500 * 2 + 2);
    expect(out.length).toBeGreaterThan(400);
  });

  it("conserve le premier point, le dernier, et l'ordre chronologique", () => {
    const pts = series(Array.from({ length: 5000 }, (_, i) => (i * 7919) % 1000));
    const out = downsampleMinMax(pts, (p) => p.v, 300);
    expect(out[0]).toBe(pts[0]);
    expect(out[out.length - 1]).toBe(pts[pts.length - 1]);
    for (let i = 1; i < out.length; i++) expect(out[i].date.getTime()).toBeGreaterThan(out[i - 1].date.getTime());
  });

  it("préserve un pic isolé et un creux isolé (ce qu'une moyenne effacerait)", () => {
    const values = Array.from({ length: 6000 }, () => 100);
    values[2777] = 5000; // pic d'un seul point
    values[4111] = -900; // creux d'un seul point
    const pts = series(values);
    const out = downsampleMinMax(pts, (p) => p.v, 200);
    expect(Math.max(...out.map((p) => p.v))).toBe(5000);
    expect(Math.min(...out.map((p) => p.v))).toBe(-900);
  });

  it("ignore les valeurs nulles pour min/max sans planter", () => {
    const pts = series(Array.from({ length: 3000 }, (_, i) => (i % 3 === 0 ? null : i)));
    const out = downsampleMinMax(pts, (p) => p.v, 100);
    expect(out.length).toBeGreaterThan(2);
  });

  it("renvoie la série telle quelle si maxBuckets < 1", () => {
    const pts = series([1, 2, 3]);
    expect(downsampleMinMax(pts, (p) => p.v, 0)).toBe(pts);
  });
});

describe("nearestIndexByTime", () => {
  const pts = series([10, 20, 30, 40]); // 5 min d'écart
  const t = (i) => pts[i].date.getTime();

  it("trouve le point exact et le plus proche", () => {
    expect(nearestIndexByTime(pts, t(2))).toBe(2);
    expect(nearestIndexByTime(pts, t(1) + 100_000)).toBe(1);
    expect(nearestIndexByTime(pts, t(1) + 200_000)).toBe(2);
  });

  it("borne aux extrémités et gère une série vide", () => {
    expect(nearestIndexByTime(pts, t(0) - 1e9)).toBe(0);
    expect(nearestIndexByTime(pts, t(3) + 1e9)).toBe(3);
    expect(nearestIndexByTime([], 5)).toBe(-1);
  });
});

describe("formatage et calculs", () => {
  it("formatMbps", () => {
    expect(formatMbps(null)).toBe("—");
    expect(formatMbps(312.44)).toBe("312.4 Mbps");
    expect(formatMbps(32000)).toBe("32.00 Gbps");
  });

  it("formatAge", () => {
    expect(formatAge(0)).toBe("0 s");
    expect(formatAge(59.6)).toBe("1 min"); // 59,6 s arrondi à 60 s -> affiché « 1 min », jamais « 60 s »
    expect(formatAge(125)).toBe("2 min");
    expect(formatAge(7300)).toBe("2 h");
    expect(formatAge(-5)).toBe("0 s");
  });

  it("backoffDelay double à chaque échec et plafonne", () => {
    expect([0, 1, 2, 3, 4, 10].map((n) => backoffDelay(n))).toEqual([4000, 8000, 16000, 30000, 30000, 30000]);
    expect(backoffDelay(-3)).toBe(4000);
  });

  it("niceDomain", () => {
    expect(niceDomain([])).toEqual([0, 1]);
    expect(niceDomain([5, 5])).toEqual([4 - 0.24, 6 + 0.24]);
    const [lo, hi] = niceDomain([10, 20], 0.1, true);
    expect(lo).toBeCloseTo(9) && expect(hi).toBeCloseTo(21);
    expect(niceDomain([0.1, 0.2], 5, true)[0]).toBe(0); // plancher à zéro
  });

  it("niceDomain supporte des séries très longues (Math.min(...) échouerait)", () => {
    const big = Array.from({ length: 300_000 }, (_, i) => i);
    expect(() => niceDomain(big)).not.toThrow();
  });

  it("formatAxisLabel : heure seule sur les plages courtes, jour relatif sur les longues", () => {
    const now = new Date(Date.UTC(2026, 8, 10, 12, 0));
    const twoDaysAgo = new Date(now.getTime() - 2 * 24 * 3600_000);
    expect(formatAxisLabel(twoDaysAgo, "12h", now)).not.toContain("j ");
    expect(formatAxisLabel(twoDaysAgo, "1w", now)).toMatch(/^-2j /);
    expect(formatAxisLabel(now, "1w", now)).not.toMatch(/^-/);
  });
});
