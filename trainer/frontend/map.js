import { dom, state } from './state.js';
import { getFloorStatus } from './api.js';

const {
    mapCanvas,
    mapPanel,
    container,
    splitter,
    tooltip,
    contextMenu,
    alertsBody
} = dom;
const ctx = mapCanvas?.getContext('2d');

let isResizing = false;
// Blink state for high-roller (red) circle animation
let blinkOn = true;
let blinkTimer = null;

export function resizeCanvas() {
    if (!ctx || !mapCanvas || !mapPanel) return;
    const dpr = window.devicePixelRatio || 1;
    const rect = mapPanel.getBoundingClientRect();
    mapCanvas.width = rect.width * dpr;
    mapCanvas.height = rect.height * dpr;
    ctx.scale(dpr, dpr);
    mapCanvas.style.width = `${rect.width}px`;
    mapCanvas.style.height = `${rect.height}px`;
    drawMap();
}

export function setTableAlert(tid, status, seatId = null) {
    if (!tid) return;
    const current = state.tableAlerts[tid];
    const isResult = (status === 'positive' || status === 'negative');
    const isCurrentOld = current && (Date.now() - current.ts > 15000);
    let shouldUpdate = false;
    if (!current || isResult || isCurrentOld || (status === 'pending' && current.status === 'pending')) {
        shouldUpdate = true;
    }

    if (shouldUpdate) {
        if (!state.tableAlerts[tid] || state.tableAlerts[tid].status !== status || isResult) {
            state.tableAlerts[tid] = { status: status, ts: Date.now(), seats: new Set() };
        }
    }

    if (seatId !== null && state.tableAlerts[tid]) {
        state.tableAlerts[tid].seats.add(String(seatId));
    }
}

export function getTableAlertStatus(tid) {
    const entry = state.tableAlerts[tid];
    return entry ? entry.status : null;
}

export function drawMap() {
    if (!ctx || !state.tableLayout.length) return;
    const rect = mapPanel.getBoundingClientRect();
    ctx.clearRect(0, 0, rect.width, rect.height);
    const padding = 15;
    const drawW = rect.width - 2 * padding;
    const drawH = rect.height - 2 * padding;

    state.tableLayout.forEach(table => {
        const px = padding + (table.x * drawW);
        const py = padding + (table.y * drawH);
        table._screenX = px;
        table._screenY = py;
        const alertEntry = state.tableAlerts[table.table_id];
        // Only consider pending alerts for map circles. MATCH or MISS no longer get special circles.
        const isPending = alertEntry && alertEntry.status === 'pending';

        let isOccupied = false;
        if (table.status) {
            isOccupied = Object.values(table.status).some(v => Number(v) === 1);
        }
        if (isPending) {
            isOccupied = true;
        }

        ctx.beginPath();
        ctx.arc(px, py, 4, 0, 2 * Math.PI);
        ctx.fillStyle = isOccupied ? '#94a3b8' : '#1e293b';
        ctx.fill();

        // Draw circle only for pending alerts. If any pending seat has avg_bet > 10000, mark as high-roller (red), otherwise amber.
        if (isPending) {
            // Check alerts table for pending rows for this table with avg bet > 10000
            let hasHighRoller = false;
            try {
                const rows = Array.from(alertsBody.querySelectorAll('tr.row-pending'));
                for (const r of rows) {
                    if (r.children[6].textContent === String(table.table_id)) {
                        const avg = Number(r.getAttribute('data-avg-bet') || 0);
                        if (!isNaN(avg) && avg > 10000) {
                            hasHighRoller = true;
                            break;
                        }
                    }
                }
            } catch (e) {
                hasHighRoller = false;
            }
            if (hasHighRoller) {
                // Blink effect: alternate between red (alarm) and amber (pending) so status remains visible
                if (blinkOn) {
                    ctx.strokeStyle = '#ef4444';
                    ctx.lineWidth = 3.0;
                } else {
                    ctx.strokeStyle = '#f59e0b';
                    ctx.lineWidth = 2.5;
                }
            } else {
                ctx.strokeStyle = '#f59e0b';
                ctx.lineWidth = 2.5;
            }
            ctx.stroke();
        } else if (isOccupied) {
            // Regular occupied tables get a subtle white stroke
            ctx.strokeStyle = '#fff';
            ctx.lineWidth = 1.0;
            ctx.stroke();
        } else {
            ctx.strokeStyle = '#334155';
            ctx.lineWidth = 1.0;
            ctx.stroke();
        }
    });
}

function clampMenuPosition(clientX, clientY, menuWidth, menuHeight) {
    let posX = clientX;
    let posY = clientY - menuHeight;
    if (posX + menuWidth > window.innerWidth) {
        posX = clientX - menuWidth;
    }
    if (posY < 10) {
        posY = clientY;
    }
    return { x: posX, y: posY };
}

function showContextMenu(clientX, clientY, targetTable, specificRow = null) {
    if (!contextMenu) return;
    if (!targetTable) {
        contextMenu.style.display = 'none';
        return;
    }

    tooltip.style.display = 'none';
    contextMenu.style.display = 'block';

    const menuWidth = 260;
    const menuHeight = contextMenu.offsetHeight || 380;
    const pos = clampMenuPosition(clientX, clientY, menuWidth, menuHeight);

    contextMenu.style.left = pos.x + 'px';
    contextMenu.style.top = pos.y + 'px';
    document.getElementById('ctx_table_name').textContent = `Table ${targetTable.table_id}`;

    const seatSelect = document.getElementById('sel_comp_seat');
    seatSelect.innerHTML = '<option value="">-</option>';
    if (targetTable.status) {
        Object.keys(targetTable.status).sort().forEach(seatNum => {
            const option = document.createElement('option');
            option.value = seatNum;
            option.textContent = seatNum;
            seatSelect.appendChild(option);
        });
    }

    if (specificRow) {
        seatSelect.value = specificRow.children[7].textContent;
    }

    const compItem = document.getElementById('item_comp');
    let isAllowed = false;
    if (specificRow) {
        isAllowed = specificRow.classList.contains('row-pending');
    } else {
        isAllowed = Array.from(alertsBody.querySelectorAll('tr.row-pending')).some(r =>
            r.children[6].textContent === targetTable.table_id
        );
    }
    if (isAllowed) {
        compItem.classList.remove('disabled');
        compItem.title = "Action: Reward correct detection";
    } else {
        compItem.classList.add('disabled');
        compItem.title = specificRow ? "Only pending alerts can be rewarded" : "No pending alerts for this table";
    }
    contextMenu.dataset.activeTable = targetTable.table_id;
}

// Expose a helper so other modules (e.g., alerts list) can open the same context menu
export function openContextMenuForTable(tableId, clientX, clientY, specificRow = null) {
    if (!tableId) return;
    const target = state.tableLayout.find(t => String(t.table_id) === String(tableId));
    showContextMenu(clientX, clientY, target, specificRow);
}

mapCanvas?.addEventListener('mousemove', (e) => {
    if (!tooltip || !mapCanvas) return;
    const rect = mapCanvas.getBoundingClientRect();
    const mouseX = e.clientX - rect.left;
    const mouseY = e.clientY - rect.top;
    let hoveredItem = null;
    for (const table of state.tableLayout) {
        const dx = mouseX - (table._screenX || 0);
        const dy = mouseY - (table._screenY || 0);
        if (Math.sqrt(dx * dx + dy * dy) < 6) {
            hoveredItem = table;
            break;
        }
    }
    if (hoveredItem && contextMenu?.style.display !== 'block') {
        tooltip.style.display = 'block';
        // Table-level statistics
        const seatInfo = hoveredItem.seat_info || {};
        const statusSeats = hoveredItem.status ? Object.keys(hoveredItem.status) : [];
        const seatInfoSeats = Object.keys(seatInfo);
        const allSeatIds = Array.from(new Set([...statusSeats, ...seatInfoSeats])).sort((a, b) => Number(a) - Number(b));
        const tableAlert = state.tableAlerts[hoveredItem.table_id];
        const pendingSeats = (tableAlert && tableAlert.status === 'pending' && tableAlert.seats)
            ? new Set(Array.from(tableAlert.seats).map(String))
            : new Set();

        // Occupied seat count
        const occupiedSeats = allSeatIds.filter(seatKey => hoveredItem.status && hoveredItem.status[seatKey] === 1).length;
        const totalSeats = allSeatIds.length;

        // Average bet per table (mean of avg bets for all seats with data)
        const avgBets = Object.values(seatInfo)
            .map(s => s && s.avg_bet)
            .filter(v => v !== null && v !== undefined && !isNaN(Number(v)) && Number(v) > 0)
            .map(Number);
        const avgBetTable = hoveredItem.table_metrics && hoveredItem.table_metrics.avg_bet !== null
            ? hoveredItem.table_metrics.avg_bet
            : (avgBets.length > 0 ? (avgBets.reduce((a, b) => a + b, 0) / avgBets.length) : 0);

        // Header
        let html = `<div class="tooltip-header" style="font-size:1.08em; font-weight:600; margin-bottom:7px; letter-spacing:0.5px; padding-bottom:2px; border-bottom:1px solid #e5e7eb;"><span>Table ${hoveredItem.table_id}</span></div>`;

        // Table stats (use available fields or placeholders)
        const fmtInt = (val) => (val === null || val === undefined || isNaN(Number(val))) ? '—' : Number(val).toLocaleString();
        const fmtMoney = (val) => (val === null || val === undefined || isNaN(Number(val))) ? '—' : `$${Number(val).toLocaleString()}`;
        const dealerId = hoveredItem.dealer_id || hoveredItem.dealer || '123456789';
        const minBetRaw = hoveredItem.min_bet ?? hoveredItem.minBet ?? 2000;
        const maxBetRaw = hoveredItem.max_bet ?? hoveredItem.maxBet ?? 2_000_000;
        const tableStatus = hoveredItem.table_status || 'Open';
        const tableMetrics = hoveredItem.table_metrics || {};
        const turnover = tableMetrics.turnover ?? hoveredItem.turnover;
        const casinoWin = tableMetrics.win ?? hoveredItem.win;

        html += `<div class="tooltip-section">
            <div style="font-size:0.9em; color:var(--text-muted); font-weight:700;">Table Stats</div>
            <div class="tooltip-table-stats">
                <div class="tooltip-stat"><div class="label">Dealer</div><div class="value">${dealerId}</div></div>
                <div class="tooltip-stat"><div class="label">Min Bet</div><div class="value">$${fmtInt(minBetRaw)}</div></div>
                <div class="tooltip-stat"><div class="label">Max Bet</div><div class="value">$${fmtInt(maxBetRaw)}</div></div>
                <div class="tooltip-stat"><div class="label">Status</div><div class="value">${tableStatus}</div></div>
                <div class="tooltip-stat"><div class="label">Turnover</div><div class="value">${fmtMoney(turnover)}</div></div>
                <div class="tooltip-stat"><div class="label">Casino Win</div><div class="value">${fmtMoney(casinoWin)}</div></div>
                <div class="tooltip-stat"><div class="label">Occupied Seats</div><div class="value">${occupiedSeats} / ${totalSeats}</div></div>
                <div class="tooltip-stat"><div class="label">Avg Bet (table)</div><div class="value">$${Math.round(avgBetTable).toLocaleString()}</div></div>
            </div>
        </div>`;



        if (allSeatIds.length === 0) {
            html += `<div style="color: var(--text-muted); font-size: 0.92em; font-style: italic; padding: 2px 0 2px 2px;">No active seats</div>`;
        } else {
            allSeatIds.forEach(seatKey => {
                const isOccupied = hoveredItem.status && hoveredItem.status[seatKey] === 1;
                const isPendingSeat = pendingSeats.has(String(seatKey));
                const seatStatus = isPendingSeat ? 'Pending' : (isOccupied ? 'Occupied' : 'Empty');
                const lampClass = isOccupied ? 'lamp-occupied' : 'lamp-empty';
                const seatData = seatInfo[seatKey];
                let avgBetText = "";
                const seatNameColor = '#e0e6ef';
                const seatStatusColor = isPendingSeat ? 'var(--status-amber)' : (isOccupied ? '#e0e6ef' : '#bfc8d9');
                const lossStreak = seatData && seatData.loss_streak != null ? Number(seatData.loss_streak) : null;
                const bets5 = seatData && seatData.bets_last_5m != null ? Number(seatData.bets_last_5m) : null;
                const bets15 = seatData && seatData.bets_last_15m != null ? Number(seatData.bets_last_15m) : null;
                const bets30 = seatData && seatData.bets_last_30m != null ? Number(seatData.bets_last_30m) : null;
                const wager10 = seatData && seatData.wager_last_10m != null ? Number(seatData.wager_last_10m) : null;
                const wager30 = seatData && seatData.wager_last_30m != null ? Number(seatData.wager_last_30m) : null;
                const bpm = seatData && seatData.bets_per_minute != null ? Number(seatData.bets_per_minute) : null;
                if (seatData && seatData.avg_bet !== null && seatData.avg_bet !== undefined && Number(seatData.avg_bet) > 0) {
                    avgBetText = `<span class=\"seat-status\" style=\\"color: var(--status-amber); font-weight: 800; font-size:0.98em;\\">$${Math.round(Number(seatData.avg_bet)).toLocaleString()}</span>`;
                }
                const statsTextParts = [];
                if (lossStreak !== null) statsTextParts.push(`LS ${lossStreak}`);
                if (bets5 !== null) statsTextParts.push(`5m ${bets5}`);
                if (bets15 !== null) statsTextParts.push(`15m ${bets15}`);
                if (bets30 !== null) statsTextParts.push(`30m ${bets30}`);
                if (wager10 !== null) statsTextParts.push(`10m $${Math.round(wager10).toLocaleString()}`);
                if (wager30 !== null) statsTextParts.push(`30m $${Math.round(wager30).toLocaleString()}`);
                if (bpm !== null) statsTextParts.push(`BPM ${bpm.toFixed(2)}`);
                const statsText = statsTextParts.length
                    ? `<div class="seat-stats">${statsTextParts.map(p => `<span class="seat-stat-chip">${p}</span>`).join('')}</div>`
                    : '';
                html += `
                    <div class=\"seat-row\" style=\"display: flex; justify-content: space-between; align-items:center; gap: 18px; padding: 2px 0 2px 0; font-size:0.97em;\">
                        <div style=\"display: flex; align-items: center; gap: 8px;\">
                            <div class=\"lamp ${lampClass}\"></div>
                            <span class=\"seat-name\" style='min-width:54px; color:${seatNameColor};'>Seat ${seatKey}</span>
                            <span class=\"seat-status\" style=\"color: ${seatStatusColor}; font-size: 0.93em; margin-left: 2px; font-weight:600;\">${seatStatus}</span>
                        </div>
                        <div style="display: flex; flex-direction:column; align-items: flex-end; min-width:140px;">
                            ${avgBetText}
                            ${statsText}
                        </div>
                    </div>
                `;
            });
        }
        tooltip.innerHTML = html;

        // Position: prefer right of cursor, but flip to left if near right edge; clamp to viewport
        const margin = 12;
        const viewportW = window.innerWidth;
        const viewportH = window.innerHeight;
        const ttRect = tooltip.getBoundingClientRect();
        let left = e.clientX + margin;
        if (left + ttRect.width > viewportW - margin) {
            left = e.clientX - ttRect.width - margin;
        }
        left = Math.max(margin, left);
        let top = e.clientY + margin;
        if (top + ttRect.height > viewportH - margin) {
            top = Math.max(margin, viewportH - margin - ttRect.height);
        }
        tooltip.style.left = `${left}px`;
        tooltip.style.top = `${top}px`;
    } else {
        tooltip.style.display = 'none';
    }
});

mapCanvas?.addEventListener('mouseleave', () => {
    tooltip && (tooltip.style.display = 'none');
});

mapCanvas?.addEventListener('contextmenu', (e) => {
    e.preventDefault();
    if (!mapCanvas) return;
    const rect = mapCanvas.getBoundingClientRect();
    const mouseX = e.clientX - rect.left;
    const mouseY = e.clientY - rect.top;
    let targetTable = null;
    for (const table of state.tableLayout) {
        const dx = mouseX - (table._screenX || 0);
        const dy = mouseY - (table._screenY || 0);
        if (Math.sqrt(dx * dx + dy * dy) < 6) {
            targetTable = table;
            break;
        }
    }
    showContextMenu(e.clientX, e.clientY, targetTable);
});

document.addEventListener('click', (e) => {
    if (!contextMenu || contextMenu.style.display === 'none') return;
    if (!contextMenu.contains(e.target)) {
        contextMenu.style.display = 'none';
    }
});

document.getElementById('item_status')?.addEventListener('click', function(e) {
    e.stopPropagation();
    const tableId = contextMenu?.dataset.activeTable;
    const toggle = this.querySelector('.toggle-status');
    const isActive = toggle.classList.toggle('active');
    toggle.classList.toggle('inactive', !isActive);
    toggle.textContent = isActive ? 'ON' : 'OFF';
    console.log(`[ACTION] Table ${tableId} Status: ${isActive ? 'ON' : 'OFF'}`);
});

document.getElementById('sel_min_bet')?.addEventListener('click', e => e.stopPropagation());
document.getElementById('sel_min_bet')?.addEventListener('change', function(e) {
    const tableId = contextMenu?.dataset.activeTable;
    console.log(`[ACTION] Table ${tableId} Min Bet set to: ${this.value}`);
});

document.getElementById('sel_max_bet')?.addEventListener('click', e => e.stopPropagation());
document.getElementById('sel_max_bet')?.addEventListener('change', function(e) {
    const tableId = contextMenu?.dataset.activeTable;
    console.log(`[ACTION] Table ${tableId} Max Bet set to: ${this.value}`);
});

document.getElementById('sel_comp_seat')?.addEventListener('click', e => e.stopPropagation());
document.getElementById('sel_comp_type')?.addEventListener('click', e => e.stopPropagation());

document.getElementById('item_comp')?.addEventListener('click', function(e) {
    e.stopPropagation();
    if (!contextMenu) return;
    if (this.classList.contains('disabled')) return;
    const tableId = contextMenu.dataset.activeTable;
    const seatId = document.getElementById('sel_comp_seat').value;
    const compType = document.getElementById('sel_comp_type').value;
    if (!seatId) {
        alert("Please select a Seat number first.");
        return;
    }
    const originalContent = this.innerHTML;
    const originalBg = this.style.background;
    this.style.background = "#059669";
    this.innerHTML = "<span>Sending...</span>";
    setTimeout(() => {
        this.innerHTML = `<span>Sent!</span>`;
        console.log(`[ACTION] Comp [${compType}] successfully issued to Table ${tableId} Seat ${seatId}`);
        setTimeout(() => {
            this.innerHTML = originalContent;
            this.style.background = originalBg;
            contextMenu.style.display = 'none';
        }, 1000);
    }, 600);
});

export async function fetchMapStatus() {
    try {
        const data = await getFloorStatus();
        let occupiedTables = 0;
        let occupiedSeats = 0;
        if (data.layout) {
            state.tableLayout = data.layout;
            state.tableLayout.forEach(table => {
                if (table.status) {
                    const tid = String(table.table_id);
                    const alert = state.tableAlerts[tid];
                    const effectiveStatus = { ...table.status };
                    if (alert && alert.status === 'pending' && alert.seats) {
                        alert.seats.forEach(s => effectiveStatus[s] = 1);
                    }
                    const seats = Object.values(effectiveStatus).filter(v => Number(v) === 1).length;
                    if (seats > 0) {
                        occupiedTables++;
                        occupiedSeats += seats;
                    }
                }
            });
        } else if (data.occupied) {
            occupiedSeats = data.occupied.length;
            occupiedTables = new Set(data.occupied.map(s => String(s.table_id))).size;
        }
        document.getElementById('stat_occ_tables').textContent = occupiedTables;
        document.getElementById('stat_occ_seats').textContent = occupiedSeats;
        const utilEl = document.getElementById('stat_utilization');
        if (utilEl) {
            const util = occupiedTables > 0 ? (occupiedSeats / occupiedTables) : 0;
            utilEl.textContent = util.toFixed(1);
            utilEl.title = `${occupiedSeats} seats / ${occupiedTables} tables`;
        }
        state.prevOccupiedSeats = occupiedSeats;
        drawMap();
    } catch (err) {
        console.error('Map fetch error:', err);
    }
}

splitter?.addEventListener('mousedown', () => {
    isResizing = true;
    document.body.style.cursor = 'row-resize';
    document.body.style.userSelect = 'none';
});

document.addEventListener('mousemove', (e) => {
    if (!isResizing) return;
    const containerRect = container.getBoundingClientRect();
    let newHeight = e.clientY - containerRect.top;
    const minH = 100;
    const maxH = containerRect.height - 100;
    if (newHeight < minH) newHeight = minH;
    if (newHeight > maxH) newHeight = maxH;
    mapPanel.style.height = newHeight + 'px';
    resizeCanvas();
});

document.addEventListener('mouseup', () => {
    if (isResizing) {
        isResizing = false;
        document.body.style.cursor = 'default';
        document.body.style.userSelect = 'auto';
    }
});

export function initMapInteractions() {
    window.addEventListener('resize', () => {
        const h = container.offsetHeight;
        if (mapPanel.offsetHeight > h - 100) {
            mapPanel.style.height = (h - 100) + 'px';
        }
        resizeCanvas();
    });
    resizeCanvas();

    // Start blink timer for high roller indication (toggle opacity)
    if (blinkTimer) {
        clearInterval(blinkTimer);
        blinkTimer = null;
    }
    blinkOn = true;
    blinkTimer = setInterval(() => {
        blinkOn = !blinkOn;
        // Redraw map to reflect blink state
        drawMap();
    }, 600);
}

// Crosshair / highlight functionality
let crossHideTimer = null;
export function showCrosshair(tableId, opts = { duration: 2200 }) {
    if (!mapPanel || !state.tableLayout) return;
    const table = state.tableLayout.find(t => String(t.table_id) === String(tableId));
    if (!table) return;
    // ensure layout positions are up-to-date
    const px = table._screenX || 0;
    const py = table._screenY || 0;
    const panelRect = mapPanel.getBoundingClientRect();

    // Create or reuse elements
    let h = mapPanel.querySelector('.map-cross-h');
    let v = mapPanel.querySelector('.map-cross-v');
    let c = mapPanel.querySelector('.map-cross-center');
    if (!h) {
        h = document.createElement('div');
        h.className = 'map-cross-h';
        mapPanel.appendChild(h);
    }
    if (!v) {
        v = document.createElement('div');
        v.className = 'map-cross-v';
        mapPanel.appendChild(v);
    }
    if (!c) {
        c = document.createElement('div');
        c.className = 'map-cross-center';
        mapPanel.appendChild(c);
    }

    // Clear any running timer
    if (crossHideTimer) {
        clearTimeout(crossHideTimer);
        crossHideTimer = null;
    }

    // Initialize start positions (off the bottom / right)
    h.style.width = panelRect.width + 'px';
    h.style.left = '0px';
    h.style.top = (panelRect.height) + 'px'; // start below

    v.style.height = panelRect.height + 'px';
    v.style.top = '0px';
    v.style.left = (panelRect.width) + 'px'; // start right

    c.style.left = (px - 8) + 'px';
    c.style.top = (py - 8) + 'px';
    c.style.opacity = '0';

    // Force layout then animate to target
    requestAnimationFrame(() => {
        requestAnimationFrame(() => {
            h.style.top = py + 'px';
            v.style.left = px + 'px';
            c.style.opacity = '1';
            c.classList.add('pulse');
        });
    });

    // Auto-hide after duration
    crossHideTimer = setTimeout(() => {
        // animate back out
        h.style.top = (panelRect.height) + 'px';
        v.style.left = (panelRect.width) + 'px';
        c.style.opacity = '0';
        c.classList.remove('pulse');
        // remove after transition
        setTimeout(() => {
            h.remove(); v.remove(); c.remove();
        }, 400);
    }, opts.duration);
}

