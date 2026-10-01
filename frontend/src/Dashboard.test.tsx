import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
