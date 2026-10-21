/**
 * Tests de bout en bout : un vrai navigateur (Chromium), le vrai backend (e2e/server.py : boucle de
 * monitoring, détection, SQLite) et l'interface compilée, sur la même origine.
 *
 * Le backend injecte un incident déterministe sur OUT_001 (chute de trafic, 3 h 50 simulées, soit
 * ~14 s réelles) et le même un pas plus tard sur OUT_002, déclarée en aval de OUT_001 ; il va 8× plus vite
 * que la démonstration : les tests lisent ses effets dans l'interface.
 */
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

// Rien ne doit casser en silence : aucune erreur JS, aucun échec de chargement, aucune requête hors origine.
const problemsByTest = new Map<string, string[]>();
const allowedByTest = new Map<string, RegExp[]>();
/** Un test qui provoque volontairement une erreur (mauvais mot de passe : 401) la déclare ici. */
const expectProblem = (testId: string, pattern: RegExp) => allowedByTest.set(testId, [...(allowedByTest.get(testId) ?? []), pattern]);

test.beforeEach(async ({ page, baseURL }, testInfo) => {
  const problems: string[] = [];
  page.on("pageerror", (e) => problems.push(`erreur JS : ${e.message}`));
  page.on("console", (m) => {
    if (m.type() === "error") problems.push(`console : ${m.text()}`);
  });
  page.on("requestfailed", (r) => problems.push(`requête échouée : ${r.url()} ${r.failure()?.errorText}`));
  page.on("request", (r) => {
    if (!r.url().startsWith(baseURL as string) && !r.url().startsWith("data:") && !r.url().startsWith("blob:")) {
      problems.push(`requête hors origine : ${r.url()}`);
    }
  });
  problemsByTest.set(testInfo.testId, problems);
});

test.afterEach(async ({}, testInfo) => {
  const allowed = allowedByTest.get(testInfo.testId) ?? [];
  const problems = (problemsByTest.get(testInfo.testId) ?? []).filter((p) => !allowed.some((a) => a.test(p)));
  expect(problems, problems.join("\n")).toEqual([]);
});

const chart = (page: Page, id: string) => page.getByRole("group", { name: new RegExp(`Trafic et pertes de ${id}`) });
const readout = (page: Page, id: string) => page.locator(`#readout-${id}`);

async function waitForData(page: Page, id = "OUT_001") {
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(/Signal Room/);
  // Le backend vient de démarrer : on attend que la courbe ait de vraies données (plusieurs segments).
  await expect
    .poll(async () => (await chart(page, id).locator('path[stroke-linejoin="round"]').first().getAttribute("d"))?.length ?? 0, { timeout: 40_000 })
    .toBeGreaterThan(300);
}

test("affiche les trois sorties et l'horloge du serveur, qui avance", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("status").filter({ hasText: /données simulées/i })).toBeVisible(); // le serveur de test utilise le Mock
  for (const id of ["OUT_001", "OUT_002", "OUT_003"]) {
    await expect(page.getByRole("button", { name: new RegExp(`Replier ${id}|Déplier ${id}`) })).toBeVisible();
  }
  await expect(page.getByText("live", { exact: true })).toBeVisible();
  await expect(page.getByText(/3 sorties/)).toBeVisible();
  const clock = page.locator(".nm-clock");
  await expect(clock).toHaveText(/^\d{2}:\d{2}$/);
  const first = await clock.textContent();
  await expect(clock).not.toHaveText(first as string); // l'horloge affichée est celle du serveur : elle bouge
});

test("les graphes tracent des données réelles, sans axe tronqué", async ({ page }) => {
  await waitForData(page);
  for (const id of ["OUT_001", "OUT_002", "OUT_003"]) {
    const svg = chart(page, id);
    await expect(svg).toBeVisible();
    // Libellés de l'axe des ordonnées : entièrement dans le cadre du graphe (régression : marge trop étroite).
    const box = (await svg.boundingBox())!;
    const labels = svg.locator("text").filter({ hasText: /(Mbps|Gbps|Kbps)$/ });
    expect(await labels.count()).toBeGreaterThanOrEqual(3);
    for (let i = 0; i < (await labels.count()); i++) {
      const b = (await labels.nth(i).boundingBox())!;
      expect(b.x, `libellé ${await labels.nth(i).textContent()} de ${id}`).toBeGreaterThanOrEqual(box.x - 0.5);
    }
  }
});

test("le libellé « maintenant » ne recouvre aucun libellé d'axe", async ({ page }) => {
  await waitForData(page);
  const svg = chart(page, "OUT_001");
  const now = (await svg.locator("text", { hasText: "maintenant" }).boundingBox())!;
  const labels = svg.locator("text").filter({ hasText: /%$|bps$/ });
  for (let i = 0; i < (await labels.count()); i++) {
    const b = (await labels.nth(i).boundingBox())!;
    const overlap = now.x < b.x + b.width && b.x < now.x + now.width && now.y < b.y + b.height && b.y < now.y + now.height;
    expect(overlap, `« maintenant » recouvre « ${await labels.nth(i).textContent()} »`).toBe(false);
  }
});

test("la souris lit les valeurs au point survolé (géométrie réelle)", async ({ page }) => {
  await waitForData(page);
  const svg = chart(page, "OUT_001");
  const out = readout(page, "OUT_001");
  await expect(out).toContainText("Survolez le graphe");
  const box = (await svg.boundingBox())!;
  await page.mouse.move(box.x + box.width * 0.25, box.y + box.height / 2);
  await expect(out).toContainText(/trafic .*(Mbps|Gbps|Kbps) · pertes [\d.]+ %/);
  const left = await out.textContent();
  await page.mouse.move(box.x + box.width * 0.8, box.y + box.height / 2);
  await expect(out).not.toHaveText(left as string); // un autre point, donc d'autres valeurs
  await page.mouse.move(box.x + box.width + 40, box.y - 40); // hors du graphe
  await expect(out).toContainText("Survolez le graphe");
});

test("le graphe se lit au clavier", async ({ page }) => {
  await waitForData(page);
  const svg = chart(page, "OUT_001");
  const out = readout(page, "OUT_001");
  await svg.focus();
  await expect(out).toContainText(/trafic/);
  const last = await out.textContent();
  await page.keyboard.press("ArrowLeft");
  await expect(out).not.toHaveText(last as string);
  await page.keyboard.press("Home");
  const first = await out.textContent();
  await page.keyboard.press("End");
  await expect(out).not.toHaveText(first as string);
  await page.keyboard.press("Escape");
  await expect(out).toContainText("Survolez le graphe");
});

test("un incident injecté s'ouvre puis se résout, sans bruit de signaux faibles", async ({ page }) => {
  await page.goto("/");
  const alerts = page.getByRole("region", { name: /alertes récentes/i });
  const incidents = page.getByRole("region", { name: /incidents/i });
  await expect(alerts).toContainText(/OUT_001 — opened/, { timeout: 60_000 });
  await expect(alerts).toContainText(/OUT_001 — closed : traffic_drop/, { timeout: 60_000 });
  await expect(incidents).toContainText(/OUT_001 — traffic_drop \(/);
  // Le bruit (signal faible jamais confirmé, étapes intermédiaires) n'a rien à faire dans « alertes ».
  await expect(alerts).not.toContainText(/suspect|Signal faible|recovering/);
  await expect(page.getByText(/0 incidents actifs/)).toBeVisible();
});

test("le rapport de disponibilité reflète l'incident sur la période observée", async ({ page }) => {
  await page.goto("/");
  const panel = page.getByRole("region", { name: /disponibilité/i });
  await expect(page.getByRole("region", { name: /alertes récentes/i })).toContainText(/OUT_001 — closed : traffic_drop/, { timeout: 60_000 });
  const row1 = panel.getByRole("row", { name: /OUT_001/ });
  const row2 = panel.getByRole("row", { name: /OUT_003/ }); // OUT_002 a aussi son incident (aval de OUT_001)
  await expect(row1).toContainText(/\d+ h \d+|\d+ min/, { timeout: 70_000 });
  // Indisponibilité = la chute injectée, de son début réel à sa fin réelle : 46 pas de 5 min = 3 h 50 (début rétro-daté).
  await expect(row1).toContainText("3 h 50", { timeout: 70_000 });
  const availability = row1.getByRole("cell").nth(1); // 0 : période observée, 1 : disponibilité
  await expect(availability).toHaveText(/^\d{2}\.\d{3} %$/);
  await expect(availability).not.toHaveText("100.000 %"); // un incident critique a eu lieu
  await expect(row2).toContainText("100.000 %");
  // La période observée est affichée : le rapport ne se prévaut pas des 30 jours demandés.
  await expect(row1.getByRole("cell").first()).toHaveText(/^\d+( h \d+| min| h)$/);

  await expect(panel.getByRole("button", { name: "30 j" })).toHaveAttribute("aria-pressed", "true");
  await panel.getByRole("button", { name: "7 j" }).click();
  await expect(panel.getByRole("button", { name: "7 j" })).toHaveAttribute("aria-pressed", "true");

  const [download] = await Promise.all([page.waitForEvent("download"), panel.getByRole("link", { name: /csv/i }).click()]);
  expect(download.suggestedFilename()).toMatch(/^disponibilite-\d{8}-\d{8}\.csv$/);
  const csv = await (await page.request.get(await panel.getByRole("link", { name: /csv/i }).getAttribute("href") as string)).text();
  expect(csv.split("\n")[0]).toContain("availability_percent");
  expect(csv).toContain("OUT_001");
});

test("connexion : refus générique, session, puis modification effective de la configuration", async ({ page, request }, testInfo) => {
  expectProblem(testInfo.testId, /status of 401/); // le mauvais mot de passe ci-dessous provoque ce 401, journalisé par le navigateur
  await page.goto("/");
  await page.getByRole("button", { name: "Déplier la configuration", exact: true }).click();
  await page.getByLabel("Identifiant").fill("alice");
  await page.getByLabel("Mot de passe").fill("mauvais");
  await page.getByRole("button", { name: /se connecter/i }).click();
  await expect(page.getByRole("alert").filter({ hasText: /incorrect/ })).toBeVisible();

  await page.getByLabel("Mot de passe").fill("e2e-pass");
  await page.getByRole("button", { name: /se connecter/i }).click();
  await expect(page.getByText(/connecté en tant que/i)).toBeVisible();
  // Rien de la session dans le stockage du navigateur.
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0);

  const before = (await (await request.get("/config")).json()).incident.open_score;
  const field = page.getByLabel(/seuil open \(open_score\)/i).first();
  await field.fill("0.8");
  await page.getByRole("button", { name: /envoyer à l'api/i }).click();
  await expect.poll(async () => (await (await request.get("/config")).json()).incident.open_score).toBe(0.8);
  await field.fill(String(before));
  await page.getByRole("button", { name: /envoyer à l'api/i }).click();
  await expect.poll(async () => (await (await request.get("/config")).json()).incident.open_score).toBe(before);
});

test("trafic réseau : gzip, 304 et rafraîchissement incrémental dans un vrai navigateur", async ({ page }) => {
  const seen: { url: string; status: number; encoding: string | undefined }[] = [];
  page.on("response", (r) => seen.push({ url: r.url(), status: r.status(), encoding: r.headers()["content-encoding"] }));
  await waitForData(page);
  await page.waitForTimeout(12_000); // quelques cycles de rafraîchissement
  // Requêtes du graphe détaillé : sans `until` (le tableau comparatif, lui, interroge des fenêtres bornées).
  const history = seen.filter((r) => /\/outputs\/OUT_001\/history/.test(r.url) && !r.url.includes("until="));
  expect(history.length).toBeGreaterThanOrEqual(3);
  expect(history[0].url).toContain("span_hours="); // première charge : toute la fenêtre
  expect(history.slice(1).every((r) => r.url.includes("since="))).toBe(true); // ensuite : seulement les nouveaux points
  expect(history[0].encoding).toBe("gzip");
  const polls = seen.filter((r) => /\/outputs$|\/monitoring\/status$/.test(r.url));
  expect(polls.length).toBeGreaterThanOrEqual(6);
});

test("aucune requête pendant que l'onglet est masqué", async ({ page }) => {
  await waitForData(page);
  let requests = 0;
  page.on("request", (r) => {
    if (r.url().includes("/api/") || /\/(outputs|incidents|alerts|monitoring|reports)/.test(new URL(r.url()).pathname)) requests++;
  });
  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await page.waitForTimeout(1_000);
  requests = 0;
  await page.waitForTimeout(10_000); // 10 s : plusieurs périodes de polling si rien ne les suspendait
  expect(requests).toBe(0);
  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", { configurable: true, get: () => false });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect.poll(() => requests, { timeout: 5_000 }).toBeGreaterThan(0); // retour : rafraîchissement immédiat
});

test("sur un écran de téléphone, la page ne déborde pas horizontalement", async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 375, height: 800 } });
  const page = await context.newPage();
  await waitForData(page);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow, "défilement horizontal de la page").toBeLessThanOrEqual(0);
  await expect(page.getByRole("main")).toBeVisible();
  await context.close();
});

async function signIn(page: Page, user = "alice") {
  await page.goto("/");
  await page.getByRole("button", { name: "Déplier la configuration", exact: true }).click();
  await page.getByLabel("Identifiant").fill(user);
  await page.getByLabel("Mot de passe").fill("e2e-pass");
  await page.getByRole("button", { name: /se connecter/i }).click();
  await expect(page.getByText(/connecté en tant que/i)).toBeVisible();
}

const incidentsPanel = (page: Page) => page.getByRole("region", { name: /^incidents/i });

test("un incident en aval d'un incident déjà signalé est rattaché à sa cause et retenu", async ({ page, request }) => {
  await page.goto("/");
  const panel = incidentsPanel(page);
  const down = panel.locator(".nm-log-row", { hasText: /OUT_002 — traffic_drop/ });
  const up = panel.locator(".nm-log-row", { hasText: /OUT_001 — traffic_drop/ });
  await expect(down).toContainText("cause : OUT_001", { timeout: 60_000 });
  await expect(up).not.toContainText("cause :"); // la cause, elle, n'a pas de cause
  const link = down.getByRole("link", { name: /cause : OUT_001/ });
  const target = (await link.getAttribute("href")) as string; // « #incident-<id> » : mène à la ligne de la cause
  expect(await panel.locator(target).count()).toBe(1);

  // Côté serveur : l'aval est rattaché au bon incident, ses alertes sont retenues, celles de l'amont ne le sont pas.
  const incidents = (await (await request.get("/incidents")).json()) as { id: string; output_id: string; inhibited: boolean; root_cause_incident_id: string | null }[];
  const upstream = incidents.find((i) => i.output_id === "OUT_001")!;
  const downstream = incidents.find((i) => i.output_id === "OUT_002")!;
  expect(target).toBe(`#incident-${upstream.id}`);
  expect(downstream).toMatchObject({ inhibited: true, root_cause_incident_id: upstream.id });
  const alerts = (await (await request.get("/alerts?all=true&limit=500")).json()) as { output_id: string; suppressed: string | null; event: string }[];
  expect(alerts.filter((a) => a.output_id === "OUT_002" && a.event === "opened").map((a) => a.suppressed)).toEqual(["inhibited"]);
  expect(alerts.filter((a) => a.output_id === "OUT_001").every((a) => a.suppressed === null)).toBe(true);
  // Et l'interface le dit dans le panneau d'alertes.
  await expect(page.getByRole("region", { name: /alertes récentes/i })).toContainText("retenue : cause amont");
});

test("sans session, juger un incident invite à se connecter et n'envoie rien", async ({ page }) => {
  const puts: string[] = [];
  page.on("request", (r) => { if (r.method() === "PUT") puts.push(r.url()); });
  await page.goto("/");
  await incidentsPanel(page).getByRole("button", { name: /Juger l'incident de OUT_001 : faux positif/ }).click({ timeout: 60_000 });
  await expect(incidentsPanel(page).getByRole("alert")).toContainText(/Connectez-vous \(panneau Configuration\)/);
  expect(puts).toEqual([]);
});

test("juger un incident change la précision mesurée et le rapport de disponibilité, puis le verdict se retire", async ({ page }) => {
  await signIn(page);
  const panel = incidentsPanel(page);
  const precision = panel.getByRole("status");
  const report = page.getByRole("region", { name: /disponibilité/i });
  const row = report.getByRole("row", { name: /OUT_001/ });
  const refreshReport = async (days: string, other: string) => { // le rapport se rafraîchit toutes les 60 s : on le force en changeant de période
    await report.getByRole("button", { name: other }).click();
    await report.getByRole("button", { name: days }).click();
  };
  await expect(panel).toContainText(/OUT_001 — traffic_drop \(closed/, { timeout: 60_000 });
  await expect(row).toContainText("3 h 50", { timeout: 70_000 });
  await expect(precision).toContainText(/Précision mesurée sur 30 j : — \(0 vrai, 0 faux ; \d+ sans verdict/); // aucun verdict : pas de pourcentage

  await panel.getByRole("button", { name: "Juger l'incident de OUT_001 : faux positif" }).click();
  await expect(precision).toContainText(/Précision mesurée sur 30 j : 0\.0 % \(0 vrai, 1 faux ; \d+ sans verdict — \d+ % jugés\)/);
  await expect(panel.locator(".nm-log-row", { hasText: /OUT_001 — traffic_drop/ })).toContainText("faux positif");
  await refreshReport("30 j", "7 j");
  await expect(row).toContainText(/\+1 faux positif exclu\)/); // exclu du rapport, et dit comme tel
  await expect(row.getByRole("cell").nth(1)).toHaveText("100.000 %");

  await panel.getByRole("button", { name: "Juger l'incident de OUT_001 : vrai incident" }).click();
  await expect(precision).toContainText(/Précision mesurée sur 30 j : 100\.0 % \(1 vrai, 0 faux/);
  await refreshReport("30 j", "7 j");
  await expect(row).toContainText("3 h 50"); // de nouveau compté
  await expect(row).not.toContainText("faux positif exclu");

  await panel.getByRole("button", { name: "Retirer le verdict de l'incident de OUT_001" }).click();
  await expect(precision).toContainText(/Précision mesurée sur 30 j : — \(0 vrai, 0 faux/);
  await expect(panel.getByRole("button", { name: /Retirer le verdict/ })).toHaveCount(0);
});

test("créer puis annuler une maintenance : horaires relatifs à l'horloge du serveur", async ({ page, request }) => {
  await signIn(page);
  const panel = page.getByRole("region", { name: /maintenance planifiée/i });
  await panel.getByLabel("Sortie").selectOption("OUT_003");
  await panel.getByLabel("Début dans (min)").fill("600");
  await panel.getByLabel("Durée (min)").fill("120");
  await panel.getByLabel("Motif").fill("E2E remplacement de carte");
  await panel.getByRole("button", { name: "Planifier" }).click();
  const row = panel.locator(".nm-log-row", { hasText: "E2E remplacement de carte" });
  await expect(row).toContainText("OUT_003");
  await expect(row).toContainText("à venir");

  // L'horloge du serveur est simulée (8× plus vite) et sans rapport avec celle du navigateur : si le début
  // était calculé par le navigateur, il serait très loin de « maintenant + 600 min » du serveur.
  const status = (await (await request.get("/monitoring/status")).json()) as { simulated_now: string };
  const windows = (await (await request.get("/maintenance")).json()) as { output_id: string; start: string; end: string; reason: string }[];
  const mine = windows.find((w) => w.reason === "E2E remplacement de carte")!;
  const minutesAhead = (new Date(mine.start).getTime() - new Date(status.simulated_now).getTime()) / 60_000;
  expect(Math.abs(minutesAhead - 600)).toBeLessThan(30);
  expect((new Date(mine.end).getTime() - new Date(mine.start).getTime()) / 60_000).toBe(120);

  await row.getByRole("button", { name: /Annuler la maintenance/ }).click();
  await expect(row).toContainText("annulée");
  await expect(row.getByRole("button", { name: /Annuler la maintenance/ })).toHaveCount(0);
  const after = (await (await request.get("/maintenance")).json()) as { reason: string; state: string }[];
  expect(after.find((w) => w.reason === "E2E remplacement de carte")?.state).toBe("cancelled");
});

test("le comparatif charge ses valeurs en une requête par rafraîchissement, sans interroger l'historique par sortie et par instant", async ({ page }) => {
  const nearest: URLSearchParams[] = [];
  const boundedHistory: string[] = [];
  page.on("request", (r) => {
    const url = new URL(r.url());
    if (url.pathname === "/outputs/nearest") nearest.push(url.searchParams);
    if (/^\/outputs\/[^/]+\/history$/.test(url.pathname) && url.searchParams.has("until")) boundedHistory.push(r.url()); // l'ancienne méthode
  });
  await page.goto("/");
  const row = page.getByRole("region", { name: /comparatif temporel/i }).getByRole("row", { name: /OUT_001/ });
  await expect(row.getByRole("cell").nth(1)).toContainText(/\d+\.\d\d/, { timeout: 30_000 }); // une vraie valeur, issue du vrai backend
  await expect(row.getByRole("cell").nth(3)).toHaveText("—"); // −1 j : le stockage ne remonte pas avant le démarrage, et le dit
  expect(nearest.length).toBeGreaterThanOrEqual(1);
  for (const params of nearest) expect(params.getAll("ago_minutes")).toEqual(["0", "5", "10", "1440", "1445", "10080", "10085"]);
  expect(boundedHistory).toEqual([]); // avant : 3 sorties × 4 fenêtres × 2 instants = 24 requêtes par rafraîchissement
});

test("sans prise en charge, un incident escalade une seule fois ; celui expliqué par une cause amont jamais", async ({ page, request }) => {
  await page.goto("/");
  const panel = incidentsPanel(page);
  await expect(panel.locator(".nm-log-row", { hasText: /OUT_001 — traffic_drop/ })).toContainText("escaladé", { timeout: 60_000 }); // 3 h simulées après l'ouverture
  await expect(panel.locator(".nm-log-row", { hasText: /OUT_002 — traffic_drop/ })).not.toContainText("escaladé");
  const alerts = (await (await request.get("/alerts?all=true&limit=500")).json()) as { output_id: string; event: string; incident_id: string }[];
  const escalated = alerts.filter((a) => a.event === "escalated" && a.output_id !== "OUT_003"); // OUT_003 a son propre incident tardif
  expect(escalated.map((a) => a.output_id)).toEqual(["OUT_001"]); // une seule, et pas pour l'aval
  await expect(page.getByRole("region", { name: /alertes récentes/i })).toContainText("escalated");
});

test("prendre en charge un incident en cours le dit dans l'interface, persiste côté serveur et se retire", async ({ page, request }) => {
  await signIn(page);
  const panel = incidentsPanel(page);
  const take = panel.getByRole("button", { name: "Prendre en charge l'incident de OUT_003" });
  await expect(take).toBeVisible({ timeout: 90_000 }); // l'incident tardif de OUT_003
  await take.click();
  const row = panel.locator(".nm-log-row", { hasText: /OUT_003 — traffic_drop/ });
  await expect(row).toContainText("pris en charge par alice");
  type Incident = { output_id: string; acknowledged_by: string | null; escalated_at: string | null; opened_at: string };
  const get = async () => ((await (await request.get("/incidents")).json()) as Incident[]).find((i) => i.output_id === "OUT_003")!;
  const incident = await get();
  expect(incident.acknowledged_by).toBe("alice");

  // Pris en charge à temps (moins de 3 h simulées après l'ouverture), il ne doit JAMAIS escalader, même une fois le délai dépassé.
  const nowOf = async () => new Date(((await (await request.get("/monitoring/status")).json()) as { simulated_now: string }).simulated_now).getTime();
  const deadline = new Date(incident.opened_at).getTime() + 3 * 3600_000;
  if ((await nowOf()) < deadline - 30 * 60_000) {
    await expect.poll(nowOf, { timeout: 40_000, intervals: [500] }).toBeGreaterThan(deadline + 10 * 60_000);
    expect((await get()).escalated_at).toBeNull();
  } else {
    test.info().annotations.push({ type: "note", description: "pris en charge trop tard (machine lente) : l'absence d'escalade n'est pas vérifiée ici" });
  }

  await page.reload();
  await expect(panel.locator(".nm-log-row", { hasText: /OUT_003 — traffic_drop/ })).toContainText("pris en charge par alice"); // vient du serveur
  await page.getByRole("button", { name: "Déplier la configuration", exact: true }).click();
  await page.getByLabel("Identifiant").fill("alice");
  await page.getByLabel("Mot de passe").fill("e2e-pass");
  await page.getByRole("button", { name: /se connecter/i }).click();
  await expect(page.getByText(/connecté en tant que/i)).toBeVisible();
  await panel.getByRole("button", { name: "Retirer la prise en charge de l'incident de OUT_003" }).click();
  await expect(panel.locator(".nm-log-row", { hasText: /OUT_003 — traffic_drop/ })).not.toContainText("pris en charge par");
  expect((await get()).acknowledged_by).toBeNull();
});

async function tokenOf(request: APIRequestContext, user: string) {
  const res = await request.post("/auth/login", { data: { username: user, password: "e2e-pass" } });
  const body = (await res.json()) as { access_token: string; role: string };
  return { headers: { Authorization: `Bearer ${body.access_token}` }, role: body.role };
}

test("un lecteur voit tout mais ne peut rien modifier : l'interface le dit, et le serveur refuse", async ({ page, request }) => {
  await signIn(page, "carol");
  await expect(page.getByText(/connecté en tant que/i)).toContainText("carol (lecteur");
  const verdict = incidentsPanel(page).getByRole("button", { name: "Juger l'incident de OUT_001 : faux positif" });
  await expect(verdict).toBeDisabled({ timeout: 60_000 });
  await expect(verdict).toHaveAttribute("title", "Réservé au rôle opérateur ou supérieur");
  await expect(page.getByRole("region", { name: /maintenance planifiée/i }).getByRole("button", { name: "Planifier" })).toBeDisabled();
  const save = page.getByRole("button", { name: /envoyer à l'API/i });
  await expect(save).toBeDisabled();
  await expect(save).toHaveAttribute("title", "Réservé au rôle administrateur ou supérieur");

  // Désactiver un bouton n'est qu'un confort : c'est le serveur qui refuse, avec une raison lisible.
  const { headers, role } = await tokenOf(request, "carol");
  expect(role).toBe("viewer");
  const incidents = (await (await request.get("/incidents")).json()) as { id: string; output_id: string }[];
  const incident = incidents.find((i) => i.output_id === "OUT_001")!;
  const refused = await request.put(`/incidents/${incident.id}/feedback`, { headers, data: { verdict: "false_positive" } });
  expect(refused.status()).toBe(403);
  expect(((await refused.json()) as { detail: string }).detail).toMatch(/« operator » est requis.*« viewer »/);
  expect((await request.get("/notifications", { headers })).status()).toBe(200); // la lecture protégée reste permise
});

test("un opérateur juge et planifie, mais ne touche ni aux seuils ni à l'arrêt de la surveillance", async ({ page, request }) => {
  await signIn(page, "bob");
  await expect(page.getByText(/connecté en tant que/i)).toContainText("bob (opérateur");
  await expect(incidentsPanel(page).getByRole("button", { name: "Juger l'incident de OUT_001 : faux positif" })).toBeEnabled({ timeout: 60_000 });
  await expect(page.getByRole("region", { name: /maintenance planifiée/i }).getByRole("button", { name: "Planifier" })).toBeDisabled(); // pas de motif saisi
  await expect(page.getByRole("button", { name: /envoyer à l'API/i })).toBeDisabled(); // réservé à l'administrateur

  const { headers, role } = await tokenOf(request, "bob");
  expect(role).toBe("operator");
  expect((await request.put("/config", { headers, data: { open_score: 0.7 } })).status()).toBe(403);
  expect((await request.post("/monitoring/stop", { headers })).status()).toBe(403);
  expect((await request.get("/monitoring/status")).status()).toBe(200);
  const incidents = (await (await request.get("/incidents")).json()) as { id: string; output_id: string }[];
  const id = incidents.find((i) => i.output_id === "OUT_001")!.id;
  expect((await request.put(`/incidents/${id}/feedback`, { headers, data: { verdict: "true_positive" } })).status()).toBe(200);
  expect((await request.put(`/incidents/${id}/feedback`, { headers, data: { verdict: null } })).status()).toBe(200); // état rétabli
});

test("un compte au format historique (sans rôle) garde tous ses droits d'administrateur", async ({ page, request }) => {
  await signIn(page, "alice");
  await expect(page.getByText(/connecté en tant que/i)).toContainText("alice (administrateur");
  await expect(page.getByRole("button", { name: /envoyer à l'API/i })).toBeEnabled();
  const { headers, role } = await tokenOf(request, "alice");
  expect(role).toBe("admin");
  const config = (await (await request.get("/config")).json()) as { alerts: unknown; incident: { open_score: number } };
  const same = await request.put("/config", { headers, data: { open_score: config.incident.open_score } });
  expect(same.status()).toBe(200); // modifier la configuration passe, avec la valeur déjà en place
});
