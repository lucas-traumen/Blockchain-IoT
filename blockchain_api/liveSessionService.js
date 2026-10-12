const crypto = require('node:crypto');
const { ethers } = require('ethers');
const { ApiError, identifier, canonicalJson } = require('./eventPayload');
const { normalizeOutcome, sessionKey } = require('./rewardPolicy');
const { IotSessionClient } = require('./iotSessionClient');
const { validateTaskEvidence, validateTaskOutcome } = require('./taskEvidencePolicy');
const { validateActivityEvidence } = require('./activityEvidencePolicy');

const FINAL = new Set(['SETTLED', 'START_FAILED', 'INTENT_EXPIRED']);
const TERMINAL_ERRORS = new Set(['SETTLEMENT_CONFLICT', 'STORE_INTEGRITY_ERROR', 'BENEFICIARY_MISMATCH', 'TRANSACTION_REVERTED', 'IOT_SESSION_MISMATCH']);
const NO_STAKE_ERRORS = new Set(['APPROVAL_REQUIRED', 'INSUFFICIENT_HRC_BALANCE', 'TRANSACTION_PREPARE_FAILED', 'TRANSACTION_REVERTED', 'SENSOR_UNAVAILABLE']);
const WALLET_INTENT_TTL_MS = 120000;
const ISO_TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;

function unixSecondsFromIso(value) {
    if (typeof value !== 'string' || !ISO_TIMESTAMP.test(value)) {
        throw new ApiError(409, 'SESSION_END_TIME_INVALID', 'Closed sensor evidence must include an ISO-8601 ended_at timestamp');
    }
    const milliseconds = Date.parse(value);
    if (!Number.isFinite(milliseconds)) {
        throw new ApiError(409, 'SESSION_END_TIME_INVALID', 'Closed sensor ended_at timestamp is invalid');
    }
    return Math.floor(milliseconds / 1000);
}

class LiveSessionService {
    constructor({ walletService, iot, settler, completionStore = null, deviceId = 'esp32_01', clock = Date.now, logger = message => console.log(message) }) {
        Object.assign(this, { walletService, iot, settler, completionStore, deviceId, clock, logger });
        this.store = walletService.store;
        this.tail = Promise.resolve(); this.timer = null; this.deviceRead = null; this.deviceCache = null; this.deviceVersion = 0;
        // All operator routes use the same durable transaction store once the live bridge is loaded.
        if (walletService.transactionSender) settler.transactionSender ||= walletService.transactionSender;
    }
    serial(work) { const task = this.tail.catch(() => {}).then(work); this.tail = task.catch(() => {}); return task; }
    log(event, record, extra = {}) { this.logger(JSON.stringify({ event, sessionId: record.sessionId, userId: record.userId, stage: record.stage, ...extra })); }
    setAssignmentStage(record, status, fields = {}) {
        if (!record.assignmentId) return;
        const store = this.walletService.identityStore;
        const assignment = store?.getAssignment?.(record.assignmentId);
        if (assignment) store.updateAssignment(record.assignmentId, { status, sessionId: record.sessionId, ...fields });
    }
    owner(userId, id) {
        this.walletService._user(userId);
        identifier(id, 'sessionId');
        const record = this.store.get(id);
        if (!record) {
            const legacyOwner = this.walletService.legacySessionOwner(id);
            if (legacyOwner && legacyOwner !== userId) throw new ApiError(403, 'SESSION_OWNER_MISMATCH', 'This session belongs to another user');
            throw new ApiError(404, 'SESSION_NOT_FOUND', 'Session reference was not found');
        }
        if (record.userId !== userId) throw new ApiError(403, 'SESSION_OWNER_MISMATCH', 'This session belongs to another user');
        return record;
    }
    validateBody(body, allowed = []) {
        if (!body || typeof body !== 'object' || Array.isArray(body) || Object.keys(body).some(key => !allowed.includes(key))) {
            throw new ApiError(400, 'INVALID_PAYLOAD', 'Only the session reference is accepted; outcome, beneficiary and amounts are calculated on the backend');
        }
    }
    async checkOperator(userId) {
        await this.walletService._requireConfiguredChain(userId);
        const address = await this.settler.contract.getAddress();
        if (address.toLowerCase() !== this.walletService.config.rewardAddress.toLowerCase()
            || !await this.settler.contract.operators(await this.settler.wallet.getAddress())) {
            throw new ApiError(503, 'OPERATOR_NOT_CONFIGURED', 'The backend operator is not configured for this reward contract');
        }
    }
    reserveWalletStart(userId, sessionId, assignment = null) {
        return this.serial(async () => {
            const user = this.walletService._user(userId);
            if (!user.walletFirst) throw new ApiError(400, 'NOT_WALLET_FIRST', 'Wallet intent is only for wallet-first users');
            const id = identifier(sessionId, 'sessionId');
            if (id.length > 63) throw new ApiError(400, 'INVALID_SESSION_REF', 'Sensor session identifier must fit within 63 characters');
            const existing = this.store.get(id);
            if (existing) {
                if (existing.userId !== userId) throw new ApiError(403, 'SESSION_OWNER_MISMATCH', 'This session belongs to another user');
                if (existing.stage === 'WALLET_INTENT' && existing.intentExpiresAt > this.clock()) return existing;
                if (existing.stage === 'WALLET_INTENT') throw new ApiError(409, 'SESSION_INTENT_EXPIRED', 'This session intent expired; prepare a new session reference');
                if (existing.transactionHash || ['RUNNING', 'SETTLED', 'START_FAILED'].includes(existing.stage)) return existing;
                throw new ApiError(409, 'SESSION_INTENT_EXPIRED', 'Prepare a new session reference');
            }

            if (this.walletService.config.rewardProtocolVersion === 'task-rewards-v2') {
                if (!assignment || assignment.userId !== userId
                    || assignment.walletAddress?.toLowerCase() !== user.walletAddress.toLowerCase()) {
                    throw new ApiError(403, 'TASK_ASSIGNMENT_REQUIRED', 'A task assignment owned by this wallet is required');
                }
                if (assignment.policySnapshot?.task?.deviceId !== this.deviceId) {
                    throw new ApiError(409, 'TASK_DEVICE_MISMATCH', 'The assigned task does not use the configured sensor');
                }
            }
            await this.checkOperator(userId);
            const [wallet, device] = await Promise.all([
                this.walletService.getWallet(userId), this.deviceState({ fresh: true })
            ]);
            if (!device.ready) throw new ApiError(503, 'SENSOR_UNAVAILABLE', device.reason);
            if (wallet.activeSessionCount) throw new ApiError(409, 'SESSION_IN_PROGRESS', 'Your wallet already has a staked session');
            const lease = this.store.list().find(row => {
                if (row.sessionId === id || FINAL.has(row.stage)) return false;
                if (row.stage === 'WALLET_INTENT') return row.intentExpiresAt > this.clock();
                return Boolean(row.source === 'esp32' && row.stage);
            });
            if (device.busySessionId || lease) throw new ApiError(409, 'DEVICE_BUSY', 'The sensor is reserved by another session');
            if (Number(wallet.balance) < Number(wallet.stakeAmount)) throw new ApiError(409, 'INSUFFICIENT_HRC_BALANCE', 'Not enough HRC for the stake');
            if (Number(wallet.allowance) < Number(wallet.requiredAllowance)) throw new ApiError(409, 'APPROVAL_REQUIRED', 'Approve the stake before starting');

            const network = await this.walletService.provider.getNetwork();
            if (Number(network.chainId) !== Number(wallet.chainId)) throw new ApiError(503, 'WRONG_CHAIN', 'Reward wallet and configured chain do not match');
            const gasBalance = await this.walletService.provider.getBalance(user.walletAddress);
            if (gasBalance <= 0n) throw new ApiError(409, 'INSUFFICIENT_GAS', 'Wallet needs native gas currency before starting');
            const policySnapshot = {};
            for (const [key, method] of Object.entries({
                stakeAmount: 'stakeAmount', rewardAmount: 'rewardAmount', penaltyAmount: 'penaltyAmount',
                minRewardDuration: 'MIN_REWARD_DURATION', fullBonusDuration: 'FULL_BONUS_DURATION',
                maxRewardBonusPercent: 'MAX_REWARD_BONUS_PERCENT'
            })) {
                if (typeof this.walletService.rewards[method] === 'function') {
                    policySnapshot[key] = String(await this.walletService.rewards[method]());
                }
            }
            if (this.walletService.config.rewardProtocolVersion === 'task-rewards-v2') {
                policySnapshot.stakeAmount = String(await this.walletService.rewards.STAKE_AMOUNT());
                policySnapshot.rewardAmount = String(await this.walletService.rewards.SAFE_REWARD());
                if (typeof this.walletService.rewards.BASE_REWARD === 'function') {
                    policySnapshot.baseReward = String(await this.walletService.rewards.BASE_REWARD());
                }
                policySnapshot.penaltyAmount = String(await this.walletService.rewards.VIOLATION_PENALTY());
                policySnapshot.dailyRewardCap = String(await this.walletService.rewards.DAILY_REWARD_CAP());
                policySnapshot.minimumSafeDuration = String(await this.walletService.rewards.MIN_SAFE_DURATION());
                policySnapshot.fullRewardDuration = String(assignment.policySnapshot.task.completionRule.maximumDurationS);
            }
            const now = this.clock();
            return this.store.save({
                sessionId: id, sessionRef: null, userId, beneficiary: user.walletAddress,
                createdAt: new Date(now).toISOString(), source: 'esp32', deviceId: this.deviceId,
                stage: 'WALLET_INTENT', intentExpiresAt: now + WALLET_INTENT_TTL_MS,
                taskId: assignment?.taskId || null, assignmentId: assignment?.assignmentId || null,
                taskSnapshot: assignment ? structuredClone(assignment.policySnapshot) : null,
                policySnapshot, leaseId: crypto.randomUUID(), startAttempts: 0
            });
        });
    }
    async verifyWalletStartReceipt(user, sessionId, transactionHash, record) {
        const provider = this.walletService.provider;
        const expectedChainId = Number(this.walletService.config.chainId);
        const network = await provider.getNetwork();
        if (Number(network.chainId) !== expectedChainId) throw new ApiError(409, 'WRONG_CHAIN', 'Start transaction was sent to the wrong chain');
        const [transaction, receipt] = await Promise.all([
            provider.getTransaction(transactionHash), provider.getTransactionReceipt(transactionHash)
        ]);
        if (!transaction || !receipt) return null;
        const expectedAddress = this.walletService.config.rewardAddress.toLowerCase();
        if (Number(transaction.chainId) !== expectedChainId
            || String(transaction.from || '').toLowerCase() !== user.walletAddress.toLowerCase()
            || String(transaction.to || '').toLowerCase() !== expectedAddress
            || Number(receipt.status) !== 1
            || String(receipt.from || '').toLowerCase() !== user.walletAddress.toLowerCase()
            || String(receipt.to || '').toLowerCase() !== expectedAddress
            || String(receipt.hash || receipt.transactionHash || '').toLowerCase() !== transactionHash.toLowerCase()) {
            throw new ApiError(409, 'START_RECEIPT_MISMATCH', 'Transaction receipt does not match the wallet, chain or reward contract');
        }
        const taskRewards = this.walletService.config.rewardProtocolVersion === 'task-rewards-v2';
        let parsedTransaction;
        try { parsedTransaction = this.walletService.rewardInterface.parseTransaction({ data: transaction.data, value: transaction.value }); }
        catch { parsedTransaction = null; }
        const expectedSessionKey = sessionKey(sessionId);
        if (!parsedTransaction || parsedTransaction.name !== 'startSession'
            || String(parsedTransaction.args[0]).toLowerCase() !== expectedSessionKey.toLowerCase()
            || (taskRewards && (!record.taskSnapshot?.task?.taskId
                || String(parsedTransaction.args[1]).toLowerCase() !== ethers.id(record.taskSnapshot.task.taskId).toLowerCase()
                || String(parsedTransaction.args[2]).toLowerCase() !== String(record.taskSnapshot.snapshotHash).toLowerCase()
                || String(parsedTransaction.args[3]) !== String(record.taskSnapshot.task.completionRule.maximumDurationS)))) {
            throw new ApiError(409, 'START_TRANSACTION_MISMATCH', 'Transaction calldata does not start this session');
        }
        let startedEvent = null;
        for (const log of receipt.logs || []) {
            if (String(log.address || '').toLowerCase() !== expectedAddress) continue;
            try {
                const parsed = this.walletService.rewardInterface.parseLog({ topics: log.topics, data: log.data });
                if (parsed?.name === 'SessionStarted') startedEvent = parsed;
            } catch { /* Ignore unrelated logs from the reward contract. */ }
        }
        if (!startedEvent
            || String(startedEvent.args.sessionId).toLowerCase() !== expectedSessionKey.toLowerCase()
            || String(startedEvent.args.beneficiary ?? startedEvent.args[taskRewards ? 2 : 1]).toLowerCase() !== user.walletAddress.toLowerCase()
            || (taskRewards && (String(startedEvent.args.taskIdHash ?? startedEvent.args[1]).toLowerCase() !== ethers.id(record.taskSnapshot.task.taskId).toLowerCase()
                || String(startedEvent.args.policyHash ?? startedEvent.args[3]).toLowerCase() !== String(record.taskSnapshot.snapshotHash).toLowerCase()
                || String(startedEvent.args.fullRewardDuration ?? startedEvent.args[6]) !== String(record.taskSnapshot.task.completionRule.maximumDurationS)))
            || (record.policySnapshot?.stakeAmount && String(startedEvent.args.amount ?? startedEvent.args.stakeAmount ?? startedEvent.args[taskRewards ? 4 : 2]) !== record.policySnapshot.stakeAmount)) {
            throw new ApiError(409, 'SESSION_STARTED_EVENT_MISSING', 'Successful receipt lacks the expected SessionStarted event');
        }
        return { receipt, event: startedEvent };
    }
    invalidateDeviceState() { this.deviceVersion++; this.deviceCache = null; this.deviceRead = null; }
    async deviceState({ fresh = false } = {}) {
        const version = this.deviceVersion;
        if (!fresh && this.deviceCache && this.clock() - this.deviceCache.at < 2000) return this.deviceCache.data;
        if (!fresh && this.deviceRead) return this.deviceRead;
        const minDurationS = 30;
        const minSamplesAtMinimumDuration = Math.ceil(minDurationS * 10 * 0.9);
        const task = (async () => {
            try {
                const [health, latest, sessions] = await Promise.all([this.iot.health(), this.iot.latest(), this.iot.list()]);
                const sample = latest.latest;
                const sampleDevice = sample?.telemetry?.device_id || sample?.assessment?.sample_ref?.device_id;
                const ageMs = this.clock() - Date.parse(sample?.received_at);
                const running = sessions.find(row => row.device_id === this.deviceId && row.status === 'running');
                let reason = null;
                if (health.reward_auto_start || health.reward_auto_settle) reason = 'The fixed-beneficiary IoT reward bridge must be disabled for account-bound sessions';
                else if (latest.source_mode !== 'esp32') reason = 'The active source is not ESP32';
                else if (!health.mqtt || sampleDevice !== this.deviceId || !Number.isFinite(ageMs) || ageMs < -1000 || ageMs > 5000) reason = 'No recent sensor telemetry is available';
                const data = { ready: !reason, reason, deviceId: this.deviceId, source: latest.source_mode, latestReceivedAt: sample?.received_at || null,
                    busySessionId: running?.session_id || null, minDurationS, sampleRateHz: 10,
                    minSamplesAtMinimumDuration };
                if (version === this.deviceVersion) this.deviceCache = { at: this.clock(), data };
                return data;
            } catch (error) {
                const data = { ready: false, reason: error.message, deviceId: this.deviceId,
                    busySessionId: null, minDurationS, sampleRateHz: 10,
                    minSamplesAtMinimumDuration };
                if (version === this.deviceVersion) this.deviceCache = { at: this.clock(), data };
                return data;
            }
        })();
        this.deviceRead = task;
        try { return await task; } finally { if (this.deviceRead === task) this.deviceRead = null; }
    }
    async wallet(userId) {
        const [wallet, device] = await Promise.all([this.walletService.getWallet(userId), this.deviceState()]);
        const record = this.store.list(userId).find(row => row.stage && !FINAL.has(row.stage));
        const sessionMinimum = Number(record?.taskSnapshot?.task?.completionRule?.minimumDurationS
            ?? record?.policySnapshot?.minimumSafeDuration ?? device.minDurationS);
        const minDurationS = Number.isFinite(sessionMinimum) && sessionMinimum > 0 ? sessionMinimum : device.minDurationS;
        const sessionDevice = { ...device, minDurationS,
            minSamplesAtMinimumDuration: Math.ceil(minDurationS * (device.sampleRateHz || 10) * 0.9) };
        let operatorReady = true;
        try { await this.checkOperator(userId); } catch { operatorReady = false; }
        return { ...wallet, device: { ...sessionDevice, operatorReady }, activeSessionId: record?.sessionId || null,
            canEnd: Boolean(record?.source === 'esp32' && ['RUNNING', 'STOPPING_SENSOR', 'SETTLEMENT_PENDING'].includes(record.stage)),
            canStart: wallet.canStart && device.ready && operatorReady && !device.busySessionId };
    }
    async session(userId, id) {
        const record = this.owner(userId, id);
        await this.refreshSensorProgress(record);
        const result = await this.walletService.getSession(userId, id);
        return { ...result, session: { ...result.session, taskCompletion: this.taskCompletion(this.store.get(id)) } };
    }
    async refreshSensorProgress(record) {
        if (record.source !== 'esp32' || FINAL.has(record.stage) || record.evidence || record.settlementPayload) return;
        try {
            const info = await this.iot.get(record.sessionId);
            const current = this.store.get(record.sessionId);
            if (info && current && !FINAL.has(current.stage) && !current.evidence && !current.settlementPayload) {
                this.store.update(record.sessionId, { iotSession: this.sensorInfo(info) });
            }
        } catch { /* Keep the durable progress; freshness marks stale telemetry UNKNOWN. */ }
    }
    taskCompletion(record) {
        const rule = record?.taskSnapshot?.task?.completionRule;
        if (!rule) return null;
        if (rule.type === 'sensor_activity') return this.activityCompletion(record);
        // Once End freezes evidence, later store changes cannot change the reported decision.
        const evidence = record.evidence ? record.evidence.completionEvidence
            : this.completionStore?.getCompletionEvidence?.(record.sessionId);
        const progress = record.evidence ? record.evidence.cycleProgress
            : evidence?.cycleProgress || this.completionStore?.getCycleProgress?.(record.sessionId);
        const recordedCycles = progress?.recordedCycles ?? 0;
        const observedCycles = evidence?.observedCycles ?? (recordedCycles > 0 ? recordedCycles : null);
        return { status: evidence ? (evidence.achieved ? 'APPROVED' : 'INSUFFICIENT') : recordedCycles > 0 ? 'TRACKING' : 'MISSING',
            requiredCycles: rule.requiredCycles, observedCycles, recordedCycles,
            remainingCycles: Math.max(0, rule.requiredCycles - (observedCycles ?? 0)),
            source: progress?.source ?? (evidence ? evidence.source || 'owner_approved' : 'operator_recorded'),
            lastRecordedAt: progress?.lastRecordedAt ?? null, approvedAt: evidence?.createdAt ?? null,
            reviewable: record.stage === 'RUNNING' && !record.endRequested && !record.iotSession?.ended_at
                && !record.evidence && !record.settlementPayload && !evidence };
    }
    activityCompletion(record) {
        const task = record.taskSnapshot.task;
        const frozen = record.evidence;
        const info = frozen?.session || record.iotSession;
        const stats = frozen?.stats || info || {};
        const activity = frozen?.activityProgress || stats.activity;
        const validation = validateActivityEvidence(activity, { session: info, stats, task, sessionId: record.sessionId });
        const elapsedSinceSample = this.clock() - Date.parse(activity?.last_sample_at);
        const state = !frozen && (!Number.isFinite(elapsedSinceSample) || elapsedSinceSample > 2000 || elapsedSinceSample < -1000)
            ? 'UNKNOWN' : activity?.state || 'UNKNOWN';
        return { type: 'sensor_activity', status: validation.achieved ? 'APPROVED' : activity && !validation.reasons.length ? 'TRACKING' : 'MISSING',
            requiredActiveSeconds: task.completionRule.requiredActiveSeconds, activeSeconds: validation.activeSeconds,
            remainingActiveSeconds: Math.max(0, task.completionRule.requiredActiveSeconds - validation.activeSeconds),
            stationarySeconds: validation.reasons.length ? 0 : activity.stationary_seconds,
            unknownSeconds: validation.reasons.length ? 0 : activity.unknown_seconds,
            state, source: 'sensor_activity', lastRecordedAt: activity?.last_sample_at || null,
            reviewable: false, frozen: Boolean(frozen), reasonCodes: validation.reasons };
    }
    async sessions(userId) {
        for (const record of this.store.list(userId).filter(row => row.source === 'esp32' && !FINAL.has(row.stage))) await this.session(userId, record.sessionId);
        const result = await this.walletService.listSessions(userId);
        return { ...result, sessions: result.sessions.map(session => ({ ...session,
            taskCompletion: this.taskCompletion(this.store.get(session.sessionId)) })) };
    }
    sensorInfo(info) {
        return { ...info, durationS: info.started_at ? Math.max(0, ((info.ended_at ? Date.parse(info.ended_at) : this.clock()) - Date.parse(info.started_at)) / 1000) : 0 };
    }
    async response(record, extra = {}) {
        const result = await this.walletService.getSession(record.userId, record.sessionId);
        return { success: true, httpStatus: ['RUNNING', 'SETTLED', 'START_FAILED'].includes(record.stage) ? 200 : 202,
            pending: !['RUNNING', 'SETTLED', 'START_FAILED'].includes(record.stage), ...result.session, session: result.session, ...extra };
    }
    retry(record, error) {
        const attempts = (record.retryAttempts || 0) + 1;
        const delayMs = [5000, 10000, 30000][Math.min(attempts - 1, 2)];
        const fields = { retryAttempts: attempts, lastError: error.message, nextRetryAt: this.clock() + delayMs };
        if (TERMINAL_ERRORS.has(error.code)) { fields.stage = 'RECOVERY_REQUIRED'; fields.nextRetryAt = null; }
        const updated = this.store.update(record.sessionId, fields);
        this.log('SESSION_RETRY', updated, { errorCode: error.code || 'UNAVAILABLE' });
        return updated;
    }
    // Wallet-first users sign the stake transaction in MetaMask. The backend
    // must still own the sensor reservation and persist the session after the
    // wallet confirms that on-chain start succeeded.
    confirmWalletStart(userId, sessionId, body = {}) {
        this.validateBody(body, ['transactionHash']);
        if (!/^0x[0-9a-fA-F]{64}$/.test(String(body.transactionHash || ''))) {
            throw new ApiError(400, 'INVALID_PAYLOAD', 'transactionHash must be a 32-byte transaction hash');
        }
        return this.serial(() => this.confirmWalletStartNow(userId, sessionId, body));
    }
    async confirmWalletStartNow(userId, sessionId, body) {
            const user = this.walletService._user(userId);
            const taskRewards = this.walletService.config.rewardProtocolVersion === 'task-rewards-v2';
            const id = identifier(sessionId, 'sessionId');
            let record = this.store.get(id);
            if (!record) throw new ApiError(409, 'SESSION_INTENT_REQUIRED', 'A live sensor lease must be prepared before signing the stake');
            if (record.userId !== userId) throw new ApiError(403, 'SESSION_OWNER_MISMATCH', 'This session belongs to another user');
            if (record.transactionHash && record.transactionHash.toLowerCase() !== body.transactionHash.toLowerCase()) {
                throw new ApiError(409, 'SESSION_TRANSACTION_CONFLICT', 'A different start transaction is already linked to this session');
            }
            if (['RUNNING', 'SETTLED', 'SETTLEMENT_PENDING', 'STOPPING_SENSOR'].includes(record.stage)) return this.response(record, { duplicate: true });
            const verified = await this.verifyWalletStartReceipt(user, id, body.transactionHash, record);
            if (!verified) {
                record = this.store.update(id, { transactionHash: body.transactionHash, stage: 'WALLET_INTENT' });
                return { success: true, httpStatus: 202, pending: true, sessionId: id, transactionHash: body.transactionHash, stage: 'START_PENDING' };
            }
            if (record.intentExpiresAt <= this.clock()) {
                record = this.store.update(id, { stage: 'START_PENDING', transactionHash: body.transactionHash,
                    receiptBlockNumber: verified.receipt.blockNumber, expiredAt: this.clock() });
                return this.freezeIncomplete(record, 'wallet_start_lease_expired');
            }
            record = this.store.update(id, {
                stage: 'START_PENDING', transactionHash: body.transactionHash,
                receiptBlockNumber: verified.receipt.blockNumber,
                startedEvent: {
                    sessionKey: String(verified.event.args.sessionId),
                    beneficiary: String(verified.event.args.beneficiary ?? verified.event.args[2]),
                    amount: String(verified.event.args.amount ?? verified.event.args.stakeAmount ?? verified.event.args[4]),
                    startedAt: String(verified.event.args.startedAt ?? verified.event.args[5]),
                    fullRewardDuration: taskRewards ? String(verified.event.args.fullRewardDuration ?? verified.event.args[6]) : null,
                    taskIdHash: taskRewards ? String(verified.event.args.taskIdHash ?? verified.event.args[1]) : null,
                    policyHash: taskRewards ? String(verified.event.args.policyHash ?? verified.event.args[3]) : null
                }
            });

            const chain = (await this.walletService.getSession(userId, id)).session;
            if (!chain.started) throw new ApiError(409, 'START_CHAIN_STATE_MISMATCH', 'Confirmed SessionStarted event is not reflected by the contract state');
            if (chain.beneficiary.toLowerCase() !== user.walletAddress.toLowerCase()) {
                throw new ApiError(409, 'BENEFICIARY_MISMATCH', 'The on-chain session belongs to another wallet');
            }
            if (chain.settled) {
                record = this.store.update(id, { stage: 'SETTLED', lastError: null, nextRetryAt: null });
                return this.response(record, { duplicate: true, transactionHash: body.transactionHash });
            }

            try {
                let info = await this.iot.get(id);
                if (!info) {
                    const device = await this.deviceState({ fresh: true });
                    const competingLease = this.store.list().find(row => row.sessionId !== id
                        && !FINAL.has(row.stage)
                        && (row.stage !== 'WALLET_INTENT' || row.intentExpiresAt > this.clock()));
                    if (!device.ready || (device.busySessionId && device.busySessionId !== id) || competingLease) {
                        if (record.intentExpiresAt <= this.clock() || competingLease) {
                            return this.freezeIncomplete(record, 'wallet_start_lease_expired_or_lost');
                        }
                        throw new ApiError(503, 'SENSOR_UNAVAILABLE', device.reason || 'The sensor is busy');
                    }
                    info = await this.iot.start(id);
                    this.invalidateDeviceState();
                }
                if (info.device_id !== this.deviceId || info.session_id !== id) {
                    throw new ApiError(409, 'IOT_SESSION_MISMATCH', 'Sensor session identity does not match the wallet session');
                }
                record = this.store.update(id, { source: 'esp32', stage: 'RUNNING', iotSession: this.sensorInfo(info), lastError: null, nextRetryAt: null });
                this.setAssignmentStage(record, 'ACTIVE', { startedAt: record.startedEvent?.startedAt || new Date(this.clock()).toISOString() });
                this.log('WALLET_SESSION_STARTED', record, { transactionHash: body.transactionHash });
                return this.response(record, { duplicate: false, transactionHash: body.transactionHash });
            } catch (error) {
                record = this.store.update(id, { source: 'esp32', stage: 'RUNNING', lastError: error.message });
                return this.freezeIncomplete(record, 'wallet_session_sensor_start_failed');
            }
    }
    async advanceStart(record) {
        const id = record.sessionId;
        try {
            if (record.stage === 'COMPENSATING') return this.compensate(record);
            const chain = (await this.walletService.getSession(record.userId, id)).session;
            let info = await this.iot.get(id);
            if (chain.settled) {
                if (info?.status === 'running') { info = await this.iot.stop(id); this.invalidateDeviceState(); }
                record = this.store.update(id, { stage: 'SETTLED', iotSession: info ? this.sensorInfo(info) : record.iotSession, nextRetryAt: null, lastError: null });
                return this.response(record, { duplicate: true });
            }
            if (chain.started) {
                if (!info) return this.freezeIncomplete(record, 'sensor_session_missing');
                record = this.store.update(id, { stage: 'RUNNING', iotSession: this.sensorInfo(info), lastError: null, nextRetryAt: null });
                if (info.status === 'closed' || record.endRequested) return this.advanceEnd(record);
                return this.response(record, { duplicate: true });
            }
            // Recovery never signs a customer stake. Only a verified wallet receipt can start a sensor.
            if (record.transactionHash) return this.confirmWalletStartNow(record.userId, id, { transactionHash: record.transactionHash });
            if (info?.status === 'running') { await this.iot.stop(id); this.invalidateDeviceState(); }
            record = this.store.update(id, { stage: 'START_FAILED', lastError: 'Wallet-signed stake was not confirmed' });
            return this.response(record);
        } catch (error) {
            if (NO_STAKE_ERRORS.has(error.code)) {
                this.store.update(id, { stage: 'COMPENSATING', lastError: error.message });
                return this.compensate(this.store.get(id));
            }
            record = this.retry(this.store.get(id), error);
            return this.response(record);
        }
    }
    async compensate(record) {
        try {
            const info = await this.iot.get(record.sessionId);
            if (info && info.status !== 'closed') { await this.iot.stop(record.sessionId); this.invalidateDeviceState(); }
            record = this.store.update(record.sessionId, { stage: 'START_FAILED', nextRetryAt: null });
            this.log('SESSION_START_FAILED', record);
            return this.response(record);
        } catch (error) { return this.response(this.retry(record, error)); }
    }
    settleFrozen(id, body = {}) {
        this.validateBody(body);
        return this.serial(async () => {
            const sessionId = identifier(id, 'sessionId');
            const record = this.store.get(sessionId);
            if (!record) throw new ApiError(404, 'SESSION_NOT_FOUND', 'Session reference was not found');
            if (!record.settlementPayload || !record.evidence) {
                throw new ApiError(409, 'EVIDENCE_NOT_FROZEN', 'Sensor evidence must be closed and frozen before operator settlement');
            }
            if (FINAL.has(record.stage)) return this.response(record, { duplicate: true });
            return this.advanceEnd(record);
        });
    }

    end(userId, id, body = {}) {
        this.validateBody(body);
        return this.serial(async () => {
            let record = this.owner(userId, id);
            if (FINAL.has(record.stage) || record.stage === 'RECOVERY_REQUIRED') return this.response(record, { duplicate: true });
            if (!record.source) { await this.recoverLegacy(record); return this.response(this.store.get(id)); }
            record = this.store.update(id, { endRequested: true });
            if (['OPENING_SENSOR', 'START_PENDING'].includes(record.stage)) {
                await this.advanceStart(record);
                record = this.store.get(id);
                if (record.stage !== 'RUNNING') return this.response(record);
            }
            return this.advanceEnd(record);
        });
    }
    freezeEvidence(record, document) {
        if (record.settlementPayload) return record;
        normalizeOutcome(document.result.outcome);
        const frozenEvidence = structuredClone(document);
        if (record.taskSnapshot?.task) {
            const task = record.taskSnapshot.task;
            let completion = this.completionStore?.getCompletionEvidence?.(record.sessionId) || null;
            let activityValidation = null;
            if (task.completionRule?.type === 'sensor_activity') {
                frozenEvidence.activityProgress = frozenEvidence.stats?.activity || null;
                activityValidation = validateActivityEvidence(frozenEvidence.activityProgress,
                    { session: frozenEvidence.session, stats: frozenEvidence.stats, task, sessionId: record.sessionId });
                completion = { schemaVersion: 'activity-completion-evidence-v1', taskId: record.taskId,
                    sessionId: record.sessionId, assignmentId: record.assignmentId, userId: record.userId,
                    source: 'sensor_activity', requiredActiveSeconds: activityValidation.requiredActiveSeconds,
                    activeSeconds: activityValidation.activeSeconds, achieved: activityValidation.achieved,
                    sensorEvidenceHash: frozenEvidence.activityProgress?.evidence_hash || null,
                    reasonCodes: activityValidation.reasons };
                completion.evidenceHash = '0x' + crypto.createHash('sha256').update(canonicalJson(completion)).digest('hex');
                frozenEvidence.activityEvidenceValidation = activityValidation;
            }
            frozenEvidence.completionEvidence = completion;
            frozenEvidence.cycleProgress = completion?.cycleProgress
                || this.completionStore?.getCycleProgress?.(record.sessionId) || null;
            const requestedOutcome = frozenEvidence.result.outcome;
            const reasonCodes = [];
            const validation = validateTaskEvidence(frozenEvidence, task);
            const outcomeValidation = validateTaskOutcome(frozenEvidence);
            frozenEvidence.taskEvidenceValidation = validation;
            frozenEvidence.taskOutcomeValidation = outcomeValidation;
            if (requestedOutcome === 'VALID_NO_VIOLATION') {
                if (activityValidation) {
                    reasonCodes.push(...activityValidation.reasons);
                    if (!activityValidation.achieved && !activityValidation.reasons.length) reasonCodes.push('task_activity_duration_below_target');
                } else if (!completion?.achieved) reasonCodes.push(completion ? 'task_completion_rule_not_met' : 'task_completion_evidence_missing');
                reasonCodes.push(...validation.reasons);
            } else if (requestedOutcome === 'VIOLATION_CONFIRMED' || requestedOutcome === 'SENSOR_FAULT') {
                // Never let an IoT result string select a penalty/refund by itself.
                // The independently-derived event/stat counters must prove it.
                reasonCodes.push(...outcomeValidation.reasons);
            } else if (requestedOutcome !== 'INCOMPLETE') {
                reasonCodes.push(...outcomeValidation.reasons);
            }
            if (reasonCodes.length) {
                frozenEvidence.result.outcome = 'INCOMPLETE';
                frozenEvidence.result.reason = reasonCodes[0];
                frozenEvidence.result.dataValid = false;
                frozenEvidence.result.coverageValid = false;
            }
            frozenEvidence.result.reasonCodes = [...new Set([...(frozenEvidence.result.reasonCodes || []), ...reasonCodes])];
            frozenEvidence.task = { taskId: task.taskId, definitionHash: this.store.get(record.sessionId)?.taskSnapshot?.snapshotHash || null,
                assignmentId: record.assignmentId, policySnapshot: record.taskSnapshot };
        }
        const endedAtIso = frozenEvidence.session?.ended_at || frozenEvidence.observedAt;
        const endedAt = unixSecondsFromIso(endedAtIso);
        if (!frozenEvidence.session) frozenEvidence.session = { ended_at: endedAtIso, boundary_source: 'recovery_observed_at' };
        if (frozenEvidence.session.ended_at !== endedAtIso) {
            throw new ApiError(409, 'SESSION_END_TIME_INVALID', 'Frozen evidence ended_at does not match the settlement boundary');
        }
        const evidenceHash = '0x' + crypto.createHash('sha256').update(canonicalJson(frozenEvidence)).digest('hex');
        return this.store.update(record.sessionId, { evidence: frozenEvidence, stage: 'SETTLEMENT_PENDING', endRequested: true,
            settlementPayload: { sessionId: record.sessionId, beneficiary: this.walletService._user(record.userId).walletAddress,
                outcome: frozenEvidence.result.outcome, evidenceHash, timestamp: Math.floor(this.clock() / 1000), endedAt } });
    }
    async freezeIncomplete(record, reason) {
        record = this.store.update(record.sessionId, { source: 'recovery', recoveryReason: reason });
        const chainSession = (await this.walletService.getSession(record.userId, record.sessionId)).session;
        const latestBlock = await this.walletService.provider.getBlock('latest');
        const startedAt = Number(chainSession.startedAt);
        const endedAt = Math.min(Math.max(Math.floor(this.clock() / 1000), startedAt), Number(latestBlock.timestamp));
        const recoveryEndedAt = new Date(endedAt * 1000).toISOString();
        record = this.freezeEvidence(record, { source: 'recovery', sessionId: record.sessionId, deviceId: this.deviceId,
            session: { ended_at: recoveryEndedAt, boundary_source: 'recovery_chain_bounded' },
            result: { outcome: 'INCOMPLETE', reason, sampleCount: 0, durationS: null }, observedAt: new Date(this.clock()).toISOString() });
        return this.advanceEnd(record);
    }
    async advanceEnd(record) {
        const id = record.sessionId;
        try {
            if (!record.settlementPayload) {
                record = this.store.update(id, { stage: 'STOPPING_SENSOR', endRequested: true });
                const info = await this.iot.get(id);
                if (!info) return this.freezeIncomplete(record, 'sensor_session_missing');
                const closed = await this.iot.stop(id);
                this.invalidateDeviceState();
                record = this.store.update(id, { iotSession: this.sensorInfo(closed) });
                const outcome = await this.iot.outcome(id);
                if (outcome.sessionId !== id || outcome.deviceId !== this.deviceId || outcome.session?.status !== 'closed') {
                    throw new ApiError(409, 'IOT_SESSION_MISMATCH', 'Outcome does not belong to this closed sensor session');
                }
                record = this.freezeEvidence(record, { source: 'esp32', sessionId: id, deviceId: this.deviceId,
                    session: outcome.session, stats: outcome.stats, result: outcome.result });
                this.log('SESSION_OUTCOME', record, { outcome: outcome.result.outcome, reason: outcome.result.reason });
            }
            await this.checkOperator(record.userId);
            const result = await this.settler.processSessionOutcome(record.settlementPayload);
            if (result.pending) return this.response(record, { transactionHash: result.transactionHash });
            record = this.store.update(id, { stage: 'SETTLED', settlementResult: result, lastError: null, nextRetryAt: null });
            this.setAssignmentStage(record, 'SETTLED', { outcome: result.outcome, evidenceHash: record.settlementPayload.evidenceHash,
                transactionHash: result.transactionHash || null, settledAt: new Date(this.clock()).toISOString() });
            this.log('SESSION_SETTLED', record, { outcome: result.outcome, stakeReturned: result.stakeReturned, rewardPaid: result.rewardPaid,
                penaltyCollected: result.penaltyCollected, transactionHash: result.transactionHash });
            return this.response(record, { duplicate: Boolean(result.duplicate) });
        } catch (error) { return this.response(this.retry(this.store.get(id), error)); }
    }
    async recoverLegacy(record) {
        const chain = (await this.walletService.getSession(record.userId, record.sessionId).catch(error => {
            if (error.code === 'SESSION_NOT_FOUND') return { session: { started: false } }; throw error;
        })).session;
        if (!chain.started) { this.store.update(record.sessionId, { stage: 'START_FAILED', recoveryReason: 'legacy_session_not_on_chain' }); return; }
        if (chain.settled) { this.store.update(record.sessionId, { stage: 'SETTLED', source: 'legacy' }); return; }
        await this.checkOperator(record.userId);
        const info = await this.iot.get(record.sessionId);
        if (!info) { await this.freezeIncomplete(record, 'missing_iot_session'); return; }
        record = this.store.update(record.sessionId, { source: 'esp32', stage: 'RUNNING', iotSession: this.sensorInfo(info) });
        if (info.status === 'closed') await this.advanceEnd(record);
    }
    drain() {
        return this.serial(async () => {
            for (let record of this.store.list()) {
                if (FINAL.has(record.stage) || record.stage === 'RECOVERY_REQUIRED' || (record.nextRetryAt && record.nextRetryAt > this.clock())) continue;
                try {
                    // Unmapped historical fixed accounts remain archived until ownership is proven.
                    try { this.walletService._user(record.userId); } catch { continue; }
                    if (record.stage === 'WALLET_INTENT') {
                        if (record.transactionHash) { await this.confirmWalletStartNow(record.userId, record.sessionId, { transactionHash: record.transactionHash }); continue; }
                        if (record.intentExpiresAt <= this.clock()) this.store.update(record.sessionId, { stage: 'INTENT_EXPIRED', expiredAt: this.clock() });
                        continue;
                    }
                    if (!record.source) { await this.recoverLegacy(record); continue; }
                    if (record.stage === 'RUNNING') {
                        const chain = (await this.walletService.getSession(record.userId, record.sessionId)).session;
                        if (chain.settled) { await this.advanceStart(record); continue; }
                        const info = await this.iot.get(record.sessionId);
                        if (info?.status !== 'closed' && info) continue;
                        if (!info) await this.freezeIncomplete(record, 'sensor_session_missing');
                        else await this.advanceEnd(record);
                    } else if (record.endRequested || record.stage === 'SETTLEMENT_PENDING' || record.stage === 'STOPPING_SENSOR') await this.advanceEnd(record);
                    else await this.advanceStart(record);
                } catch (error) { this.retry(this.store.get(record.sessionId), error); }
            }
        });
    }
    startRecovery() {
        if (this.timer) return;
        const run = () => {
            if (this.recoveryTask) return;
            this.recoveryTask = this.drain().catch(error => this.logger(JSON.stringify({ event: 'SESSION_RECOVERY_UNAVAILABLE', code: error.code || 'UNAVAILABLE' })))
                .finally(() => { this.recoveryTask = null; });
        };
        run(); this.timer = setInterval(run, 5000); this.timer.unref();
    }
    stopRecovery() { clearInterval(this.timer); this.timer = null; }
}
function buildLiveSessionService(walletService, env = process.env) {
    const { getRewardService } = require('./rewardService');
    let completionStore = null;
    try { completionStore = require('./customerService').getCustomerService(env).store; } catch { /* Task workflow is unavailable without identity storage. */ }
    return new LiveSessionService({ walletService, completionStore, settler: getRewardService(), deviceId: env.HRC_DEVICE_ID || 'esp32_01',
        iot: new IotSessionClient({ baseUrl: env.HRC_IOT_API_URL || 'http://127.0.0.1:8002' }) });
}
module.exports = { LiveSessionService, buildLiveSessionService, unixSecondsFromIso };
