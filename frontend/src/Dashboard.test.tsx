import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./api", () => {
  const T0 = Date.UTC(2026, 8, 1, 12);
  const history = Array.from({ length: 40 }, (_, i) => ({
    timestamp: new Date(T0 + i * 300_000).toISOString(), output_id: "OUT_001", traffic_mbps: 1000 + i, loss_percent: 0.1,
  }));
  const cfg = { incident: { open_score: 0.75, close_score: 0.4, warning_score: 0.5 }, alerts: { enabled: true, critical_score: 0.8 } };
  return {
    currentSession: vi.fn(() => null),
    login: vi.fn(async (username: string) => ({ username, expiresAt: new Date(Date.now() + 3_600_000) })),
    logout: vi.fn(),
    api: {
      outputs: vi.fn(async () => [{ output_id: "OUT_001", latest_sample: history.at(-1), status: "normal", active_incident_id: null }]),
      incidents: vi.fn(async () => []),
      alerts: vi.fn(async () => [{ incident_id: "i1", output_id: "OUT_001", severity: "critical", event: "opened", timestamp: new Date(T0).toISOString(), message: "chute de trafic" }]),
      monitoringStatus: vi.fn(async () => ({ running: true, simulated_now: new Date(T0).toISOString() })),
      getConfig: vi.fn(async () => cfg),
      history: vi.fn(async () => history),
      forecast: vi.fn(async () => []),
      forecastAhead: vi.fn(async () => []),
      availability: vi.fn(async () => ({
        start: "2026-08-01T00:00:00Z", end: "2026-09-01T00:00:00Z", downtime_severity: "critical",
        outputs: [{ output_id: "OUT_001", period_seconds: 2_592_000, incidents: 2, downtime_seconds: 5400, degraded_seconds: 600,
          availability_percent: 99.8264, coverage_percent: 99.5, mttd_seconds: 900, mttr_seconds: 3000, longest_incident_seconds: 4200 }],
      })),
      availabilityCsvUrl: vi.fn((days: number) => `http://api/reports/availability?format=csv&days=${days}`),
    },
  };
});

import { api, login, logout } from "./api";
import Dashboard, { DetailChart } from "./Dashboard";

const visibility = { traffic: true, loss: true, baseline: false, prediction: false };
beforeEach(() => vi.clearAllMocks());
afterEach(cleanup);

describe("DetailChart", () => {
  const renderChart = () =>
    render(<DetailChart outputId="OUT_001" rangeKey="12h" onRangeChange={() => {}} visibility={visibility} onToggle={() => {}} current={{ traffic: 1039, loss: 0.1 }} />);

  it("demande une fenêtre relative à l'horloge du serveur, pas des dates du navigateur", async () => {
    renderChart();
    await screen.findByRole("group");
    expect(api.history).toHaveBeenCalledWith("OUT_001", undefined, undefined, 12);
  });

  it("se lit au clavier : focus, flèches, Échap", async () => {
    const user = userEvent.setup();
    renderChart();
    const chart = await screen.findByRole("group");
    const readout = screen.getByRole("status");
    expect(readout.textContent).toMatch(/Survolez le graphe/);

    await user.tab(); // première pastille : Range 12h… on cible directement le graphe
    chart.focus();
    await waitFor(() => expect(readout.textContent).toMatch(/1[  ]?039|1,04 Gbps|1\.04 Gbps/));
    const before = readout.textContent;
    await user.keyboard("{ArrowLeft}");
    expect(readout.textContent).not.toBe(before);
    await user.keyboard("{Home}");
    expect(readout.textContent).toMatch(/1000\.0 Mbps|1\.00 Gbps/);
    await user.keyboard("{Escape}");
    expect(readout.textContent).toMatch(/Survolez le graphe/);
  });
});

describe("DetailChart : rafraîchissement incrémental", () => {
  afterEach(() => vi.useRealTimers());

  it("ne redemande à l'API que les points postérieurs au dernier connu", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    render(<DetailChart outputId="OUT_001" rangeKey="12h" onRangeChange={() => {}} visibility={visibility} onToggle={() => {}} current={{ traffic: 1039, loss: 0.1 }} />);
    await screen.findByRole("group");
    expect(api.history).toHaveBeenCalledTimes(1);
    expect(api.history).toHaveBeenLastCalledWith("OUT_001", undefined, undefined, 12); // première charge : toute la fenêtre

    const last = new Date(Date.UTC(2026, 8, 1, 12) + 39 * 300_000).toISOString();
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(api.history).toHaveBeenCalledTimes(2);
    expect(api.history).toHaveBeenLastCalledWith("OUT_001", last); // ensuite : seulement depuis le dernier point
    expect(screen.getByRole("group")).toBeTruthy(); // le graphe est toujours là, sans clignotement de chargement
  });

  it("recharge toute la fenêtre quand on change de plage", async () => {
    const { rerender } = render(<DetailChart outputId="OUT_001" rangeKey="12h" onRangeChange={() => {}} visibility={visibility} onToggle={() => {}} current={{ traffic: 1, loss: 0 }} />);
    await screen.findByRole("group");
    vi.mocked(api.history).mockClear();
    rerender(<DetailChart outputId="OUT_001" rangeKey="1d" onRangeChange={() => {}} visibility={visibility} onToggle={() => {}} current={{ traffic: 1, loss: 0 }} />);
    await waitFor(() => expect(api.history).toHaveBeenCalled());
    expect(api.history).toHaveBeenLastCalledWith("OUT_001", undefined, undefined, 24);
  });
});

describe("DetailChart à la souris", () => {
  it("affiche les valeurs du point le plus proche du pointeur, puis les efface à la sortie", async () => {
    const renderChart = () =>
      render(<DetailChart outputId="OUT_001" rangeKey="12h" onRangeChange={() => {}} visibility={visibility} onToggle={() => {}} current={{ traffic: 1039, loss: 0.1 }} />);
    renderChart();
    const chart = await screen.findByRole("group");
    const readout = screen.getByRole("status");
    // jsdom n'a pas de mise en page : on fixe la géométrie du SVG (2 px par unité du viewBox, 640 unités)
    vi.spyOn(chart, "getBoundingClientRect").mockReturnValue({ left: 0, top: 0, width: 1280, height: 460, right: 1280, bottom: 460, x: 0, y: 0, toJSON: () => ({}) });

    fireEvent.mouseMove(chart, { clientX: 2 * 50 }); // bord gauche de la zone de tracé : premier point (1000 Mbps)
    await waitFor(() => expect(readout.textContent).toMatch(/1000\.0 Mbps|1\.00 Gbps/));
    const first = readout.textContent;
    fireEvent.mouseMove(chart, { clientX: 2 * 588 }); // bord droit : dernier point (1039 Mbps)
    await waitFor(() => expect(readout.textContent).not.toBe(first));
    expect(readout.textContent).toMatch(/1039\.0 Mbps|1\.04 Gbps/);
    fireEvent.mouseLeave(chart);
    expect(readout.textContent).toMatch(/Survolez le graphe/);
  });
});

describe("Rapport de disponibilité", () => {
  it("affiche le tableau, change de période et propose l'export CSV", async () => {
    const user = userEvent.setup();
    render(<Dashboard />);
    const panel = await screen.findByRole("region", { name: /disponibilité/i });
    const row = await within(panel).findByRole("row", { name: /OUT_001/ });
    expect(row.textContent).toMatch(/99\.826 %/); // 3 décimales : 99,826 % se distingue de 99,8 %
    expect(row.textContent).toMatch(/1 h 30/); // 5 400 s d'indisponibilité
    expect(row.textContent).toMatch(/15 min/); // MTTD 900 s
    expect(api.availability).toHaveBeenCalledWith(30);
    await user.click(within(panel).getByRole("button", { name: "7 j" }));
    await waitFor(() => expect(api.availability).toHaveBeenLastCalledWith(7));
    const csv = within(panel).getByRole("link", { name: /csv/i });
    expect(csv.getAttribute("href")).toBe("http://api/reports/availability?format=csv&days=7");
  });

  it("signale un rapport indisponible sans casser le reste", async () => {
    vi.mocked(api.availability).mockRejectedValueOnce(new Error("503"));
    render(<Dashboard />);
    const panel = await screen.findByRole("region", { name: /disponibilité/i });
    expect((await within(panel).findByRole("alert")).textContent).toMatch(/indisponible/i);
    expect(screen.getByRole("main")).toBeTruthy();
  });
});

describe("Dashboard", () => {
  it("expose des repères de navigation et des zones live", async () => {
    render(<Dashboard />);
    expect(await screen.findByRole("heading", { level: 1 })).toBeTruthy();
    expect(screen.getByRole("main")).toBeTruthy();
    expect(screen.getByRole("link", { name: /aller au contenu/i })).toBeTruthy();
    const alertsLog = within(screen.getByRole("region", { name: /alertes récentes/i })).getByRole("log");
    expect(alertsLog.getAttribute("aria-live")).toBe("polite");
    expect(alertsLog.textContent).toMatch(/chute de trafic/);
  });

  it("Entrée sur l'en-tête d'une sortie la replie une seule fois", async () => {
    const user = userEvent.setup();
    render(<Dashboard />);
    const toggle = await screen.findByRole("button", { name: /replier out_001/i });
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    const head = screen.getAllByRole("button").find((b) => b.classList.contains("nm-channel-head"))!;
    head.focus();
    await user.keyboard("{Enter}");
    expect(screen.getByRole("button", { name: /déplier out_001/i }).getAttribute("aria-expanded")).toBe("false");
  });

  it("ouvre une session par identifiant et mot de passe, sans rien écrire dans le navigateur", async () => {
    const user = userEvent.setup();
    render(<Dashboard />);
    await user.click(await screen.findByRole("button", { name: /déplier la configuration/i }));
    const connect = screen.getByRole("button", { name: /se connecter/i });
    expect((connect as HTMLButtonElement).disabled).toBe(true); // rien de saisi
    await user.type(screen.getByLabelText("Identifiant"), "alice");
    await user.type(screen.getByLabelText("Mot de passe"), "s3cret");
    await user.click(connect);
    expect(login).toHaveBeenCalledWith("alice", "s3cret");
    expect(await screen.findByText(/connecté en tant que/i)).toBeTruthy();
    expect(screen.queryByLabelText("Mot de passe")).toBeNull(); // le champ disparaît avec la session
    expect(localStorage.length + sessionStorage.length).toBe(0);
    await user.click(screen.getByRole("button", { name: /se déconnecter/i }));
    expect(logout).toHaveBeenCalled();
    expect(screen.getByLabelText("Mot de passe")).toBeTruthy();
  });

  it("affiche un message générique si la connexion échoue", async () => {
    vi.mocked(login).mockRejectedValueOnce(new Error("POST /auth/login -> 401 {}"));
    const user = userEvent.setup();
    render(<Dashboard />);
    await user.click(await screen.findByRole("button", { name: /déplier la configuration/i }));
    await user.type(screen.getByLabelText("Identifiant"), "alice");
    await user.type(screen.getByLabelText("Mot de passe"), "mauvais");
    await user.click(screen.getByRole("button", { name: /se connecter/i }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/identifiant ou mot de passe incorrect/i);
  });
});
