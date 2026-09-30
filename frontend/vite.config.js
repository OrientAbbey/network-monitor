import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Pas de proxy : l'API autorise CORS depuis localhost:5173 (autre origine : NETWORK_MONITOR_CORS_ORIGINS).
// L'URL de l'API vient de VITE_API_BASE_URL (voir .env.example).
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
