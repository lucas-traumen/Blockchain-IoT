const { ethers } = require("ethers");
const { ApiError, identifier, canonicalJson, preparePayload, TYPES } = require("./eventPayload");

function failure(error) {
    return { success: false, httpStatus: error.statusCode || 503,
        code: error instanceof ApiError ? error.code : "BLOCKCHAIN_UNAVAILABLE",
        error: error instanceof ApiError ? error.message : "Blockchain request could not be completed; retry the same event identity" };
}

class EventService {
    constructor({ provider, wallet, contract, store, chainId, contractAddress, waitMs = 15000 }) {
        Object.assign(this, { provider, wallet, contract, store, chainId, contractAddress, waitMs });
        this.tail = Promise.resolve();
    }
    serial(work) {
        const task = this.tail.then(work);
        this.tail = task.catch(() => {});
        return task.catch(failure);
    }
    async domain() {
        const network = await this.provider.getNetwork();
        const actual = await this.contract.getAddress();
        if (BigInt(network.chainId) !== BigInt(this.chainId) || actual.toLowerCase() !== this.contractAddress.toLowerCase()) {
            throw new ApiError(503, "NETWORK_MISMATCH", "RPC network or contract does not match the configured deployment");
        }
        return { name: "HRCSafetyLog", version: "1", chainId: this.chainId, verifyingContract: this.contractAddress };
    }
    key(kind, id) { return `${this.chainId}:${this.contractAddress.toLowerCase()}:${kind}:${id}`; }
    async authenticateDevice(prepared, domain) {
        const device = await this.contract.getDevice(prepared.payload.deviceId);
        const signer = device.signer ?? device[0];
        const revoked = device.revoked ?? device[1];
        if (revoked) throw new ApiError(403, "DEVICE_REVOKED", "Device has been revoked");
        if (signer.toLowerCase() === ethers.ZeroAddress) {
            if (prepared.signature) throw new ApiError(400, "DEVICE_NOT_REGISTERED", "Signed events require an on-chain registered device");
            return false;
        }
        if (!prepared.signature) throw new ApiError(403, "SIGNATURE_REQUIRED", "Registered device requires an EIP-712 signature");
        const value = { eventId: prepared.payload.eventId, deviceId: prepared.payload.deviceId,
            timestamp: prepared.payload.timestamp, dataHash: prepared.dataHash, riskLevel: prepared.riskLevel };
        let recovered;
        try { recovered = ethers.verifyTypedData(domain, TYPES, value, prepared.signature); }
        catch { throw new ApiError(403, "INVALID_SIGNATURE", "Invalid device signature"); }
        if (recovered.toLowerCase() !== signer.toLowerCase()) throw new ApiError(403, "INVALID_SIGNATURE", "Signature does not match the registered device and payload");
        return true;
    }
    async chainEvent(id) {
        try { return await this.contract.getSafetyEvent(id); }
        catch (error) {
            let name = error.revert?.name;
            if (!name && error.data) {
                try { name = this.contract.interface.parseError(error.data)?.name; } catch { /* provider error */ }
            }
            if (name === "EventNotFound") return null;
            throw error;
        }
    }
    assertEventMatches(event, record) {
        const p = record.payload;
        if (event[0] !== p.eventId || event[1] !== p.deviceId || BigInt(event[2]) !== BigInt(p.timestamp) ||
            event[3].toLowerCase() !== record.dataHash.toLowerCase() || Number(event[4]) !== p.aiResult.riskLevel) {
            throw new ApiError(409, "ONCHAIN_CONFLICT", "Event identity already exists on-chain with different data");
        }
    }
    async settle(record, receipt) {
        // Keep receipt status durably, even if the subsequent robot read fails.
        record.transactionStatus = Number(receipt.status) === 1 ? "confirmed" : "failed";
        record.blockNumber = receipt.blockNumber;
        record.emergencyStopTriggered = false;
        for (const log of receipt.logs || []) {
            if (log.address && log.address.toLowerCase() !== this.contractAddress.toLowerCase()) continue;
            try {
                if (this.contract.interface.parseLog(log)?.name === "EmergencyStopTriggered") record.emergencyStopTriggered = true;
            } catch { /* unrelated receipt log */ }
        }
        this.store.save(record);
        return record;
    }
    async refresh(record) {
        if (record.transactionStatus === "signed" || record.transactionStatus === "pending") {
            const receipt = await this.provider.getTransactionReceipt(record.transactionHash);
            if (receipt) return this.settle(record, receipt);
        }
        return record;
    }
    async ensureNoPending(except) {
        for (let record of this.store.list()) {
            if (record.key === except || !["signed", "pending"].includes(record.transactionStatus)) continue;
            record = await this.refresh(record);
            if (["signed", "pending"].includes(record.transactionStatus)) {
                throw new ApiError(409, "TRANSACTION_IN_PROGRESS", "Another transaction is unresolved; retry its original request before creating a new transaction");
            }
        }
    }
    async sign(record, method, args) {
        await this.ensureNoPending(record.key);
        const nonce = await this.provider.getTransactionCount(await this.wallet.getAddress(), "pending");
        const call = await this.contract[method].populateTransaction(...args);
        const transaction = await this.wallet.populateTransaction({ ...call, nonce });
        const rawTransaction = await this.wallet.signTransaction(transaction);
        record.rawTransaction = rawTransaction;
        record.transactionHash = ethers.keccak256(rawTransaction);
        record.transactionStatus = "signed";
        // Critical boundary: persist signed bytes BEFORE the first network send.
        this.store.save(record);
        return record;
    }
    async broadcast(record) {
        if (!record.rawTransaction || ethers.keccak256(record.rawTransaction) !== record.transactionHash) {
            throw new ApiError(503, "STORE_INTEGRITY_ERROR", "Stored transaction integrity check failed");
        }
        record = await this.refresh(record);
        if (!["signed", "pending"].includes(record.transactionStatus)) return record;
        try {
            await this.provider.broadcastTransaction(record.rawTransaction);
            record.transactionStatus = "pending";
            this.store.save(record);
        } catch {
            // A send timeout may have broadcast successfully. Never allocate a
            // new nonce or sign a replacement here: retry only these same bytes.
        }
        let receipt = await this.provider.getTransactionReceipt(record.transactionHash);
        if (!receipt && this.waitMs > 0) {
            try { receipt = await this.provider.waitForTransaction(record.transactionHash, 1, this.waitMs); }
            catch { /* still uncertain; return 202 with durable hash */ }
        }
        return receipt ? this.settle(record, receipt) : record;
    }
    async result(record, duplicate = false) {
        if (record.transactionStatus === "failed") throw new ApiError(409, "TRANSACTION_REVERTED", "Transaction reverted; inspect its receipt before creating a corrected event");
        const confirmed = record.transactionStatus === "confirmed";
        const p = record.payload;
        if (confirmed && record.kind === "event") {
            const event = await this.chainEvent(p.eventId);
            if (!event) throw new ApiError(503, "CONFIRMATION_UNAVAILABLE", "Receipt exists but event is unavailable; retry the same request");
            this.assertEventMatches(event, record);
        }
        const robotLocked = confirmed ? await this.contract.isRobotLocked(p.deviceId) : null;
        const base = { success: true, httpStatus: confirmed ? 200 : 202, duplicate,
            transactionStatus: confirmed ? "confirmed" : "pending", deviceId: p.deviceId, robotLocked,
            status: confirmed ? (robotLocked ? "LOCKED" : "UNLOCKED") : "PENDING" };
        if (record.kind === "unlock") return { ...base, transactionHash: record.transactionHash };
        return { ...base, ...p, riskLevel: p.aiResult.riskLevel, dataHash: record.dataHash,
            hashFormat: "hrc-canonical-json-v1", recordTransactionHash: record.transactionHash,
            recordBlockNumber: record.blockNumber ?? null,
            emergencyStopTriggered: confirmed ? record.emergencyStopTriggered : null,
            emergencyStopTransactionHash: record.emergencyStopTriggered ? record.transactionHash : null };
    }
    processSafetyEvent(input) {
        return this.serial(async () => {
            const prepared = preparePayload(input);
            const domain = await this.domain();
            const signed = await this.authenticateDevice(prepared, domain);
            const key = this.key("event", prepared.payload.eventId);
            let record = this.store.get(key);
            const duplicate = Boolean(record);
            if (record) {
                if (canonicalJson(record.payload) !== record.canonicalPayload) {
                    throw new ApiError(503, "STORE_INTEGRITY_ERROR", "Stored payload integrity check failed");
                }
                if (record.canonicalPayload !== prepared.canonicalPayload || record.dataHash !== prepared.dataHash) {
                    throw new ApiError(409, "IDEMPOTENCY_CONFLICT", "Event identity was already used with a different payload");
                }
            } else {
                const event = await this.chainEvent(prepared.payload.eventId);
                if (event) throw new ApiError(409, "ONCHAIN_EVENT_EXISTS", "Event already exists on-chain; original local transaction/payload is unavailable");
                record = { key, kind: "event", payload: prepared.payload, canonicalPayload: prepared.canonicalPayload,
                    dataHash: prepared.dataHash, hashFormat: "hrc-canonical-json-v1", transactionStatus: "prepared" };
                this.store.save(record);
            }
            if (record.transactionStatus === "prepared") {
                const p = prepared.payload;
                const args = [p.eventId, p.deviceId, p.timestamp, prepared.dataHash, prepared.riskLevel];
                if (signed) args.push(prepared.signature);
                record = await this.sign(record, signed ? "recordSafetyEventSigned" : "recordSafetyEvent", args);
            }
            if (["signed", "pending"].includes(record.transactionStatus)) record = await this.broadcast(record);
            return this.result(record, duplicate);
        });
    }
    unlockRobot(deviceId) {
        return this.serial(async () => {
            identifier(deviceId, "deviceId");
            await this.domain();
            const lock = await this.contract.getLockInfo(deviceId);
            const key = this.key("unlock", canonicalJson({ deviceId, eventId: lock.eventId ?? lock[1], lockedAt: String(lock.lockedAt ?? lock[3]) }));
            let record = this.store.get(key);
            const duplicate = Boolean(record);
            if (!record) {
                if (!(lock.locked ?? lock[0])) throw new ApiError(409, "ALREADY_UNLOCKED", "Robot is already unlocked");
                if ((await this.contract.owner()).toLowerCase() !== (await this.wallet.getAddress()).toLowerCase()) {
                    throw new ApiError(403, "NOT_OWNER", "Backend wallet is not the contract owner");
                }
                record = { key, kind: "unlock", payload: { deviceId }, transactionStatus: "prepared" };
                this.store.save(record);
            }
            if (record.transactionStatus === "prepared") record = await this.sign(record, "unlockRobot", [deviceId]);
            if (["signed", "pending"].includes(record.transactionStatus)) record = await this.broadcast(record);
            return this.result(record, duplicate);
        });
    }
    getStoredPayload(eventId) {
        identifier(eventId, "eventId");
        const record = this.store.get(this.key("event", eventId));
        if (!record) throw new ApiError(404, "PAYLOAD_NOT_FOUND", "No locally archived payload for this event (historical events are not reconstructed)");
        const actualHash = ethers.keccak256(ethers.toUtf8Bytes(canonicalJson(record.payload)));
        if (canonicalJson(record.payload) !== record.canonicalPayload || actualHash !== record.dataHash) throw new ApiError(503, "STORE_INTEGRITY_ERROR", "Stored payload integrity check failed");
        return { success: true, eventId, payload: record.payload, canonicalPayload: record.canonicalPayload,
            dataHash: record.dataHash, hashFormat: record.hashFormat, transactionStatus: record.transactionStatus,
            transactionHash: record.transactionHash ?? null };
    }
}
module.exports = { EventService };
