import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

/** Audit d'accessibilité automatisé (axe-core, règles WCAG 2.x A/AA) sur la page réelle. */
test("aucune violation d'accessibilité sérieuse ou critique (WCAG 2.x A/AA)", async ({ page, request }) => {
  // Les panneaux d'incidents (pastilles, boutons de verdict) et de maintenance doivent être audités AVEC du contenu :
  // une vraie fenêtre (créée par l'API, annulée ensuite) et un incident confirmé avec sa cause amont.
  const login = await request.post("/auth/login", { data: { username: "alice", password: "e2e-pass" } });
  const headers = { Authorization: `Bearer ${(await login.json()).access_token}` };
  const created = await request.post("/maintenance", { headers, data: { output_id: "OUT_003", reason: "audit axe", starts_in_minutes: 600, duration_minutes: 60 } });
  const windowId = (await created.json()).id as string;
  await page.goto("/");
  await expect(page.getByRole("region", { name: /^incidents/i }).getByRole("link", { name: /cause : OUT_001/ })).toBeVisible({ timeout: 60_000 });
  await expect(page.getByRole("region", { name: /maintenance planifiée/i })).toContainText("audit axe");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await expect(page.getByRole("group", { name: /Trafic et pertes de OUT_001/ }).locator("path").first()).toBeVisible();
  await page.getByRole("button", { name: "Déplier la configuration", exact: true }).click(); // le panneau le plus riche en champs
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  const blocking = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
  const report = blocking.map((v) => `${v.id} (${v.impact}) : ${v.help}\n  ${v.nodes.slice(0, 3).map((n) => n.target.join(" ")).join("\n  ")}`).join("\n");
  await request.delete(`/maintenance/${windowId}`, { headers });
  expect(blocking, report).toEqual([]);
});
