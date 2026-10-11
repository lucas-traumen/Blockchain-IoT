require("dotenv").config();

const fs = require("node:fs");
const path = require("node:path");
const { ethers } = require("ethers");
const { ApiError, identifier } = require("./eventPayload");
const {
    POLICY_VERSION,
    normalizeOutcome,
    sessionKey,
    evidenceHash,
    statusName
} = require("./rewardPolicy");
const { createRpcProvider } = require("./rpcProvider");
const { withSignerWrite } = require("./signerQueue");
const { configuredNetwork } = require("./networkConfig");
const { operatorPrivateKey } = require("./operatorKey");

const DEFAULT_REWARD_DEPLOYMENT = path.join(__dirname, "../smart_contracts/deployments/reward-localhost.json");
const REWARD_ABI_PATH = path.join(__dirname, "../smart_contracts/artifacts/contracts/HRCSessionRewards.sol/HRCSessionRewards.json");

function readJson(file) {
    try { return JSON.parse(fs.readFileSync(file, "utf8")); }
    catch { return null; }
}

function rewardConfig(env = process.env) {
    const deployment = readJson(env.HRC_REWARD_DEPLOYMENT || DEFAULT_REWARD_DEPLOYMENT) || {};
    const address = env.HRC_REWARD_ADDRESS || deployment.rewardsAddress;
    const rpcUrl = env.RPC_URL || "http://127.0.0.1:8545";
    const network = configuredNetwork(env, deployment.network || "localhost");
    const privateKey = operatorPrivateKey(env, network);
    if (!address || !ethers.isAddress(address)) {
        throw new ApiError(503, "REWARD_NOT_CONFIGURED", "HRC reward contract address is not configured");
    }
    const artifact = readJson(env.HRC_REWARD_ABI || REWARD_ABI_PATH);
    if (!artifact?.abi) throw new ApiError(503, "REWARD_NOT_CONFIGURED", "HRC reward contract ABI is unavailable");
    return { address, rpcUrl, privateKey, abi: artifact.abi, deployment, network };
}

function readSessionTuple(tuple) {
    return {
        beneficiary: tuple.beneficiary ?? tuple[0],
        startedAt: BigInt(tuple.startedAt ?? tuple[1]),
        settledAt: BigInt(tuple.settledAt ?? tuple[2]),
        stakedAmount: BigInt(tuple.stakedAmount ?? tuple[3]),
        rewardPaid: BigInt(tuple.rewardPaid ?? tuple[4]),
        penaltyCollected: BigInt(tuple.penaltyCollected ?? tuple[5]),
        outcome: Number(tuple.outcome ?? tuple[6]),
        settlementStatus: Number(tuple.settlementStatus ?? tuple[7]),
        evidenceHash: tuple.evidenceHash ?? tuple[8],
        outcomeTimestamp: BigInt(tuple.outcomeTimestamp ?? tuple[9]),
        started: Boolean(tuple.started ?? tuple[10]),
        settled: Boolean(tuple.settled ?? tuple[11])
    };
}

function amountToCoins(value) {
    return ethers.formatEther(value);
}

class RewardService {
    constructor({ provider, wallet, contract, transactionSender = null, waitConfirmations = 1 } = {}) {
        this.provider = provider;
        this.wallet = wallet;
        this.contract = contract;
        this.transactionSender = transactionSender;
        this.waitConfirmations = waitConfirmations;
    }

    static fromEnv(env = process.env) {
        const config = rewardConfig(env);
        const provider = createRpcProvider(config.rpcUrl, { cacheTimeout: -1 });
        const wallet = new ethers.Wallet(config.privateKey, provider);
        const contract = new ethers.Contract(config.address, config.abi, wallet);
        return new RewardService({ provider, wallet, contract });
    }

    async startSession(input) {
        return withSignerWrite(await this.wallet.getAddress(), () => this._startSession(input));
    }

    async _startSession(input) {
        if (!input || typeof input !== "object" || Array.isArray(input)) {
            throw new ApiError(400, "INVALID_PAYLOAD", "payload must be an object");
        }
        let sessionId;
        try { sessionId = identifier(input.sessionId, "sessionId"); }
        catch (error) { throw new ApiError(400, "INVALID_PAYLOAD", error.message); }
        const beneficiary = input.beneficiary;
        if (!ethers.isAddress(beneficiary) || beneficiary === ethers.ZeroAddress) {
            throw new ApiError(400, "INVALID_PAYLOAD", "beneficiary must be a non-zero address");
        }
        const key = sessionKey(sessionId);
        const existing = readSessionTuple(await this.contract.getSession(key));
        if (existing.started) {
            if (existing.beneficiary.toLowerCase() !== beneficiary.toLowerCase()) {
                throw new ApiError(409, "BENEFICIARY_MISMATCH", "Session beneficiary does not match the requested wallet");
            }
            return {
                success: true, duplicate: true, sessionId, sessionKey: key,
                beneficiary: existing.beneficiary, transactionHash: null, blockNumber: null
            };
        }
        try {
            const transaction = await this.contract.startSessionFor(key, beneficiary);
            const receipt = await transaction.wait(this.waitConfirmations);
            return {
                success: true, duplicate: false, sessionId, sessionKey: key, beneficiary,
                transactionHash: transaction.hash, blockNumber: receipt.blockNumber
            };
        } catch (error) {
            throw new ApiError(503, "REWARD_SESSION_START_FAILED", error.shortMessage || error.message || "Reward session start failed");
        }
    }

    async processSessionOutcome(input, options = {}) {
        return withSignerWrite(await this.wallet.getAddress(), () => this._processSessionOutcome(input, options));
    }

    async _processSessionOutcome(input, options) {
        if (!input || typeof input !== "object" || Array.isArray(input)) {
            throw new ApiError(400, "INVALID_PAYLOAD", "payload must be an object");
        }
        let sessionId;
        try { sessionId = identifier(input.sessionId, "sessionId"); }
        catch (error) { throw new ApiError(400, "INVALID_PAYLOAD", error.message); }
        let beneficiary = input.beneficiary;
        if (beneficiary !== undefined && (!ethers.isAddress(beneficiary) || beneficiary === ethers.ZeroAddress)) {
            throw new ApiError(400, "INVALID_PAYLOAD", "beneficiary must be a non-zero address");
        }
        let outcome;
        let evidence;
        try {
            outcome = normalizeOutcome(input.outcome);
            evidence = evidenceHash(input.evidenceHash);
        } catch (error) {
            throw new ApiError(400, "INVALID_PAYLOAD", error.message);
        }
        const timestamp = input.timestamp;
        if (!Number.isSafeInteger(timestamp) || timestamp <= 0) {
            throw new ApiError(400, "INVALID_PAYLOAD", "timestamp must be a positive safe integer");
        }
        const key = sessionKey(sessionId);
        const existing = readSessionTuple(await this.contract.getSession(key));
        if (!existing.started) throw new ApiError(409, "SESSION_NOT_STARTED", "Session stake was not started on-chain");
        beneficiary ||= existing.beneficiary;
        if (existing.beneficiary.toLowerCase() !== beneficiary.toLowerCase()) {
            throw new ApiError(409, "BENEFICIARY_MISMATCH", "Session beneficiary does not match the requested wallet");
        }
        if (existing.settled) {
            if (existing.outcome !== outcome.code || existing.evidenceHash.toLowerCase() !== evidence.toLowerCase()) {
                throw new ApiError(409, "SETTLEMENT_CONFLICT", "Session was already settled with different outcome/evidence");
            }
            return this._formatResult({ sessionId, key, beneficiary, outcome, evidence, timestamp: Number(existing.outcomeTimestamp), existing, duplicate: true });
        }

        let transaction;
        try {
            let receipt;
            const writer = options.transactionSender || this.transactionSender;
            if (writer) {
                transaction = await writer.send('settlement:' + sessionId, this.wallet, this.contract, 'submitSessionOutcome', [key, beneficiary, outcome.code, evidence, timestamp]);
                if (transaction.pending) return { success: true, httpStatus: 202, pending: true, sessionId, transactionHash: transaction.hash, outcome: outcome.name };
                receipt = transaction.receipt;
            } else {
                transaction = await this.contract.submitSessionOutcome(key, beneficiary, outcome.code, evidence, timestamp);
                receipt = await transaction.wait(this.waitConfirmations);
            }
            const after = readSessionTuple(await this.contract.getSession(key));
            return this._formatResult({ sessionId, key, beneficiary, outcome, evidence, timestamp, existing: after, transaction, receipt, duplicate: false });
        } catch (error) {
            if (error instanceof ApiError) throw error;
            throw new ApiError(503, "REWARD_SETTLEMENT_FAILED", error.shortMessage || error.message || "Reward settlement failed");
        }
    }

    _formatResult({ sessionId, key, beneficiary, outcome, evidence, timestamp, existing, transaction, receipt, duplicate }) {
        return {
            success: true,
            duplicate,
            policyVersion: POLICY_VERSION,
            sessionId,
            sessionKey: key,
            beneficiary,
            outcome: outcome.name,
            outcomeCode: outcome.code,
            settlementStatus: statusName(existing.settlementStatus),
            stakeReturned: amountToCoins(existing.stakedAmount - existing.penaltyCollected),
            rewardPaid: amountToCoins(existing.rewardPaid),
            penaltyCollected: amountToCoins(existing.penaltyCollected),
            evidenceHash: evidence,
            outcomeTimestamp: timestamp,
            transactionHash: transaction?.hash || null,
            blockNumber: receipt?.blockNumber ?? null
        };
    }
}

let singleton;
function getRewardService() {
    if (!singleton) singleton = RewardService.fromEnv();
    return singleton;
}

module.exports = { RewardService, getRewardService, rewardConfig, readSessionTuple };
