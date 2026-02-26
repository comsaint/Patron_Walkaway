import { state } from './state.js';
import { isTruePositive } from './utils.js';
import { getValidations } from './api.js';
import { applyFilters, updateSidebarCounts, applySingleValidation } from './alerts.js';
import { drawMap } from './map.js';

let accuracyChart = state.accuracyChart;

export async function fetchValidations() {
    try {
        const data = await getValidations(state.lastValidationTs);
        if (data.results && data.results.length > 0) {
            console.log(`[Validation API] Received ${data.results.length} new records`);
            const rowCache = new Map();
            document.querySelectorAll('#alerts_body tr').forEach(row => {
                const pid = row.getAttribute('data-pid');
                const tsEpoch = row.getAttribute('data-ts-epoch') || (row.getAttribute('data-ts') && String(Math.floor(new Date(row.getAttribute('data-ts')).getTime()/1000)));
                if (pid && tsEpoch) rowCache.set(`${pid}_${tsEpoch}`, row);
            });
            let matchCount = 0;
            data.results.forEach(res => {
                // Normalize to epoch-second key to avoid small formatting differences
                const resEpoch = String(Math.floor(new Date(res.ts).getTime()/1000));
                const vKey = `${res.player_id}_${resEpoch}`;
                state.validationCache[vKey] = res;
                if (!state.allValidations.find(v => v.player_id == res.player_id && v.ts === res.ts)) {
                    state.allValidations.push(res);
                }
                let row = rowCache.get(vKey);
                if (!row && res.bet_id) {
                    // Fallback: try to match the DOM row by bet_id in column 4
                    const rows = document.querySelectorAll('#alerts_body tr');
                    for (const r of rows) {
                        const cell = r.querySelector('td:nth-child(4)');
                        if (cell && String(cell.textContent).trim() === String(res.bet_id)) {
                            row = r;
                            break;
                        }
                    }
                }
                if (row) {
                    applySingleValidation(row, res);
                    matchCount++;
                }
                if (!state.lastValidationTs || new Date(res.sync_ts) > new Date(state.lastValidationTs)) {
                    state.lastValidationTs = res.sync_ts;
                }
            });
            if (matchCount > 0) {
                console.log(`[Validation API] Successfully matched and updated ${matchCount} UI rows`);
            }
            applyFilters();
            updateSidebarCounts();
            updateAccuracyChart();
            drawMap();
        }
    } catch (err) {
        console.error('Fetch validations error:', err);
    }
}

export function updateAccuracyChart(forceResize = false) {
    const canvas = document.getElementById('accuracyChart');
    if (!canvas) return;
    const sorted = [...state.allValidations].sort((a, b) => {
        const aTime = a.bet_ts ? new Date(a.bet_ts) : new Date(a.ts);
        const bTime = b.bet_ts ? new Date(b.bet_ts) : new Date(b.ts);
        return aTime - bTime;
    });
        const qualifiedAll = sorted.filter(v =>
        v.reason === 'MATCH' ||
        v.reason === 'MISS'
    );
        // Filter to the selected time window
        const hours = Math.max(1, Math.min(24, Number(state.trendsHours || 24)));
        const cutoff = new Date(Date.now() - (hours * 3600 * 1000));
        const qualified = qualifiedAll.filter(v => {
            const t = v.bet_ts ? new Date(v.bet_ts) : new Date(v.ts);
            return t >= cutoff;
        });
    let displayPoints = [];
    let chartMin = 0;
    let chartMax = 100;
    if (qualified.length === 0) {
        document.getElementById('trend_accuracy_value').textContent = '--%';
    } else {
        // Compute cumulative TP across the entire history (qualifiedAll) so that the accuracy
        // shown for a window is the global cumulative up to each point (avoids reset/distortion).
        let tpCountAll = 0;
        const fullDataPointsAll = qualifiedAll.map((res, index) => {
            const timestamp = res.bet_ts ? new Date(res.bet_ts) : new Date(res.ts);
            const isTP = isTruePositive(res);
            if (isTP) tpCountAll++;
            return { ts: timestamp, y: (tpCountAll / (index + 1)) * 100 };
        });
        // Now filter the precomputed cumulative points down to the selected time window
        displayPoints = fullDataPointsAll.filter(p => p.ts >= cutoff).map(p => ({ x: p.ts, y: p.y }));
        // Use the last point in the DISPLAY window for the displayed accuracy number
        if (displayPoints.length > 0) {
            const latestFullPoint = { ts: displayPoints[displayPoints.length - 1].x, y: displayPoints[displayPoints.length - 1].y };
            document.getElementById('trend_accuracy_value').textContent = latestFullPoint.y.toFixed(1) + '%';
        } else {
            document.getElementById('trend_accuracy_value').textContent = '--%';
        }
        // Update chart y-range using points in the display window
        const yVals = displayPoints.map(p => p.y);
        if (yVals.length > 0) {
            const minY = Math.min(...yVals);
            const maxY = Math.max(...yVals);
            let amp = maxY - minY;
            if (amp < 0.1) amp = 2.0;
            const center = (minY + maxY) / 2;
            const dynRange = amp * 5;
            chartMin = Math.max(0, center - (dynRange / 2));
            chartMax = Math.min(100, center + (dynRange / 2));
        }

    }
    if (accuracyChart) {
        // Ensure x-axis uses hour labels with weekday (e.g., Mon 14:00)
        if (accuracyChart.options && accuracyChart.options.scales && accuracyChart.options.scales.x && accuracyChart.options.scales.x.time) {
            accuracyChart.options.scales.x.time.unit = 'hour';
            accuracyChart.options.scales.x.time.displayFormats = { hour: 'EEE HH:mm' };
        }
        accuracyChart.data.datasets[0].data = displayPoints;
        accuracyChart.options.scales.y.min = chartMin;
        accuracyChart.options.scales.y.max = chartMax;
        if (forceResize) accuracyChart.resize();
        accuracyChart.update();
    } else {
        const ctx = canvas.getContext('2d');
        accuracyChart = new Chart(ctx, {
            type: 'line',
            data: {
                datasets: [{
                    label: 'Accuracy (%)',
                    data: displayPoints,
                    borderColor: '#3b82f6',
                    backgroundColor: 'rgba(59, 130, 246, 0.1)',
                    borderWidth: 2,
                    pointRadius: 2,
                    fill: true,
                    tension: 0.1
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    x: {
                        type: 'time',
                        time: { unit: 'hour', displayFormats: { hour: 'HH:mm' } },
                        title: { display: false },
                        grid: { color: 'rgba(255, 255, 255, 0.05)' },
                        ticks: { color: '#94a3b8', font: { size: 10 } }
                    },

                    y: {
                        min: chartMin,
                        max: chartMax,
                        title: { display: true, text: 'Accuracy %', color: '#64748b', font: { size: 10, weight: 'bold' } },
                        grid: { color: 'rgba(255, 255, 255, 0.05)' },
                        ticks: { color: '#94a3b8', font: { size: 10 } }
                    }
                },
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        backgroundColor: '#1e293b',
                        titleColor: '#fff',
                        bodyColor: '#cbd5e1',
                        borderColor: '#334155',
                        borderWidth: 1
                    }
                }
            }
        });
        state.accuracyChart = accuracyChart;
    }
}

export function refreshTrendsPanel(forceResize = false) {
    updateSidebarCounts();
    updateAccuracyChart(forceResize);
}

// Set trends hours (cap to 24), update UI and refresh both charts
export function setTrendsHours(hours) {
    const h = Math.max(1, Math.min(24, Number(hours) || 24));
    state.trendsHours = h;
    const sel = document.getElementById('trend_range');
    if (sel) sel.value = String(h);
    // Refresh accuracy and HC charts
    updateAccuracyChart(true);
    // fetch hc data with new hours window
    import('./hc.js').then(mod => { mod.updateHCChart(true); mod.fetchHCHistory && mod.fetchHCHistory(); }).catch(() => {});
}

// Initialize selector handler (call once)
(function initTrendRangeHandler(){
    document.addEventListener('DOMContentLoaded', () => {
        const sel = document.getElementById('trend_range');
        if (!sel) return;
        sel.value = String(state.trendsHours || 24);
        sel.addEventListener('change', (e) => {
            setTrendsHours(Number(e.target.value));
        });
    });
})();
