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
