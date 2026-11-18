import { canAct, type Role } from "./api";
import { errorMessage } from "./format";

export const NOT_LOGGED_IN = "Connectez-vous (panneau Configuration) pour effectuer cette action.";

/** Message lisible pour un échec d'action protégée : session absente ou expirée (401), authentification non configurée (503). */
export const ROLE_LABEL: Record<Role, string> = { viewer: "lecteur", operator: "opérateur", admin: "administrateur" };

/** Infobulle d'un bouton désactivé parce que le compte connecté n'a pas le rôle requis (sinon rien : le bouton reste actif). */
export const needs = (minimum: Role): string | undefined => (canAct(minimum) ? undefined : `Réservé au rôle ${ROLE_LABEL[minimum]} ou supérieur`);

export function actionError(err: unknown): string {
  const msg = errorMessage(err);
  if (msg.includes(" 403 ")) {
    try {
      return String(JSON.parse(msg.slice(msg.indexOf(" 403 ") + 5)).detail); // le serveur dit quel rôle manque
    } catch {
      return "Droits insuffisants pour cette action.";
    }
  }
  if (msg.includes(" 401 ")) return NOT_LOGGED_IN;
  if (msg.includes(" 503 ")) return "Authentification non configurée sur le serveur : action impossible.";
  return "Rejeté par l'API : " + msg;
}
