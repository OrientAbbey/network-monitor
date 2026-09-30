import "@testing-library/jest-dom/vitest";

// jsdom peut déclarer la page cachée, ce qui suspendrait nos boucles de rafraîchissement.
Object.defineProperty(document, "hidden", { configurable: true, get: () => false });
