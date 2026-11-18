import { useCallback, useState } from "react";
import { api, canAct, hasApiToken } from "./api";
import { formatWhen } from "./format";
import { actionError, needs, NOT_LOGGED_IN } from "./operatorActions";
import { usePolling } from "./polling";
import type { MaintenanceWindow } from "./types";

export const MAINTENANCE_STATE: Record<MaintenanceWindow["state"], string> = { upcoming: "à venir", active: "en cours", past: "terminée", cancelled: "annulée" };

export function MaintenancePanel({ outputIds }: { outputIds: string[] }) {
  const [windows, setWindows] = useState<MaintenanceWindow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [output, setOutput] = useState("");
  const [startsIn, setStartsIn] = useState(0);
  const [duration, setDuration] = useState(60);
  const [reason, setReason] = useState("");
  const refresh = useCallback(async () => {
    try {
      setWindows(await api.maintenance(7));
    } catch {
      setWindows([]);
    }
  }, []);
  usePolling(refresh, 15_000, [refresh]);

  const act = async (action: () => Promise<unknown>) => {
    if (!hasApiToken()) return setError(NOT_LOGGED_IN);
    setError(null);
    try {
      await action();
      await refresh();
    } catch (err) {
      setError(actionError(err));
    }
  };
  const create = () => act(async () => {
    await api.createMaintenance({ output_id: output || null, reason: reason.trim(), starts_in_minutes: startsIn, duration_minutes: duration });
    setReason("");
  });

  return (
    <section className="nm-panel" aria-labelledby="h-maintenance">
      <h2 id="h-maintenance">Maintenance planifiée</h2>
      <p className="settings-field-hint">
        Pendant une fenêtre, les incidents de la sortie sont enregistrés mais n'envoient aucune alerte ; son temps est retiré de la disponibilité.
        Les horaires sont relatifs à l'horloge du serveur.
      </p>
      {error && <p className="settings-warning" role="alert">{error}</p>}
      <div className="nm-log" role="log" aria-live="polite" aria-relevant="additions">
        {windows.length === 0 && <div className="nm-log-empty">Aucune maintenance planifiée.</div>}
        {windows.map((w) => (
          <div className="nm-log-row" key={w.id}>
            <span className="nm-log-msg">
              {w.output_id ?? "Toutes les sorties"} — {formatWhen(w.start)} → {formatWhen(w.end)} — {w.reason}
              <span className="nm-tag">{MAINTENANCE_STATE[w.state]}</span>
              {(w.state === "upcoming" || w.state === "active") && (
                <button type="button" className="range-btn" disabled={!canAct("operator")} title={needs("operator")} aria-label={`Annuler la maintenance « ${w.reason} »`} onClick={() => void act(() => api.cancelMaintenance(w.id))}>
                  Annuler
                </button>
              )}
            </span>
          </div>
        ))}
      </div>
      <fieldset className="nm-form">
        <legend>Planifier une fenêtre</legend>
        <label>Sortie
          <select className="settings-input" style={{ width: 150 }} value={output} onChange={(e) => setOutput(e.target.value)}>
            <option value="">Toutes les sorties</option>
            {outputIds.map((id) => <option key={id} value={id}>{id}</option>)}
          </select>
        </label>
        <label>Début dans (min)
          <input className="settings-input mono" type="number" min={0} value={startsIn} onChange={(e) => setStartsIn(Number(e.target.value))} />
        </label>
        <label>Durée (min)
          <input className="settings-input mono" type="number" min={1} value={duration} onChange={(e) => setDuration(Number(e.target.value))} />
        </label>
        <label>Motif
          <input className="settings-input" style={{ width: 220 }} maxLength={200} value={reason} onChange={(e) => setReason(e.target.value)} />
        </label>
        <button type="button" className="settings-reset" disabled={!reason.trim() || duration <= 0 || !canAct("operator")} title={needs("operator")} onClick={() => void create()}>Planifier</button>
        {!hasApiToken() && <span className="settings-field-hint">{NOT_LOGGED_IN}</span>}
      </fieldset>
    </section>
  );
}
