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
    const operator = ethers.Wallet.createRandom();
    const customerService = new CustomerService({ store, chainId: 31337, adminWallets: [owner.address], operatorWallets: [operator.address], appOrigin: "http://localhost:8081" });
    const walletService = {
        store: { list: () => [{ sessionId: "active-session", taskId: "task-1", assignmentId: "asg-1", userId: "usr_owner",
            stage: "RUNNING", taskSnapshot: { task: { taskId: "task-1", title: "Cycles", completionRule: { requiredCycles: 5 } } } }] },
        walletUser(address) { return { userId: "usr_" + address.toLowerCase(), walletAddress: address, role: "customer" }; },
        loginWithWallet(address) { return { user: this.walletUser(address) }; }
    };
    const records = walletService.store.list();
    walletService.store.list = () => records;
    walletService.store.get = id => records.find(row => row.sessionId === id) || null;
    walletService.getSession = async (userId, sessionId) => ({ success: true,
        session: { ...records.find(row => row.userId === userId && row.sessionId === sessionId) } });
    const liveService = { completionStore: store, taskCompletion: require('../liveSessionService').LiveSessionService.prototype.taskCompletion };
    const app = createApp({ service: {}, customerService, walletService, liveService, tokens: { HRC_APP_AUTH_MODE: "siwe", HRC_CORS_ORIGINS: "http://localhost:8081" } });
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
    return { owner, user, operator, call, login, store, records };
}

test("task routes enforce SIWE roles and preserve idempotent assignments with audit", async t => {
    const f = await fixture(t);
    const ownerToken = await f.login(f.owner);
    const userToken = await f.login(f.user);
    assert.equal((await f.call("/api/admin/task-sessions", { token: userToken })).status, 403);
    const payload = { title: "Safe cycles", description: "Complete robot cycles in zone A", deviceId: "esp32_01", zoneId: "zone-a",
        completionRule: { type: "robot_cycle_count", requiredCycles: 5, minimumDurationS: 30, maximumDurationS: 180 } };
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


test('HRC report route includes settled refunds and running sessions only for admin/operator roles', async t => {
    const f = await fixture(t);
    f.records.push({ sessionId: 'refunded-session', userId: 'usr_customer', stage: 'SETTLED', settled: true,
        outcomeCode: 3, stakeReturned: '0.2', rewardPaid: '0', penaltyCollected: '0',
        outcomeReason: 'task_completion_evidence_missing', evidence: { completionEvidence: null } });
    const ownerToken = await f.login(f.owner);
    const userToken = await f.login(f.user);
    assert.equal((await f.call('/api/admin/wallet/sessions')).status, 401);
    assert.equal((await f.call('/api/admin/wallet/sessions', { token: userToken })).status, 403);
    const reports = await f.call('/api/admin/wallet/sessions', { token: ownerToken });
    assert.equal(reports.status, 200);
    assert.equal(reports.body.count, 2);
    const refund = reports.body.sessions.find(row => row.sessionId === 'refunded-session');
    assert.equal(refund.rewardPaid, '0');
    assert.equal(refund.stakeReturned, '0.2');
    assert.equal(refund.taskCompletion, null);
    assert.equal(refund.outcomeReason, 'task_completion_evidence_missing');
    assert.equal(f.store.listAudit(100).length, 0, 'report reads must not create approvals or settlements');
    const operatorToken = await f.login(f.operator);
    assert.equal((await f.call('/api/admin/wallet/sessions', { token: operatorToken })).status, 200);
});


test('cycle HTTP recording requires operator authority, returns progress and freezes completion at the target', async t => {
    const f = await fixture(t);
    const ownerToken = await f.login(f.owner);
    const operatorToken = await f.login(f.operator);
    const userToken = await f.login(f.user);
    const created = await f.call('/api/tasks', { method: 'POST', token: ownerToken, body: {
        title: 'Two actions', description: 'Observe completed robot actions', completionRule: { type: 'robot_cycle_count', requiredCycles: 2 } } });
    const taskId = created.body.task.taskId;
    await f.call(`/api/tasks/${taskId}/publish`, { method: 'POST', token: ownerToken, body: {} });
    const accepted = await f.call(`/api/tasks/${taskId}/accept`, { method: 'POST', token: userToken, body: {} });
    const assignment = accepted.body.assignment;
    const session = { sessionId: 'two-action-session', userId: assignment.userId, taskId, assignmentId: assignment.assignmentId,
        taskSnapshot: assignment.policySnapshot, stage: 'RUNNING' };
    f.records.push(session);
    const route = '/api/sessions/two-action-session/cycles';
    const body = { taskId, eventId: 'observed-1' };
    assert.equal((await f.call(route, { method: 'POST', body })).status, 401);
    assert.equal((await f.call(route, { method: 'POST', body, token: userToken })).status, 403);
    assert.equal(f.store.getCycleProgress(session.sessionId).recordedCycles, 0);
    const one = await f.call(route, { method: 'POST', body, token: operatorToken });
    assert.equal(one.status, 200);
    assert.equal(one.body.progress.recordedCycles, 1);
    assert.equal(one.body.evidence, null);
    assert.equal((await f.call(route, { method: 'POST', body, token: operatorToken })).body.duplicate, true);
    const adminRows = await f.call('/api/admin/task-sessions', { token: ownerToken });
    const current = adminRows.body.sessions.find(row => row.sessionId === session.sessionId);
    assert.equal(current.taskCompletion.status, 'TRACKING');
    assert.equal(current.taskCompletion.remainingCycles, 1);
    assert.equal((await f.call(route, { method: 'POST', body: { ...body, observedCycles: 200 }, token: ownerToken })).status, 400);
    const two = await f.call(route, { method: 'POST', body: { ...body, eventId: 'observed-2' }, token: ownerToken });
    assert.equal(two.body.evidence.achieved, true);
    assert.equal(two.body.evidence.observedCycles, 2);
    const reports = await f.call('/api/admin/wallet/sessions', { token: ownerToken });
    assert.equal(reports.body.sessions.find(row => row.sessionId === session.sessionId).taskCompletion.status, 'APPROVED');
    session.stage = 'STOPPING_SENSOR';
    assert.equal((await f.call(route, { method: 'POST', body: { ...body, eventId: 'observed-3' }, token: operatorToken })).status, 409);
    assert.equal(f.store.getCycleProgress(session.sessionId).recordedCycles, 2);
    assert.ok(f.store.listAudit(100).some(entry => entry.action === 'session.cycle.record' && entry.actor === f.operator.address.toLowerCase()));
});

test('HTTP activity tasks store the released rule and reject all manual completion routes', async t => {
    const f = await fixture(t);
    const ownerToken = await f.login(f.owner), operatorToken = await f.login(f.operator), userToken = await f.login(f.user);
    const created = await f.call('/api/tasks', { method: 'POST', token: ownerToken, body: {
        title: 'User movement', description: '20 seconds of measured activity', completionRule: { type: 'sensor_activity', requiredActiveSeconds: 20 } } });
    assert.equal(created.status, 200);
    const taskId = created.body.task.taskId;
    assert.equal(created.body.task.completionRule.activityPolicy.version, 'distance-activity-v1');
    await f.call(`/api/tasks/${taskId}/publish`, { method: 'POST', token: ownerToken, body: {} });
    const accepted = await f.call(`/api/tasks/${taskId}/accept`, { method: 'POST', token: userToken, body: {} });
    const assignment = accepted.body.assignment;
    f.records.push({ sessionId: 'auto-activity-session', taskId, assignmentId: assignment.assignmentId, userId: assignment.userId,
        stage: 'RUNNING', taskSnapshot: assignment.policySnapshot });
    for (const [suffix, fields] of [['cycles', { eventId: 'manual-1' }], ['completion-evidence', { observedCycles: 100, reason: 'override' }]]) {
        assert.equal((await f.call(`/api/sessions/auto-activity-session/${suffix}`, { method: 'POST', token: userToken,
            body: { taskId, ...fields } })).status, 403);
        const rejected = await f.call(`/api/sessions/auto-activity-session/${suffix}`, { method: 'POST', token: operatorToken,
            body: { taskId, ...fields } });
        assert.equal(rejected.status, 409); assert.equal(rejected.body.code, 'AUTOMATIC_COMPLETION_REQUIRED');
    }
    assert.equal(f.store.getCompletionEvidence('auto-activity-session'), null);
    assert.equal(f.store.getCycleProgress('auto-activity-session').recordedCycles, 0);
});
