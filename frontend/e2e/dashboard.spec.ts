/**
 * Tests de bout en bout : un vrai navigateur (Chromium), le vrai backend (e2e/server.py : boucle de
 * monitoring, détection, SQLite) et l'interface compilée, sur la même origine.
 *
 * Le backend injecte un incident déterministe sur OUT_001 (chute de trafic, 3 h 50 simulées, soit
 * ~14 s réelles) et va 8× plus vite que la démonstration : les tests lisent ses effets dans l'interface.
 */
import { expect, test, type Page } from "@playwright/test";

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
  const row2 = panel.getByRole("row", { name: /OUT_002/ });
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
