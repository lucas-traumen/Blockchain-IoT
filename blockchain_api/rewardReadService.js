require("dotenv").config();

const fs = require("node:fs");
const path = require("node:path");
const { ethers } = require("ethers");
const { createRpcProvider } = require("./rpcProvider");
const { OUTCOMES, SETTLEMENT_STATUS, sessionKey, statusName } = require("./rewardPolicy");
const { ApiError, identifier } = require("./eventPayload");
const { readSessionTuple } = require("./rewardService");
const { configuredNetwork, chainIdForNetwork } = require("./networkConfig");

const DEFAULT_REWARD_DEPLOYMENT = path.join(__dirname, "../smart_contracts/deployments/reward-localhost.json");
const DEFAULT_TOKEN_ABI_PATH = path.join(__dirname, "../smart_contracts/artifacts/contracts/HRCRewardToken.sol/HRCRewardToken.json");
const DEFAULT_REWARD_ABI_PATH = path.join(__dirname, "../smart_contracts/artifacts/contracts/HRCSessionRewards.sol/HRCSessionRewards.json");

const OUTCOME_NAMES = Object.fromEntries(Object.entries(OUTCOMES).map(([name, code]) => [code, name]));
const REWARD_EVENT_NAMES = new Set(["SessionStarted", "SessionSettled", "RewardPaid", "PenaltyCollected"]);

function readJson(file) {
    try { return JSON.parse(fs.readFileSync(file, "utf8")); }
    catch { return null; }
}

function readConfig(env = process.env) {
    const deployment = readJson(env.HRC_REWARD_DEPLOYMENT || DEFAULT_REWARD_DEPLOYMENT) || {};
    const network = configuredNetwork(env, deployment.network || "localhost");
    const tokenAddress = env.HRC_REWARD_TOKEN_ADDRESS || deployment.tokenAddress;
    const rewardsAddress = env.HRC_REWARD_ADDRESS || deployment.rewardsAddress;
    if (!ethers.isAddress(tokenAddress) || !ethers.isAddress(rewardsAddress)) {
        throw new ApiError(503, "REWARD_NOT_CONFIGURED", "HRC reward contract addresses are not configured");
    }
    const tokenArtifact = readJson(env.HRC_REWARD_TOKEN_ABI || DEFAULT_TOKEN_ABI_PATH);
    const rewardArtifact = readJson(env.HRC_REWARD_ABI || DEFAULT_REWARD_ABI_PATH);
    if (!tokenArtifact?.abi || !rewardArtifact?.abi) {
        throw new ApiError(503, "REWARD_NOT_CONFIGURED", "HRC reward contract ABI is unavailable");
    }
    const deploymentBlock = Number(env.HRC_REWARD_DEPLOYMENT_BLOCK ?? deployment.deploymentBlock ?? 0);
    if (!Number.isSafeInteger(deploymentBlock) || deploymentBlock < 0) {
        throw new ApiError(503, "REWARD_NOT_CONFIGURED", "HRC_REWARD_DEPLOYMENT_BLOCK must be a non-negative integer");
    }
    return {
        rpcUrl: env.RPC_URL || "http://127.0.0.1:8545",
        network,
        chainId: Number(deployment.chainId || chainIdForNetwork(network)),
        tokenAddress,
        rewardsAddress,
        deploymentBlock,
        dashboardBeneficiary: env.HRC_DASHBOARD_BENEFICIARY || null,
        tokenAbi: tokenArtifact.abi,
        rewardAbi: rewardArtifact.abi,
        deployment
    };
}

function addressOrNull(value) {
    return ethers.isAddress(value || "") ? ethers.getAddress(value) : null;
}

function bytes32OrNull(value) {
    return /^0x[0-9a-fA-F]{64}$/.test(value || "") ? value : null;
}

function named(args, name, index) {
    return args?.[name] ?? args?.[index];
}

function formatSession(sessionId, tuple, event = null, key = sessionKey(sessionId)) {
    const row = readSessionTuple(tuple);
    return {
        sessionId,
        sessionKey: key,
        beneficiary: row.beneficiary,
        startedAt: Number(row.startedAt),
        settledAt: Number(row.settledAt),
        stakedAmount: ethers.formatEther(row.stakedAmount),
        rewardPaid: ethers.formatEther(row.rewardPaid),
        penaltyCollected: ethers.formatEther(row.penaltyCollected),
        stakeReturned: ethers.formatEther(row.stakedAmount - row.penaltyCollected),
        outcome: row.started && row.settled ? (OUTCOME_NAMES[row.outcome] || "UNKNOWN") : null,
        outcomeCode: row.started && row.settled ? row.outcome : null,
        settlementStatus: row.started && row.settled ? statusName(row.settlementStatus) : null,
        evidenceHash: bytes32OrNull(row.evidenceHash),
        outcomeTimestamp: Number(row.outcomeTimestamp),
        started: row.started,
        settled: row.settled,
        transactionHash: event?.transactionHash || null,
        blockNumber: event?.blockNumber ?? null
    };
}

class RewardReadService {
    constructor({ provider, token, rewards, config } = {}) {
        this.provider = provider;
        this.token = token;
        this.rewards = rewards;
        this.config = config;
        this.rewardInterface = new ethers.Interface(config.rewardAbi);
        this._logsPromise = null;
    }

    static fromEnv(env = process.env) {
        const config = readConfig(env);
        const provider = createRpcProvider(config.rpcUrl);
        const token = new ethers.Contract(config.tokenAddress, config.tokenAbi, provider);
        const rewards = new ethers.Contract(config.rewardsAddress, config.rewardAbi, provider);
        return new RewardReadService({ provider, token, rewards, config });
    }

    async _parsedLogs() {
        if (!this._logsPromise) {
            this._logsPromise = this._loadLogs().finally(() => { this._logsPromise = null; });
        }
        return this._logsPromise;
    }

    async _loadLogs() {
        const latestBlock = await this.provider.getBlockNumber();
        if (latestBlock < this.config.deploymentBlock) return [];
        const logs = await this.provider.getLogs({
            address: this.config.rewardsAddress,
            fromBlock: this.config.deploymentBlock,
            toBlock: latestBlock
        });
        return logs.flatMap(log => {
            try {
                const parsed = this.rewardInterface.parseLog({ topics: log.topics, data: log.data });
                if (!parsed || !REWARD_EVENT_NAMES.has(parsed.name)) return [];
                return [{
                    eventName: parsed.name,
                    args: parsed.args,
                    transactionHash: log.transactionHash,
                    blockNumber: log.blockNumber,
                    logIndex: log.index ?? log.logIndex ?? 0
                }];
            } catch { return []; }
        });
    }

    async getSummary(beneficiary) {
        const address = this._beneficiary(beneficiary);
        const [network, balance, poolBalance, contractBalance, symbol, totalSupply, stake, reward, penalty] = await Promise.all([
            this.provider.getNetwork(),
            this.token.balanceOf(address),
            this.rewards.rewardPoolBalance(),
            this.token.balanceOf(this.config.rewardsAddress),
            this.token.symbol(),
            this.token.totalSupply(),
            this.rewards.stakeAmount(),
            this.rewards.rewardAmount(),
            this.rewards.penaltyAmount()
        ]);
        return {
            success: true,
            network: this.config.network,
            chainId: Number(network.chainId),
            tokenAddress: this.config.tokenAddress,
            rewardsAddress: this.config.rewardsAddress,
            beneficiary: address,
            symbol,
            balance: ethers.formatEther(balance),
            rewardPoolBalance: ethers.formatEther(poolBalance),
            rewardPoolTokenBalance: ethers.formatEther(contractBalance),
            totalSupply: ethers.formatEther(totalSupply),
            policy: {
                stake: ethers.formatEther(stake),
                reward: ethers.formatEther(reward),
                penalty: ethers.formatEther(penalty),
                dailyRewardLimit: 1
            }
        };
    }

    async getSession(sessionId) {
        const id = identifier(sessionId, "sessionId");
        const tuple = await this.rewards.getSession(sessionKey(id));
        const event = (await this._parsedLogs())
            .filter(item => item.eventName === "SessionSettled" && named(item.args, "sessionId", 0).toLowerCase() === sessionKey(id).toLowerCase())
            .sort((a, b) => b.blockNumber - a.blockNumber || b.logIndex - a.logIndex)[0] || null;
        return { success: true, session: formatSession(id, tuple, event) };
    }

    async getSessions({ beneficiary, limit = 100 } = {}) {
        const address = beneficiary ? this._beneficiary(beneficiary) : null;
        const count = this._limit(limit);
        const logs = (await this._parsedLogs())
            .filter(item => item.eventName === "SessionSettled")
            .filter(item => !address || named(item.args, "beneficiary", 1).toLowerCase() === address.toLowerCase())
            .sort((a, b) => b.blockNumber - a.blockNumber || b.logIndex - a.logIndex)
            .slice(0, count);
        const sessions = await Promise.all(logs.map(async event => {
            const key = named(event.args, "sessionId", 0);
            const tuple = await this.rewards.getSession(key);
            return formatSession("onchain-session", tuple, event, key);
        }));
        // Event logs contain bytes32 session keys, while the human session id is
        // not recoverable from keccak256. Keep the canonical key for on-chain
        // rows and let the IoT bridge provide the human session id in metadata.
        return { success: true, count: sessions.length, sessions: sessions.map((row, index) => ({
            ...row,
            sessionId: named(logs[index].args, "sessionId", 0),
            sessionKey: named(logs[index].args, "sessionId", 0)
        })) };
    }

    async getTransactions({ beneficiary, limit = 100 } = {}) {
        const address = beneficiary ? this._beneficiary(beneficiary) : null;
        const count = this._limit(limit);
        const transactions = (await this._parsedLogs())
            .filter(item => ["SessionSettled", "RewardPaid", "PenaltyCollected"].includes(item.eventName))
            .filter(item => !address || named(item.args, "beneficiary", 1).toLowerCase() === address.toLowerCase())
            .sort((a, b) => b.blockNumber - a.blockNumber || b.logIndex - a.logIndex)
            .slice(0, count)
            .map(item => ({
                transactionHash: item.transactionHash,
                blockNumber: item.blockNumber,
                eventType: item.eventName,
                sessionKey: named(item.args, "sessionId", 0),
                beneficiary: named(item.args, "beneficiary", 1) || null,
                outcome: item.eventName === "SessionSettled" ? OUTCOME_NAMES[Number(named(item.args, "outcome", 2))] || "UNKNOWN" : null,
                settlementStatus: item.eventName === "SessionSettled" ? statusName(Number(named(item.args, "settlementStatus", 3))) : null,
                amount: item.eventName === "RewardPaid" ? ethers.formatEther(named(item.args, "amount", 2))
                    : item.eventName === "PenaltyCollected" ? ethers.formatEther(named(item.args, "amount", 2)) : null
            }));
        return { success: true, count: transactions.length, transactions };
    }

    async getConfig() {
        let defaultBeneficiary = addressOrNull(this.config.dashboardBeneficiary);
        if (!defaultBeneficiary) {
            const logs = (await this._parsedLogs()).filter(item => ["SessionStarted", "SessionSettled"].includes(item.eventName));
            const latest = logs.sort((a, b) => b.blockNumber - a.blockNumber || b.logIndex - a.logIndex)[0];
            defaultBeneficiary = latest ? addressOrNull(named(latest.args, "beneficiary", 1)) : null;
        }
        const network = await this.provider.getNetwork();
        return {
            success: true,
            network: this.config.network,
            chainId: Number(network.chainId),
            rpcUrl: this.config.rpcUrl,
            tokenAddress: this.config.tokenAddress,
            rewardsAddress: this.config.rewardsAddress,
            defaultBeneficiary
        };
    }

    _limit(value) {
        const count = value === undefined || value === null || value === "" ? 100 : Number(value);
        if (!Number.isSafeInteger(count) || count < 1 || count > 500) {
            throw new ApiError(400, "INVALID_LIMIT", "limit must be an integer from 1 to 500");
        }
        return count;
    }

    _beneficiary(value) {
        if (!ethers.isAddress(value || "") || value === ethers.ZeroAddress) {
            throw new ApiError(400, "INVALID_BENEFICIARY", "beneficiary must be a non-zero address");
        }
        return ethers.getAddress(value);
    }
}

let singleton;
function getRewardReadService() {
    if (!singleton) singleton = RewardReadService.fromEnv();
    return singleton;
}

function resetRewardReadServiceForTests() { singleton = undefined; }

module.exports = { RewardReadService, getRewardReadService, resetRewardReadServiceForTests, readConfig, formatSession };
