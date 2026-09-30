import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Dashboard from "./Dashboard.jsx";
import ErrorBoundary from "./ErrorBoundary.jsx";

// Horloge SIMULÉE du serveur : volontairement très différente de l'horloge réelle du navigateur.
const SIM_NOW = new Date("2031-01-01T10:00:00Z");
const IDS = ["OUT_001", "OUT_002", "OUT_003"];

const sample = (id, minutesAgo, traffic = 250, loss = 0.1) => ({
  timestamp: new Date(SIM_NOW.getTime() - minutesAgo * 60_000).toISOString(),
  output_id: id,
  traffic_mbps: traffic,
  loss_percent: loss,
});

const historyOf = (id, n) => Array.from({ length: n }, (_, i) => sample(id, (n - i) * 5, 200 + (i % 50), 0.1 + (i % 7) / 100));

function makeServer({ historyLength = 40, fail = false } = {}) {
  const state = { fail, calls: [] };
  const fetchMock = vi.fn(async (input) => {
    const url = new URL(input);
    state.calls.push({ path: url.pathname, params: url.searchParams });
    if (state.fail) throw new TypeError("Failed to fetch");
    const json = (data) => ({ ok: true, status: 200, json: async () => data, text: async () => "" });
    const m = url.pathname.match(/^\/outputs\/([^/]+)\/(history|forecast\/ahead|forecast|snapshots)$/);
    if (url.pathname === "/outputs") {
      return json(IDS.map((id, i) => ({ output_id: id, latest_sample: sample(id, 5, 250 + i), status: i === 0 ? "open" : "normal", active_incident_id: null })));
    }
    if (url.pathname === "/incidents") {
      return json([
        {
          id: "inc-1", output_id: "OUT_001", status: "open", severity: "critical", incident_type: "traffic_drop",
          last_seen_at: SIM_NOW.toISOString(), score: 0.9, current_score: 0.76, evidence: [],
        },
      ]);
    }
    if (url.pathname === "/monitoring/status") {
      return json({ running: true, calibrated: true, simulated_now: SIM_NOW.toISOString(), interval_seconds: 300, seconds_per_tick: 2, outputs: IDS });
    }
    if (url.pathname === "/config") {
      return json({ incident: { open_score: 0.75, close_score: 0.4, warning_score: 0.5, minimum_duration: 600, recovery_duration: 600 } });
    }
    if (m) {
      const [, id, kind] = m;
      if (kind === "history") return json(historyOf(id, historyLength));
      if (kind === "snapshots") {
        return json(url.searchParams.getAll("offsets").map((o) => ({ offset_seconds: Number(o), current: sample(id, 5, 2000, 0.5), previous: sample(id, 10, 1000, 0.25) })));
      }
      return json([]);
    }
    return { ok: false, status: 404, text: async () => "not found" };
  });
  return { state, fetchMock };
}

const callsTo = (server, path) => server.state.calls.filter((c) => c.path === path || c.path.endsWith(path));

async function renderReady(server) {
  vi.stubGlobal("fetch", server.fetchMock);
  const utils = render(<Dashboard />);
  await screen.findByRole("heading", { level: 1, name: /Signal Room/ });
  return utils;
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("structure et accessibilité", () => {
  it("expose un h1, des landmarks, un lien d'évitement et un journal live", async () => {
    await renderReady(makeServer());
    expect(screen.getByRole("banner")).toBeInTheDocument();
    expect(screen.getByRole("main")).toBeInTheDocument();
    const skip = screen.getByRole("link", { name: /Aller au contenu/ });
    expect(skip).toHaveAttribute("href", "#main");
    expect(document.getElementById("main")).toBeTruthy();

    for (const name of [/Sorties/, /Comparatif temporel/, /Configuration/, /Incidents/]) {
      expect(screen.getByRole("heading", { level: 2, name })).toBeInTheDocument();
    }
    const log = screen.getByRole("log", { name: /Journal des incidents/ });
    expect(log).toHaveAttribute("aria-live", "polite");
    expect(within(log).getByText(/traffic_drop/)).toBeInTheDocument();
  });

  it("affiche le score maximal ET le score actuel quand ils diffèrent (pas de contradiction apparente)", async () => {
    await renderReady(makeServer());
    const log = screen.getByRole("log");
    expect(within(log).getByText(/score max 0\.90, actuel 0\.76/)).toBeInTheDocument();
  });

  it("le graphe est décrit aux lecteurs d'écran (role=img + libellé synthétique)", async () => {
    await renderReady(makeServer());
    const img = await screen.findByRole("img", { name: /Trafic et pertes de OUT_001 sur 12 h : 40 mesures/ });
    expect(img.getAttribute("aria-label")).toMatch(/dernière valeur .* Mbps/);
    expect(img.getAttribute("aria-label")).toMatch(/pertes [\d.]+ %/);
  });

  it("le tableau a une légende, des en-têtes de ligne et de colonne", async () => {
    await renderReady(makeServer());
    const table = await screen.findByRole("table", { name: /Comparatif temporel/ });
    expect(within(table).getAllByRole("rowheader").map((th) => th.textContent)).toEqual(IDS);
    expect(within(table).getAllByRole("columnheader").length).toBeGreaterThanOrEqual(9);
  });
});

describe("clavier (repli des panneaux)", () => {
  it("Entrée et Espace sur le bouton replient/déplient EXACTEMENT une fois", async () => {
    const user = userEvent.setup();
    await renderReady(makeServer());
    const button = screen.getByRole("button", { name: "Replier OUT_001" });
    expect(button).toHaveAttribute("aria-expanded", "true");

    button.focus();
    await user.keyboard("{Enter}");
    expect(screen.getByRole("button", { name: "Déplier OUT_001" })).toHaveAttribute("aria-expanded", "false");

    screen.getByRole("button", { name: "Déplier OUT_001" }).focus();
    await user.keyboard(" ");
    expect(screen.getByRole("button", { name: "Replier OUT_001" })).toHaveAttribute("aria-expanded", "true");
  });

  it("la barre d'en-tête n'est plus un faux bouton imbriqué, mais reste cliquable à la souris", async () => {
    const user = userEvent.setup();
    await renderReady(makeServer());
    const head = [...document.querySelectorAll(".nm-card-id")].find((el) => el.textContent === "OUT_002").closest(".nm-channel-head");
    expect(head).not.toHaveAttribute("role");
    expect(head).not.toHaveAttribute("tabindex");
    await user.click(head);
    expect(screen.getByRole("button", { name: "Déplier OUT_002" })).toBeInTheDocument();
  });

  it("un clic sur le bouton ne déclenche pas aussi le clic de la barre parente", async () => {
    const user = userEvent.setup();
    await renderReady(makeServer());
    await user.click(screen.getByRole("button", { name: "Replier le tableau" }));
    expect(screen.getByRole("button", { name: "Déplier le tableau" })).toHaveAttribute("aria-expanded", "false");
  });

  it("CONTRÔLE : le motif d'origine (bouton dans un div role=button + onKeyDown) annulait la touche Entrée", async () => {
    function LegacyHeader() {
      const [open, setOpen] = useState(true);
      const toggle = () => setOpen((v) => !v);
      return (
        <div onClick={toggle} role="button" tabIndex={0} onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") toggle(); }}>
          <button type="button" aria-expanded={open} aria-label="legacy" onClick={(e) => { e.stopPropagation(); toggle(); }}>x</button>
        </div>
      );
    }
    const user = userEvent.setup();
    render(<LegacyHeader />);
    const btn = screen.getByRole("button", { name: "legacy" });
    btn.focus();
    await user.keyboard("{Enter}");
    // click natif (toggle) + keydown remontant au parent (toggle) => retour à l'état initial : le bouton « ne fait rien ».
    expect(btn).toHaveAttribute("aria-expanded", "true");
  });
});

describe("contrat d'horloge et requêtes", () => {
  it("les fenêtres des graphes sont calées sur l'horloge SIMULÉE du serveur, sans `until`", async () => {
    const server = makeServer();
    await renderReady(server);
    await screen.findByRole("img", { name: /OUT_001/ });
    const histories = callsTo(server, "/history");
    expect(histories.length).toBeGreaterThanOrEqual(3);
    for (const call of histories) {
      const since = new Date(call.params.get("since")).getTime();
      expect(since).toBe(SIM_NOW.getTime() - 12 * 3600_000); // simNow − 12 h, pas « maintenant du navigateur − 12 h »
      expect(call.params.has("until")).toBe(false);
    }
  });

  it("le tableau fait UNE requête /snapshots par sortie (et aucune requête /history de sa part)", async () => {
    const server = makeServer();
    await renderReady(server);
    await screen.findByRole("table");
    await waitFor(() => expect(callsTo(server, "/snapshots")).toHaveLength(3));
    const snaps = callsTo(server, "/snapshots");
    expect(snaps.map((c) => c.path).sort()).toEqual(IDS.map((id) => `/outputs/${id}/snapshots`));
    expect(snaps[0].params.getAll("offsets")).toEqual(["0", "300", "86400", "604800"]);
    // 3 graphes ouverts = 3 requêtes /history ; l'ancien tableau en ajoutait 24.
    expect(callsTo(server, "/history")).toHaveLength(3);
  });

  it("affiche les valeurs du serveur dans le tableau (Gbps)", async () => {
    await renderReady(makeServer());
    const table = await screen.findByRole("table");
    await waitFor(() => expect(within(table).getAllByText("2.00").length).toBeGreaterThan(0));
    expect(within(table).getAllByText(/▲ 100\.0%/).length).toBeGreaterThan(0); // 2000 vs 1000
  });
});

describe("graphe : curseur et volume de données", () => {
  it("le curseur clavier (range) met à jour la lecture de la valeur et dessine le repère", async () => {
    await renderReady(makeServer());
    const slider = (await screen.findAllByRole("slider", { name: /Parcourir les valeurs du graphe de OUT_001/ }))[0];
    expect(screen.getAllByTestId("chart-readout")[0].textContent).toMatch(/Survolez le graphe/);
    expect(screen.queryByTestId("chart-cursor")).toBeNull();

    fireEvent.change(slider, { target: { value: "3" } });
    const readout = screen.getAllByTestId("chart-readout")[0].textContent;
    expect(readout).toMatch(/Mbps/);
    expect(readout).toMatch(/% de pertes/);
    expect(slider.getAttribute("aria-valuetext")).toBe(readout);
    expect(screen.getAllByTestId("chart-cursor").length).toBe(1);
  });

  it("limite le nombre de points tracés sur une longue série (8 640 points ≈ 1 mois)", async () => {
    const server = makeServer({ historyLength: 8640 });
    const { container } = await renderReady(server);
    await screen.findAllByRole("img", { name: /8640 mesures/ });
    const counts = [...container.querySelectorAll("svg path")].map((p) => (p.getAttribute("d").match(/[ML]/g) || []).length);
    expect(Math.max(...counts)).toBeGreaterThan(100);
    expect(Math.max(...counts)).toBeLessThanOrEqual(1100);
  });
});

describe("robustesse réseau", () => {
  it("échec initial : message d'erreur explicite (role=alert), pas d'écran blanc", async () => {
    vi.stubGlobal("fetch", makeServer({ fail: true }).fetchMock);
    render(<Dashboard />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/Impossible de joindre l'API/);
  });

  it("panne après un premier succès : les données restent affichées avec un avertissement, puis reprise", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const server = makeServer();
    await renderReady(server);
    expect(screen.getByText(/màj il y a/)).toBeInTheDocument();

    server.state.fail = true;
    await act(async () => { await vi.advanceTimersByTimeAsync(4100); });
    const banner = await screen.findByRole("alert");
    expect(banner).toHaveTextContent(/Connexion à l'API perdue/);
    expect(screen.getAllByText("OUT_001").length).toBeGreaterThan(0); // toujours affiché

    server.state.fail = false;
    await act(async () => { await vi.advanceTimersByTimeAsync(8100); });
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  });

  it("espace les tentatives après un échec (4 s puis 8 s) au lieu de marteler l'API", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const server = makeServer();
    await renderReady(server);
    const count = () => server.state.calls.filter((c) => c.path === "/outputs").length;
    const before = count();

    server.state.fail = true;
    await act(async () => { await vi.advanceTimersByTimeAsync(4100); });
    const afterFirstFailure = count();
    expect(afterFirstFailure).toBe(before + 1);

    await act(async () => { await vi.advanceTimersByTimeAsync(4000); }); // < 8 s depuis l'échec : pas de nouvelle tentative
    expect(count()).toBe(afterFirstFailure);
    await act(async () => { await vi.advanceTimersByTimeAsync(4200); }); // ≥ 8 s : nouvelle tentative
    expect(count()).toBe(afterFirstFailure + 1);
  });

  it("annule les requêtes en vol au démontage (aucune fuite)", async () => {
    const signals = [];
    // fetch qui ne répond jamais : les requêtes restent « en vol » jusqu'à annulation
    vi.stubGlobal(
      "fetch",
      vi.fn((_url, init) => {
        signals.push(init.signal);
        return new Promise((_resolve, reject) => {
          init.signal.addEventListener("abort", () => reject(init.signal.reason));
        });
      })
    );
    const { unmount } = render(<Dashboard />);
    await waitFor(() => expect(signals.length).toBeGreaterThanOrEqual(3)); // outputs + incidents + status (+ config)
    expect(signals.every((sig) => !sig.aborted)).toBe(true);
    unmount();
    expect(signals.every((sig) => sig.aborted)).toBe(true);
  });
});

describe("ErrorBoundary", () => {
  it("affiche un message et permet de réessayer quand un composant plante", async () => {
    const user = userEvent.setup();
    let shouldThrow = true;
    const Boom = () => {
      if (shouldThrow) throw new Error("boum");
      return <p>rétabli</p>;
    };
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    render(<ErrorBoundary><Boom /></ErrorBoundary>);
    expect(screen.getByRole("alert")).toHaveTextContent(/boum/);
    shouldThrow = false;
    await user.click(screen.getByRole("button", { name: "Réessayer" }));
    expect(screen.getByText("rétabli")).toBeInTheDocument();
    spy.mockRestore();
  });
});
