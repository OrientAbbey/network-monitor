import { useCallback, useState } from "react";
import { api, canAct, hasApiToken } from "./api";
import { formatClock } from "./format";
import { actionError, needs, NOT_LOGGED_IN } from "./operatorActions";
import { usePolling } from "./polling";
import { STATUS_STYLE } from "./theme";
import type { Incident, PrecisionReport, Verdict } from "./types";

export const VERDICT_LABEL: Record<Verdict, string> = { true_positive: "vrai incident", false_positive: "faux positif" };

export function IncidentsPanel({ incidents }: { incidents: Incident[] }) {
  const [precision, setPrecision] = useState<PrecisionReport | null>(null);
  const [verdicts, setVerdicts] = useState<Record<string, Verdict | null>>({}); // jugements posés ici, avant le prochain rafraîchissement
  const [owners, setOwners] = useState<Record<string, string | null>>({}); // prises en charge posées ici (nom, ou null si retirée)
  const [error, setError] = useState<string | null>(null);
  const refreshPrecision = useCallback(async () => {
    try {
      setPrecision(await api.precision(30));
    } catch {
      setPrecision(null);
    }
  }, []);
  usePolling(refreshPrecision, 30_000, [refreshPrecision]);

  const judge = async (inc: Incident, verdict: Verdict | null) => {
    if (!hasApiToken()) return setError(NOT_LOGGED_IN);
    setError(null);
    try {
      const { verdict: saved } = await api.setFeedback(inc.id, verdict); // lu ici : une réponse inattendue est attrapée par le catch
      setVerdicts((prev) => ({ ...prev, [inc.id]: saved }));
      void refreshPrecision();
    } catch (err) {
      setError(actionError(err));
    }
  };

  const takeCharge = async (inc: Incident, release: boolean) => {
    if (!hasApiToken()) return setError(NOT_LOGGED_IN);
    setError(null);
    try {
      const { acknowledged_by: by } = await (release ? api.release(inc.id) : api.acknowledge(inc.id));
      setOwners((prev) => ({ ...prev, [inc.id]: by }));
    } catch (err) {
      setError(actionError(err));
    }
  };

  const outputOf = (id: string) => incidents.find((i) => i.id === id)?.output_id;
  return (
    <section className="nm-panel" aria-labelledby="h-incidents">
      <h2 id="h-incidents">Incidents (récents et actifs)</h2>
      <p className="settings-field-hint" role="status">
        {precision === null ? "Précision indisponible."
          : `Précision mesurée sur ${precision.days} j : ${precision.precision_percent === null ? "—" : `${precision.precision_percent.toFixed(1)} %`} `
            + `(${precision.true_positives} vrai${precision.true_positives > 1 ? "s" : ""}, ${precision.false_positives} faux ; ${precision.unjudged} sans verdict`
            + `${precision.judged_share_percent === null ? "" : ` — ${precision.judged_share_percent.toFixed(0)} % jugés`})`}
      </p>
      {error && <p className="settings-warning" role="alert">{error}</p>}
      <div className="nm-log" role="log" aria-live="polite" aria-relevant="additions">
        {incidents.length === 0 && <div className="nm-log-empty">Aucun incident pour l'instant.</div>}
        {incidents.map((inc) => {
          const verdict = inc.id in verdicts ? verdicts[inc.id] : inc.verdict;
          const cause = inc.root_cause_incident_id;
          const owner = inc.id in owners ? owners[inc.id] : inc.acknowledged_by;
          const live = inc.opened_at !== null && (inc.status === "open" || inc.status === "stale"); // seuls ceux-là se prennent en charge
          return (
            <div className="nm-log-row" id={`incident-${inc.id}`} key={inc.id}>
              <span className="nm-log-time mono">{formatClock(new Date(inc.last_seen_at))}</span>
              <span className="nm-log-dot" style={{ background: (STATUS_STYLE[inc.status] ?? STATUS_STYLE.normal).color, marginTop: 4 }} />
              <span className="nm-log-msg">
                {inc.output_id} — {inc.incident_type} ({(STATUS_STYLE[inc.status] ?? STATUS_STYLE.normal).label}, score {inc.score.toFixed(2)})
                {inc.maintenance && <span className="nm-tag">pendant une maintenance</span>}
                {cause && (outputOf(cause)
                  ? <a className="nm-tag" href={`#incident-${cause}`}>cause : {outputOf(cause)}</a>
                  : <span className="nm-tag">cause : un incident amont</span>)}
                {verdict && <span className="nm-tag">{VERDICT_LABEL[verdict]}</span>}
                {inc.escalated_at && <span className="nm-tag">escaladé</span>}
                {owner && <span className="nm-tag">pris en charge par {owner}</span>}
                {live && (
                  <span className="nm-verdicts">
                    <button type="button" className="range-btn" disabled={!canAct("operator")} title={needs("operator")} onClick={() => void takeCharge(inc, Boolean(owner))}
                      aria-label={owner ? `Retirer la prise en charge de l'incident de ${inc.output_id}` : `Prendre en charge l'incident de ${inc.output_id}`}>
                      {owner ? "Retirer la prise en charge" : "Prendre en charge"}
                    </button>
                  </span>
                )}
                {inc.opened_at && (
                  <span className="nm-verdicts">
                    {(["false_positive", "true_positive"] as Verdict[]).map((v) => (
                      <button key={v} type="button" className={`range-btn${verdict === v ? " active" : ""}`} aria-pressed={verdict === v} disabled={!canAct("operator")} title={needs("operator")}
                        aria-label={`Juger l'incident de ${inc.output_id} : ${VERDICT_LABEL[v]}`} onClick={() => void judge(inc, v)}>
                        {v === "false_positive" ? "Faux positif" : "Vrai incident"}
                      </button>
                    ))}
                    {verdict && (
                      <button type="button" className="range-btn" disabled={!canAct("operator")} title={needs("operator")} aria-label={`Retirer le verdict de l'incident de ${inc.output_id}`} onClick={() => void judge(inc, null)}>
                        Retirer le verdict
                      </button>
                    )}
                  </span>
                )}
              </span>
            </div>
          );
        })}
      </div>
    </section>
  );
}
