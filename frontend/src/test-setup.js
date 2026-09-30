import "@testing-library/jest-dom/vitest";

// jsdom déclare parfois la page « cachée » : nos boucles de rafraîchissement s'arrêtent alors.
Object.defineProperty(document, "hidden", { configurable: true, get: () => false });
