const test = require("node:test");
const assert = require("node:assert/strict");
const { createApp } = require("../server");

const USERS = {
    admin: { userId: "admin", role: "admin", walletAddress: null },
    user_1: { userId: "user_1", role: "customer", walletAddress: "0x0000000000000000000000000000000000000001" },
    user_2: { userId: "user_2", role: "customer", walletAddress: "0x0000000000000000000000000000000000000002" }
};

function demoFixture() {
    const tokens = new Map();
    const service = {
        listAccounts: () => Object.values(USERS),
        login(userId) {
            if (!USERS[userId]) throw Object.assign(new Error("unknown"), { statusCode: 401, code: "UNKNOWN_DEMO_ACCOUNT" });
            const token = `${userId}-token`;
            tokens.set(token, USERS[userId]);
            return { success: true, token, user: USERS[userId] };
        },
        authenticateToken(token) {
            const user = tokens.get(token);
            return user ? { user, session: { token } } : null;
        },
        logout(token) { tokens.delete(token); return { success: true }; },
        getWallet(userId) { return { success: true, userId, address: USERS[userId].walletAddress, balance: "20", allowance: "0", requiredAllowance: "2", canStart: false }; },
        getTransactions(userId) { return { success: true, userId, transactions: [{ transactionHash: "0x" + "a".repeat(64), events: [{ name: "Approval" }], blockNumber: 10 }] }; },
        approve(userId) { return { success: true, userId, transactionHash: "0x" + "a".repeat(64), blockNumber: 10, event: { name: "Approval" } }; },
        startSession(userId, body) { return { success: true, userId, sessionId: `hrc-${userId}-${body.sessionRef || "default"}`, transactionHash: "0x" + "b".repeat(64), blockNumber: 11, event: { name: "SessionStarted" } }; },
        listSessions(userId) { return { success: true, userId, count: 0, sessions: [] }; },
        getSession(userId, sessionId) {
            // Ownership is decided by the stored record, never by parsing the id.
            const owner = (String(sessionId).match(/^(?:hrc|demo)-([A-Za-z0-9_.:-]+)-/) || [])[1];
            if (owner && owner !== userId) {
                const { ApiError } = require("../eventPayload");
                throw new ApiError(403, "SESSION_OWNER_MISMATCH", "Other user session");
            }
            return { success: true, session: { userId, sessionId } };
        },
        adminOverview() { return { success: true, users: [{ userId: "user_1" }, { userId: "user_2" }] }; },
        isWalletFirst() { return false; }
    };
    return { service, tokens };
}

async function serve(t) {
    const fixture = demoFixture();
    const liveService = {
        wallet: id => fixture.service.getWallet(id), sessions: id => fixture.service.listSessions(id),
        session: (id, ref) => fixture.service.getSession(id, ref), start: (id, body) => fixture.service.startSession(id, body),
        end: (id, ref) => { fixture.service.getSession(id, ref); return { success: true, sessionId: ref, settled: true }; },
        overview: () => fixture.service.adminOverview()
    };
    const app = createApp({ demoService: fixture.service, liveService, service: {}, tokens: {
        HRC_INGEST_API_TOKEN: "i".repeat(40), HRC_ADMIN_API_TOKEN: "a".repeat(40)
    }});
    const server = app.listen(0, "127.0.0.1");
    await new Promise((resolve, reject) => { server.once("listening", resolve); server.once("error", reject); });
    t.after(() => new Promise(resolve => server.close(resolve)));
    const base = `http://127.0.0.1:${server.address().port}`;
    const call = async (path, options = {}) => {
        const headers = { ...(options.body ? { "Content-Type": "application/json" } : {}), ...(options.token ? { Authorization: `Bearer ${options.token}` } : {}) };
        const response = await fetch(base + path, { ...options, headers, body: options.body && JSON.stringify(options.body) });
        return { status: response.status, body: await response.json() };
    };
    return { call, fixture };
}

test("fixed demo accounts expose stable identities and user wallet routes are isolated", async t => {
    const f = await serve(t);
    const accounts = await f.call("/api/demo/accounts");
    assert.equal(accounts.status, 200);
    assert.deepEqual(accounts.body.accounts.map(account => account.userId), ["admin", "user_1", "user_2"]);

    const login1 = await f.call("/api/demo/login", { method: "POST", body: { userId: "user_1" } });
    const login2 = await f.call("/api/demo/login", { method: "POST", body: { userId: "user_2" } });
    assert.equal(login1.status, 200);
    assert.equal(login2.status, 200);
    assert.equal((await f.call("/api/me/wallet", { token: login1.body.token })).body.userId, "user_1");
    assert.equal((await f.call("/api/me/sessions", { token: login2.body.token })).body.userId, "user_2");
    assert.equal((await f.call("/api/me/wallet/transactions", { token: login2.body.token })).body.userId, "user_2");
    assert.equal((await f.call("/api/me/wallet/transactions")).status, 401);
    const started = await f.call("/api/me/sessions/start", { method: "POST", token: login2.body.token, body: { sessionRef: "demo" } });
    assert.equal(started.body.sessionId, "hrc-user_2-demo");
    assert.equal((await f.call("/api/me/sessions/hrc-user_1-demo", { token: login2.body.token })).status, 403);
    assert.equal((await f.call('/api/me/sessions/hrc-user_1-demo/end', { method: 'POST', token: login2.body.token })).status, 403);
    assert.equal((await f.call('/api/me/sessions/hrc-user_2-demo/end', { method: 'POST', token: login2.body.token })).body.settled, true);
    assert.equal((await f.call('/api/me/sessions/demo-user_2-demo/end', { method: 'POST' })).status, 401);
});

test("admin demo account is read-only and cannot call user wallet operations", async t => {
    const f = await serve(t);
    const login = await f.call("/api/demo/login", { method: "POST", body: { userId: "admin" } });
    assert.equal(login.status, 200);
    assert.equal((await f.call("/api/demo/admin/overview", { token: login.body.token })).status, 200);
    assert.equal((await f.call("/api/me/wallet", { token: login.body.token })).status, 403);
    assert.equal((await f.call("/api/me/wallet/approve", { method: "POST", token: login.body.token })).status, 403);
    assert.equal((await f.call("/api/me/wallet/transactions", { token: login.body.token })).status, 403);
    assert.equal((await f.call("/api/me/sessions/start", { method: "POST", token: login.body.token, body: { sessionRef: "forbidden" } })).status, 403);
    assert.equal((await f.call('/api/me/sessions/demo-user_1-demo/end', { method: 'POST', token: login.body.token })).status, 403);
    assert.equal((await f.call("/api/session-outcome", { method: "POST", token: login.body.token, body: { sessionId: "demo-user_1-x", outcome: "VALID_NO_VIOLATION" } })).status, 401);
});
