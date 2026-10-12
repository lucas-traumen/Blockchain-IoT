const crypto = require("node:crypto");
const { ApiError, identifier } = require("./eventPayload");

const DEMO_ECONOMICS = Object.freeze({
    stakeHrc: "0.200",
    baseRewardHrc: "0.001",
    rewardHrc: "0.002",
    penaltyHrc: "0.010",
    dailyRewardCapHrc: "0.010",
    minimumDurationS: 30,
    referenceValueVndPerHrc: "10000"
});

const { TASK_EVIDENCE_POLICY } = require('./taskEvidencePolicy');
const { ACTIVITY_POLICY } = require('./activityEvidencePolicy');

function canonical(value) {
    if (Array.isArray(value)) return value.map(canonical);
    if (value && typeof value === "object") return Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])]));
    return value;
}

function sha256(value) {
    return "0x" + crypto.createHash("sha256").update(typeof value === "string" ? value : JSON.stringify(canonical(value))).digest("hex");
}

function safeText(value, name, maxLength) {
    const text = String(value ?? "").trim();
    if (!text || text.length > maxLength) throw new ApiError(400, "INVALID_TASK", `${name} must contain 1-${maxLength} characters`);
    return text;
}

function validateRule(value) {
    const rule = value?.completionRule;
    if (!rule || !["robot_cycle_count", "sensor_activity"].includes(rule.type)) {
        throw new ApiError(400, "INVALID_COMPLETION_RULE", "completionRule must require sensor activity or robot cycles");
    }
    if (rule.type === "robot_cycle_count" && (!Number.isSafeInteger(rule.requiredCycles) || rule.requiredCycles < 1 || rule.requiredCycles > 100000)) {
        throw new ApiError(400, "INVALID_COMPLETION_RULE", "completionRule must require a positive robot cycle count");
    }
    const minimumDurationS = Number(rule.minimumDurationS ?? DEMO_ECONOMICS.minimumDurationS);
    const maximumDurationS = Number(rule.maximumDurationS ?? 180);
    if (!Number.isSafeInteger(minimumDurationS) || minimumDurationS < DEMO_ECONOMICS.minimumDurationS
        || !Number.isSafeInteger(maximumDurationS) || maximumDurationS < minimumDurationS || maximumDurationS > 86400) {
        throw new ApiError(400, "INVALID_COMPLETION_RULE", "duration must be between 30 seconds and 24 hours");
    }
    if (rule.type === "sensor_activity") {
        if (!Number.isSafeInteger(rule.requiredActiveSeconds) || rule.requiredActiveSeconds < 1 || rule.requiredActiveSeconds > maximumDurationS) {
            throw new ApiError(400, "INVALID_COMPLETION_RULE", "active time must be a positive number of seconds within the task duration");
        }
        if (rule.activityPolicy && (Object.keys(rule.activityPolicy).length !== Object.keys(ACTIVITY_POLICY).length
            || Object.entries(ACTIVITY_POLICY).some(([key, value]) => rule.activityPolicy[key] !== value))) {
            throw new ApiError(400, "INVALID_COMPLETION_RULE", "activity policy is not supported");
        }
        return { type: "sensor_activity", requiredActiveSeconds: rule.requiredActiveSeconds,
            activityPolicy: { ...ACTIVITY_POLICY }, minimumDurationS, maximumDurationS };
    }
    return { type: "robot_cycle_count", requiredCycles: rule.requiredCycles, minimumDurationS, maximumDurationS };
}

class TaskService {
    constructor({ store, sessionStore, clock = () => new Date() } = {}) { this.store = store; this.sessionStore = sessionStore; this.clock = clock; }
    now() { return this.clock().toISOString(); }
    get(taskId) {
        const task = this.store.getTask(identifier(taskId, "taskId"));
        if (!task) throw new ApiError(404, "TASK_NOT_FOUND", "Task was not found");
        return task;
    }
    create(actor, input = {}) {
        const now = this.now();
        const taskId = "task-" + crypto.randomUUID();
        const task = {
            schemaVersion: "task-v1", taskId,
            title: safeText(input.title, "title", 120),
            description: safeText(input.description, "description", 2000),
            deviceId: safeText(input.deviceId || "esp32_01", "deviceId", 80),
            zoneId: safeText(input.zoneId || "default-zone", "zoneId", 80),
            completionRule: validateRule(input),
            safetyPolicyVersion: safeText(input.safetyPolicyVersion || "square-bench-v2", "safetyPolicyVersion", 80),
            rewardPolicyVersion: "task-reward-v2",
            evidencePolicy: { ...TASK_EVIDENCE_POLICY },
            economics: { ...DEMO_ECONOMICS, minimumDurationS: validateRule(input).minimumDurationS },
            validFrom: input.validFrom || now,
            validUntil: input.validUntil || new Date(Date.parse(now) + 30 * 86400000).toISOString(),
            status: "DRAFT", createdBy: actor.walletAddress, createdAt: now, updatedAt: now
        };
        if (!Number.isFinite(Date.parse(task.validFrom)) || !Number.isFinite(Date.parse(task.validUntil)) || Date.parse(task.validUntil) <= Date.parse(task.validFrom)) {
            throw new ApiError(400, "INVALID_TASK_WINDOW", "validUntil must be after validFrom");
        }
        task.definitionHash = sha256(task);
        this.store.saveTask(task);
        return { success: true, task };
    }
    update(actor, taskId, input = {}) {
        const previous = this.get(taskId);
        if (previous.status !== "DRAFT") throw new ApiError(409, "TASK_IMMUTABLE", "Only draft tasks can be edited");
        const merged = { ...previous, ...input, completionRule: input.completionRule || previous.completionRule };
        const next = {
            ...previous,
            title: safeText(merged.title, "title", 120),
            description: safeText(merged.description, "description", 2000),
            deviceId: safeText(merged.deviceId, "deviceId", 80),
            zoneId: safeText(merged.zoneId, "zoneId", 80),
            completionRule: validateRule(merged),
            safetyPolicyVersion: safeText(merged.safetyPolicyVersion, "safetyPolicyVersion", 80),
            evidencePolicy: { ...TASK_EVIDENCE_POLICY },
            updatedAt: this.now(),
            updatedBy: actor.walletAddress
        };
        next.economics = { ...DEMO_ECONOMICS, minimumDurationS: next.completionRule.minimumDurationS };
        next.definitionHash = sha256({ ...next, definitionHash: undefined });
        this.store.saveTask(next);
        return { success: true, task: next };
    }
    publish(actor, taskId) {
        const task = this.get(taskId);
        if (task.status !== "DRAFT") throw new ApiError(409, "TASK_STATE_CONFLICT", "Only draft tasks can be published");
        if (Date.parse(task.validUntil) <= Date.parse(this.now())) throw new ApiError(409, "TASK_EXPIRED", "Expired tasks cannot be published");
        const published = { ...task, status: "OPEN", publishedAt: this.now(), publishedBy: actor.walletAddress };
        published.definitionHash = sha256({ ...published, definitionHash: undefined });
        this.store.saveTask(published);
        return { success: true, task: published };
    }
    listOpen(limit = 100) {
        const tasks = this.store.listTasks("OPEN", Math.min(Math.max(Number(limit) || 100, 1), 200))
            .filter(task => Date.parse(task.validUntil) > Date.parse(this.now()));
        return { success: true, tasks };
    }
    accept(user, taskId, idempotencyKey) {
        const task = this.get(taskId);
        if (task.status !== "OPEN" || Date.parse(task.validFrom) > Date.parse(this.now()) || Date.parse(task.validUntil) <= Date.parse(this.now())) {
            throw new ApiError(409, "TASK_NOT_OPEN", "Task is not currently open for acceptance");
        }
        if (idempotencyKey !== undefined && (typeof idempotencyKey !== "string" || !/^[A-Za-z0-9_.:-]{1,100}$/.test(idempotencyKey))) {
            throw new ApiError(400, "INVALID_IDEMPOTENCY_KEY", "Idempotency-Key is invalid");
        }
        const existing = this.store.getAssignmentForUser(task.taskId, user.userId);
        if (existing) return { success: true, duplicate: true, assignment: existing };
        const assignment = this.store.saveAssignment({
            assignmentId: "asg-" + crypto.randomUUID(), taskId: task.taskId, userId: user.userId,
            walletAddress: user.walletAddress, status: "ASSIGNED", taskDefinitionHash: task.definitionHash,
            policySnapshot: { task: task, economics: task.economics, snapshotHash: task.definitionHash },
            idempotencyKey: idempotencyKey || null, createdAt: this.now()
        });
        return { success: true, duplicate: false, assignment };
    }
    assignments(user) { return { success: true, assignments: this.store.listAssignments(user.userId) }; }
    assignmentSnapshot(user, taskId) {
        const task = this.get(taskId);
        const assignment = this.store.getAssignmentForUser(task.taskId, user.userId);
        if (!assignment || !["ASSIGNED", "ACTIVE"].includes(assignment.status)) throw new ApiError(403, "TASK_ASSIGNMENT_REQUIRED", "Accept this task before starting a session");
        if (assignment.taskDefinitionHash !== task.definitionHash) throw new ApiError(409, "TASK_SNAPSHOT_MISMATCH", "Task changed after it was accepted; accept the current version");
        return structuredClone(assignment);
    }
    recordCycle(actor, input = {}) {
        if (!["admin", "operator"].includes(actor?.role)) throw new ApiError(403, "FORBIDDEN", "Only an operator/admin may record completed robot cycles");
        if (!input || typeof input !== "object" || Array.isArray(input)
            || Object.keys(input).some(key => !["sessionId", "taskId", "eventId", "reason"].includes(key))) {
            throw new ApiError(400, "INVALID_CYCLE_EVENT", "Record one completed cycle with taskId, eventId and an optional observation reason");
        }
        const sessionId = identifier(input.sessionId, "sessionId");
        const taskId = identifier(input.taskId, "taskId");
        const eventId = identifier(input.eventId, "eventId");
        const reason = safeText(input.reason ?? "Operator observed one completed robot action", "reason", 1000);
        const payloadHash = sha256({ sessionId, taskId, eventId, actor: actor.walletAddress.toLowerCase(), reason });
        return this.store.transaction(() => {
            const previous = this.store.getCycleEvent(sessionId, eventId);
            if (previous) {
                if (previous.payloadHash !== payloadHash) throw new ApiError(409, "CYCLE_EVENT_CONFLICT", "This eventId was already used for a different observation");
                return { success: true, duplicate: true, event: previous, progress: this.store.getCycleProgress(sessionId),
                    evidence: this.store.getCompletionEvidence(sessionId) };
            }
            const session = this.sessionStore?.get(sessionId);
            if (!session) throw new ApiError(404, "SESSION_NOT_FOUND", "Session was not found");
            if (session.taskId !== taskId || !session.assignmentId) throw new ApiError(409, "SESSION_TASK_MISMATCH", "Session does not belong to the requested task assignment");
            if (session.taskSnapshot?.task?.completionRule?.type === "sensor_activity") {
                throw new ApiError(409, "AUTOMATIC_COMPLETION_REQUIRED", "Activity is calculated from sensor evidence; manual counts and approvals are not accepted");
            }
            if (session.stage !== "RUNNING" || session.endRequested || session.iotSession?.ended_at || session.evidence || session.settlementPayload) {
                throw new ApiError(409, "SESSION_NOT_REVIEWABLE", "Cycles can only be recorded while the session is running, before End");
            }
            if (this.store.getCompletionEvidence(sessionId)) throw new ApiError(409, "COMPLETION_EVIDENCE_CONFLICT", "Completion evidence is already frozen; no more cycles can be added");
            const assignment = this.store.getAssignmentForUser(taskId, session.userId);
            if (!assignment || assignment.assignmentId !== session.assignmentId) throw new ApiError(409, "SESSION_TASK_MISMATCH", "Session assignment does not match the recorded task");
            const requiredCycles = session.taskSnapshot?.task?.completionRule?.requiredCycles ?? assignment.policySnapshot.task.completionRule.requiredCycles;
            const progressBefore = this.store.getCycleProgress(sessionId);
            const event = { schemaVersion: "task-cycle-event-v1", sessionId, taskId, assignmentId: assignment.assignmentId,
                eventId, cycleNumber: progressBefore.recordedCycles + 1, source: "operator_recorded", actor: actor.walletAddress,
                reason, createdAt: this.now(), payloadHash, previousEventHash: progressBefore.lastEventHash };
            event.eventHash = sha256(event);
            this.store.appendCycleEvent(event);
            const progress = this.store.getCycleProgress(sessionId);
            let evidence = null;
            if (progress.recordedCycles >= requiredCycles) {
                evidence = { schemaVersion: "completion-evidence-v1", evidenceId: "cmp-" + crypto.randomUUID(),
                    taskId, sessionId, assignmentId: assignment.assignmentId, userId: assignment.userId,
                    walletAddress: assignment.walletAddress, requiredCycles, observedCycles: progress.recordedCycles, achieved: true,
                    source: "owner_approved", actor: actor.walletAddress, reason: "Required cycles reached through operator-recorded cycle events",
                    createdAt: this.now(), cycleProgress: progress };
                evidence.evidenceHash = sha256(evidence);
                evidence = this.store.saveCompletionEvidence(evidence);
            }
            return { success: true, duplicate: false, event, progress, evidence };
        });
    }
    approveCompletion(actor, input = {}) {
        const sessionId = identifier(input.sessionId, "sessionId");
        const taskId = identifier(input.taskId, "taskId");
        const session = this.sessionStore?.get(sessionId);
        if (!session) throw new ApiError(404, "SESSION_NOT_FOUND", "Session was not found");
        if (session.taskId !== taskId || !session.assignmentId) throw new ApiError(409, "SESSION_TASK_MISMATCH", "Session does not belong to the requested task assignment");
        if (session.taskSnapshot?.task?.completionRule?.type === "sensor_activity") {
            throw new ApiError(409, "AUTOMATIC_COMPLETION_REQUIRED", "Activity is calculated from sensor evidence; manual counts and approvals are not accepted");
        }
        if (session.stage !== "RUNNING" || session.endRequested || session.iotSession?.ended_at || session.settlementPayload || session.evidence) {
            throw new ApiError(409, "SESSION_NOT_REVIEWABLE", "Completion must be approved before the sensor session ends and evidence is frozen");
        }
        const assignment = this.store.getAssignmentForUser(taskId, session.userId);
        if (!assignment) throw new ApiError(404, "ASSIGNMENT_NOT_FOUND", "Task assignment was not found");
        const reason = safeText(input.reason, "reason", 1000);
        const observedCycles = Number(input.observedCycles);
        if (!Number.isSafeInteger(observedCycles) || observedCycles < 0) throw new ApiError(400, "INVALID_COMPLETION_EVIDENCE", "observedCycles must be a non-negative integer");
        const cycleProgress = this.store.getCycleProgress?.(sessionId) || null;
        if (cycleProgress?.recordedCycles > 0 && observedCycles !== cycleProgress.recordedCycles) {
            throw new ApiError(409, "CYCLE_COUNT_MISMATCH", "Final approval must use the durable recorded cycle count");
        }
        const evidence = {
            schemaVersion: "completion-evidence-v1", evidenceId: "cmp-" + crypto.randomUUID(),
            taskId, sessionId, assignmentId: assignment.assignmentId, userId: assignment.userId,
            walletAddress: assignment.walletAddress, requiredCycles: assignment.policySnapshot.task.completionRule.requiredCycles,
            observedCycles, achieved: observedCycles >= assignment.policySnapshot.task.completionRule.requiredCycles,
            source: "owner_approved", actor: actor.walletAddress, reason, createdAt: this.now(), cycleProgress
        };
        evidence.evidenceHash = sha256(evidence);
        const stored = this.store.saveCompletionEvidence(evidence);
        const duplicate = stored.evidenceId !== evidence.evidenceId;
        if (duplicate && (stored.taskId !== evidence.taskId || stored.assignmentId !== evidence.assignmentId
            || stored.observedCycles !== evidence.observedCycles || stored.reason !== evidence.reason
            || stored.actor !== evidence.actor)) {
            throw new ApiError(409, "COMPLETION_EVIDENCE_CONFLICT", "Completion evidence was already frozen with different values");
        }
        return { success: true, duplicate, evidence: stored };
    }
}

module.exports = { TaskService, DEMO_ECONOMICS, sha256 };
