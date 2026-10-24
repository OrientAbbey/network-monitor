import { useEffect, useState } from "react";
import type { Thresholds } from "./SummaryTable";
import { canAct, currentSession, login, logout, type Session } from "./api";
import { errorMessage } from "./format";
import { needs, ROLE_LABEL } from "./operatorActions";
import type { AppConfigView } from "./types";

export type IncidentDraft = AppConfigView["incident"];

export function NumberField({ label, hint, value, onChange, min, max, step }: {
  label: string; hint?: string; value: number; onChange: (v: number) => void; min: number; max: number; step: number;
}) {
  return (
    <label className="settings-field">
      <span className="settings-field-label">{label}</span>
      <input
        type="number"
        className="settings-input mono"
        value={value}
        min={min} max={max} step={step}
        onChange={(e) => { const v = parseFloat(e.target.value); if (!Number.isNaN(v)) onChange(v); }}
      />
      {hint && <span className="settings-field-hint">{hint}</span>}
    </label>
  );
}

export function SettingsPanel({ config, deltaThresholds, onDeltaThresholdsChange, onSave, saving, saveError }: {
  config: AppConfigView; deltaThresholds: Thresholds; onDeltaThresholdsChange: (t: Thresholds) => void;
  onSave: (draft: IncidentDraft) => void; saving: boolean; saveError: string | null;
}) {
  const [draft, setDraft] = useState<IncidentDraft>(config.incident);
  useEffect(() => setDraft(config.incident), [config]);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [session, setSession] = useState<Session | null>(currentSession());
  const [loginError, setLoginError] = useState<string | null>(null);
  const [loggingIn, setLoggingIn] = useState(false);
  const submitLogin = async () => {
    setLoggingIn(true);
    setLoginError(null);
    try {
      setSession(await login(username, password));
      setPassword(""); // le mot de passe ne reste pas dans l'état
    } catch (err) {
      setLoginError(errorMessage(err).includes("429") ? "Trop d'essais : patientez quelques minutes." : "Identifiant ou mot de passe incorrect.");
    } finally {
      setLoggingIn(false);
    }
  };

  const orderIssue = !(draft.close_score < draft.warning_score && draft.warning_score < draft.open_score);

  return (
    <div className="settings-grid">
      <div className="settings-group">
        <h3>Moteur d'incidents (backend réel)</h3>
        <NumberField label="Seuil SUSPECT (warning_score)" value={draft.warning_score} min={0} max={1} step={0.01}
          onChange={(v) => setDraft({ ...draft, warning_score: v })} />
        <NumberField label="Seuil OPEN (open_score)" value={draft.open_score} min={0} max={1} step={0.01}
          onChange={(v) => setDraft({ ...draft, open_score: v })} />
        <NumberField label="Seuil CLOSED (close_score)" value={draft.close_score} min={0} max={1} step={0.01}
          onChange={(v) => setDraft({ ...draft, close_score: v })} />
        {orderIssue && <p className="settings-warning">Incohérent : il faut close_score &lt; warning_score &lt; open_score. L'enregistrement sera refusé par l'API.</p>}
        {saveError && <p className="settings-warning" role="alert">{saveError}</p>}
        {session ? (
          <p className="settings-field-hint" role="status">
            Connecté en tant que <strong>{session.username}</strong> ({ROLE_LABEL[session.role]}, session valable jusqu'à{" "}
            {session.expiresAt.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" })}).{" "}
            <button type="button" className="settings-reset" onClick={() => { logout(); setSession(null); }}>Se déconnecter</button>
          </p>
        ) : (
          <fieldset className="settings-field" aria-describedby="login-hint">
            <legend className="settings-field-label">Connexion (requise pour enregistrer)</legend>
            <input aria-label="Identifiant" autoComplete="username" className="settings-input mono" style={{ width: 130 }} value={username} onChange={(e) => setUsername(e.target.value)} />
            <input aria-label="Mot de passe" type="password" autoComplete="current-password" className="settings-input mono" style={{ width: 150 }} value={password} onChange={(e) => setPassword(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && username && password) void submitLogin(); }} />
            <button type="button" className="settings-reset" disabled={loggingIn || !username || !password} onClick={() => void submitLogin()}>Se connecter</button>
            {loginError && <span className="settings-warning" role="alert">{loginError}</span>}
            <span id="login-hint" className="settings-field-hint">Session à durée limitée, gardée en mémoire : effacée au rechargement de la page, jamais écrite dans le navigateur.</span>
          </fieldset>
        )}
        <button type="button" className="settings-reset" disabled={saving || !canAct("admin")} title={needs("admin")} onClick={() => onSave(draft)}>
          {saving ? "Envoi à l'API…" : "Envoyer à l'API"}
        </button>
      </div>

      <div className="settings-group">
        <h3>Tableau comparatif (affichage local)</h3>
        <NumberField label="Seuil de coloration — Trafic" value={deltaThresholds.traffic * 100} min={0} max={100} step={1}
          onChange={(v) => onDeltaThresholdsChange({ ...deltaThresholds, traffic: v / 100 })} />
        <NumberField label="Seuil de coloration — Pertes" value={deltaThresholds.loss * 100} min={0} max={100} step={1}
          onChange={(v) => onDeltaThresholdsChange({ ...deltaThresholds, loss: v / 100 })} />
        <p className="settings-field-hint">Ces deux seuils ne concernent que l'affichage du tableau dans ce navigateur ; ils ne sont pas envoyés à l'API.</p>
      </div>
    </div>
  );
}

// --- Composant racine -----------------------------------------------------
