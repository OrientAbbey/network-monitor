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
    hasApiToken: vi.fn(() => false),
    api: {
      outputs: vi.fn(async () => [{ output_id: "OUT_001", latest_sample: history.at(-1), status: "normal", active_incident_id: null }]),
      incidents: vi.fn(async () => []),
      alerts: vi.fn(async () => [{ incident_id: "i1", output_id: "OUT_001", severity: "critical", event: "opened", timestamp: new Date(T0).toISOString(), message: "chute de trafic", suppressed: null, root_cause_incident_id: null, impacted_outputs: [] }]),
      monitoringStatus: vi.fn(async () => ({ running: true, simulated_now: new Date(T0).toISOString() })),
      getConfig: vi.fn(async () => cfg),
      history: vi.fn(async () => history),
      nearest: vi.fn(async () => ({})),
      forecast: vi.fn(async () => []),
      forecastAhead: vi.fn(async () => []),
      availability: vi.fn(async () => ({
        start: "2026-08-01T00:00:00Z", end: "2026-09-01T00:00:00Z", downtime_severity: "critical",
        outputs: [{ output_id: "OUT_001", period_seconds: 2_592_000, monitored_from: "2026-08-20T00:00:00Z", monitored_seconds: 864_000, maintenance_seconds: 1800, false_positives_excluded: 1, incidents: 2, downtime_seconds: 5400, degraded_seconds: 600,
          availability_percent: 99.8264, coverage_percent: 99.5, mttd_seconds: 900, mttr_seconds: 3000, longest_incident_seconds: 4200 }],
      })),
      precision: vi.fn(async () => ({
        days: 30, confirmed: 0, true_positives: 0, false_positives: 0, unjudged: 0, precision_percent: null, judged_share_percent: null, by_output: {}, by_type: {},
      })),
      setFeedback: vi.fn(),
      acknowledge: vi.fn(),
      release: vi.fn(),
      maintenance: vi.fn(async () => []),
      createMaintenance: vi.fn(async () => ({})),
      cancelMaintenance: vi.fn(async () => ({})),
      availabilityCsvUrl: vi.fn((days: number) => `http://api/reports/availability?format=csv&days=${days}`),
    },
  };
});

import { api, hasApiToken, login, logout } from "./api";
import Dashboard, { DetailChart } from "./Dashboard";
import type { Alert, Incident, MaintenanceWindow, PrecisionReport } from "./types";

const visibility = { traffic: true, loss: true, baseline: false, prediction: false };
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(hasApiToken).mockReturnValue(false); // clearAllMocks ne remet pas les valeurs de retour : chaque test repart déconnecté
});
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

describe("Données simulées", () => {
  it("avertit visiblement quand la source est le Mock, et seulement dans ce cas", async () => {
    vi.mocked(api.getConfig).mockResolvedValueOnce({
      incident: { open_score: 0.75, close_score: 0.4, warning_score: 0.5 }, alerts: { enabled: true, critical_score: 0.8 }, source: { type: "mock" },
    });
    const { unmount } = render(<Dashboard />);
    expect((await screen.findByText(/données simulées/i)).textContent).toMatch(/pas votre réseau/);
    unmount();
    vi.mocked(api.getConfig).mockResolvedValueOnce({
      incident: { open_score: 0.75, close_score: 0.4, warning_score: 0.5 }, alerts: { enabled: true, critical_score: 0.8 }, source: { type: "prometheus" },
    });
    render(<Dashboard />);
    await screen.findByRole("heading", { level: 1 });
    await waitFor(() => expect(api.getConfig).toHaveBeenCalledTimes(2));
    expect(screen.queryByText(/données simulées/i)).toBeNull();
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

  it("le bouton de repli est le seul contrôle clavier de l'en-tête (pas de bouton imbriqué), et replie une seule fois", async () => {
    const user = userEvent.setup();
    render(<Dashboard />);
    const toggle = await screen.findByRole("button", { name: /replier out_001/i });
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    // Aucun rôle « bouton » sur l'en-tête lui-même : axe-core signale les contrôles interactifs imbriqués.
    expect(screen.getAllByRole("button").some((b) => b.classList.contains("nm-channel-head"))).toBe(false);
    toggle.focus();
    await user.keyboard("{Enter}");
    expect(screen.getByRole("button", { name: /déplier out_001/i }).getAttribute("aria-expanded")).toBe("false");
    await user.keyboard(" ");
    expect(screen.getByRole("button", { name: /replier out_001/i }).getAttribute("aria-expanded")).toBe("true");
  });

  it("un clic sur l'en-tête (souris) replie aussi, sans double bascule avec le bouton", async () => {
    const user = userEvent.setup();
    const { container } = render(<Dashboard />);
    await screen.findByRole("button", { name: /replier out_001/i });
    await user.click(container.querySelector(".nm-channel-head") as HTMLElement);
    expect(screen.getByRole("button", { name: /déplier out_001/i })).toBeTruthy();
    await user.click(screen.getByRole("button", { name: /déplier out_001/i }));
    expect(screen.getByRole("button", { name: /replier out_001/i })).toBeTruthy(); // une bascule par clic, pas deux
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

const NOW = new Date(Date.UTC(2026, 8, 1, 12)).toISOString();
const incident = (over: Partial<Incident> = {}): Incident => ({
  id: "inc-1", output_id: "OUT_001", status: "open", severity: "critical", incident_type: "traffic_drop", last_seen_at: NOW, opened_at: NOW,
  started_at: NOW, ended_at: null, score: 0.9, description: "", verdict: null, verdict_by: null, verdict_at: null, verdict_note: null,
  acknowledged_by: null, acknowledged_at: null, escalated_at: null, maintenance: false, root_cause_incident_id: null, inhibited: false, ...over,
});
const precision = (over: Partial<PrecisionReport> = {}): PrecisionReport => ({
  days: 30, confirmed: 0, true_positives: 0, false_positives: 0, unjudged: 0, precision_percent: null, judged_share_percent: null, by_output: {}, by_type: {}, ...over,
});
const incidentsPanel = async () => screen.findByRole("region", { name: /^incidents/i });

describe("Incidents : verdicts et précision", () => {
  it("affiche « — » quand rien n'est jugé, jamais un pourcentage inventé", async () => {
    vi.mocked(api.incidents).mockResolvedValueOnce([incident()]);
    render(<Dashboard />);
    const panel = await incidentsPanel();
    await waitFor(() => expect(panel.textContent).toMatch(/Précision mesurée sur 30 j : — \(0 vrai, 0 faux ; 0 sans verdict\)/));
    expect(panel.textContent).not.toMatch(/\d %/);
  });

  it("détaille la précision mesurée et la part jugée", async () => {
    vi.mocked(api.precision).mockResolvedValueOnce(precision({ confirmed: 5, true_positives: 3, false_positives: 1, unjudged: 1, precision_percent: 75, judged_share_percent: 80 }));
    vi.mocked(api.incidents).mockResolvedValueOnce([incident()]);
    render(<Dashboard />);
    const panel = await incidentsPanel();
    await waitFor(() => expect(panel.textContent).toMatch(/75\.0 % \(3 vrais, 1 faux ; 1 sans verdict — 80 % jugés\)/));
  });

  it("marque les incidents (maintenance, cause amont, verdict) et ne propose de juger que les incidents confirmés", async () => {
    vi.mocked(api.incidents).mockResolvedValueOnce([
      incident({ id: "up", output_id: "OUT_001", maintenance: true }),
      incident({ id: "down", output_id: "OUT_002", root_cause_incident_id: "up", inhibited: true, verdict: "false_positive" }),
      incident({ id: "weak", output_id: "OUT_003", status: "suspect", opened_at: null }),
    ]);
    render(<Dashboard />);
    const panel = await incidentsPanel();
    await within(panel).findByText(/pendant une maintenance/);
    const cause = within(panel).getByRole("link", { name: /cause : OUT_001/ });
    expect(cause.getAttribute("href")).toBe("#incident-up"); // mène à la ligne de la cause
    expect(panel.querySelector("#incident-up")).toBeTruthy();
    expect(within(panel).getByText("faux positif")).toBeTruthy();
    expect(within(panel).getAllByRole("button", { name: /Juger l'incident de OUT_001/ })).toHaveLength(2);
    expect(within(panel).getAllByRole("button", { name: /Retirer le verdict/ })).toHaveLength(1); // seul l'incident déjà jugé
    expect(within(panel).queryByRole("button", { name: /OUT_003/ })).toBeNull(); // signal faible : non confirmé, rien à juger
  });

  it("envoie le verdict (connecté) et rafraîchit la précision", async () => {
    vi.mocked(hasApiToken).mockReturnValue(true);
    vi.mocked(api.incidents).mockResolvedValueOnce([incident()]);
    vi.mocked(api.setFeedback).mockResolvedValueOnce(incident({ verdict: "false_positive" })).mockResolvedValueOnce(incident({ verdict: null }));
    vi.mocked(api.precision).mockResolvedValueOnce(precision()).mockResolvedValueOnce(precision({ confirmed: 1, false_positives: 1, precision_percent: 0, judged_share_percent: 100 }));
    const user = userEvent.setup();
    render(<Dashboard />);
    const panel = await incidentsPanel();
    await user.click(await within(panel).findByRole("button", { name: /Juger l'incident de OUT_001 : faux positif/ }));
    expect(api.setFeedback).toHaveBeenCalledWith("inc-1", "false_positive");
    await waitFor(() => expect(panel.textContent).toMatch(/0\.0 % \(0 vrai, 1 faux/));
    expect(within(panel).getByRole("button", { name: /Retirer le verdict/ })).toBeTruthy();
    await user.click(within(panel).getByRole("button", { name: /Retirer le verdict/ }));
    expect(api.setFeedback).toHaveBeenLastCalledWith("inc-1", null);
    await waitFor(() => expect(within(panel).queryByRole("button", { name: /Retirer le verdict/ })).toBeNull()); // le verdict a bien été retiré
  });

  it("invite à se connecter, sans appeler l'API, quand il n'y a pas de session", async () => {
    vi.mocked(api.incidents).mockResolvedValueOnce([incident()]);
    const user = userEvent.setup();
    render(<Dashboard />);
    const panel = await incidentsPanel();
    await user.click(await within(panel).findByRole("button", { name: /Juger l'incident de OUT_001 : vrai incident/ }));
    expect((await within(panel).findByRole("alert")).textContent).toMatch(/Connectez-vous \(panneau Configuration\)/);
    expect(api.setFeedback).not.toHaveBeenCalled();
  });

  it("traite un 401 du serveur (session expirée) comme une invitation à se reconnecter", async () => {
    vi.mocked(hasApiToken).mockReturnValue(true);
    vi.mocked(api.incidents).mockResolvedValueOnce([incident()]);
    vi.mocked(api.setFeedback).mockRejectedValueOnce(new Error("PUT /incidents/inc-1/feedback -> 401 {}"));
    const user = userEvent.setup();
    render(<Dashboard />);
    const panel = await incidentsPanel();
    await user.click(await within(panel).findByRole("button", { name: /faux positif/ }));
    expect((await within(panel).findByRole("alert")).textContent).toMatch(/Connectez-vous/);
  });
});

describe("Maintenance planifiée", () => {
  const window_ = (over: Partial<MaintenanceWindow> = {}): MaintenanceWindow => ({
    id: "w1", output_id: "OUT_001", start: NOW, end: new Date(Date.UTC(2026, 8, 1, 14)).toISOString(), reason: "Mise à jour du routeur",
    created_by: "alice", cancelled_at: null, state: "upcoming", ...over,
  });
  const maintenancePanel = async () => screen.findByRole("region", { name: /maintenance planifiée/i });

  it("liste les fenêtres avec leur état ; annuler n'est proposé que pour une fenêtre à venir ou en cours", async () => {
    vi.mocked(api.maintenance).mockResolvedValueOnce([
      window_(), window_({ id: "w2", output_id: null, reason: "Coupure électrique", state: "active" }),
      window_({ id: "w3", reason: "Ancienne", state: "past" }), window_({ id: "w4", reason: "Abandonnée", state: "cancelled" }),
    ]);
    render(<Dashboard />);
    const panel = await maintenancePanel();
    await within(panel).findByText(/Mise à jour du routeur/);
    expect(within(panel).getByText(/Toutes les sorties — .*Coupure électrique/)).toBeTruthy();
    for (const label of ["à venir", "en cours", "terminée", "annulée"]) expect(within(panel).getByText(label)).toBeTruthy();
    expect(within(panel).getAllByRole("button", { name: /Annuler la maintenance/ })).toHaveLength(2);
    expect(api.maintenance).toHaveBeenCalledWith(7); // fenêtre calculée par le serveur
  });

  it("crée une fenêtre relative à l'horloge du serveur, sans date fabriquée par le navigateur", async () => {
    vi.mocked(hasApiToken).mockReturnValue(true);
    const user = userEvent.setup();
    render(<Dashboard />);
    const panel = await maintenancePanel();
    const submit = within(panel).getByRole("button", { name: "Planifier" }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true); // pas de motif
    await user.selectOptions(within(panel).getByLabelText("Sortie"), "OUT_001");
    await user.clear(within(panel).getByLabelText("Début dans (min)"));
    await user.type(within(panel).getByLabelText("Début dans (min)"), "15");
    await user.clear(within(panel).getByLabelText("Durée (min)"));
    await user.type(within(panel).getByLabelText("Durée (min)"), "90");
    await user.type(within(panel).getByLabelText("Motif"), "  Changement de carte  ");
    await user.click(submit);
    expect(api.createMaintenance).toHaveBeenCalledWith({ output_id: "OUT_001", reason: "Changement de carte", starts_in_minutes: 15, duration_minutes: 90 });
    await waitFor(() => expect((within(panel).getByLabelText("Motif") as HTMLInputElement).value).toBe(""));
  });

  it("annule une fenêtre (connecté)", async () => {
    vi.mocked(hasApiToken).mockReturnValue(true);
    vi.mocked(api.maintenance).mockResolvedValueOnce([window_()]);
    const user = userEvent.setup();
    render(<Dashboard />);
    const panel = await maintenancePanel();
    await user.click(await within(panel).findByRole("button", { name: /Annuler la maintenance/ }));
    expect(api.cancelMaintenance).toHaveBeenCalledWith("w1");
  });

  it("demande de se connecter avant de créer ou d'annuler, sans appeler l'API", async () => {
    vi.mocked(api.maintenance).mockResolvedValueOnce([window_()]);
    const user = userEvent.setup();
    render(<Dashboard />);
    const panel = await maintenancePanel();
    await user.click(await within(panel).findByRole("button", { name: /Annuler la maintenance/ }));
    expect((await within(panel).findByRole("alert")).textContent).toMatch(/Connectez-vous/);
    await user.type(within(panel).getByLabelText("Motif"), "Test");
    await user.click(within(panel).getByRole("button", { name: "Planifier" }));
    expect(api.cancelMaintenance).not.toHaveBeenCalled();
    expect(api.createMaintenance).not.toHaveBeenCalled();
  });

  it("affiche le refus du serveur (422) sans le masquer", async () => {
    vi.mocked(hasApiToken).mockReturnValue(true);
    vi.mocked(api.createMaintenance).mockRejectedValueOnce(new Error("POST /maintenance -> 422 cette fenêtre est déjà terminée"));
    const user = userEvent.setup();
    render(<Dashboard />);
    const panel = await maintenancePanel();
    await user.type(within(panel).getByLabelText("Motif"), "Test");
    await user.click(within(panel).getByRole("button", { name: "Planifier" }));
    expect((await within(panel).findByRole("alert")).textContent).toMatch(/Rejeté par l'API.*déjà terminée/);
  });
});

describe("Alertes retenues et disponibilité", () => {
  const alert_ = (over: Partial<Alert>): Alert => ({
    incident_id: "i", output_id: "OUT_001", severity: "critical", event: "opened", timestamp: NOW, message: "chute de trafic", incident_type: "traffic_drop",
    duration_seconds: null, traffic_mbps: null, expected_traffic_mbps: null, traffic_deviation_percent: null, loss_percent: null, expected_loss_percent: null,
    suppressed: null, root_cause_incident_id: null, impacted_outputs: [], ...over,
  });

  it("marque les alertes retenues et nomme les sorties qu'une cause amont explique aussi", async () => {
    vi.mocked(api.alerts).mockResolvedValueOnce([
      alert_({ incident_id: "a", suppressed: "maintenance" }),
      alert_({ incident_id: "b", output_id: "OUT_002", suppressed: "inhibited", root_cause_incident_id: "a" }),
      alert_({ incident_id: "c", impacted_outputs: ["OUT_002", "OUT_003"] }),
      alert_({ incident_id: "d", output_id: "OUT_003" }),
    ]);
    render(<Dashboard />);
    const log = within(await screen.findByRole("region", { name: /alertes récentes/i })).getByRole("log");
    await within(log).findByText("retenue : maintenance");
    expect(within(log).getByText("retenue : cause amont")).toBeTruthy();
    expect(within(log).getByText(/Explique aussi : OUT_002, OUT_003/)).toBeTruthy();
    expect(within(log).getAllByText(/retenue/)).toHaveLength(2); // l'alerte ordinaire n'est pas marquée
  });

  it("montre le temps de maintenance et les faux positifs exclus du rapport de disponibilité", async () => {
    render(<Dashboard />);
    const row = await within(await screen.findByRole("region", { name: /disponibilité/i })).findByRole("row", { name: /OUT_001/ });
    expect(row.textContent).toMatch(/30 min/); // 1 800 s de maintenance
    expect(row.textContent).toMatch(/\+1 faux positif exclu\)/);
  });
});

describe("Comparatif temporel", () => {
  const point = (traffic: number, loss: number) => ({ timestamp: NOW, output_id: "OUT_001", traffic_mbps: traffic, loss_percent: loss });
  const comparison = async () => within(await screen.findByRole("region", { name: /comparatif temporel/i }));

  it("charge toutes les valeurs en UNE requête, sur les sept instants distincts, au lieu d'une par sortie et par instant", async () => {
    render(<Dashboard />);
    await waitFor(() => expect(api.nearest).toHaveBeenCalledTimes(1));
    expect(api.nearest).toHaveBeenCalledWith([0, 5, 10, 1440, 1445, 10080, 10085]); // (0, 5, 1 j, 1 sem.) × (valeur, 5 min avant), sans doublon
    // Le graphe lit encore l'historique, mais le tableau ne le fait plus : aucune requête `history` bornée par deux dates.
    expect(vi.mocked(api.history).mock.calls.every(([, , until]) => until === undefined)).toBe(true);
  });

  it("affiche valeur, variation et « — » quand une mesure manque", async () => {
    vi.mocked(api.nearest).mockResolvedValueOnce({
      OUT_001: { "0": point(5000, 0.5), "5": point(4000, 0.4), "10": point(4000, 0.4), "1440": null, "1445": null, "10080": point(2500, 0.1), "10085": null },
    });
    render(<Dashboard />);
    const row = (await (await comparison()).findByRole("row", { name: /OUT_001/ }));
    const cells = within(row).getAllByRole("cell");
    expect(cells[1].textContent).toMatch(/5\.00.*▲ 25\.0%/); // actuelle : 5 Gbps contre 4 Gbps cinq minutes plus tôt
    expect(cells[2].textContent).toMatch(/4\.00.*–/); // précédente : 4 contre 4
    expect(cells[3].textContent).toBe("—"); // −1 j : aucune mesure assez proche
    expect(cells[4].textContent).toBe("2.50"); // −1 sem. : une valeur mais rien à comparer, pas de variation inventée
  });

  it("montre « — » partout si la requête échoue, sans planter", async () => {
    vi.mocked(api.nearest).mockRejectedValueOnce(new Error("GET /outputs/nearest -> 500"));
    render(<Dashboard />);
    const row = await (await comparison()).findByRole("row", { name: /OUT_001/ });
    expect(within(row).getAllByRole("cell").slice(1).every((c) => c.textContent === "—")).toBe(true);
  });
});

describe("Prise en charge et escalade", () => {
  const take = (panel: ReturnType<typeof within>, output = "OUT_001") => panel.findByRole("button", { name: `Prendre en charge l'incident de ${output}` });

  it("ne propose la prise en charge que pour un incident confirmé et en cours", async () => {
    vi.mocked(api.incidents).mockResolvedValueOnce([
      incident({ id: "live", output_id: "OUT_001" }),
      incident({ id: "done", output_id: "OUT_002", status: "closed" }),
      incident({ id: "weak", output_id: "OUT_003", status: "suspect", opened_at: null }),
    ]);
    render(<Dashboard />);
    const panel = within(await incidentsPanel());
    await take(panel);
    expect(panel.getAllByRole("button", { name: /Prendre en charge/ })).toHaveLength(1); // ni le résolu, ni le signal faible
  });

  it("prend en charge puis retire la prise en charge (connecté), avec le nom renvoyé par le serveur", async () => {
    vi.mocked(hasApiToken).mockReturnValue(true);
    vi.mocked(api.incidents).mockResolvedValueOnce([incident()]);
    vi.mocked(api.acknowledge).mockResolvedValueOnce(incident({ acknowledged_by: "alice", acknowledged_at: NOW }));
    vi.mocked(api.release).mockResolvedValueOnce(incident());
    const user = userEvent.setup();
    render(<Dashboard />);
    const panel = within(await incidentsPanel());
    await user.click(await take(panel));
    expect(api.acknowledge).toHaveBeenCalledWith("inc-1");
    await panel.findByText("pris en charge par alice");
    await user.click(await panel.findByRole("button", { name: "Retirer la prise en charge de l'incident de OUT_001" }));
    expect(api.release).toHaveBeenCalledWith("inc-1");
    await waitFor(() => expect(panel.queryByText(/pris en charge par/)).toBeNull());
    expect(await take(panel)).toBeTruthy(); // le bouton est revenu
  });

  it("affiche ce que le serveur sait déjà : responsable et escalade", async () => {
    vi.mocked(api.incidents).mockResolvedValueOnce([incident({ acknowledged_by: "bob", acknowledged_at: NOW, escalated_at: NOW })]);
    render(<Dashboard />);
    const panel = within(await incidentsPanel());
    await panel.findByText("pris en charge par bob");
    expect(panel.getByText("escaladé")).toBeTruthy();
  });

  it("invite à se connecter, sans appeler l'API, quand il n'y a pas de session", async () => {
    vi.mocked(api.incidents).mockResolvedValueOnce([incident()]);
    const user = userEvent.setup();
    render(<Dashboard />);
    const panel = within(await incidentsPanel());
    await user.click(await take(panel));
    expect((await panel.findByRole("alert")).textContent).toMatch(/Connectez-vous/);
    expect(api.acknowledge).not.toHaveBeenCalled();
  });

  it("montre le refus du serveur (409 : l'incident s'est résolu entre-temps) sans le masquer", async () => {
    vi.mocked(hasApiToken).mockReturnValue(true);
    vi.mocked(api.incidents).mockResolvedValueOnce([incident()]);
    vi.mocked(api.acknowledge).mockRejectedValueOnce(new Error("PUT /incidents/inc-1/ack -> 409 Seul un incident confirmé et en cours peut être pris en charge"));
    const user = userEvent.setup();
    render(<Dashboard />);
    const panel = within(await incidentsPanel());
    await user.click(await take(panel));
    expect((await panel.findByRole("alert")).textContent).toMatch(/Rejeté par l'API.*en cours/);
    expect(panel.queryByText(/pris en charge par/)).toBeNull();
  });
});
