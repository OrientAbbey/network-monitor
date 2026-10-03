import { useEffect, useRef } from "react";

/**
 * Appelle `fn` tout de suite puis toutes les `ms`, **sauf onglet masqué** : un dashboard resté
 * ouvert en arrière-plan ne doit pas interroger l'API en continu. Au retour sur l'onglet, un
 * rafraîchissement immédiat remet l'affichage à jour. `deps` relance la boucle (comme useEffect).
 */
export function usePolling(fn: () => void | Promise<void>, ms: number, deps: unknown[]): void {
  const latest = useRef(fn);
  latest.current = fn;

  useEffect(() => {
    const run = () => {
      if (!document.hidden) void latest.current();
    };
    run();
    const id = setInterval(run, ms);
    const onVisible = () => {
      if (!document.hidden) void latest.current();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ms, ...deps]);
}
