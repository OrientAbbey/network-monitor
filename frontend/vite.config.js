import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Aucun proxy nécessaire : l'API (app/api.py) autorise CORS depuis les origines
// de développement locales (http://localhost:5173 et http://127.0.0.1:5173) ;
// pour une autre origine, définir NETWORK_MONITOR_CORS_ORIGINS côté API. L'URL de
// l'API est lue depuis VITE_API_BASE_URL (voir .env.example) plutôt que
// codée en dur, pour pouvoir pointer vers un autre hôte/port sans toucher au code.
export default defineConfig({
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.js"],
    css: false,
  },
  plugins: [react()],
  server: {
    port: 5173,
  },
});
