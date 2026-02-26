import { startProgressBar } from './utils.js';
import { fetchAlerts, syncFilters, reconcilePendingRows, loadFilterState, applyFilters } from './alerts.js';
import { fetchMapStatus, initMapInteractions, resizeCanvas } from './map.js';
import { fetchValidations, refreshTrendsPanel } from './trends.js';
import { fetchHCHistory, updateHCChart } from './hc.js';

async function openTab(evt, tabId) {
    const contents = document.getElementsByClassName('tab-content');
    for (let i = 0; i < contents.length; i++) {
        contents[i].classList.add('hidden');
    }
    const buttons = document.getElementsByClassName('tab-button');
    for (let i = 0; i < buttons.length; i++) {
        buttons[i].classList.remove('active');
    }
    document.getElementById(tabId).classList.remove('hidden');
    evt.currentTarget.classList.add('active');
    if (tabId === 'tab_trends') {
        await fetchValidations();
        refreshTrendsPanel(true);
    } else if (tabId === 'tab_hc') {
        await fetchHCHistory();
        updateHCChart(true);
    }
}

function updateClock() {
    const now = new Date();
    const options = { timeZone: 'Asia/Hong_Kong', hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' };
    const dateOptions = { timeZone: 'Asia/Hong_Kong', year: 'numeric', month: '2-digit', day: '2-digit' };
    const timeEl = document.getElementById('stat_casino_time');
    const dayEl = document.getElementById('stat_game_day');
    if (timeEl) timeEl.textContent = now.toLocaleTimeString('en-GB', options);
    if (dayEl) dayEl.textContent = now.toLocaleDateString('en-GB', dateOptions);
}

async function init() {
    resizeCanvas();
    startProgressBar();

    // Load persisted filter state (if any) before we fetch alerts so they render correctly
    loadFilterState();

    await fetchAlerts();
    // Ensure filters are applied immediately after alerts have been rendered
    applyFilters();

    await fetchMapStatus();
    await fetchValidations();
    refreshTrendsPanel();
    await fetchHCHistory();

    // Run an initial reconciliation pass for stale Pending rows (older than configured threshold)
    reconcilePendingRows();
    // Also periodically reconcile every 10 minutes
    setInterval(reconcilePendingRows, 10 * 60 * 1000);

    setInterval(async () => {
        await fetchAlerts();
        refreshTrendsPanel();
    }, 45000);
    setTimeout(() => setInterval(fetchMapStatus, 45000), 5000);
    setTimeout(async () => setInterval(async () => {
        await fetchValidations();
        refreshTrendsPanel();
    }, 45000), 10000);
    setTimeout(() => setInterval(fetchHCHistory, 45000), 15000);
    // Refresh trends panel and re-run filters if fallback reconciliation reports updates
    document.addEventListener('validations:updated', () => {
        refreshTrendsPanel(true);
        applyFilters();
    });
    initMapInteractions();
}

window.openTab = openTab;
window.syncFilters = syncFilters;

setInterval(updateClock, 1000);

init();
