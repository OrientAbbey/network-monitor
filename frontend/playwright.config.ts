import { defineConfig, devices } from "@playwright/test";

const PORT = 8765;

/**
 * Tests de bout en bout : un vrai navigateur, le vrai backend (e2e/server.py), l'interface compilée
 * servie sur la même origine. `npm run e2e` construit l'interface (mode « e2e ») puis lance les tests.
 *
 * Premier lancement : `npx playwright install chromium`. Dans un environnement qui ne peut pas
 * télécharger les navigateurs, `PW_CHROMIUM_PATH` désigne un Chromium existant (`PW_NO_SANDBOX=1` s'il
 * tourne sous root, comme dans un conteneur).
 */
export default defineConfig({
  testDir: "./e2e",
  timeout: 90_000,
  expect: { timeout: 15_000 },
  fullyParallel: false, // un seul backend, un incident injecté : les tests partagent son horloge
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    launchOptions: {
      executablePath: process.env.PW_CHROMIUM_PATH || undefined,
      args: process.env.PW_NO_SANDBOX ? ["--no-sandbox", "--headless=shell", "--disable-gpu"] : [],
    },
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: `${process.env.PYTHON ?? "python"} frontend/e2e/server.py ${PORT} ${process.env.E2E_TMP ?? "frontend/test-results/e2e-data"}`,
    cwd: "..",
    url: `http://127.0.0.1:${PORT}/health`,
    reuseExistingServer: false,
    timeout: 120_000,
    env: { PYTHONPATH: ".", PYTHONUNBUFFERED: "1" },
  },
});
