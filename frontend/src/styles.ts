import { COLORS } from "./theme";

export const BASE_STYLE = `

  .nm-root {
    background: ${COLORS.bg};
    background-image: repeating-linear-gradient(0deg, rgba(127,231,196,0.012) 0px, rgba(127,231,196,0.012) 1px, transparent 1px, transparent 3px);
    color: ${COLORS.text}; font-family: 'Space Grotesk', sans-serif; min-height: 100vh; padding: 28px 32px 48px; box-sizing: border-box;
  }
  .nm-root * { box-sizing: border-box; }
  .mono { font-family: 'JetBrains Mono', monospace; font-variant-numeric: tabular-nums; }

  .connection-error { max-width: 560px; margin: 60px auto; text-align: center; color: ${COLORS.textDim}; }
  .connection-error h1 { color: ${COLORS.text}; }
  .chart-loading { color: ${COLORS.textFaint}; font-size: 12px; padding: 20px 0; }
  .empty-range { color: ${COLORS.textFaint}; font-size: 12px; padding: 30px 10px; text-align: center; border: 1px dashed ${COLORS.border}; margin: 10px 0; }

  .nm-header { display: flex; align-items: baseline; justify-content: space-between; border-bottom: 1px solid ${COLORS.border}; padding-bottom: 18px; margin-bottom: 24px; flex-wrap: wrap; gap: 12px; }
  .nm-title { margin: 0; font-size: 22px; font-weight: 600; letter-spacing: -0.01em; }
  .nm-title span { color: ${COLORS.textDim}; font-weight: 400; }
  .nm-header-meta { display: flex; align-items: center; gap: 22px; font-size: 13px; color: ${COLORS.textDim}; }
  .nm-live { display: flex; align-items: center; gap: 7px; }
  .nm-live-dot { width: 7px; height: 7px; border-radius: 50%; background: ${COLORS.trace}; }
  @media (prefers-reduced-motion: no-preference) { .nm-live-dot { animation: pulse 2.2s ease-in-out infinite; } }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.35; } }
  .nm-incident-count { color: ${COLORS.coral}; font-weight: 600; }
  .nm-clock { font-size: 15px; color: ${COLORS.text}; }

  .nm-channels { display: flex; flex-direction: column; gap: 14px; margin-bottom: 28px; }
  .nm-channel { background: ${COLORS.panel}; border: 1px solid ${COLORS.border}; border-left: 3px solid ${COLORS.border}; }
  .nm-channel.is-active { box-shadow: 0 0 0 1px var(--stripe) inset, 0 0 28px -10px var(--stripe); }
  .nm-channel-head { display: flex; align-items: center; gap: 24px; padding: 14px 18px; cursor: pointer; flex-wrap: wrap; }
  .nm-channel-head:focus-visible { outline: 2px solid ${COLORS.trace}; outline-offset: -2px; }
  .nm-channel-id-block { display: flex; flex-direction: column; min-width: 130px; }
  .nm-card-id { font-size: 14px; font-weight: 600; }
  .nm-channel-readout { display: flex; flex-direction: column; align-items: flex-end; font-size: 15px; min-width: 100px; }
  .nm-readout-loss { font-size: 11px; color: ${COLORS.textDim}; }
  .nm-status-row { display: flex; align-items: center; gap: 8px; font-size: 12px; flex: 1; min-width: 140px; }
  .nm-status-dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
  .nm-collapse-btn { background: transparent; border: 1px solid ${COLORS.border}; color: ${COLORS.textDim}; width: 26px; height: 26px; font-size: 15px; line-height: 1; cursor: pointer; flex-shrink: 0; display: flex; align-items: center; justify-content: center; }
  .nm-collapse-btn:hover { border-color: ${COLORS.borderBright}; color: ${COLORS.text}; }
  .nm-collapse-btn:focus-visible { outline: 2px solid ${COLORS.trace}; outline-offset: 1px; }
  .nm-channel-body { padding: 4px 18px 18px; border-top: 1px solid ${COLORS.grid}; }

  .range-selector { display: flex; gap: 6px; margin: 12px 0 10px; }
  .range-btn { background: transparent; border: 1px solid ${COLORS.border}; color: ${COLORS.textFaint}; font-family: 'JetBrains Mono', monospace; font-size: 11px; padding: 4px 10px; cursor: pointer; }
  .range-btn.active { color: ${COLORS.text}; border-color: ${COLORS.trace}; }
  .range-btn:hover { border-color: ${COLORS.borderBright}; }
  .range-btn:focus-visible, .legend-chip:focus-visible, .settings-reset:focus-visible, .settings-input:focus-visible, .chart-svg:focus-visible, .skip-link:focus-visible { outline: 2px solid ${COLORS.trace}; outline-offset: 2px; }
  .nm-sim-banner { margin: 0 0 16px; padding: 8px 12px; border: 1px solid ${COLORS.amber}; border-radius: 4px; color: ${COLORS.amber}; font-size: 12.5px; }
  .skip-link { position: absolute; left: -9999px; top: 8px; background: ${COLORS.panel}; color: ${COLORS.text}; padding: 8px 12px; z-index: 10; }
  .skip-link:focus { left: 8px; }
  #nm-main:focus { outline: none; }
  .chart-readout { min-height: 18px; margin-top: 6px; font-size: 11.5px; color: ${COLORS.textDim}; }
  @media (max-width: 720px) {
    .nm-root { padding: 16px 12px; }
    .nm-header-meta { flex-wrap: wrap; gap: 8px 16px; }
    .nm-channel-head { gap: 12px; padding: 12px; }
    .nm-channel-body { padding: 4px 10px 14px; }
  }

  .chart-legend { display: flex; gap: 8px; margin-top: 6px; flex-wrap: wrap; }
  .legend-chip { display: flex; align-items: center; gap: 6px; background: transparent; border: 1px solid ${COLORS.border}; color: ${COLORS.textFaint}; font-family: 'Space Grotesk', sans-serif; font-size: 11px; padding: 4px 9px; cursor: pointer; }
  .legend-chip.active { color: ${COLORS.text}; border-color: ${COLORS.borderBright}; }
  .legend-chip-value { color: ${COLORS.textDim}; margin-left: 2px; }

  .nm-panel { background: ${COLORS.panel}; border: 1px solid ${COLORS.border}; padding: 18px 20px; margin-bottom: 20px; }
  .nm-panel h2 { font-size: 13px; font-weight: 500; color: ${COLORS.textDim}; margin: 0; }
  .nm-panel-head:focus-visible { outline: 2px solid ${COLORS.trace}; outline-offset: 2px; }
  .nm-panel-head { display: flex; align-items: center; justify-content: space-between; cursor: pointer; margin-bottom: 14px; }
  .nm-table-panel { overflow-x: auto; }

  .nm-table { border-collapse: collapse; width: 100%; margin-top: 14px; font-size: 12.5px; min-width: 760px; }
  .nm-table th, .nm-table td { padding: 7px 10px; text-align: right; border-bottom: 1px solid ${COLORS.grid}; }
  .nm-table thead tr:first-child th { padding-bottom: 4px; font-weight: 500; color: ${COLORS.textDim}; }
  .nm-table thead tr:last-child th { font-size: 10.5px; color: ${COLORS.textFaint}; font-weight: 400; padding-top: 0; }
  .nm-table-group { text-align: center; border-bottom: 1px solid ${COLORS.border}; }
  .nm-table-group:first-of-type { border-right: 1px solid ${COLORS.border}; }
  .nm-table tbody td:nth-child(5) { border-right: 1px solid ${COLORS.border}; }
  .nm-table-id-head { text-align: left; vertical-align: bottom; }
  .nm-table-id { text-align: left; font-weight: 600; }
  .trend-cell { display: flex; flex-direction: column; align-items: flex-end; gap: 1px; }
  .trend-value { font-size: 12.5px; }
  .trend-delta { font-size: 10.5px; }

  .settings-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; margin-top: 4px; }
  @media (max-width: 700px) { .settings-grid { grid-template-columns: 1fr; } }
  .settings-group h3 { font-size: 12px; font-weight: 500; color: ${COLORS.textDim}; margin: 0 0 12px; }
  .settings-field { display: flex; flex-direction: column; gap: 2px; margin-bottom: 14px; }
  .settings-field-label { font-size: 12px; color: ${COLORS.text}; }
  .settings-field-hint { font-size: 10.5px; color: ${COLORS.textFaint}; }
  .settings-input { background: ${COLORS.bg}; border: 1px solid ${COLORS.border}; color: ${COLORS.text}; padding: 5px 8px; font-size: 12.5px; width: 100px; margin-top: 3px; }
  .settings-warning { font-size: 11.5px; color: ${COLORS.amber}; border: 1px solid ${COLORS.amber}; padding: 8px 10px; margin: 4px 0; }
  .settings-reset { background: transparent; border: 1px solid ${COLORS.border}; color: ${COLORS.textDim}; font-family: 'Space Grotesk', sans-serif; font-size: 12px; padding: 7px 14px; cursor: pointer; }
  .settings-reset:hover { border-color: ${COLORS.borderBright}; color: ${COLORS.text}; }
  .settings-reset:disabled { opacity: 0.5; cursor: default; }

  .nm-log { display: flex; flex-direction: column; gap: 9px; max-height: 340px; overflow-y: auto; }
  .nm-log-row { display: flex; gap: 10px; align-items: baseline; font-size: 12.5px; }
  .nm-log-time { color: ${COLORS.textFaint}; flex-shrink: 0; width: 40px; }
  .nm-log-dot { width: 5px; height: 5px; border-radius: 50%; flex-shrink: 0; margin-top: 4px; }
  .nm-tag { margin-left: 8px; padding: 1px 6px; border: 1px solid ${COLORS.border}; color: ${COLORS.textDim}; font-size: 11px; white-space: nowrap; }
  a.nm-tag { color: ${COLORS.trace}; }
  .nm-note { color: ${COLORS.textDim}; font-size: 11px; }
  .nm-form { display: flex; flex-wrap: wrap; gap: 12px 16px; align-items: flex-end; margin: 14px 0 0; padding: 10px 14px 14px; border: 1px solid ${COLORS.border}; }
  .nm-form legend { font-size: 12px; color: ${COLORS.text}; padding: 0 6px; }
  .nm-form label { display: flex; flex-direction: column; gap: 3px; font-size: 11.5px; color: ${COLORS.textDim}; }
  .nm-form .settings-input { margin-top: 0; }
  .nm-verdicts { display: inline-flex; gap: 6px; margin-left: 10px; }
  .nm-log-empty { color: ${COLORS.textFaint}; font-size: 12.5px; }
`;
