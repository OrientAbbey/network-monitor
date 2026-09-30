import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { REQUEST_TIMEOUT_MS, api, isAbortError } from "./api.js";

// fetch qui ne répond jamais mais respecte l'annulation, comme le vrai fetch.
const hangingFetch = () =>
  vi.fn(
    (_url, { signal }) =>
      new Promise((_resolve, reject) => {
        // comme le vrai fetch : rejet immédiat si le signal est DÉJÀ annulé
        if (signal.aborted) return reject(signal.reason);
        signal.addEventListener("abort", () => reject(signal.reason));
      })
  );

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("client HTTP", () => {
  it("abandonne une requête pendante après le délai maximal (erreur de délai, pas une annulation voulue)", async () => {
    vi.stubGlobal("fetch", hangingFetch());
    const pending = api.outputs().then(() => null, (e) => e);
    await vi.advanceTimersByTimeAsync(REQUEST_TIMEOUT_MS + 10);
    const err = await pending;
    expect(err.name).toBe("TimeoutError");
    expect(isAbortError(err)).toBe(false);
  });

  it("propage l'annulation demandée par l'appelant (AbortError)", async () => {
    vi.stubGlobal("fetch", hangingFetch());
    const controller = new AbortController();
    const pending = api.outputs(controller.signal).then(() => null, (e) => e);
    controller.abort();
    const err = await pending;
    expect(isAbortError(err)).toBe(true);
  });

  it("refuse tout de suite si le signal est déjà annulé", async () => {
    vi.stubGlobal("fetch", hangingFetch());
    const controller = new AbortController();
    controller.abort();
    const err = await api.outputs(controller.signal).then(() => null, (e) => e);
    expect(isAbortError(err)).toBe(true);
  });

  it("sérialise offsets en paramètres répétés et n'envoie pas les paramètres absents", async () => {
    const fetchMock = vi.fn(async () => ({ ok: true, status: 200, json: async () => [] }));
    vi.stubGlobal("fetch", fetchMock);
    await api.snapshots("OUT_001", [0, 300, 86400]);
    await api.history("OUT_001", "2026-09-01T00:00:00Z", undefined);
    const snapUrl = new URL(fetchMock.mock.calls[0][0]);
    expect(snapUrl.pathname).toBe("/outputs/OUT_001/snapshots");
    expect(snapUrl.searchParams.getAll("offsets")).toEqual(["0", "300", "86400"]);
    const histUrl = new URL(fetchMock.mock.calls[1][0]);
    expect(histUrl.searchParams.get("since")).toBe("2026-09-01T00:00:00Z");
    expect(histUrl.searchParams.has("until")).toBe(false);
  });

  it("transforme une réponse HTTP d'erreur en Error portant le statut", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 503, text: async () => "surcharge" })));
    const err = await api.outputs().then(() => null, (e) => e);
    expect(err.status).toBe(503);
    expect(err.message).toContain("503");
  });

  it("le délai maximal ne se déclenche pas après une réponse réussie", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ ok: 1 }) })));
    await expect(api.outputs()).resolves.toEqual({ ok: 1 });
    expect(vi.getTimerCount()).toBe(0); // le minuteur a été nettoyé
  });
});
