import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Aucun proxy nécessaire : l'API (app/api.py) autorise déjà CORS depuis
// n'importe quelle origine (voir sa configuration CORSMiddleware). L'URL de
// l'API est lue depuis VITE_API_BASE_URL (voir .env.example) plutôt que
// codée en dur, pour pouvoir pointer vers un autre hôte/port sans toucher au code.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
  },
});
