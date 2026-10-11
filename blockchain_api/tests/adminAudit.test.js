const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { createApp } = require("../server");
const { CustomerStore } = require("../customerStore");
const { CustomerService } = require("../customerService");
const { AuditLog } = require("../auditLog");

const ADMIN_TOKEN = "a".repeat(40);

async function fixture(t, { rewardService } = {}) {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "hrc-admin-"));
    const store = new CustomerStore(path.join(directory, "customers.json"));
    const customerService = new CustomerService({ store, chainId: 31337, authMode: "wallet" });
    const app = createApp({
        service: { getAdminAccess: async () => ({ success: true, owner: "0xowner", backendAddress: "0xbackend", isOwner: true }) },
        customerService,
        rewardService,
        auditLog: new AuditLog({ store }),
        tokens: { HRC_ADMIN_API_TOKEN: ADMIN_TOKEN, HRC_INGEST_API_TOKEN: "i".repeat(40), HRC_APP_AUTH_MODE: "demo" }
    });
    const server = app.listen(0, "127.0.0.1");
    await new Promise((resolve, reject) => { server.once("listening", resolve); server.once("error", reject); });
    t.after(() => { server.close(); store.close(); fs.rmSync(directory, { recursive: true, force: true }); });
    const base = `http://127.0.0.1:${server.address().port}`;
    const call = async (route, { method = "GET", body, token = ADMIN_TOKEN, actor } = {}) => {
        const headers = {};
        if (body !== undefined) headers["Content-Type"] = "application/json";
        if (token) headers.Authorization = "Bearer " + token;
        if (actor) headers["x-hrc-actor"] = actor;
        const response = await fetch(base + route, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
        return { status: response.status, body: await response.json() };
    };
    return { call, store, auditLog: new AuditLog({ store }) };
}

function fakeRewardService(overrides = {}) {
    const calls = [];
    return {
        calls,
        async setPaused(paused) { calls.push(["setPaused", paused]); return { success: true, paused, transactionHash: "0x" + "1".repeat(64), blockNumber: 5 }; },
        async setPolicy(policy) { calls.push(["setPolicy", policy]); return { success: true, transactionHash: "0x" + "2".repeat(64), blockNumber: 6 }; },
        async fundRewardPool(amount) { calls.push(["fundRewardPool", amount]); return { success: true, transactionHash: "0x" + "3".repeat(64), blockNumber: 7 }; },
        async sweepExcessToken(to, amount) { calls.push(["sweepExcessToken", to, amount]); return { success: true, transactionHash: "0x" + "4".repeat(64), blockNumber: 8 }; },
        async setPenaltyTreasury(treasury) { calls.push(["setPenaltyTreasury", treasury]); return { success: true, treasury, transactionHash: "0x" + "5".repeat(64), blockNumber: 9 }; },
        async getAdminStatus() { return { success: true, operator: "0xop", owner: "0xop", isOwner: true, paused: false, rewardPoolBalance: "1000.0", maxRewardPool: "100000.0", penaltyTreasury: "0xtreasury", token: "0xtoken", treasuryBalance: "2.0", contractTokenBalance: "1000.0" }; },
        ...overrides
    };
}

test("audit log records success and failure without losing the error", async t => {
    const f = await fixture(t);
    const ok = await f.auditLog.run({ actor: "admin", action: "reward.pause" }, async () => ({ transactionHash: "0xabc", blockNumber: 3 }));
    assert.equal(ok.transactionHash, "0xabc");

    await assert.rejects(
        f.auditLog.run({ actor: "admin", action: "reward.policy", details: { stakeAmount: "0" } }, async () => { throw new Error("boom"); }),
        /boom/
    );
    const entries = f.store.listAudit();
    assert.equal(entries.length, 2);
    assert.equal(entries[0].status, "error");
    assert.equal(entries[0].action, "reward.policy");
    assert.match(entries[0].details.error, /boom/);
    assert.equal(entries[1].status, "ok");
    assert.equal(entries[1].transactionHash, "0xabc");
    assert.equal(entries[1].blockNumber, 3);
    // Newest first, and the failure path must not drop the audit entry.
    assert.equal(entries[0].at >= entries[1].at, true);
});

test("admin reward routes require the admin token and are audited", async t => {
    const f = await fixture(t, { rewardService: fakeRewardService() });
    assert.equal((await f.call("/api/admin/reward/status", { token: null })).status, 401);
    assert.equal((await f.call("/api/admin/reward/status", { token: "w".repeat(40) })).status, 401);
    assert.equal((await f.call("/api/admin/reward/status")).status, 200);

    assert.equal((await f.call("/api/admin/reward/pause", { method: "POST", actor: "alice" })).status, 200);
    assert.equal((await f.call("/api/admin/reward/unpause", { method: "POST", actor: "alice" })).status, 200);
    assert.equal((await f.call("/api/admin/reward/policy", { method: "POST", actor: "bob", body: {
        stakeAmount: "2000000000000000000", rewardAmount: "3000000000000000000", penaltyAmount: "2000000000000000000",
        minRewardDuration: "30", fullBonusDuration: "60", maxRewardBonusPercent: "100"
    }})).status, 200);
    assert.equal((await f.call("/api/admin/reward/pool/fund", { method: "POST", actor: "bob", body: { amount: "1000000000000000000" } })).status, 200);
    assert.equal((await f.call("/api/admin/reward/pool/sweep", { method: "POST", actor: "bob", body: { to: "0x" + "9".repeat(40), amount: "1" } })).status, 200);
    assert.equal((await f.call("/api/admin/reward/treasury", { method: "POST", actor: "bob", body: { treasury: "0x" + "8".repeat(40) } })).status, 200);

    const audit = await f.call("/api/admin/audit", { actor: "auditor" });
    assert.equal(audit.status, 200);
    assert.equal(audit.body.count, 6);
    assert.deepEqual(audit.body.entries.map(entry => entry.action), [
        "reward.treasury", "reward.pool.sweep", "reward.pool.fund", "reward.policy", "reward.unpause", "reward.pause"
    ]);
    assert.equal(audit.body.entries[0].actor, "bob");
    assert.equal(audit.body.entries[0].status, "ok");
    assert.equal(audit.body.entries[3].details.stakeAmount, "2000000000000000000");
});

test("a failing privileged action is audited as an error and returns 503", async t => {
    const failing = fakeRewardService({ async setPaused() { const { ApiError } = require("../eventPayload"); throw new ApiError(503, "REWARD_PAUSE_FAILED", "not the owner"); } });
    const f = await fixture(t, { rewardService: failing });
    const response = await f.call("/api/admin/reward/pause", { method: "POST", actor: "alice" });
    assert.equal(response.status, 503);
    assert.equal(response.body.code, "REWARD_PAUSE_FAILED");
    const entries = f.store.listAudit();
    assert.equal(entries.length, 1);
    assert.equal(entries[0].status, "error");
    assert.equal(entries[0].action, "reward.pause");
    assert.equal(entries[0].details.code, "REWARD_PAUSE_FAILED");
});

test("audit listing is bounded", async t => {
    const f = await fixture(t);
    for (let index = 0; index < 12; index += 1) {
        await f.auditLog.record({ actor: "admin", action: "tick", status: "ok", details: { index } });
    }
    assert.equal(f.auditLog.list(5).count, 5);
    assert.equal(f.auditLog.list(5).entries[0].details.index, 11);
    assert.equal(f.auditLog.list(9999).count, 12);
});
