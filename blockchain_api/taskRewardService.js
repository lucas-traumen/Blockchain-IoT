require("dotenv").config();

const { ethers } = require("ethers");
const { ApiError, identifier } = require("./eventPayload");
const { sessionKey, evidenceHash } = require("./rewardPolicy");
const { rewardConfig } = require("./rewardService");
const { createRpcProvider } = require("./rpcProvider");
const { operatorPrivateKey } = require("./operatorKey");
const { withSignerWrite } = require("./signerQueue");

function outcomeCode(value) {
    const outcome = typeof value === "string" ? value.toUpperCase() : value;
    if (outcome === "VALID_NO_VIOLATION" || outcome === "COMPLETED_SAFE" || outcome === 0) return 0;
    if (outcome === "VIOLATION_CONFIRMED" || outcome === 1) return 1;
    if (outcome === "SENSOR_FAULT" || outcome === 2) return 2;
    if (outcome === "INCOMPLETE" || outcome === 3) return 3;
    if (outcome === "OWNER_CANCELLED" || outcome === 4) return 4;
    if (outcome === "SYSTEM_CANCELLED" || outcome === 5) return 5;
    throw new ApiError(400, "INVALID_PAYLOAD", "outcome is not supported by task-rewards-v2");
}

function taskSession(tuple) {
    return {
        beneficiary: tuple.beneficiary ?? tuple[0],
        taskIdHash: tuple.taskIdHash ?? tuple[1],
        policyHash: tuple.policyHash ?? tuple[2],
        evidenceHash: tuple.evidenceHash ?? tuple[3],
        startedAt: BigInt(tuple.startedAt ?? tuple[4]),
        endedAt: BigInt(tuple.endedAt ?? tuple[5]),
        settledAt: BigInt(tuple.settledAt ?? tuple[6]),
        stakedAmount: BigInt(tuple.stakedAmount ?? tuple[7]),
        rewardPaid: BigInt(tuple.rewardPaid ?? tuple[8]),
        penaltyCollected: BigInt(tuple.penaltyCollected ?? tuple[9]),
        outcome: Number(tuple.outcome ?? tuple[10]),
        started: Boolean(tuple.started ?? tuple[11]),
        settled: Boolean(tuple.settled ?? tuple[12])
    };
}

class TaskRewardService {
    constructor({ provider, wallet, contract, transactionSender = null, waitConfirmations = 1 } = {}) {
        Object.assign(this, { provider, wallet, contract, transactionSender, waitConfirmations });
    }
    static fromEnv(env = process.env) {
        const config = rewardConfig(env);
        if (config.protocolVersion !== "task-rewards-v2") throw new ApiError(503, "REWARD_PROTOCOL_MISMATCH", "Task reward V2 deployment is not selected");
        const provider = createRpcProvider(config.rpcUrl, { cacheTimeout: -1 });
        const wallet = new ethers.Wallet(config.privateKey, provider);
        const contract = new ethers.Contract(config.address, config.abi, wallet);
        return new TaskRewardService({ provider, wallet, contract });
    }
    async getOperatorStatus() {
        const operator = await this.wallet.getAddress();
        const [owner, paused, rewardPoolBalance, maxRewardPool] = await Promise.all([
            this.contract.owner(), this.contract.paused(), this.contract.rewardPoolBalance(), this.contract.maxRewardPool()
        ]);
        return { success: true, operator, owner, isOwner: operator.toLowerCase() === owner.toLowerCase(),
            paused: Boolean(paused), rewardPoolBalance: ethers.formatEther(rewardPoolBalance), maxRewardPool: ethers.formatEther(maxRewardPool) };
    }
    async getAdminStatus() {
        const status = await this.getOperatorStatus();
        const [tokenAddress, treasury] = await Promise.all([this.contract.token(), this.contract.penaltyTreasury()]);
        const token = new ethers.Contract(tokenAddress, ["function balanceOf(address) view returns (uint256)"], this.provider);
        const [treasuryBalance, contractTokenBalance] = await Promise.all([
            token.balanceOf(treasury), token.balanceOf(await this.contract.getAddress())
        ]);
        return { ...status, success: true, token: tokenAddress, penaltyTreasury: treasury,
            treasuryBalance: ethers.formatEther(treasuryBalance), contractTokenBalance: ethers.formatEther(contractTokenBalance) };
    }
    async fundRewardPool(amount) {
        if (!/^\d+$/.test(String(amount)) || BigInt(amount) <= 0n) throw new ApiError(400, "INVALID_PAYLOAD", "amount must be a positive wei value");
        return withSignerWrite(await this.wallet.getAddress(), async () => {
            const tx = await this.contract.fundRewardPool(BigInt(amount));
            const receipt = await tx.wait(1);
            return { success: true, transactionHash: tx.hash, blockNumber: receipt.blockNumber };
        });
    }
    async setPaused(paused) {
        return withSignerWrite(await this.wallet.getAddress(), async () => {
            const tx = await (paused ? this.contract.pause() : this.contract.unpause());
            const receipt = await tx.wait(1);
            return { success: true, paused: Boolean(paused), transactionHash: tx.hash, blockNumber: receipt.blockNumber };
        });
    }
    async setPenaltyTreasury(treasury) {
        if (!ethers.isAddress(treasury)) throw new ApiError(400, "INVALID_PAYLOAD", "treasury must be an address");
        return withSignerWrite(await this.wallet.getAddress(), async () => {
            const tx = await this.contract.setPenaltyTreasury(treasury);
            const receipt = await tx.wait(1);
            return { success: true, treasury, transactionHash: tx.hash, blockNumber: receipt.blockNumber };
        });
    }
    async sweepExcessToken(to, amount) {
        if (!ethers.isAddress(to) || !/^\d+$/.test(String(amount)) || BigInt(amount) <= 0n) throw new ApiError(400, "INVALID_PAYLOAD", "to and positive wei amount are required");
        return withSignerWrite(await this.wallet.getAddress(), async () => {
            const tx = await this.contract.sweepExcessToken(to, BigInt(amount));
            const receipt = await tx.wait(1);
            return { success: true, transactionHash: tx.hash, blockNumber: receipt.blockNumber };
        });
    }
    async setPolicy() { throw new ApiError(409, "POLICY_IMMUTABLE", "Task reward economics are immutable in this contract version"); }
    async processSessionOutcome(input, options = {}) {
        return withSignerWrite(await this.wallet.getAddress(), () => this._processSessionOutcome(input, options));
    }
    async _processSessionOutcome(input, options = {}) {
        if (!input || typeof input !== "object" || Array.isArray(input)) throw new ApiError(400, "INVALID_PAYLOAD", "payload must be an object");
        const id = identifier(input.sessionId, "sessionId");
        if (!ethers.isAddress(input.beneficiary || "") || input.beneficiary === ethers.ZeroAddress) throw new ApiError(400, "INVALID_PAYLOAD", "beneficiary must be a non-zero address");
        const outcome = outcomeCode(input.outcome);
        const proof = evidenceHash(input.evidenceHash);
        if (!Number.isSafeInteger(input.endedAt) || input.endedAt <= 0) throw new ApiError(400, "INVALID_PAYLOAD", "endedAt must be Unix seconds");
        const key = sessionKey(id);
        const current = taskSession(await this.contract.sessions(key));
        if (!current.started) throw new ApiError(409, "SESSION_NOT_STARTED", "Task session was not started on-chain");
        if (current.beneficiary.toLowerCase() !== input.beneficiary.toLowerCase()) throw new ApiError(409, "BENEFICIARY_MISMATCH", "Session beneficiary differs from frozen evidence");
        if (current.settled) {
            if (current.outcome !== outcome || current.evidenceHash.toLowerCase() !== proof.toLowerCase() || current.endedAt !== BigInt(input.endedAt)) {
                throw new ApiError(409, "SETTLEMENT_CONFLICT", "V2 session is already settled with different evidence");
            }
            return this.result(id, key, current, proof, null, null, true);
        }
        const sender = options.transactionSender || this.transactionSender;
        let transaction;
        let receipt;
        try {
            if (sender) {
                transaction = await sender.send(`settlement:${id}`, this.wallet, this.contract, "submitOutcome",
                    [key, current.beneficiary, outcome, proof, input.endedAt]);
                if (transaction.pending) return { success: true, pending: true, httpStatus: 202, sessionId: id,
                    transactionHash: transaction.hash, outcomeCode: outcome, endedAt: input.endedAt };
                receipt = transaction.receipt;
            } else {
                transaction = await this.contract.submitOutcome(key, current.beneficiary, outcome, proof, input.endedAt);
                receipt = await transaction.wait(this.waitConfirmations);
            }
            const settled = taskSession(await this.contract.sessions(key));
            if (!settled.settled || settled.outcome !== outcome || settled.evidenceHash.toLowerCase() !== proof.toLowerCase()
                || settled.endedAt !== BigInt(input.endedAt) || settled.beneficiary.toLowerCase() !== input.beneficiary.toLowerCase()) {
                throw new ApiError(503, "SETTLEMENT_STATE_MISMATCH", "Confirmed reward state differs from the frozen settlement payload");
            }
            return this.result(id, key, settled, proof, transaction, receipt, false);
        } catch (error) {
            if (error instanceof ApiError) throw error;
            throw new ApiError(503, "REWARD_SETTLEMENT_FAILED", error.shortMessage || error.message || "Task reward settlement failed");
        }
    }
    result(sessionId, key, session, proof, transaction, receipt, duplicate) {
        const outcomes = ["COMPLETED_SAFE", "VIOLATION_CONFIRMED", "SENSOR_FAULT", "INCOMPLETE", "OWNER_CANCELLED", "SYSTEM_CANCELLED"];
        return { success: true, duplicate, policyVersion: "task-reward-v2", sessionId, sessionKey: key,
            beneficiary: session.beneficiary, outcome: outcomes[session.outcome] || "UNKNOWN", outcomeCode: session.outcome,
            stakeReturned: ethers.formatEther(session.stakedAmount - session.penaltyCollected),
            rewardPaid: ethers.formatEther(session.rewardPaid), penaltyCollected: ethers.formatEther(session.penaltyCollected),
            evidenceHash: proof, endedAt: Number(session.endedAt), settledAt: Number(session.settledAt),
            transactionHash: transaction?.hash || null, blockNumber: receipt?.blockNumber ?? null };
    }
}

module.exports = { TaskRewardService, outcomeCode, taskSession };
