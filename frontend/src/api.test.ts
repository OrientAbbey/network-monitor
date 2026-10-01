import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, currentSession, hasApiToken, login, logout, setApiToken } from "./api";

const ok = (body: unknown = []) => Promise.resolve(new Response(JSON.stringify(body), { status: 200 }));
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn(() => ok());
  vi.stubGlobal("fetch", fetchMock);
  setApiToken(undefined);
});
afterEach(() => vi.unstubAllGlobals());

const lastCall = () => {
  const [url, init] = fetchMock.mock.calls.at(-1)!;
  return { url: new URL(String(url)), init: init as RequestInit };
};

describe("client API", () => {
  it("encode les identifiants dans le chemin", async () => {
    await api.history("OUT/1 é", undefined, undefined, 12);
    const { url } = lastCall();
    expect(url.pathname).toBe("/outputs/OUT%2F1%20%C3%A9/history");
    expect(url.searchParams.get("span_hours")).toBe("12");
  });

  it("n'envoie que les paramètres renseignés, et les listes répétées", async () => {
    await api.incidents(["open", "stale"], 50);
    const { url } = lastCall();
    expect(url.searchParams.getAll("status")).toEqual(["open", "stale"]);
    expect(url.searchParams.get("limit")).toBe("50");
    await api.alerts(undefined, 5);
    expect(lastCall().url.searchParams.has("output_id")).toBe(false);
  });

  it("n'ajoute le jeton qu'aux requêtes de contrôle, et seulement s'il est défini", async () => {
    await api.stopMonitoring();
    expect((lastCall().init.headers as Record<string, string>).Authorization).toBeUndefined();
    setApiToken("  secret  ");
    expect(hasApiToken()).toBe(true);
    await api.stopMonitoring();
    expect((lastCall().init.headers as Record<string, string>).Authorization).toBe("Bearer secret");
    await api.outputs();
    expect((lastCall().init.headers as Record<string, string>).Authorization).toBeUndefined();
    setApiToken("   ");
    expect(hasApiToken()).toBe(false);
  });

  it("remonte le statut et le détail d'une erreur HTTP", async () => {
    fetchMock.mockImplementationOnce(() => Promise.resolve(new Response("Jeton invalide", { status: 401 })));
    await expect(api.updateConfig({ open_score: 0.6 })).rejects.toThrow(/401.*Jeton invalide/);
  });

  it("abandonne une requête sans réponse au bout du délai maximal", async () => {
    vi.useFakeTimers();
    fetchMock.mockImplementationOnce(
      (_url: unknown, init: RequestInit) =>
        new Promise((_res, rej) => init.signal!.addEventListener("abort", () => rej(new DOMException("aborted", "AbortError")))),
    );
    const pending = expect(api.outputs()).rejects.toThrow(/abort/i);
    await vi.advanceTimersByTimeAsync(10_500);
    await pending;
    vi.useRealTimers();
  });

  it("se connecte, envoie le jeton de session, puis oublie tout à la déconnexion", async () => {
    fetchMock.mockImplementationOnce(() => ok({ access_token: "sess.ion", expires_in: 3600, username: "alice" }));
    const session = await login("alice", "pw");
    const [url, init] = fetchMock.mock.calls[0];
    expect(new URL(String(url)).pathname).toBe("/auth/login");
    expect(JSON.parse(String((init as RequestInit).body))).toEqual({ username: "alice", password: "pw" });
    expect(session.username).toBe("alice");
    expect(currentSession()?.username).toBe("alice");

    await api.stopMonitoring();
    expect((lastCall().init.headers as Record<string, string>).Authorization).toBe("Bearer sess.ion");

    logout();
    expect(hasApiToken()).toBe(false);
    expect(currentSession()).toBeNull();
    await api.stopMonitoring();
    expect((lastCall().init.headers as Record<string, string>).Authorization).toBeUndefined();
  });

  it("abandonne une session expirée au lieu de renvoyer un jeton périmé", async () => {
    vi.useFakeTimers();
    fetchMock.mockImplementationOnce(() => ok({ access_token: "sess.ion", expires_in: 60, username: "alice" }));
    await login("alice", "pw");
    expect(hasApiToken()).toBe(true);
    vi.setSystemTime(Date.now() + 61_000);
    expect(hasApiToken()).toBe(false);
    expect(currentSession()).toBeNull();
    vi.useRealTimers();
  });

  it("n'enregistre pas le mot de passe si la connexion échoue", async () => {
    fetchMock.mockImplementationOnce(() => Promise.resolve(new Response("{}", { status: 401 })));
    await expect(login("alice", "mauvais")).rejects.toThrow(/401/);
    expect(hasApiToken()).toBe(false);
  });
});
