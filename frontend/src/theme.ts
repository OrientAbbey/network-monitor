export const COLORS = {
  bg: "#060B0A",
  panel: "#0B1512",
  grid: "#12201C",
  border: "#1C2E28",
  borderBright: "#2A423A",
  text: "#DCEAE3",
  // Contrastes WCAG AA (≥ 4,5:1) sur les fonds de panneau : #5E7C72 (4,07) et #3C544D (2,27) échouaient (axe-core).
  textDim: "#7C9A90",
  textFaint: "#718C81",
  trace: "#7FE7C4",
  lossTrace: "#5FB3E8",
  amber: "#E8A33D",
  coral: "#E8604A",
  violet: "#9098B0",
};

export const STATUS_STYLE = {
  normal: { color: COLORS.textDim, label: "normal" },
  suspect: { color: COLORS.amber, label: "SUSPECT" },
  open: { color: COLORS.coral, label: "OPEN" },
  stale: { color: COLORS.violet, label: "STALE" },
  closed: { color: COLORS.textDim, label: "closed" },
};
