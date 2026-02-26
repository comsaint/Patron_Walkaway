import { config } from './state.js';

function buildUrl(endpoint, params) {
    let url = `${config.API_BASE}${endpoint}`;
    if (params) {
        const query = new URLSearchParams();
        for (const [key, value] of Object.entries(params)) {
            if (value !== undefined && value !== null) {
                query.set(key, value);
            }
        }
        url += `?${query.toString()}`;
    }
    return url;
}

async function fetchJson(url) {
    const response = await fetch(url);
    if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
    }
    return response.json();
}

export async function getAlerts(ts) {
    const endpoint = buildUrl('/get_alerts', ts ? { ts } : null);
    return fetchJson(endpoint);
}

export async function getValidations(ts) {
    const endpoint = buildUrl('/get_validation', ts ? { ts } : null);
    return fetchJson(endpoint);
}

export async function getFloorStatus() {
    const endpoint = buildUrl('/get_floor_status', null);
    return fetchJson(endpoint);
}

export async function getHCHistory(hours) {
    const endpoint = buildUrl('/get_hc_history', (hours ? { hours } : null));
    return fetchJson(endpoint);
}
