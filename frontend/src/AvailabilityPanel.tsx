import { useCallback, useState } from "react";
import { api } from "./api";
import { formatDuration } from "./format";
import { usePolling } from "./polling";
import type { AvailabilityReport } from "./types";

export const PERIODS = [7, 30, 90];

export function AvailabilityPanel() {
  const [days, setDays] = useState(30);
  const [report, setReport] = useState<AvailabilityReport | null>(null);
  const [failed, setFailed] = useState(false);
  const refresh = useCallback(async () => {
    try {
      setReport(await api.availability(days));
      setFailed(false);
    } catch {
      setFailed(true);
    }
  }, [days]);
  usePolling(refresh, 60_000, [refresh]);

  return (
    <section className="nm-panel nm-table-panel" aria-labelledby="h-availability">
      <h2 id="h-availability">Disponibilité</h2>
      <div className="range-selector" role="group" aria-label="Période du rapport">
        {PERIODS.map((d) => (
          <button key={d} type="button" className={`range-btn${d === days ? " active" : ""}`} aria-pressed={d === days} onClick={() => setDays(d)}>
            {d} j
          </button>
        ))}
        <a className="range-btn" href={api.availabilityCsvUrl(days)} download>Exporter en CSV</a>
      </div>
      {failed && <p className="settings-warning" role="alert">Rapport indisponible.</p>}
      {report && (
        <div style={{ overflowX: "auto" }}>
          <table className="nm-table">
            <caption style={{ position: "absolute", left: -9999 }}>Disponibilité par sortie sur les {days} derniers jours</caption>
            <thead>
              <tr>
                <th scope="col">Sortie</th><th scope="col">Observé</th><th scope="col">Disponibilité</th><th scope="col">Indisponible</th>
                <th scope="col">Dégradé</th><th scope="col">Maintenance</th><th scope="col">Incidents</th><th scope="col">Détection</th>
                <th scope="col">Durée moy.</th><th scope="col">Couverture</th>
              </tr>
            </thead>
            <tbody>
              {report.outputs.map((o) => (
                <tr key={o.output_id}>
                  <th scope="row">{o.output_id}</th>
                  <td className="mono">{formatDuration(o.monitored_seconds)}</td>
                  <td className="mono">{o.availability_percent === null ? "—" : `${o.availability_percent.toFixed(3)} %`}</td>
                  <td className="mono">{formatDuration(o.downtime_seconds)}</td>
                  <td className="mono">{formatDuration(o.degraded_seconds)}</td>
                  <td className="mono">{formatDuration(o.maintenance_seconds)}</td>
                  <td className="mono">
                    {o.incidents}
                    {o.false_positives_excluded > 0 && <span className="nm-note"> (+{o.false_positives_excluded} faux positif{o.false_positives_excluded > 1 ? "s" : ""} exclu{o.false_positives_excluded > 1 ? "s" : ""})</span>}
                  </td>
                  <td className="mono">{formatDuration(o.mttd_seconds)}</td>
                  <td className="mono">{formatDuration(o.mttr_seconds)}</td>
                  <td className="mono">{o.coverage_percent === null ? "—" : `${o.coverage_percent.toFixed(1)} %`}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="settings-field-hint">
            Calculé sur la période <strong>observée</strong> (depuis la première mesure conservée, au plus tôt le début de la
            période choisie), pas sur la période demandée. Indisponibilité = incidents critiques, du début réel à la fin réelle ; le temps de maintenance planifiée est retiré de l'indisponibilité et du temps dû, et les incidents jugés faux positifs ne sont pas comptés.
            Couverture = part des mesures attendues effectivement reçues (une couverture basse rend la disponibilité moins fiable).
          </p>
        </div>
      )}
    </section>
  );
}
