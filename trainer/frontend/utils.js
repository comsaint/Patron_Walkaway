import { state, dom } from './state.js';

// Single source of truth for refresh cadence (seconds)
const REFRESH_INTERVAL_SEC = 45;

export function getProgressColor(pct) {
    const start = { r: 34, g: 197, b: 94 };
    const end = { r: 239, g: 68, b: 68 };
    const ratio = pct / 100;
    const r = Math.round(end.r + (start.r - end.r) * ratio);
    const g = Math.round(end.g + (start.g - end.g) * ratio);
    const b = Math.round(end.b + (start.b - end.b) * ratio);
    return `rgb(${r}, ${g}, ${b})`;
}

export function updateProgressBarVisual() {
    if (!dom.splitter) return;
    const pct = Math.max(0, Math.min(100, (state.refreshSecondsLeft / REFRESH_INTERVAL_SEC) * 100));
    dom.splitter.style.setProperty('--splitter-progress', pct.toFixed(2) + '%');
    dom.splitter.style.setProperty('--splitter-color', getProgressColor(pct));
}

export function startProgressBar() {
    if (state.progressTimer) clearInterval(state.progressTimer);
    state.refreshSecondsLeft = REFRESH_INTERVAL_SEC;
    // Expose the refresh cadence to CSS for animations (e.g., alert heartbeat fade)
    document.documentElement.style.setProperty('--refresh-interval-sec', `${REFRESH_INTERVAL_SEC}s`);
    document.documentElement.style.setProperty('--alert-fade-duration', `${REFRESH_INTERVAL_SEC}s`);
    updateProgressBarVisual();
    state.progressTimer = setInterval(() => {
        state.refreshSecondsLeft -= 1;
        if (state.refreshSecondsLeft < 0) state.refreshSecondsLeft = REFRESH_INTERVAL_SEC;
        updateProgressBarVisual();
    }, 1000);
}

export function isTruePositive(res) {
    if (!res) return false;
    const val = res.TP !== undefined ? res.TP : (res.result !== undefined ? res.result : (res.reason === 'MATCH'));
    if (typeof val === 'string') return val.toLowerCase() === 'true';
    return val === true || val === 1;
}

export function getChecked(...ids) {
    for (const id of ids) {
        const el = document.getElementById(id);
        if (el) return el.checked;
    }
    return true;
}
