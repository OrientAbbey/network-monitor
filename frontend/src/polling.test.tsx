import { render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { usePolling } from "./polling";

function Probe({ fn, ms = 1000 }: { fn: () => void; ms?: number }) {
  usePolling(fn, ms, []);
  return null;
}

const setHidden = (hidden: boolean) => {
  Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
  document.dispatchEvent(new Event("visibilitychange"));
};

beforeEach(() => {
  vi.useFakeTimers();
  setHidden(false);
});
afterEach(() => {
  vi.useRealTimers();
  setHidden(false);
});

describe("usePolling", () => {
  it("appelle tout de suite puis à chaque période", () => {
    const fn = vi.fn();
    render(<Probe fn={fn} />);
    expect(fn).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(3000);
    expect(fn).toHaveBeenCalledTimes(4);
  });

  it("ne fait aucun appel pendant que l'onglet est masqué, et rattrape son retard au retour", () => {
    const fn = vi.fn();
    render(<Probe fn={fn} />);
    setHidden(true);
    fn.mockClear();
    vi.advanceTimersByTime(60_000);
    expect(fn).not.toHaveBeenCalled(); // une minute en arrière-plan : zéro requête
    setHidden(false);
    expect(fn).toHaveBeenCalledTimes(1); // rafraîchissement immédiat au retour
  });

  it("s'arrête au démontage", () => {
    const fn = vi.fn();
    const { unmount } = render(<Probe fn={fn} />);
    unmount();
    fn.mockClear();
    vi.advanceTimersByTime(10_000);
    setHidden(true);
    setHidden(false);
    expect(fn).not.toHaveBeenCalled();
  });
});
