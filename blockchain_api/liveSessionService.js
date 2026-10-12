const crypto = require('node:crypto');
const { ApiError, identifier, canonicalJson } = require('./eventPayload');
const { normalizeOutcome, sessionKey } = require('./rewardPolicy');
const { IotSessionClient } = require('./iotSessionClient');

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
    constructor({ walletService, iot, settler, deviceId = 'esp32_01', clock = Date.now, logger = message => console.log(message) }) {
        Object.assign(this, { walletService, iot, settler, deviceId, clock, logger });
        this.store = walletService.store;
        this.tail = Promise.resolve(); this.timer = null; this.deviceRead = null; this.deviceCache = null; this.deviceVersion = 0;
        // All operator routes use the same durable transaction store once the live bridge is loaded.
        if (walletService.transactionSender) settler.transactionSender ||= walletService.transactionSender;
    }
    serial(work) { const task = this.tail.catch(() => {}).then(work); this.tail = task.catch(() => {}); return task; }
    log(event, record, extra = {}) { this.logger(JSON.stringify({ event, sessionId: record.sessionId, userId: record.userId, stage: record.stage, ...extra })); }
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
    reserveWalletStart(userId, sessionId) {
        return this.serial(async () => {
            const user = this.walletService._user(userId);
            if (!user.walletFirst) throw new ApiError(400, 'NOT_WALLET_FIRST', 'Wallet intent is only for wallet-first users');
            const id = identifier(sessionId, 'sessionId');
            const existing = this.store.get(id);
            if (existing) {
                if (existing.userId !== userId) throw new ApiError(403, 'SESSION_OWNER_MISMATCH', 'This session belongs to another user');
                if (existing.stage === 'WALLET_INTENT' && existing.intentExpiresAt > this.clock()) return existing;
                if (existing.stage === 'WALLET_INTENT') throw new ApiError(409, 'SESSION_INTENT_EXPIRED', 'This session intent expired; prepare a new session reference');
                if (!FINAL.has(existing.stage)) throw new ApiError(409, 'SESSION_IN_PROGRESS', 'This session is already being processed');
            }

            await this.checkOperator(userId);
            const [wallet, device] = await Promise.all([
                this.walletService.getWallet(userId), this.deviceState({ fresh: true })
            ]);
            if (!device.ready) throw new ApiError(503, 'SENSOR_UNAVAILABLE', device.reason);
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
            const now = this.clock();
            return this.store.save({
                sessionId: id, sessionRef: null, userId, beneficiary: user.walletAddress,
                createdAt: new Date(now).toISOString(), source: 'esp32', deviceId: this.deviceId,
                stage: 'WALLET_INTENT', intentExpiresAt: now + WALLET_INTENT_TTL_MS,
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
        let parsedTransaction;
        try { parsedTransaction = this.walletService.rewardInterface.parseTransaction({ data: transaction.data, value: transaction.value }); }
        catch { parsedTransaction = null; }
        const expectedSessionKey = sessionKey(sessionId);
        if (!parsedTransaction || parsedTransaction.name !== 'startSession'
            || String(parsedTransaction.args[0]).toLowerCase() !== expectedSessionKey.toLowerCase()) {
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
            || String(startedEvent.args.beneficiary).toLowerCase() !== user.walletAddress.toLowerCase()
            || (record.policySnapshot?.stakeAmount && String(startedEvent.args.amount) !== record.policySnapshot.stakeAmount)) {
            throw new ApiError(409, 'SESSION_STARTED_EVENT_MISSING', 'Successful receipt lacks the expected SessionStarted event');
        }
        return { receipt, event: startedEvent };
    }
    invalidateDeviceState() { this.deviceVersion++; this.deviceCache = null; this.deviceRead = null; }
    async deviceState({ fresh = false } = {}) {
        const version = this.deviceVersion;
        if (!fresh && this.deviceCache && this.clock() - this.deviceCache.at < 2000) return this.deviceCache.data;
        if (!fresh && this.deviceRead) return this.deviceRead;
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
                    busySessionId: running?.session_id || null, minDurationS: 30, sampleRateHz: 10,
                    minSamplesAtMinimumDuration: 270 };
                if (version === this.deviceVersion) this.deviceCache = { at: this.clock(), data };
                return data;
            } catch (error) {
                const data = { ready: false, reason: error.message, deviceId: this.deviceId,
                    busySessionId: null, minDurationS: 30, sampleRateHz: 10,
                    minSamplesAtMinimumDuration: 270 };
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
        let operatorReady = true;
        try { await this.checkOperator(userId); } catch { operatorReady = false; }
        return { ...wallet, device: { ...device, operatorReady }, activeSessionId: record?.sessionId || null,
            canEnd: Boolean(record?.source === 'esp32' && ['RUNNING', 'STOPPING_SENSOR', 'SETTLEMENT_PENDING'].includes(record.stage)),
            canStart: wallet.canStart && device.ready && operatorReady && !device.busySessionId };
    }
    async session(userId, id) {
        const record = this.owner(userId, id);
        if (record.source === 'esp32' && !FINAL.has(record.stage)) {
            try {
                const info = await this.iot.get(id);
                if (info) this.store.update(id, { iotSession: this.sensorInfo(info) });
            } catch { /* Last durable sensor state remains visible while the device API is down. */ }
        }
        return this.walletService.getSession(userId, id);
    }
    async sessions(userId) {
        for (const record of this.store.list(userId).filter(row => row.source === 'esp32' && !FINAL.has(row.stage))) await this.session(userId, record.sessionId);
        return this.walletService.listSessions(userId);
    }
    async overview() {
        const users = [];
        for (const account of this.walletService.listAccounts().filter(row => row.role === 'customer')) {
            const [wallet, sessions] = await Promise.all([this.wallet(account.userId), this.sessions(account.userId)]);
            users.push({ ...account, wallet, sessions: sessions.sessions });
        }
        return { success: true, chainId: 31337, users };
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
    start(userId, body = {}) {
        this.validateBody(body, ['sessionRef', 'sessionId']);
        return this.serial(async () => {
            const user = this.walletService._user(userId);
            const id = this.walletService._sessionId(userId, body);
            if (id.length > 63) throw new ApiError(400, 'INVALID_SESSION_REF', 'The sensor session identifier must fit within 63 characters');
            let record = this.store.get(id);
            if (record) {
                this.owner(userId, id);
                if (FINAL.has(record.stage) || record.stage === 'RUNNING') return this.response(record, { duplicate: true });
                if (!record.source) { await this.recoverLegacy(record); return this.response(this.store.get(id), { duplicate: true }); }
                if (record.stage === 'RECOVERY_REQUIRED') return this.response(record, { duplicate: true });
                if (record.endRequested) return this.advanceEnd(record);
                return this.advanceStart(record);
            }
            await this.checkOperator(userId);
            const [wallet, device] = await Promise.all([this.walletService.getWallet(userId), this.deviceState({ fresh: true })]);
            if (!device.ready) throw new ApiError(503, 'SENSOR_UNAVAILABLE', device.reason);
            if (device.busySessionId || this.store.list().some(row => row.source === 'esp32' && ['OPENING_SENSOR', 'START_PENDING', 'RUNNING', 'STOPPING_SENSOR', 'COMPENSATING'].includes(row.stage))) {
                throw new ApiError(409, 'DEVICE_BUSY', 'The sensor is measuring another session. End that session before starting a new one.');
            }
            if (wallet.activeSessionCount || this.store.list(userId).some(row => row.stage && !FINAL.has(row.stage))) throw new ApiError(409, 'SESSION_IN_PROGRESS', 'Your previous session is still being processed');
            if (Number(wallet.balance) < Number(wallet.stakeAmount)) throw new ApiError(409, 'INSUFFICIENT_HRC_BALANCE', 'Not enough available HRC for the stake');
            if (Number(wallet.allowance) < Number(wallet.requiredAllowance)) throw new ApiError(409, 'APPROVAL_REQUIRED', 'Approve 2 HRC before starting');
            record = this.store.save({ sessionId: id, sessionRef: body.sessionRef || body.sessionId || null, userId, beneficiary: user.walletAddress,
                createdAt: new Date(this.clock()).toISOString(), source: 'esp32', deviceId: this.deviceId, stage: 'OPENING_SENSOR' });
            this.log('SESSION_RESERVED', record);
            return this.advanceStart(record);
        });
    }

    // Wallet-first users sign the stake transaction in MetaMask. The backend
    // must still own the sensor reservation and persist the session after the
    // wallet confirms that on-chain start succeeded.
    confirmWalletStart(userId, sessionId, body = {}) {
        this.validateBody(body, ['transactionHash']);
        if (!/^0x[0-9a-fA-F]{64}$/.test(String(body.transactionHash || ''))) {
            throw new ApiError(400, 'INVALID_PAYLOAD', 'transactionHash must be a 32-byte transaction hash');
        }
        return this.serial(async () => {
            const user = this.walletService._user(userId);
            const id = identifier(sessionId, 'sessionId');
            let record = this.store.get(id);
            if (!record) throw new ApiError(409, 'SESSION_INTENT_REQUIRED', 'A live sensor lease must be prepared before signing the stake');
            if (record.userId !== userId) throw new ApiError(403, 'SESSION_OWNER_MISMATCH', 'This session belongs to another user');
            if (record.transactionHash && record.transactionHash.toLowerCase() !== body.transactionHash.toLowerCase()) {
                throw new ApiError(409, 'SESSION_TRANSACTION_CONFLICT', 'A different start transaction is already linked to this session');
            }
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
                    beneficiary: String(verified.event.args.beneficiary),
                    amount: String(verified.event.args.amount),
                    startedAt: String(verified.event.args.startedAt)
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
                this.log('WALLET_SESSION_STARTED', record, { transactionHash: body.transactionHash });
                return this.response(record, { duplicate: false, transactionHash: body.transactionHash });
            } catch (error) {
                record = this.store.update(id, { source: 'esp32', stage: 'RUNNING', lastError: error.message });
                return this.freezeIncomplete(record, 'wallet_session_sensor_start_failed');
            }
        });
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
            if (!info) { info = await this.iot.start(id); this.invalidateDeviceState(); }
            if (info.device_id !== this.deviceId || info.session_id !== id) throw new ApiError(409, 'IOT_SESSION_MISMATCH', 'Sensor session identity does not match the user session');
            if (info.status === 'closed' && !this.store.getTransaction('stake:' + id)) {
                record = this.store.update(id, { stage: 'START_FAILED', lastError: 'Sensor session closed before the stake was confirmed', iotSession: this.sensorInfo(info) });
                return this.response(record);
            }
            if (!this.store.getTransaction('stake:' + id)) {
                const device = await this.deviceState({ fresh: true });
                if (!device.ready) throw new ApiError(503, 'SENSOR_UNAVAILABLE', device.reason);
            }
            record = this.store.update(id, { stage: 'START_PENDING', iotSession: this.sensorInfo(info) });
            const started = await this.walletService.startSession(record.userId, { sessionId: id, sessionRef: record.sessionRef });
            if (started.pending) return this.response(record, { transactionHash: started.transactionHash });
            record = this.store.update(id, { stage: 'RUNNING', lastError: null, nextRetryAt: null });
            this.log('SESSION_STARTED', record, { transactionHash: started.transactionHash });
            if (info.status === 'closed' || record.endRequested) return this.advanceEnd(record);
            return this.response(record, { duplicate: Boolean(started.duplicate), transactionHash: started.transactionHash });
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
                    if (record.stage === 'WALLET_INTENT') {
                        if (record.transactionHash) continue;
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
    return new LiveSessionService({ walletService, settler: getRewardService(), deviceId: env.HRC_DEMO_DEVICE_ID || 'esp32_01',
        iot: new IotSessionClient({ baseUrl: env.HRC_IOT_API_URL || 'http://127.0.0.1:8001' }) });
}
module.exports = { LiveSessionService, buildLiveSessionService, unixSecondsFromIso };
