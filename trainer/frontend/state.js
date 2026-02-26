// Shared application state and DOM caches
export const config = {
    API_BASE: window.location.port === '5500' ? 'http://127.0.0.1:8000' : ''
};

export const dom = {
    splitter: document.getElementById('splitter'),
    mapPanel: document.getElementById('map_panel'),
    listPanel: document.getElementById('list_panel'),
    container: document.getElementById('container'),
    alertsBody: document.getElementById('alerts_body'),
    mapCanvas: document.getElementById('map_canvas'),
    mapCtx: document.getElementById('map_canvas')?.getContext('2d') || null,
    tooltip: document.getElementById('canvas_tooltip'),
    contextMenu: document.getElementById('context_menu')
};

export const state = {
    refreshSecondsLeft: 45,
    progressTimer: null,
    isResizing: false,
    lastAlertTs: null,
    lastValidationTs: null,
    tableLayout: [],
    prevOccupiedSeats: null,
    allValidations: [],
    validationCache: {},
    tableAlerts: {},
    accuracyChart: null,
    hcChart: null,
    hcHistory: [],
    // Trends time window in hours (max 24)
    trendsHours: 24
};
