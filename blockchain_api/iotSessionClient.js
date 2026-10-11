const { ApiError } = require('./eventPayload');
class IotSessionClient {
    constructor({ baseUrl = 'http://127.0.0.1:8001', fetchImpl = fetch, timeoutMs = 12000 } = {}) {
        this.baseUrl = baseUrl.replace(/\/$/, ''); this.fetch = fetchImpl; this.timeoutMs = timeoutMs;
    }
    async request(path, body) {
        let response;
        try {
            response = await this.fetch(this.baseUrl + path, { method: body === undefined ? 'GET' : 'POST',
                headers: { Accept: 'application/json', ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
                body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(this.timeoutMs) });
        } catch { throw new ApiError(503, 'IOT_UNAVAILABLE', 'Sensor API is unavailable; the same session will be recovered'); }
        const data = await response.json();
        if (!response.ok) throw new ApiError(response.status, 'IOT_REQUEST_FAILED', typeof data.detail === 'string' ? data.detail : 'Sensor API request failed');
        return data;
    }
    health() { return this.request('/health'); }
    latest() { return this.request('/live/latest'); }
    list() { return this.request('/sessions'); }
    async get(id) {
        try { return await this.request('/sessions/' + encodeURIComponent(id)); }
        catch (error) { if (error.statusCode === 404) return null; throw error; }
    }
    async start(id) {
        const existing = await this.get(id);
        if (existing) return existing;
        try { return await this.request('/sessions/start', { session_id: id, scenario: 'user_session', object: 'person', zone_note: 'Account-bound live sensor session' }); }
        catch (error) { const recovered = await this.get(id); if (recovered) return recovered; throw error; }
    }
    async stop(id) {
        const existing = await this.get(id);
        if (!existing) throw new ApiError(404, 'IOT_SESSION_NOT_FOUND', 'The sensor session does not exist');
        if (existing.status === 'closed') return existing;
        try { return await this.request('/sessions/' + encodeURIComponent(id) + '/stop', {}); }
        catch (error) { const recovered = await this.get(id); if (recovered?.status === 'closed') return recovered; throw error; }
    }
    outcome(id) { return this.request('/sessions/' + encodeURIComponent(id) + '/outcome'); }
}
module.exports = { IotSessionClient };
