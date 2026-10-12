const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { ethers } = require("ethers");
const { createApp } = require("../server");
const { CustomerService } = require("../customerService");
const { SqliteCustomerStore } = require("../sqliteStores");

async function fixture(t) {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "hrc-task-api-"));
    const store = new SqliteCustomerStore(path.join(directory, "app.db"));
    const owner = ethers.Wallet.createRandom();
    const user = ethers.Wallet.createRandom();
    const customerService = new CustomerService({ store, chainId: 31337, adminWallets: [owner.address], appOrigin: "http://localhost:8081" });
    const walletService = {
        store: { list: () => [{ sessionId: "active-session", taskId: "task-1", assignmentId: "asg-1", userId: "usr_owner",
            stage: "RUNNING", taskSnapshot: { task: { taskId: "task-1", title: "Cycles", completionRule: { requiredCycles: 5 } } } }] },
        walletUser(address) { return { userId: "usr_" + address.toLowerCase(), walletAddress: address, role: "customer" }; },
        loginWithWallet(address) { return { user: this.walletUser(address) }; }
    };
    const app = createApp({ service: {}, customerService, walletService, tokens: { HRC_APP_AUTH_MODE: "siwe", HRC_CORS_ORIGINS: "http://localhost:8081" } });
    const server = app.listen(0, "127.0.0.1");
    await new Promise((resolve, reject) => { server.once("listening", resolve); server.once("error", reject); });
    t.after(() => { server.close(); store.close(); fs.rmSync(directory, { recursive: true, force: true }); });
    const call = async (route, { method = "GET", body, token, headers = {} } = {}) => {
        const response = await fetch(`http://127.0.0.1:${server.address().port}${route}`, {
            method, headers: { ...(body === undefined ? {} : { "content-type": "application/json" }), ...headers, ...(token ? { authorization: `Bearer ${token}` } : {}) },
            body: body === undefined ? undefined : JSON.stringify(body)
        });
        return { status: response.status, body: await response.json() };
    };
    const login = async wallet => {
        const challenge = await call("/api/auth/challenge", { method: "POST", body: { walletAddress: wallet.address, chainId: 31337 } });
        const signature = await wallet.signMessage(challenge.body.message);
        const result = await call("/api/auth/wallet", { method: "POST", body: { walletAddress: wallet.address, chainId: 31337, message: challenge.body.message, signature } });
        return result.body.token;
    };
    return { owner, user, call, login, store };
}

test("task routes enforce SIWE roles and preserve idempotent assignments with audit", async t => {
    const f = await fixture(t);
    const ownerToken = await f.login(f.owner);
    const userToken = await f.login(f.user);
    assert.equal((await f.call("/api/admin/task-sessions", { token: userToken })).status, 403);
    const payload = { title: "Safe cycles", description: "Complete robot cycles in zone A", deviceId: "esp32_01", zoneId: "zone-a",
        completionRule: { type: "robot_cycle_count", requiredCycles: 5, minimumDurationS: 120, maximumDurationS: 600 } };
    assert.equal((await f.call("/api/tasks", { method: "POST", body: payload, token: userToken })).status, 403);
    const created = await f.call("/api/tasks", { method: "POST", body: payload, token: ownerToken });
    assert.equal(created.status, 200);
    const taskId = created.body.task.taskId;
    assert.equal((await f.call(`/api/tasks/${taskId}/publish`, { method: "POST", body: {}, token: ownerToken })).status, 200);
    assert.equal((await f.call(`/api/tasks/${taskId}/accept`, { method: "POST", body: {}, token: userToken, headers: { "Idempotency-Key": "accept-task-1" } })).body.duplicate, false);
    const retry = await f.call(`/api/tasks/${taskId}/accept`, { method: "POST", body: {}, token: userToken, headers: { "Idempotency-Key": "accept-task-1" } });
    assert.equal(retry.status, 200);
    assert.equal(retry.body.duplicate, true);
    assert.equal((await f.call("/api/me/assignments", { token: userToken })).body.assignments.length, 1);
    const adminSessions = await f.call("/api/admin/task-sessions", { token: ownerToken });
    assert.equal(adminSessions.status, 200);
    assert.equal(adminSessions.body.sessions[0].sessionId, "active-session");
    const audit = f.store.listAudit(10).map(row => row.action);
    assert.deepEqual(audit, ["task.publish", "task.create"]);
});
