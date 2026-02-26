import { state } from './state.js';
import { getHCHistory } from './api.js';

const axisMap = ['y', 'y1', 'y2', 'y3'];
const defaultLegendClick = Chart.defaults?.plugins?.legend?.onClick;

let hcChart = state.hcChart;

export function updateMomentumUI() {
    const momEl = document.getElementById('stat_momentum');
    if (!momEl || state.hcHistory.length < 2) return;
    const latest = state.hcHistory[state.hcHistory.length - 1];
    const nowTs = latest.ts.getTime();
    const targetTs = nowTs - (10 * 60 * 1000);
    let bestBack = state.hcHistory[0];
    let minDiff = Math.abs(state.hcHistory[0].ts.getTime() - targetTs);
    for (let i = 1; i < state.hcHistory.length; i++) {
        const diff = Math.abs(state.hcHistory[i].ts.getTime() - targetTs);
        if (diff < minDiff) {
            minDiff = diff;
            bestBack = state.hcHistory[i];
        }
    }
    const diff = latest.seats - bestBack.seats;
    const pct = bestBack.seats > 0 ? (diff / bestBack.seats * 100) : 0;
    const timeSpanMins = (latest.ts.getTime() - bestBack.ts.getTime()) / 60000;
    if (timeSpanMins < 1.0) {
        momEl.textContent = '...';
        return;
    }
    const pctStr = pct.toFixed(1) + '%';
    const colorClass = diff > 0 ? 'status-positive' : (diff < 0 ? 'status-negative' : '');
    const sign = diff > 0 ? '+' : '';
    if (diff > 0) {
        momEl.innerHTML = `<span><span class="${colorClass}">${sign}${pctStr}</span> <span style="font-size:0.65em; opacity:0.5; font-weight:400; margin-left:2px;">(${sign}${diff})</span></span> <span class="momentum-up" style="font-size:0.8em;">▲</span>`;
    } else if (diff < 0) {
        momEl.innerHTML = `<span><span class="${colorClass}">${pctStr}</span> <span style="font-size:0.65em; opacity:0.5; font-weight:400; margin-left:2px;">(${diff})</span></span> <span class="momentum-down" style="font-size:0.8em;">▼</span>`;
    } else {
        momEl.innerHTML = `<span><span style="opacity:0.6;">0.0%</span> <span style="font-size:0.65em; opacity:0.5; font-weight:400; margin-left:2px;">(0)</span></span> <span style="font-size:0.8em; vertical-align:middle; opacity:0.3;">-</span>`;
    }
    momEl.title = `Change in seats over last ${Math.round(timeSpanMins)} mins`;
}

export function updateHCChart(forceResize = false) {
    const canvas = document.getElementById('hcChart');
    if (!canvas) return;
    const ctxHC = canvas.getContext('2d');
    const labels = state.hcHistory.map(d => d.ts);
    const tablesData = state.hcHistory.map(d => d.tables);
    const seatsData = state.hcHistory.map(d => d.seats);
    const utilData = state.hcHistory.map(d => d.tables > 0 ? (d.seats / d.tables) : 0);
    const momentumData = state.hcHistory.length === 0 ? [] : state.hcHistory.map((d, i) => {
        const targetTs = d.ts.getTime() - (10 * 60 * 1000);
        let bestIdx = 0;
        let minDiff = Math.abs(state.hcHistory[0].ts.getTime() - targetTs);
        for (let j = 0; j <= i; j++) {
            const diff = Math.abs(state.hcHistory[j].ts.getTime() - targetTs);
            if (diff < minDiff) {
                minDiff = diff;
                bestIdx = j;
            }
        }
        const timeSpan = (d.ts.getTime() - state.hcHistory[bestIdx].ts.getTime()) / 60000;
        if (timeSpan < 1.0) return null;
        const diff = d.seats - state.hcHistory[bestIdx].seats;
        const pct = state.hcHistory[bestIdx].seats > 0 ? (diff / state.hcHistory[bestIdx].seats * 100) : 0;
        return pct;
    });
    if (hcChart) {
        // ensure x-axis shows weekday + hour labels
        if (hcChart.options && hcChart.options.scales && hcChart.options.scales.x && hcChart.options.scales.x.time) {
            hcChart.options.scales.x.time.unit = 'hour';
            hcChart.options.scales.x.time.displayFormats = { hour: 'EEE HH:mm' };
        }
        hcChart.data.labels = labels;
        hcChart.data.datasets[0].data = tablesData;
        hcChart.data.datasets[1].data = seatsData;
        hcChart.data.datasets[2].data = momentumData;
        hcChart.data.datasets[3].data = utilData;
        if (forceResize) hcChart.resize();
        hcChart.update('none');
    } else {
        hcChart = new Chart(ctxHC, {
            type: 'line',
            data: {
                labels,
                datasets: [
                    {
                        label: 'Occupied Tables',
                        data: tablesData,
                        borderColor: '#3b82f6',
                        backgroundColor: 'rgba(59, 130, 246, 0.1)',
                        borderWidth: 2,
                        pointRadius: 0,
                        fill: true,
                        tension: 0.3,
                        yAxisID: 'y'
                    },
                    {
                        label: 'Occupied Seats',
                        data: seatsData,
                        borderColor: '#10b981',
                        backgroundColor: 'transparent',
                        borderWidth: 2,
                        pointRadius: 0,
                        tension: 0.3,
                        yAxisID: 'y1'
                    },
                    {
                        label: 'Momentum (%)',
                        data: momentumData,
                        borderColor: '#f59e0b',
                        backgroundColor: 'transparent',
                        borderWidth: 1.5,
                        borderDash: [5, 5],
                        pointRadius: 0,
                        tension: 0.4,
                        yAxisID: 'y2'
                    },
                    {
                        label: 'Occupancy (S/T)',
                        data: utilData,
                        borderColor: '#a855f7',
                        backgroundColor: 'transparent',
                        borderWidth: 2,
                        pointRadius: 0,
                        tension: 0.3,
                        yAxisID: 'y3'
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                layout: {
                    padding: { top: 12, bottom: 12, left: 30, right: 32 }
                },
                scales: {
                    x: {
                        type: 'time',
                        time: { unit: 'hour', displayFormats: { hour: 'EEE HH:mm' } },
                        grid: { color: 'rgba(255, 255, 255, 0.05)' },
                        ticks: { color: '#94a3b8' }
                    },

                    y: {
                        type: 'linear',
                        display: true,
                        position: 'left',
                        title: { display: true, text: 'Occupied Tables', color: '#3b82f6', font: { size: 11 } },
                        grid: { color: 'rgba(255, 255, 255, 0.05)' },
                        ticks: { color: '#3b82f6', font: { size: 11 } }
                    },
                    y1: {
                        type: 'linear',
                        display: true,
                        position: 'right',
                        title: { display: true, text: 'Occupied Seats', color: '#10b981', font: { size: 11 } },
                        grid: { drawOnChartArea: false },
                        ticks: { color: '#10b981', font: { size: 11 } }
                    },
                    y2: {
                        type: 'linear',
                        display: true,
                        position: 'right',
                        title: { display: true, text: 'Momentum (%)', color: '#f59e0b', font: { size: 11 }, padding: { top: 4, bottom: 4 } },
                        grid: { drawOnChartArea: false },
                        ticks: { color: '#f59e0b', font: { size: 11 }, callback: (v) => `${parseFloat(v).toFixed(1)}%` }
                    },
                    y3: {
                        type: 'linear',
                        display: true,
                        position: 'left',
                        title: { display: true, text: 'Occupancy (S/T)', color: '#a855f7', font: { size: 11 }, padding: { top: 4, bottom: 4 } },
                        grid: { drawOnChartArea: false },
                        ticks: { color: '#a855f7', font: { size: 11 }, callback: (v) => v.toFixed(1) }
                    }
                },
                plugins: {
                    legend: {
                        position: 'top',
                        labels: { color: '#cbd5e1', boxWidth: 10, usePointStyle: true, font: { size: 10 } },
                        onClick: function(e, legendItem, legend) {
                            if (defaultLegendClick) {
                                defaultLegendClick.call(this, e, legendItem, legend);
                            }
                            const chart = legend.chart;
                            const axisId = axisMap[legendItem.datasetIndex];
                            if (axisId && chart.scales[axisId]) {
                                const hasVisible = chart.data.datasets.some((dataset, idx) => chart.isDatasetVisible(idx) && dataset.yAxisID === axisId);
                                chart.scales[axisId].options.display = hasVisible;
                            }
                            chart.update();
                        }
                    },
                    tooltip: {
                        backgroundColor: '#1e293b',
                        titleColor: '#fff',
                        bodyColor: '#cbd5e1',
                        borderColor: '#334155',
                        borderWidth: 1,
                        callbacks: {
                            label: function(context) {
                                let label = context.dataset.label || '';
                                if (label) label += ': ';
                                if (context.parsed.y !== null) {
                                    if (context.dataset.yAxisID === 'y2') label += context.parsed.y.toFixed(1) + '%';
                                    else if (context.dataset.yAxisID === 'y3') label += context.parsed.y.toFixed(2);
                                    else label += Math.round(context.parsed.y);
                                }
                                return label;
                            }
                        }
                    }
                }
            }
        });
        state.hcChart = hcChart;
    }
}

export async function fetchHCHistory() {
    try {
        const hours = Math.max(1, Math.min(24, Number(state.trendsHours || 24)));
        const data = await getHCHistory(hours);
        if (Array.isArray(data)) {
            state.hcHistory = data.map(item => ({
                ts: new Date(item.ts),
                tables: item.tables,
                seats: item.seats
            }));
            updateHCChart();
            updateMomentumUI();
        }
    } catch (err) {
        console.error('Fetch HC history error:', err);
    }
}
