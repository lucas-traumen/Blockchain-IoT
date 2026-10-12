const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { TaskService } = require("../taskService");
const { SqliteCustomerStore } = require("../sqliteStores");

function setup(t) {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "hrc-task-domain-"));
    const store = new SqliteCustomerStore(path.join(directory, "app.db"));
    const sessions = new Map();
    const service = new TaskService({ store, sessionStore: { get: id => sessions.get(id) || null } });
    t.after(() => { store.close(); fs.rmSync(directory, { recursive: true, force: true }); });
    return { store, service, sessions };
}

test("task publication freezes deterministic economics and acceptance is idempotent", t => {
    const { service } = setup(t);
    const admin = { userId: "owner", walletAddress: "0xowner" };
    const user = { userId: "user-1", walletAddress: "0xuser" };
    const created = service.create(admin, { title: "Run robot cycles", description: "Safe test", completionRule: { type: "robot_cycle_count", requiredCycles: 8 } }).task;
    assert.equal(created.status, "DRAFT");
    assert.equal(created.economics.stakeHrc, "0.200");
    assert.equal(created.economics.baseRewardHrc, "0.001");
    assert.equal(created.economics.rewardHrc, "0.002");
    assert.equal(created.completionRule.minimumDurationS, 30);
    assert.equal(created.completionRule.maximumDurationS, 180);
    assert.equal(created.evidencePolicy.sampleRateHz, 10);
    assert.equal(created.evidencePolicy.minimumSampleCoverage, 0.9);
    assert.equal(created.evidencePolicy.minimumProcessedCoverage, 0.95);
    service.update(admin, created.taskId, { evidencePolicy: { minimumSampleCoverage: 0.01 } });
    const published = service.publish(admin, created.taskId).task;
    assert.equal(published.evidencePolicy.minimumSampleCoverage, 0.9);
    assert.equal(service.listOpen().tasks[0].definitionHash, published.definitionHash);
    assert.throws(() => service.update(admin, created.taskId, { title: "mutated" }), { code: "TASK_IMMUTABLE" });
    const first = service.accept(user, created.taskId, "accept-1");
    const again = service.accept(user, created.taskId, "accept-1");
    assert.equal(first.assignment.assignmentId, again.assignment.assignmentId);
    assert.equal(again.duplicate, true);
    assert.equal(first.assignment.policySnapshot.snapshotHash, published.definitionHash);
    assert.deepEqual(first.assignment.policySnapshot.task.evidencePolicy, published.evidencePolicy);
});

test("owner completion evidence is bound to an existing assigned session and frozen once", t => {
    const { service, sessions } = setup(t);
    const owner = { userId: "owner", walletAddress: "0xowner" };
    const user = { userId: "user-1", walletAddress: "0xuser" };
    const task = service.publish(owner, service.create(owner, { title: "Cycle task", description: "Task", completionRule: { type: "robot_cycle_count", requiredCycles: 4 } }).task.taskId).task;
    const assignment = service.accept(user, task.taskId).assignment;
    sessions.set("session-1", { sessionId: "session-1", taskId: task.taskId, assignmentId: assignment.assignmentId,
        userId: user.userId, stage: "RUNNING" });
    const input = { taskId: task.taskId, sessionId: "session-1", observedCycles: 4, reason: "Controller counter verified" };
    const first = service.approveCompletion(owner, input);
    const retry = service.approveCompletion(owner, input);
    assert.equal(first.evidence.achieved, true);
    assert.equal(first.evidence.source, "owner_approved");
    assert.match(first.evidence.evidenceHash, /^0x[0-9a-f]{64}$/);
    assert.equal(retry.duplicate, true);
    assert.equal(retry.evidence.evidenceHash, first.evidence.evidenceHash);
    assert.throws(() => service.approveCompletion(owner, { ...input, observedCycles: 5 }), { code: "COMPLETION_EVIDENCE_CONFLICT" });
    assert.throws(() => service.approveCompletion(owner, { ...input, taskId: "another-task" }), { code: "SESSION_TASK_MISMATCH" });
    sessions.set("session-1", { ...sessions.get("session-1"), stage: "SETTLEMENT_PENDING", evidence: { frozen: true } });
    assert.throws(() => service.approveCompletion(owner, input), { code: "SESSION_NOT_REVIEWABLE" });
});

test("unsafe duration rules and missing completion reasons fail closed", t => {
    const { service } = setup(t);
    const owner = { userId: "owner", walletAddress: "0xowner" };
    assert.throws(() => service.create(owner, { title: "x", description: "y", completionRule: { type: "robot_cycle_count", requiredCycles: 2, minimumDurationS: 29 } }),
        { code: "INVALID_COMPLETION_RULE" });
});

function cycleFixture(t, requiredCycles = 3) {
    const f = setup(t);
    const operator = { role: "operator", userId: "operator", walletAddress: "0xoperator" };
    const user = { role: "customer", userId: "cycle-user", walletAddress: "0xuser" };
    const task = f.service.publish(operator, f.service.create(operator, { title: "Observed actions", description: "Count full robot actions",
        completionRule: { type: "robot_cycle_count", requiredCycles } }).task.taskId).task;
    const assignment = f.service.accept(user, task.taskId).assignment;
    const session = { sessionId: "cycle-session", taskId: task.taskId, assignmentId: assignment.assignmentId,
        userId: user.userId, stage: "RUNNING", taskSnapshot: assignment.policySnapshot };
    f.sessions.set(session.sessionId, session);
    const input = { sessionId: session.sessionId, taskId: task.taskId, eventId: "cycle-1" };
    return { ...f, operator, user, task, assignment, session, input };
}

test("cycle observations persist progress and auto-approve only at the target with idempotent retries", t => {
    const f = cycleFixture(t);
    assert.equal(f.store.getCycleProgress(f.session.sessionId).recordedCycles, 0);
    const one = f.service.recordCycle(f.operator, f.input);
    assert.equal(one.progress.recordedCycles, 1);
    assert.equal(one.evidence, null);
    assert.equal(f.service.recordCycle(f.operator, f.input).duplicate, true);
    assert.equal(f.store.getCycleProgress(f.session.sessionId).recordedCycles, 1);
    const two = f.service.recordCycle(f.operator, { ...f.input, eventId: "cycle-2" });
    assert.equal(two.evidence, null);
    assert.equal(two.event.previousEventHash, one.event.eventHash);
    const three = f.service.recordCycle(f.operator, { ...f.input, eventId: "cycle-3" });
    assert.equal(three.progress.recordedCycles, 3);
    assert.equal(three.evidence.achieved, true);
    assert.equal(three.evidence.observedCycles, 3);
    assert.equal(three.evidence.cycleProgress.lastEventHash, three.event.eventHash);
    assert.throws(() => f.service.recordCycle(f.operator, { ...f.input, eventId: "cycle-4" }), { code: "COMPLETION_EVIDENCE_CONFLICT" });
    const file = f.store.filePath;
    f.store.close();
    const reopened = new SqliteCustomerStore(file);
    t.after(() => reopened.close());
    f.service.store = reopened;
    const replay = f.service.recordCycle(f.operator, { ...f.input, eventId: "cycle-3" });
    assert.equal(replay.duplicate, true);
    assert.equal(replay.progress.recordedCycles, 3);
    assert.equal(replay.evidence.evidenceHash, three.evidence.evidenceHash);
    f.sessions.set(f.session.sessionId, { ...f.session, stage: "SETTLED", evidence: { completionEvidence: three.evidence } });
    assert.equal(f.service.recordCycle(f.operator, f.input).duplicate, true, "a lost response can be reconciled after End without adding a cycle");
    assert.throws(() => f.service.recordCycle(f.operator, { ...f.input, eventId: "new-after-end" }), { code: "SESSION_NOT_REVIEWABLE" });
});

test("cycle events reject forged totals, changed idempotency payloads, wrong tasks and frozen sessions", t => {
    const f = cycleFixture(t);
    assert.throws(() => f.service.recordCycle(f.user, f.input), { code: "FORBIDDEN" });
    assert.throws(() => f.service.recordCycle(f.operator, { ...f.input, observedCycles: 100 }), { code: "INVALID_CYCLE_EVENT" });
    assert.throws(() => f.service.recordCycle(f.operator, { ...f.input, taskId: "wrong-task" }), { code: "SESSION_TASK_MISMATCH" });
    assert.throws(() => f.service.recordCycle(f.operator, { ...f.input, eventId: "" }), { code: "INVALID_PAYLOAD" });
    f.service.recordCycle(f.operator, f.input);
    assert.throws(() => f.service.recordCycle(f.operator, { ...f.input, reason: "changed" }), { code: "CYCLE_EVENT_CONFLICT" });
    assert.throws(() => f.service.recordCycle({ ...f.operator, walletAddress: "0xother" }, f.input), { code: "CYCLE_EVENT_CONFLICT" });
    for (const fields of [{ endRequested: true }, { iotSession: { ended_at: "2026-10-13T00:00:00Z" } },
        { stage: "STOPPING_SENSOR" }, { evidence: {} }, { settlementPayload: {} }]) {
        f.sessions.set(f.session.sessionId, { ...f.session, ...fields });
        assert.throws(() => f.service.recordCycle(f.operator, { ...f.input, eventId: "later" }), { code: "SESSION_NOT_REVIEWABLE" });
    }
    assert.equal(f.store.getCycleProgress(f.session.sessionId).recordedCycles, 1);
});

test("failed final completion write rolls back the cycle so retry can approve it once", t => {
    const f = cycleFixture(t, 1);
    const save = f.store.saveCompletionEvidence.bind(f.store);
    f.store.saveCompletionEvidence = () => { throw new Error("storage failure"); };
    assert.throws(() => f.service.recordCycle(f.operator, f.input), /storage failure/);
    assert.equal(f.store.getCycleProgress(f.session.sessionId).recordedCycles, 0);
    assert.equal(f.store.getCycleEvent(f.session.sessionId, f.input.eventId), null);
    f.store.saveCompletionEvidence = save;
    const result = f.service.recordCycle(f.operator, f.input);
    assert.equal(result.evidence.achieved, true);
    assert.equal(result.progress.recordedCycles, 1);
});


test("final manual approval preserves the recorded count and cannot bypass the remaining cycles", t => {
    const f = cycleFixture(t, 3);
    f.service.recordCycle(f.operator, f.input);
    const input = { sessionId: f.session.sessionId, taskId: f.task.taskId, observedCycles: 3, reason: "Observed total" };
    assert.throws(() => f.service.approveCompletion(f.operator, input), { code: "CYCLE_COUNT_MISMATCH" });
    assert.equal(f.store.getCompletionEvidence(f.session.sessionId), null);
    const result = f.service.approveCompletion(f.operator, { ...input, observedCycles: 1 });
    assert.equal(result.evidence.achieved, false);
    assert.equal(result.evidence.cycleProgress.recordedCycles, 1);
    assert.equal(f.service.approveCompletion(f.operator, { ...input, observedCycles: 1 }).duplicate, true);
});

test('activity rules freeze a sensor policy and forbid manual cycle or completion overrides', t => {
    const f = setup(t);
    const operator = { role: 'operator', userId: 'operator', walletAddress: '0xoperator' };
    const user = { userId: 'movement-user', walletAddress: '0xuser' };
    const task = f.service.publish(operator, f.service.create(operator, { title: 'Active movement', description: '20 active seconds',
        completionRule: { type: 'sensor_activity', requiredActiveSeconds: 20, maximumDurationS: 50 } }).task.taskId).task;
    const assignment = f.service.accept(user, task.taskId).assignment;
    assert.deepEqual(assignment.policySnapshot.task.completionRule.activityPolicy, require('../activityEvidencePolicy').ACTIVITY_POLICY);
    assert.equal(assignment.policySnapshot.task.completionRule.requiredCycles, undefined);
    f.sessions.set('activity-session', { sessionId: 'activity-session', taskId: task.taskId,
        assignmentId: assignment.assignmentId, userId: user.userId, stage: 'RUNNING', taskSnapshot: assignment.policySnapshot });
    const input = { sessionId: 'activity-session', taskId: task.taskId };
    assert.throws(() => f.service.recordCycle(operator, { ...input, eventId: 'manual-1' }), { code: 'AUTOMATIC_COMPLETION_REQUIRED' });
    assert.throws(() => f.service.approveCompletion(operator, { ...input, observedCycles: 100, reason: 'manual bypass' }), { code: 'AUTOMATIC_COMPLETION_REQUIRED' });
    assert.equal(f.store.getCycleProgress(input.sessionId).recordedCycles, 0);
    assert.equal(f.store.getCompletionEvidence(input.sessionId), null);
});
test('invalid activity targets and weaker movement policy fail at task creation', t => {
    const { service } = setup(t);
    for (const rule of [
        { requiredActiveSeconds: 0 }, { requiredActiveSeconds: 20.5 }, { requiredActiveSeconds: 51, maximumDurationS: 50 },
        { requiredActiveSeconds: 20, activityPolicy: { ...require('../activityEvidencePolicy').ACTIVITY_POLICY, displacementMm: 1 } }
    ]) assert.throws(() => service.create({ walletAddress: '0xowner' }, { title: 'x', description: 'y',
        completionRule: { type: 'sensor_activity', ...rule } }), { code: 'INVALID_COMPLETION_RULE' });
});
