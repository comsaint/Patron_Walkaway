import { dom, state, config } from './state.js';
import { isTruePositive, getChecked, updateProgressBarVisual } from './utils.js';
import { getAlerts } from './api.js';
import { setTableAlert, drawMap, showCrosshair, openContextMenuForTable } from './map.js';

const { alertsBody } = dom;

function highlightNewRow(row) {
    const badge = row.querySelector('.status-badge');
    if (badge) {
        badge.classList.add('new-alert');
    }
}

function clearNewBadges() {
    if (!alertsBody) return;
    alertsBody.querySelectorAll('.status-badge.new-alert').forEach(badge => {
        badge.classList.remove('new-alert');
    });
}

// Sorting helpers
function sortAlertsTableByCol(idx, asc = true) {
    const table = document.getElementById('alerts_table');
    if (!table) return;
    const tbody = table.querySelector('tbody');
    const rows = Array.from(tbody.querySelectorAll('tr'));
    rows.sort((a, b) => {
        let va = a.children[idx].textContent.replace(/[$,]/g, '');
        let vb = b.children[idx].textContent.replace(/[$,]/g, '');
        // Try parse as date
        const da = Date.parse(va);
        const db = Date.parse(vb);
        if (!isNaN(da) && !isNaN(db)) {
            return (da - db) * (asc ? 1 : -1);
        }
        // Try numeric
        if (!isNaN(Number(va)) && !isNaN(Number(vb))) {
            return (Number(va) - Number(vb)) * (asc ? 1 : -1);
        }
        return va.localeCompare(vb) * (asc ? 1 : -1);
    });
    rows.forEach(r => tbody.appendChild(r));
}

    function reorderAlertsByBetTimestampDesc() {
        if (!alertsBody) return;
        const rows = Array.from(alertsBody.querySelectorAll('tr'));
        rows.sort((a, b) => {
            const aTs = a.getAttribute('data-bet-ts') || a.getAttribute('data-ts');
            const bTs = b.getAttribute('data-bet-ts') || b.getAttribute('data-ts');
            return (bTs ? Date.parse(bTs) : 0) - (aTs ? Date.parse(aTs) : 0);
        });
        rows.forEach(r => alertsBody.appendChild(r));
        const ths = document.querySelectorAll('#alerts_table th');
        ths.forEach((th, idx) => {
            if (idx !== 2) {
                th.classList.remove('sort-asc', 'sort-desc');
            }
        });
        const defaultTh = document.querySelector('#alerts_table th:nth-child(3)');
        if (defaultTh) {
            defaultTh.classList.add('sort-desc');
            defaultTh.classList.remove('sort-asc');
        }
    }

function attachOrderButtons() {
    const table = document.getElementById('alerts_table');
    if (!table) return;
    const ths = table.querySelectorAll('th');
    const buttons = table.querySelectorAll('.order-btn');
    buttons.forEach(btn => {
        const idx = Number(btn.getAttribute('data-col'));
        btn.onclick = (e) => {
            e.stopPropagation();
            const th = ths[idx];
            const isAsc = th.classList.toggle('sort-asc');
            th.classList.remove('sort-desc');
            ths.forEach((oth, i) => { if (i !== idx) { oth.classList.remove('sort-asc', 'sort-desc'); } });
            if (!isAsc) th.classList.add('sort-desc');
            sortAlertsTableByCol(idx, isAsc);
        };
    });
}

// Initialize handlers early
attachOrderButtons();

export function updateSidebarCounts() {
    let countPending = alertsBody.querySelectorAll('tr.row-pending').length;
    let countP = alertsBody.querySelectorAll('tr.row-positive').length;
    let countN = alertsBody.querySelectorAll('tr.row-negative').length;

    if ((countPending + countP + countN) === 0 && state.allValidations.length > 0) {
        countPending = state.allValidations.filter(v => v.reason === 'PENDING').length;
        countP = state.allValidations.filter(v => v.reason === 'MATCH').length;
        countN = state.allValidations.filter(v => v.reason === 'MISS').length;
    }

    const total = countPending + countP + countN;

    document.getElementById('count_pending').textContent = countPending;
    document.getElementById('count_positive').textContent = countP;
    document.getElementById('count_negative').textContent = countN;

    const tTotal = document.getElementById('trend_count_total');
    const tValidated = document.getElementById('trend_count_validated');
    const tPending = document.getElementById('trend_count_pending');
    const tP = document.getElementById('trend_count_positive');
    const tN = document.getElementById('trend_count_negative');
    if (tTotal) tTotal.textContent = total;
    if (tValidated) tValidated.textContent = (countP + countN);
    if (tPending) tPending.textContent = countPending;
    if (tP) tP.textContent = countP;
    if (tN) tN.textContent = countN;
}

export function syncFilters(peerId, checked) {
    const peer = document.getElementById(peerId);
    if (peer) peer.checked = checked;
    // Persist user filter choices
    try {
        const filters = {
            pending: document.getElementById('tab_filter_pending')?.checked || false,
            match: document.getElementById('tab_filter_positive')?.checked || false,
            miss: document.getElementById('tab_filter_negative')?.checked || false,
            bet0: document.getElementById('tab_filter_bet_0')?.checked || false,
            bet1: document.getElementById('tab_filter_bet_1')?.checked || false,
            bet2: document.getElementById('tab_filter_bet_2')?.checked || false,
            bet3: document.getElementById('tab_filter_bet_3')?.checked || false
        };
        localStorage.setItem('alerts_filters', JSON.stringify(filters));
    } catch (e) {
        // ignore storage errors
    }
    applyFilters();
}

export function loadFilterState() {
    try {
        const raw = localStorage.getItem('alerts_filters');
        if (!raw) {
            // Default: only pending checked
            const p = document.getElementById('tab_filter_pending'); if (p) p.checked = true;
            const a = document.getElementById('tab_filter_positive'); if (a) a.checked = false;
            const m = document.getElementById('tab_filter_negative'); if (m) m.checked = false;
            return;
        }
        const filters = JSON.parse(raw);
        if (filters.pending !== undefined) document.getElementById('tab_filter_pending').checked = !!filters.pending;
        if (filters.match !== undefined) document.getElementById('tab_filter_positive').checked = !!filters.match;
        if (filters.miss !== undefined) document.getElementById('tab_filter_negative').checked = !!filters.miss;
        if (filters.bet0 !== undefined) document.getElementById('tab_filter_bet_0').checked = !!filters.bet0;
        if (filters.bet1 !== undefined) document.getElementById('tab_filter_bet_1').checked = !!filters.bet1;
        if (filters.bet2 !== undefined) document.getElementById('tab_filter_bet_2').checked = !!filters.bet2;
        if (filters.bet3 !== undefined) document.getElementById('tab_filter_bet_3').checked = !!filters.bet3;
    } catch (e) {
        // ignore
    }
}

export function applyFilters() {
    const show_pending = getChecked('tab_filter_pending', 'filter_pending');
    const show_p = getChecked('tab_filter_positive', 'filter_positive');
    const show_n = getChecked('tab_filter_negative', 'filter_negative');
    const b0 = getChecked('tab_filter_bet_0', 'filter_bet_0');
    const b1 = getChecked('tab_filter_bet_1', 'filter_bet_1');
    const b2 = getChecked('tab_filter_bet_2', 'filter_bet_2');
    const b3 = getChecked('tab_filter_bet_3', 'filter_bet_3');

    const rows = document.querySelectorAll('#alerts_body tr');
    rows.forEach(row => {
        const is_pending = row.classList.contains('row-pending');
        const is_p = row.classList.contains('row-positive');
        const is_n = row.classList.contains('row-negative');
        const avgBet = parseFloat(row.getAttribute('data-avg-bet') || 0);
        let betMatch = false;
        if (avgBet < 1000 && b0) betMatch = true;
        else if (avgBet >= 1000 && avgBet <= 3000 && b1) betMatch = true;
        else if (avgBet > 3000 && avgBet <= 10000 && b2) betMatch = true;
        else if (avgBet > 10000 && b3) betMatch = true;
        let statusMatch = false;
        if (is_pending && show_pending) statusMatch = true;
        if (is_p && show_p) statusMatch = true;
        if (is_n && show_n) statusMatch = true;
        row.style.display = (statusMatch && betMatch) ? '' : 'none';
    });
}

export function applySingleValidation(row, res) {
    const statusCell = row.querySelector('.status-cell');
    const badge = statusCell ? statusCell.querySelector('.status-badge') : null;
    const tid = row.querySelector('td:nth-child(7)')?.textContent;
    if (!statusCell || !badge) return;
    const isTP = isTruePositive(res);
    const isPending = (res.reason === 'PENDING');
    if (isPending) {
        badge.textContent = 'Pending';
        badge.className = 'status-badge status-pending';
        row.classList.remove('row-positive', 'row-negative');
        row.classList.add('row-pending');
        if (tid) setTableAlert(tid, 'pending');
    } else if (isTP) {
        badge.textContent = 'Match';
        badge.className = 'status-badge status-positive';
        row.classList.remove('row-pending', 'row-negative');
        row.classList.add('row-positive');
        if (tid) setTableAlert(tid, 'positive');
    } else {
        badge.textContent = 'Miss';
        badge.className = 'status-badge status-negative';
        row.classList.remove('row-pending', 'row-positive');
        row.classList.add('row-negative');
        if (tid) setTableAlert(tid, 'negative');
    }
}

// --- Fallback validation fetch for stale Pending rows ---
export async function fetchValidationsByBetIds(betIds) {
    if (!betIds || betIds.length === 0) return;
    const chunkSize = 100;
    for (let i = 0; i < betIds.length; i += chunkSize) {
        const chunk = betIds.slice(i, i + chunkSize);
        const q = `?bet_ids=${encodeURIComponent(chunk.join(','))}`;
        try {
            const resp = await fetch(`${config.API_BASE}/get_validation${q}`);
            if (!resp.ok) continue;
            const body = await resp.json();
            const results = body.results || [];
            results.forEach(res => {
                // Update in-memory caches similar to fetchValidations
                try {
                    const resEpoch = String(Math.floor(new Date(res.ts).getTime() / 1000));
                    const vKey = `${res.player_id}_${resEpoch}`;
                    state.validationCache[vKey] = res;
                    if (!state.allValidations.find(v => v.player_id == res.player_id && v.ts === res.ts)) {
                        state.allValidations.push(res);
                    }
                } catch (e) {
                    // ignore
                }
                const bid = String(res.bet_id);
                const row = Array.from(document.querySelectorAll('#alerts_body tr'))
                    .find(r => r.querySelector('td:nth-child(4)')?.textContent.trim() === bid);
                if (row) applySingleValidation(row, res);
            });
            if (results.length) {
                document.dispatchEvent(new Event('validations:updated'));
            }
        } catch (e) {
            console.error('Fallback fetch error', e);
        }
    }
}

export function reconcilePendingRows() {
    const thresholdMin = 60; // only reconcile rows older than 60 minutes
    const now = Date.now();
    const pendingRows = Array.from(document.querySelectorAll('#alerts_body tr.row-pending'));
    const stale = pendingRows.filter(r => {
        const epoch = r.getAttribute('data-ts-epoch');
        if (!epoch) return false;
        const t = Number(epoch) * 1000;
        return (now - t) > (thresholdMin * 60 * 1000);
    });
    const betIds = stale.map(r => r.querySelector('td:nth-child(4)')?.textContent.trim()).filter(Boolean);
    if (betIds.length) {
        // Deduplicate
        const uniq = Array.from(new Set(betIds));
        fetchValidationsByBetIds(uniq);
    }
}

export async function fetchAlerts() {
    state.refreshSecondsLeft = 45;
    updateProgressBarVisual();
    clearNewBadges();
    try {
        const data = await getAlerts(state.lastAlertTs);
        if (data.alerts && data.alerts.length > 0) {
            // Deduplicate alerts by key: player_id, ts, bet_id, session_id, table_id, position_idx
            const seen = new Set();
            const newAlerts = data.alerts.slice().sort((a, b) => new Date(a.ts) - new Date(b.ts));
            newAlerts.forEach(alert => {
                const dedupKey = `${alert.player_id}_${alert.ts}_${alert.bet_id}_${alert.session_id}_${alert.table_id}_${alert.position_idx}`;
                if (seen.has(dedupKey)) return;
                // Skip if already present in DOM (handles overlapping fetch windows)
                if (document.querySelector(`[data-alert-key="${dedupKey}"]`)) return;
                seen.add(dedupKey);
                const date = new Date(alert.ts);
                const bDate = new Date(alert.bet_ts);
                const timeOptions = { timeZone: 'Asia/Hong_Kong', hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' };
                const dateOptions = { timeZone: 'Asia/Hong_Kong', weekday: 'short' };
                const formattedAlertTs = date.toLocaleDateString('en-GB', dateOptions) + ' ' + date.toLocaleTimeString('en-GB', timeOptions);
                const formattedBetTs = bDate.toLocaleTimeString('en-GB', timeOptions);
                const row = document.createElement('tr');
                row.setAttribute('data-pid', alert.player_id);
                row.setAttribute('data-ts', alert.ts);
                row.setAttribute('data-bet-ts', alert.bet_ts);
                row.setAttribute('data-alert-key', dedupKey);
                row.setAttribute('data-avg-bet', alert.visit_avg_bet || 0);
                row.classList.add('row-pending');
                // Set row background color based on avg_bet (0 = transparent, >100000 = gold)
                const avgBet = Number(alert.visit_avg_bet || 0);
                let bgColor = 'transparent';
                if (avgBet > 0) {
                    // Custom curve: 100=10%, 1000=30%, 10000=60%, 100000=100%
                    const gold = [255, 215, 0];
                    let alpha = 0;
                    if (avgBet >= 100000) {
                        alpha = 1.0;
                    } else if (avgBet >= 10000) {
                        // 10k (0.6) to 100k (1.0)
                        alpha = 0.6 + 0.4 * ((Math.log10(avgBet) - 4) / (5 - 4));
                    } else if (avgBet >= 1000) {
                        // 1k (0.3) to 10k (0.6)
                        alpha = 0.3 + 0.3 * ((Math.log10(avgBet) - 3) / (4 - 3));
                    } else if (avgBet >= 100) {
                        // 100 (0.1) to 1k (0.3)
                        alpha = 0.1 + 0.2 * ((Math.log10(avgBet) - 2) / (3 - 2));
                    } else if (avgBet > 0) {
                        // 0+ to 100 (0.1)
                        alpha = 0.05 + 0.05 * (avgBet / 100);
                    }
                    alpha = Math.max(0.05, Math.min(alpha, 1)) * 0.85; // scale for visibility, clamp
                    bgColor = `rgba(${gold[0]},${gold[1]},${gold[2]},${alpha})`;
                }
                // Use a bottom accent line instead of full-row background (inset box-shadow)
                row.style.background = 'transparent';
                if (bgColor !== 'transparent') {
                    row.style.boxShadow = `inset 0 -4px 0 ${bgColor}`;
                } else {
                    row.style.boxShadow = '';
                }
                const statPills = [];
                const pushStat = (val, label, formatter = (v) => v) => {
                    if (val === null || val === undefined || isNaN(Number(val))) return;
                    statPills.push(`<span class="alert-stat-chip">${label} ${formatter(Number(val))}</span>`);
                };
                pushStat(alert.loss_streak, 'LS');
                pushStat(alert.bets_last_5m, '5m');
                pushStat(alert.bets_last_15m, '15m');
                pushStat(alert.bets_last_30m, '30m');
                pushStat(alert.wager_last_10m, '10m', v => `$${Math.round(v).toLocaleString()}`);
                pushStat(alert.wager_last_30m, '30m', v => `$${Math.round(v).toLocaleString()}`);
                pushStat(alert.bets_per_minute, 'BPM', v => v.toFixed(2));

                row.innerHTML = `
                    <td style="font-weight:700; color:var(--accent);">Walkaway</td>
                    <td>${formattedAlertTs}</td>
                    <td>${formattedBetTs}</td>
                    <td>${alert.bet_id}</td>
                    <td>${alert.session_id}</td>
                    <td>${alert.player_id}</td>
                    <td>${alert.table_id}</td>
                    <td>${alert.position_idx}</td>
                    <td>$${Math.round(alert.visit_avg_bet || 0).toLocaleString()}</td>
                    <td class="status-cell"><span class="status-badge status-pending">Pending</span></td>
                    <td class="alert-stats-cell">${statPills.join('')}</td>
                `;
                // ...existing code...

                // Use epoch-second keys to avoid formatting mismatches between alert.ts and validation.ts
                const alertEpoch = Math.floor(new Date(alert.ts).getTime() / 1000);
                row.setAttribute('data-ts-epoch', String(alertEpoch));
                const alertKey = `${alert.player_id}_${alertEpoch}`;
                const cachedVal = state.validationCache[alertKey];
                if (cachedVal) {
                    applySingleValidation(row, cachedVal);
                } else {
                    const tidStr = String(alert.table_id);
                    setTableAlert(tidStr, 'pending', alert.position_idx);
                }
                // make row clickable to highlight table on map
                row.style.cursor = 'pointer';
                row.addEventListener('click', () => {
                    try { showCrosshair(alert.table_id); } catch (e) { console.error('Crosshair error', e); }
                });
                // Right-click context menu (reuse map context menu)
                row.addEventListener('contextmenu', (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    try { openContextMenuForTable(alert.table_id, e.clientX, e.clientY, row); } catch (err) { console.error('Open context error', err); }
                });
                alertsBody.insertBefore(row, alertsBody.firstChild);
                highlightNewRow(row);
                if (!state.lastAlertTs || new Date(alert.ts) > new Date(state.lastAlertTs)) {
                    state.lastAlertTs = alert.ts;
                }
            });
            reorderAlertsByBetTimestampDesc();
            applyFilters();
            attachOrderButtons();
            updateSidebarCounts();
            drawMap();
        }
    } catch (err) {
        console.error('Fetch alerts error:', err);
    }
}
