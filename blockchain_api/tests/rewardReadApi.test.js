const test = require("node:test");
const assert = require("node:assert/strict");
const { createApp } = require("../server");

async function serve(t) {
    const tokens = { HRC_INGEST_API_TOKEN: "i".repeat(40), HRC_ADMIN_API_TOKEN: "a".repeat(40) };
    const calls = [];
    const service = {
        async startSession(input) { calls.push(["start", input]); return { success: true, duplicate: false, sessionId: input.sessionId }; },
        async getRewardConfig() { return { success: true, network: "localhost", chainId: 31337, defaultBeneficiary: "0x0000000000000000000000000000000000000001" }; },
        async getRewardSummary(beneficiary) { return { success: true, beneficiary, balance: "21.0", rewardPoolBalance: "997.0" }; },
        async getRewardSessions(input) { return { success: true, count: 1, sessions: [{ sessionKey: "0x" + "1".repeat(64), settlementStatus: "REWARDED" }], input }; },
        async getRewardSession(sessionId) { return { success: true, session: { sessionId } }; },
        async getRewardTransactions(input) { return { success: true, count: 0, transactions: [], input }; }
    };
    const server = createApp({ service, tokens }).listen(0, "127.0.0.1");
    await new Promise((resolve, reject) => { server.once("listening", resolve); server.once("error", reject); });
    t.after(() => new Promise(resolve => server.close(resolve)));
    const base = `http://127.0.0.1:${server.address().port}`;
    const call = async (path, options = {}) => {
        const headers = { ...(options.body ? { "Content-Type": "application/json" } : {}) };
        if (options.token) headers.Authorization = `Bearer ${options.token}`;
        const response = await fetch(base + path, { ...options, headers, body: options.body && JSON.stringify(options.body) });
        return { status: response.status, body: await response.json() };
    };
    return { call, calls, tokens };
}

test("reward read routes expose summary/session/transaction views", async t => {
    const f = await serve(t);
    assert.equal((await f.call("/api/rewards/config")).body.chainId, 31337);
    assert.equal((await f.call("/api/rewards/summary?beneficiary=0x0000000000000000000000000000000000000001")).body.balance, "21.0");
    assert.equal((await f.call("/api/rewards/sessions?limit=20")).body.count, 1);
    assert.equal((await f.call("/api/rewards/sessions/s1")).body.session.sessionId, "s1");
    assert.equal((await f.call("/api/rewards/transactions")).body.count, 0);
});

test("reward session start is protected by the ingest token", async t => {
    const f = await serve(t);
    const body = { sessionId: "run_1", beneficiary: "0x0000000000000000000000000000000000000001" };
    assert.equal((await f.call("/api/session-start", { method: "POST", body })).status, 401);
    assert.equal((await f.call("/api/session-start", { method: "POST", body, token: f.tokens.HRC_INGEST_API_TOKEN })).status, 200);
    assert.deepEqual(f.calls, [["start", body]]);
});
