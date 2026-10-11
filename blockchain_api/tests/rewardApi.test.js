const test = require("node:test");
const assert = require("node:assert/strict");
const { createApp } = require("../server");

test("session start route requires ingest token", async t => {
    const tokens = { HRC_INGEST_API_TOKEN: "i".repeat(40), HRC_ADMIN_API_TOKEN: "a".repeat(40) };
    const calls = [];
    const service = { async startSession(input) { calls.push(input); return { success: true, sessionId: input.sessionId, duplicate: false }; } };
    const server = createApp({ service, tokens }).listen(0, "127.0.0.1");
    await new Promise((resolve, reject) => { server.once("listening", resolve); server.once("error", reject); });
    t.after(() => new Promise(resolve => server.close(resolve)));
    const url = `http://127.0.0.1:${server.address().port}/api/session-start`;
    const input = { sessionId: "run_1", beneficiary: "0x0000000000000000000000000000000000000001" };
    assert.equal((await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input) })).status, 401);
    const allowed = await fetch(url, { method: "POST", headers: { Authorization: `Bearer ${tokens.HRC_INGEST_API_TOKEN}`, "Content-Type": "application/json" }, body: JSON.stringify(input) });
    assert.equal(allowed.status, 200);
    assert.deepEqual(calls, [input]);
});

test("session outcome route requires ingest token and returns duration-policy settlement", async t => {
    const tokens = { HRC_INGEST_API_TOKEN: "i".repeat(40), HRC_ADMIN_API_TOKEN: "a".repeat(40) };
    const calls = [];
    const service = {
        async processSessionOutcome(input) {
            calls.push(input);
            return { success: true, policyVersion: "session-reward-v3-duration-bonus", outcome: input.outcome, rewardPaid: "3", penaltyCollected: "0" };
        }
    };
    const server = createApp({ service, tokens }).listen(0, "127.0.0.1");
    await new Promise((resolve, reject) => { server.once("listening", resolve); server.once("error", reject); });
    t.after(() => new Promise(resolve => server.close(resolve)));
    const url = `http://127.0.0.1:${server.address().port}/api/session-outcome`;
    const input = { sessionId: "run_1", beneficiary: "0x0000000000000000000000000000000000000001", outcome: "VALID_NO_VIOLATION" };
    const denied = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input) });
    assert.equal(denied.status, 401);
    const allowed = await fetch(url, { method: "POST", headers: {
        Authorization: `Bearer ${tokens.HRC_INGEST_API_TOKEN}`, "Content-Type": "application/json"
    }, body: JSON.stringify(input) });
    assert.equal(allowed.status, 200);
    assert.deepEqual(await allowed.json(), { success: true, policyVersion: "session-reward-v3-duration-bonus", outcome: "VALID_NO_VIOLATION", rewardPaid: "3", penaltyCollected: "0" });
    assert.deepEqual(calls, [input]);
});
