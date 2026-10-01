import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Aucun proxy : l'API autorise CORS depuis les origines listées dans
// NM_CORS_ORIGINS (par défaut http://localhost:5173, l'origine de ce serveur
// de développement). L'URL de l'API est lue depuis VITE_API_BASE_URL (voir
// .env.example) plutôt que codée en dur.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173 },
  test: { environment: "jsdom", include: ["src/**/*.test.{ts,tsx}"] },
});
