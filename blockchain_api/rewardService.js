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

    // ======================================================
    // OWNER-ONLY OPERATIONS
    // ======================================================

    async getOperatorStatus() {
        const [operator, owner, paused, poolBalance, maxRewardPool] = await Promise.all([
            this.wallet.getAddress(),
            this.contract.owner(),
            this.contract.paused(),
            this.contract.rewardPoolBalance(),
            this.contract.maxRewardPool()
        ]);
        return {
            success: true,
            operator,
            owner,
            isOwner: operator.toLowerCase() === owner.toLowerCase(),
            paused: Boolean(paused),
            rewardPoolBalance: ethers.formatEther(poolBalance),
            maxRewardPool: ethers.formatEther(maxRewardPool)
        };
    }

    async fundRewardPool(amount) {
        if (!/^\d+$/.test(String(amount)) || BigInt(amount) <= 0n) {
            throw new ApiError(400, "INVALID_PAYLOAD", "amount must be a positive wei value");
        }
        return withSignerWrite(await this.wallet.getAddress(), async () => {
            try {
                const transaction = await this.contract.fundRewardPool(BigInt(amount));
                const receipt = await transaction.wait(this.waitConfirmations);
                return { success: true, transactionHash: transaction.hash, blockNumber: receipt.blockNumber };
            } catch (error) {
                throw new ApiError(503, "REWARD_FUND_FAILED", error.shortMessage || error.message || "Funding the reward pool failed");
            }
        });
    }

    async setPaused(paused) {
        return withSignerWrite(await this.wallet.getAddress(), async () => {
            try {
                const transaction = await (paused ? this.contract.pause() : this.contract.unpause());
                const receipt = await transaction.wait(this.waitConfirmations);
                return { success: true, paused: Boolean(paused), transactionHash: transaction.hash, blockNumber: receipt.blockNumber };
            } catch (error) {
                throw new ApiError(503, "REWARD_PAUSE_FAILED", error.shortMessage || error.message || "Changing the pause state failed");
            }
        });
    }

    async setPolicy(policy) {
        const fields = ["stakeAmount", "rewardAmount", "penaltyAmount", "minRewardDuration", "fullBonusDuration", "maxRewardBonusPercent"];
        const values = fields.map(field => policy?.[field]);
        if (values.some(value => !/^\d+$/.test(String(value)) || BigInt(value) <= 0n)) {
            throw new ApiError(400, "INVALID_PAYLOAD", `policy requires positive integers: ${fields.join(", ")}`);
        }
        return withSignerWrite(await this.wallet.getAddress(), async () => {
            try {
                const transaction = await this.contract.setPolicy(...values.map(BigInt));
                const receipt = await transaction.wait(this.waitConfirmations);
                return { success: true, transactionHash: transaction.hash, blockNumber: receipt.blockNumber };
            } catch (error) {
                throw new ApiError(503, "REWARD_POLICY_FAILED", error.shortMessage || error.message || "Updating the policy failed");
            }
        });
    }

    /// @notice Penalties are paid straight to the penalty treasury wallet, so the
    ///         contract holds no accrued treasury balance. What the owner can
    ///         recover from the contract is only the surplus above the reward pool
    ///         and escrowed stake, for example tokens sent here by mistake.
    async sweepExcessToken(to, amount) {
        if (!ethers.isAddress(to)) throw new ApiError(400, "INVALID_PAYLOAD", "to must be an address");
        if (!/^\d+$/.test(String(amount)) || BigInt(amount) <= 0n) {
            throw new ApiError(400, "INVALID_PAYLOAD", "amount must be a positive wei value");
        }
        return withSignerWrite(await this.wallet.getAddress(), async () => {
            try {
                const transaction = await this.contract.sweepExcessToken(to, BigInt(amount));
                const receipt = await transaction.wait(this.waitConfirmations);
                return { success: true, transactionHash: transaction.hash, blockNumber: receipt.blockNumber };
            } catch (error) {
                throw new ApiError(503, "REWARD_SWEEP_FAILED", error.shortMessage || error.message || "Sweeping excess tokens failed");
            }
        });
    }

    async setPenaltyTreasury(treasury) {
        if (!ethers.isAddress(treasury)) throw new ApiError(400, "INVALID_PAYLOAD", "treasury must be an address");
        return withSignerWrite(await this.wallet.getAddress(), async () => {
            try {
                const transaction = await this.contract.setPenaltyTreasury(treasury);
                const receipt = await transaction.wait(this.waitConfirmations);
                return { success: true, treasury, transactionHash: transaction.hash, blockNumber: receipt.blockNumber };
            } catch (error) {
                throw new ApiError(503, "REWARD_TREASURY_FAILED", error.shortMessage || error.message || "Updating the penalty treasury failed");
            }
        });
    }

    /// @notice Treasury report for the admin surface: where penalties go, what the
    ///         treasury holds, and what is still locked in the contract.
    async getAdminStatus() {
        const status = await this.getOperatorStatus();
        const [treasury, tokenAddress] = await Promise.all([
            this.contract.penaltyTreasury(),
            this.contract.token()
        ]);
        const token = new ethers.Contract(tokenAddress, ["function balanceOf(address) view returns (uint256)"], this.provider);
        const [treasuryBalance, contractBalance] = await Promise.all([
            token.balanceOf(treasury),
            token.balanceOf(await this.contract.getAddress())
        ]);
        return {
            ...status,
            success: true,
            penaltyTreasury: treasury,
            token: tokenAddress,
            treasuryBalance: ethers.formatEther(treasuryBalance),
            contractTokenBalance: ethers.formatEther(contractBalance)
        };
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
        const endedAt = input.endedAt;
        if (!Number.isSafeInteger(endedAt) || endedAt <= 0) {
            throw new ApiError(400, "INVALID_PAYLOAD", "endedAt must be a positive Unix timestamp in seconds");
        }
        const key = sessionKey(sessionId);
        const existing = readSessionTuple(await this.contract.getSession(key));
        existing.endedAt = BigInt(await this.contract.getSessionEndTime(key));
        if (!existing.started) throw new ApiError(409, "SESSION_NOT_STARTED", "Session stake was not started on-chain");
        beneficiary ||= existing.beneficiary;
        if (existing.beneficiary.toLowerCase() !== beneficiary.toLowerCase()) {
            throw new ApiError(409, "BENEFICIARY_MISMATCH", "Session beneficiary does not match the requested wallet");
        }
        if (existing.settled) {
            if (existing.outcome !== outcome.code || existing.evidenceHash.toLowerCase() !== evidence.toLowerCase()
                || existing.endedAt !== BigInt(endedAt)) {
                throw new ApiError(409, "SETTLEMENT_CONFLICT", "Session was already settled with different outcome/evidence");
            }
            return this._formatResult({ sessionId, key, beneficiary, outcome, evidence, timestamp: Number(existing.outcomeTimestamp), endedAt: Number(existing.endedAt), existing, duplicate: true });
        }

        let transaction;
        try {
            let receipt;
            const writer = options.transactionSender || this.transactionSender;
            if (writer) {
                transaction = await writer.send('settlement:' + sessionId, this.wallet, this.contract, 'submitSessionOutcome', [key, beneficiary, outcome.code, evidence, timestamp, endedAt]);
                if (transaction.pending) return { success: true, httpStatus: 202, pending: true, sessionId, transactionHash: transaction.hash, outcome: outcome.name };
                receipt = transaction.receipt;
            } else {
                transaction = await this.contract.submitSessionOutcome(key, beneficiary, outcome.code, evidence, timestamp, endedAt);
                receipt = await transaction.wait(this.waitConfirmations);
            }
            const after = readSessionTuple(await this.contract.getSession(key));
            after.endedAt = BigInt(await this.contract.getSessionEndTime(key));
            return this._formatResult({ sessionId, key, beneficiary, outcome, evidence, timestamp, endedAt, existing: after, transaction, receipt, duplicate: false });
        } catch (error) {
            if (error instanceof ApiError) throw error;
            throw new ApiError(503, "REWARD_SETTLEMENT_FAILED", error.shortMessage || error.message || "Reward settlement failed");
        }
    }

    _formatResult({ sessionId, key, beneficiary, outcome, evidence, timestamp, endedAt, existing, transaction, receipt, duplicate }) {
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
            endedAt: endedAt ?? Number(existing.endedAt),
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
