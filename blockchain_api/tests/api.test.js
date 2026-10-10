const test = require("node:test");
const assert = require("node:assert/strict");
const { ethers } = require("ethers");
const { createApp } = require("../server");
const { preparePayload, TYPES } = require("../eventPayload");
const { fixture, payload, ADDRESS, CHAIN_ID } = require("./helpers");

async function api(t, tokens = { HRC_INGEST_API_TOKEN: "i".repeat(40), HRC_ADMIN_API_TOKEN: "a".repeat(40) }) {
    const f = fixture(t);
    const service = {
        getAdminAccess: () => require("../adminAccess").getAdminAccess({ contract: f.contract, wallet: f.owner }),
        processSafetyEvent: input => f.service.processSafetyEvent(input),
        unlockRobot: id => f.service.unlockRobot(id),
        getStoredPayload: id => f.service.getStoredPayload(id),
        async getAllSafetyEvents() {
            const events = [...f.state.events.values()].map(event => ({ eventId: event[0], deviceId: event[1],
                timestamp: Number(event[2]), dataHash: event[3], riskLevel: Number(event[4]), recordedAt: Number(event[5]),
                blockNumber: 11853429, transactionHash: [...f.state.receipts.keys()][0] }));
            return { success: true, count: events.length, events };
        },
        async getSafetyEventById(id) {
            return { success: f.state.events.has(id), event: f.state.events.has(id) ? { eventId: id } : undefined };
        },
        async getRobotStatus(id) {
            const robotLocked = await f.contract.isRobotLocked(id);
            return { success: true, deviceId: id, robotLocked, status: robotLocked ? "LOCKED" : "UNLOCKED" };
        }
    };
    const server = createApp({ service, tokens }).listen(0, "127.0.0.1");
    await new Promise((resolve, reject) => { server.once("listening", resolve); server.once("error", reject); });
    t.after(() => new Promise(resolve => server.close(resolve)));
    const base = `http://127.0.0.1:${server.address().port}`;
    const call = async (path, { method = "GET", token, body, raw } = {}) => {
        const headers = {};
        if (token) headers.Authorization = "Bearer " + token;
        if (body !== undefined || raw !== undefined) headers["Content-Type"] = "application/json";
        const response = await fetch(base + path, { method, headers, body: raw ?? (body === undefined ? undefined : JSON.stringify(body)) });
        return { status: response.status, body: await response.json() };
    };
    return { ...f, tokens, call };
}
test("HTTP end-to-end: validated ingestion, dedupe, archive, dashboard reads and authorized unlock", async t => {
    const f = await api(t);
    const input = payload();
    const event = await f.call("/api/safety-event", { method: "POST", body: input, token: f.tokens.HRC_INGEST_API_TOKEN });
    assert.equal(event.status, 200);
    assert.equal(event.body.robotLocked, true);
    assert(!Object.hasOwn(event.body, "httpStatus"));
    const retry = await f.call("/api/safety-event", { method: "POST", body: input, token: f.tokens.HRC_INGEST_API_TOKEN });
    assert.equal(retry.body.duplicate, true);
    assert.equal(retry.body.recordTransactionHash, event.body.recordTransactionHash);
    const events = await f.call("/api/events");
    assert.equal(events.body.count, 1);
    assert.equal(events.body.events[0].dataHash, event.body.dataHash);
    const detail = await f.call("/api/events/" + event.body.eventId);
    assert.equal(detail.status, 200);
    const stored = await f.call(`/api/events/${event.body.eventId}/payload`, { token: f.tokens.HRC_ADMIN_API_TOKEN });
    assert.equal(stored.status, 200);
    assert.equal(ethers.keccak256(ethers.toUtf8Bytes(stored.body.canonicalPayload)), event.body.dataHash);
    assert.equal((await f.call("/api/robots/ROBOT-ARM-01/status")).body.status, "LOCKED");
    const unlock = await f.call("/api/robots/ROBOT-ARM-01/unlock", { method: "POST", token: f.tokens.HRC_ADMIN_API_TOKEN });
    assert.equal(unlock.status, 200);
    assert.equal((await f.call("/api/robots/ROBOT-ARM-01/status")).body.status, "UNLOCKED");
    assert.equal(f.state.signed.length, 2);
});
test("HTTP write roles fail closed and unauthorized requests never sign", async t => {
    const f = await api(t);
    for (const token of [undefined, "wrong", f.tokens.HRC_ADMIN_API_TOKEN]) {
        assert.equal((await f.call("/api/safety-event", { method: "POST", body: payload(), token })).status, 401);
    }
    assert.equal((await f.call("/api/robots/ROBOT-ARM-01/unlock", { method: "POST", token: f.tokens.HRC_INGEST_API_TOKEN })).status, 401);
    assert.equal((await f.call("/api/events/old/payload", { token: f.tokens.HRC_INGEST_API_TOKEN })).status, 401);
    assert.equal(f.state.signed.length, 0);
    const unconfigured = await api(t, {});
    assert.equal((await unconfigured.call("/api/safety-event", { method: "POST", body: payload() })).status, 503);
    assert.equal((await unconfigured.call("/api/events")).status, 200);
    const same = await api(t, { HRC_INGEST_API_TOKEN: "s".repeat(40), HRC_ADMIN_API_TOKEN: "s".repeat(40) });
    assert.equal((await same.call("/api/safety-event", { method: "POST", body: payload(), token: "s".repeat(40) })).status, 503);
});

test("dashboard admin access rejects visitors and ingest tokens and checks owner", async t => {
    const f = await api(t);
    for (const token of [undefined, "wrong", f.tokens.HRC_INGEST_API_TOKEN]) {
        assert.equal((await f.call("/api/admin/access", { token })).status, 401);
    }
    const allowed = await f.call("/api/admin/access", { token: f.tokens.HRC_ADMIN_API_TOKEN });
    assert.equal(allowed.status, 200);
    assert.equal(allowed.body.canUnlock, true);
    assert.equal(allowed.body.ownerAddress, f.owner.address);
    f.contract.owner = async () => ethers.Wallet.createRandom().address;
    const denied = await f.call("/api/admin/access", { token: f.tokens.HRC_ADMIN_API_TOKEN });
    assert.equal(denied.status, 403);
    assert.equal(denied.body.code, "NOT_OWNER");
    assert.equal(f.state.signed.length, 0);
});
test("HTTP validation, malformed JSON, size limit, conflict and pending statuses", async t => {
    const f = await api(t);
    const options = { method: "POST", token: f.tokens.HRC_INGEST_API_TOKEN };
    assert.equal((await f.call("/api/safety-event", { ...options, body: payload({ aiResult: { riskLevel: null } }) })).status, 400);
    assert.equal((await f.call("/api/safety-event", { ...options, raw: '{bad json' })).status, 400);
    assert.equal((await f.call("/api/safety-event", { ...options, body: { data: "x".repeat(70000) } })).status, 413);
    assert.equal(f.state.signed.length, 0);
    f.state.confirm = false;
    const pending = await f.call("/api/safety-event", { ...options, body: payload() });
    assert.equal(pending.status, 202);
    assert.equal(pending.body.transactionStatus, "pending");
    assert.equal(pending.body.robotLocked, null);
    const conflict = await f.call("/api/safety-event", { ...options, body: payload({ sensorData: { distance: 0.2 } }) });
    assert.equal(conflict.status, 409);
    assert.equal(conflict.body.code, "IDEMPOTENCY_CONFLICT");
    const missing = await f.call("/api/events/historical/payload", { token: f.tokens.HRC_ADMIN_API_TOKEN });
    assert.equal(missing.status, 404);
});
test("HTTP registered device signatures are verified before broadcast", async t => {
    const f = await api(t);
    const device = ethers.Wallet.createRandom();
    f.state.devices.set("ROBOT-ARM-01", [device.address, false]);
    const input = payload();
    const prepared = preparePayload(input);
    const signature = await device.signTypedData({ name: "HRCSafetyLog", version: "1", chainId: CHAIN_ID, verifyingContract: ADDRESS }, TYPES,
        { eventId: prepared.payload.eventId, deviceId: input.deviceId, timestamp: input.timestamp, dataHash: prepared.dataHash, riskLevel: 3 });
    const options = { method: "POST", token: f.tokens.HRC_INGEST_API_TOKEN };
    assert.equal((await f.call("/api/safety-event", { ...options, body: input })).status, 403);
    const response = await f.call("/api/safety-event", { ...options, body: { ...input, signature } });
    assert.equal(response.status, 200);
    assert.equal(response.body.transactionStatus, "confirmed");
    assert.equal(f.state.signed.length, 1);
});
