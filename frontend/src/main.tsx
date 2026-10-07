import React from "react";
import ReactDOM from "react-dom/client";
// Polices auto-hébergées (paquets @fontsource, sous-ensemble latin) : aucune requête vers un tiers, et
// le dashboard garde son apparence sur un réseau d'exploitation sans accès à Internet.
import "@fontsource/space-grotesk/latin-400.css";
import "@fontsource/space-grotesk/latin-500.css";
import "@fontsource/space-grotesk/latin-600.css";
import "@fontsource/space-grotesk/latin-700.css";
import "@fontsource/jetbrains-mono/latin-400.css";
import "@fontsource/jetbrains-mono/latin-500.css";
import "@fontsource/jetbrains-mono/latin-600.css";
import Dashboard from "./Dashboard";

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <Dashboard />
  </React.StrictMode>
);
