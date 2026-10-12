require("dotenv").config();

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { ethers } = require("ethers");
const { ApiError, identifier } = require("./eventPayload");
const { createRpcProvider } = require("./rpcProvider");
const { sessionKey } = require("./rewardPolicy");
const { JsonSessionStore } = require("./sessionStore");
const { DurableContractWriter } = require("./durableContractWriter");
const { configuredNetwork, chainIdForNetwork } = require("./networkConfig");

const DEFAULT_DEPLOYMENT = path.join(__dirname, "../smart_contracts/deployments/reward-localhost.json");
const DEFAULT_TOKEN_ABI = path.join(__dirname, "../smart_contracts/artifacts/contracts/HRCRewardToken.sol/HRCRewardToken.json");
const DEFAULT_REWARD_ABI = path.join(__dirname, "../smart_contracts/artifacts/contracts/HRCSessionRewards.sol/HRCSessionRewards.json");
const DEFAULT_TASK_REWARD_ABI = path.join(__dirname, "../smart_contracts/artifacts/contracts/HRCTaskRewards.sol/HRCTaskRewards.json");
const TOKEN_ABI = [
    "function balanceOf(address) view returns (uint256)",
    "function allowance(address,address) view returns (uint256)",
    "function approve(address,uint256) returns (bool)",
    "function symbol() view returns (string)",
    "function decimals() view returns (uint8)",
    "event Approval(address indexed owner,address indexed spender,uint256 value)",
    "event Transfer(address indexed from,address indexed to,uint256 value)"
];
const REWARD_ABI = [
    "function stakeAmount() view returns (uint256)",
    "function getSession(bytes32) view returns (address,uint256,uint256,uint256,uint256,uint256,uint8,uint8,bytes32,uint256,bool,bool)",
    "function getSessionEndTime(bytes32) view returns (uint256)",
    "function startSession(bytes32)",
    "event SessionStarted(bytes32 indexed sessionId,address indexed beneficiary,uint256 amount,uint256 startedAt)",
    "event SessionSettled(bytes32 indexed sessionId,address indexed beneficiary,uint8 outcome,uint8 settlementStatus,uint256 stakeReturned,uint256 rewardPaid,uint256 penaltyCollected,bytes32 evidenceHash,uint256 timestamp,uint256 endedAt)",
    "event RewardPaid(bytes32 indexed sessionId,address indexed beneficiary,uint256 amount,uint256 rewardDay)",
    "event PenaltyCollected(bytes32 indexed sessionId,address indexed beneficiary,uint256 amount,address indexed treasury)"
];
const TASK_REWARD_ABI = [
    "function STAKE_AMOUNT() view returns (uint256)",
    "function sessions(bytes32) view returns (address,bytes32,bytes32,bytes32,uint64,uint64,uint64,uint128,uint128,uint128,uint8,bool,bool)",
    "function startSession(bytes32,bytes32,bytes32)",
    "function operators(address) view returns (bool)",
    "event SessionStarted(bytes32 indexed sessionId,bytes32 indexed taskIdHash,address indexed beneficiary,bytes32 policyHash,uint256 stakeAmount,uint256 startedAt)",
    "event SessionSettled(bytes32 indexed sessionId,bytes32 indexed taskIdHash,address indexed beneficiary,bytes32 policyHash,uint8 outcome,uint256 stakeReturned,uint256 rewardPaid,uint256 penaltyCollected,bytes32 evidenceHash,uint256 startedAt,uint256 endedAt,uint256 settledAt)"
];

function readJson(file) {
    try { return JSON.parse(fs.readFileSync(file, "utf8")); } catch { return null; }
}

function positiveSafeInteger(value, name) {
    const number = Number(value);
    if (!Number.isSafeInteger(number) || number < 0) throw new ApiError(503, "REWARD_NOT_CONFIGURED", `${name} must be a non-negative integer`);
    return number;
}

function normalizeSessionRef(value) {
    const ref = value === undefined || value === null || value === "" ? "default" : String(value);
    if (!/^[A-Za-z0-9_.:-]{1,80}$/.test(ref)) throw new ApiError(400, "INVALID_SESSION_REF", "sessionRef must contain 1-80 safe ASCII characters");
    return ref;
}

const SESSION_ID_PREFIX = "hrc-";
const SESSION_ID_SALT = "hrc-session-id-v1";
const LEGACY_SESSION_ID = /^demo-([A-Za-z0-9_.:-]{1,64})-/;

// Session identity no longer encodes the owner. Authorization is decided by the
// stored record's userId, not by parsing the identifier.
function legacySessionOwner(sessionId) {
    const match = LEGACY_SESSION_ID.exec(String(sessionId ?? ""));
    return match ? match[1] : null;
}

// Deterministic per (user, ref) so retries stay idempotent, but opaque: the
// identifier no longer reveals which wallet owns the session.
function newSessionId(userId, sessionRef) {
    const ref = normalizeSessionRef(sessionRef);
    const digest = crypto.createHmac("sha256", SESSION_ID_SALT)
        .update(`${userId}\u0000${ref}`, "utf8").digest("hex").slice(0, 8);
    return `${SESSION_ID_PREFIX}${digest}-${ref}`;
}

function readTuple(tuple, taskRewards = false) {
    if (taskRewards) return {
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
        settled: Boolean(tuple.settled ?? tuple[12]),
        settlementStatus: null,
        outcomeTimestamp: BigInt(tuple.endedAt ?? tuple[5])
    };
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

function formatEvent(parsed, log, taskRewards = false) {
    if (!parsed) return null;
    const args = parsed.args;
    const result = { name: parsed.name, transactionHash: log?.transactionHash || null, blockNumber: log?.blockNumber ?? null };
    if (parsed.name === "SessionStarted") {
        result.sessionKey = args.sessionId ?? args[0];
        result.taskIdHash = taskRewards ? (args.taskIdHash ?? args[1]) : null;
        result.beneficiary = args.beneficiary ?? args[taskRewards ? 2 : 1];
        result.policyHash = taskRewards ? (args.policyHash ?? args[3]) : null;
        result.amount = ethers.formatEther(args.amount ?? args.stakeAmount ?? args[taskRewards ? 4 : 2]);
        result.startedAt = Number(args.startedAt ?? args[taskRewards ? 5 : 3]);
    } else if (parsed.name === "SessionSettled") {
        result.sessionKey = args.sessionId ?? args[0];
        result.taskIdHash = taskRewards ? (args.taskIdHash ?? args[1]) : null;
        result.beneficiary = args.beneficiary ?? args[taskRewards ? 2 : 1];
        result.policyHash = taskRewards ? (args.policyHash ?? args[3]) : null;
        result.outcomeCode = Number(args.outcome ?? args[taskRewards ? 4 : 2]);
        result.settlementStatusCode = taskRewards ? null : Number(args.settlementStatus ?? args[3]);
        result.stakeReturned = ethers.formatEther(args.stakeReturned ?? args[taskRewards ? 5 : 4]);
        result.rewardPaid = ethers.formatEther(args.rewardPaid ?? args[taskRewards ? 6 : 5]);
        result.penaltyCollected = ethers.formatEther(args.penaltyCollected ?? args[taskRewards ? 7 : 6]);
        result.evidenceHash = args.evidenceHash ?? args[taskRewards ? 8 : 7];
        result.timestamp = Number(args.settledAt ?? args.timestamp ?? args[taskRewards ? 11 : 8]);
        result.endedAt = Number(args.endedAt ?? args[taskRewards ? 10 : 9]);
    } else if (parsed.name === "RewardPaid") {
        result.sessionKey = args.sessionId ?? args[0];
        result.beneficiary = args.beneficiary ?? args[1];
        result.amount = ethers.formatEther(args.amount ?? args[2]);
    } else if (parsed.name === "PenaltyCollected") {
        result.sessionKey = args.sessionId ?? args[0];
        result.beneficiary = args.beneficiary ?? args[1];
        result.amount = ethers.formatEther(args.amount ?? args[2]);
        result.treasury = args.treasury ?? args[3];
    }
    return result;
}

class WalletService {
    constructor({ provider, token, rewards, config, users, store, transactionSender = null, tokens = process.env, identityStore = null, legacyWallets = [] } = {}) {
        this.provider = provider;
        this.token = token;
        this.rewards = rewards;
        this.config = config;
        this.users = users || new Map();
        this.identityStore = identityStore;
        this.legacyWallets = legacyWallets;
        this.store = store;
        this.transactionSender = transactionSender;
        this.tokens = tokens;
        this.rewardInterface = new ethers.Interface(config.rewardAbi || REWARD_ABI);
        this.tokenInterface = new ethers.Interface(TOKEN_ABI);
    }

    static fromEnv(env = process.env, identityStore = null) {
        const deploymentPath = env.HRC_REWARD_DEPLOYMENT || (env.HRC_REWARD_PROTOCOL_VERSION === "task-rewards-v2"
            ? path.join(__dirname, "../smart_contracts/deployments/task-rewards-localhost.json") : DEFAULT_DEPLOYMENT);
        const deployment = readJson(deploymentPath) || {};
        const taskRewards = deployment.version === "task-rewards-v2";
        const tokenAddress = env.HRC_REWARD_TOKEN_ADDRESS || deployment.tokenAddress;
        const rewardAddress = env.HRC_REWARD_ADDRESS || deployment.rewardsAddress;
        if (!ethers.isAddress(tokenAddress) || !ethers.isAddress(rewardAddress)) {
            throw new ApiError(503, "REWARD_NOT_CONFIGURED", "HRC reward contract addresses are not configured");
        }
        const tokenArtifact = readJson(env.HRC_REWARD_TOKEN_ABI || DEFAULT_TOKEN_ABI);
        const rewardArtifact = readJson(env.HRC_REWARD_ABI || (taskRewards ? DEFAULT_TASK_REWARD_ABI : DEFAULT_REWARD_ABI));
        const networkName = configuredNetwork(env, deployment.network || "localhost");
        const chainId = Number(deployment.chainId || chainIdForNetwork(networkName));
        if (chainId !== chainIdForNetwork(networkName)) {
            throw new ApiError(503, "REWARD_NETWORK_MISMATCH", "Reward deployment chainId does not match HRC_BLOCKCHAIN_NETWORK");
        }
        const provider = createRpcProvider(env.RPC_URL || "http://127.0.0.1:8545", { cacheTimeout: -1 });
        const users = new Map();
        const config = {
            tokenAddress, rewardAddress,
            chainId,
            deploymentTransactions: deployment.deployTxHashes || deployment.deploymentTransactions || {},
            deploymentBlock: positiveSafeInteger(env.HRC_REWARD_DEPLOYMENT_BLOCK ?? deployment.deploymentBlock ?? 0, "HRC_REWARD_DEPLOYMENT_BLOCK"),
            tokenAbi: tokenArtifact?.abi || TOKEN_ABI,
            rewardAbi: rewardArtifact?.abi || (taskRewards ? TASK_REWARD_ABI : REWARD_ABI),
            rewardProtocolVersion: taskRewards ? "task-rewards-v2" : "session-rewards-v1"
        };
        const token = new ethers.Contract(tokenAddress, config.tokenAbi, provider);
        const rewards = new ethers.Contract(rewardAddress, config.rewardAbi, provider);
        // Preserve existing filenames on upgrade; new installations use wallet names.
        const legacySessionFile = path.join(__dirname, "data", "demo-sessions.json");
        const defaultSessionFile = path.join(__dirname, "data", "wallet-sessions.json");
        const legacyExists = fs.existsSync(legacySessionFile) || fs.existsSync(legacySessionFile.replace(/\.json$/, ".db"));
        const sessionFile = env.HRC_SESSION_STORE_PATH || env.HRC_DEMO_SESSION_STORE_PATH
            || (legacyExists ? legacySessionFile : defaultSessionFile);
        const store = env.HRC_APP_STORE !== "json"
            ? (() => { const { SqliteSessionStore } = require("./sqliteStores"); return new SqliteSessionStore(env.HRC_APP_SESSION_DB_PATH || sessionFile.replace(/\.json$/, ".db")); })()
            : new JsonSessionStore(sessionFile);
        const transactionSender = new DurableContractWriter({ provider, store, chainId });
        const service = new WalletService({ provider, token, rewards, config, users, store, transactionSender, tokens: env,
            identityStore, legacyWallets: deployment.demoWallets || [] });
        for (const user of identityStore?.listUsers() || []) service.walletUser(user.walletAddress);
        return service;
    }

    /// @notice Wallet-first onboarding. The user holds their own key, so the
    ///         backend never receives a signer for this account: it can only read
    ///         chain state and prepare transactions for the wallet to sign.
    walletUser(walletAddress) {
        const address = ethers.getAddress(walletAddress);
        const userId = "usr_" + crypto.createHash("sha256").update(address.toLowerCase()).digest("hex").slice(0, 24);
        const legacyUserId = "wallet_" + address.slice(2).toLowerCase();
        this.store.reassignOwner?.(legacyUserId, userId);
        // Old fixed-account ownership is transferred only to its manifest address.
        for (const legacy of this.legacyWallets) {
            if (ethers.isAddress(legacy.address) && legacy.address.toLowerCase() === address.toLowerCase()) {
                this.store.reassignOwner?.(legacy.userId, userId);
            }
        }
        const existing = this.users.get(userId);
        if (existing) return existing;
        const user = {
            userId,
            label: address.slice(0, 6) + "..." + address.slice(-4),
            role: "customer",
            wallet: null,
            walletAddress: address,
            walletFirst: true
        };
        this.users.set(userId, user);
        return user;
    }

    loginWithWallet(walletAddress) {
        const user = this.walletUser(walletAddress);
        return { success: true, user: this.publicUser(user) };
    }

    publicUser(user) {
        return {
            userId: user.userId,
            label: user.label,
            role: user.role,
            walletAddress: user.walletAddress || null,
            walletFirst: Boolean(user.walletFirst)
        };
    }

    _user(userId) {
        const user = this.users.get(userId);
        if (!user) {
            const identity = this.identityStore?.getUser(userId);
            if (identity?.walletAddress) return this.walletUser(identity.walletAddress);
            throw new ApiError(403, "WALLET_ACCOUNT_REQUIRED", "A verified wallet account is required");
        }
        return user;
    }

    isWalletFirst(userId) { return Boolean(this.users.get(userId)?.walletFirst); }

    async _requireConfiguredChain(userId) {
        let network;
        try { network = await this.provider.getNetwork(); }
        catch { throw new ApiError(503, "REWARD_CHAIN_UNAVAILABLE", "Configured reward RPC is unavailable. Transactions are paused; session references are preserved."); }
        const expectedChainId = Number(this.config.chainId);
        if (Number(network.chainId) !== expectedChainId) {
            throw new ApiError(503, "WRONG_CHAIN", `Wallet reward service requires configured chain ${expectedChainId}`);
        }
        const [tokenCode, rewardCode] = await Promise.all([
            this.provider.getCode(this.config.tokenAddress), this.provider.getCode(this.config.rewardAddress)
        ]);
        if (!tokenCode || tokenCode === "0x" || !rewardCode || rewardCode === "0x") {
            throw new ApiError(503, "REWARD_DEPLOYMENT_MISSING", "Configured reward contracts are not deployed on this chain");
        }
        const anchors = Object.entries(this.config.deploymentTransactions || {}).map(([name, hash]) => ({ hash,
            address: name === "token" ? this.config.tokenAddress : this.config.rewardAddress }));
        const confirmed = this.store.listTransactions().filter(row => row.status === "CONFIRMED")
            .sort((a, b) => b.blockNumber - a.blockNumber)[0];
        if (confirmed) anchors.push({ hash: confirmed.transactionHash, blockNumber: confirmed.blockNumber });
        for (const anchor of anchors) {
            const receipt = await this.provider.getTransactionReceipt(anchor.hash);
            if (!receipt || Number(receipt.status) !== 1 || (anchor.address && receipt.contractAddress?.toLowerCase() !== anchor.address.toLowerCase())
                || (anchor.blockNumber !== undefined && receipt.blockNumber !== anchor.blockNumber)) {
                throw new ApiError(503, "CHAIN_HISTORY_MISMATCH", "The RPC is missing the saved deployment or transaction history. Restore the existing chain before continuing.");
            }
        }
        return network;
    }

    async getWallet(userId) {
        const user = this._user(userId);
        const taskRewards = this.config.rewardProtocolVersion === "task-rewards-v2";
        const [network, balance, allowance, stakeAmount, symbol, decimals] = await Promise.all([
            this._requireConfiguredChain(userId), this.token.balanceOf(user.walletAddress),
            this.token.allowance(user.walletAddress, this.config.rewardAddress),
            taskRewards ? this.rewards.STAKE_AMOUNT() : this.rewards.stakeAmount(),
            this.token.symbol(), this.token.decimals()
        ]);
        const [rewardPoolBalance, safeReward, dailyRewardCap] = taskRewards
            ? await Promise.all([this.rewards.rewardPoolBalance(), this.rewards.SAFE_REWARD(), this.rewards.DAILY_REWARD_CAP()])
            : [null, null, null];
        const tuples = await Promise.all(this.store.list(userId).map(record => this._readSession(sessionKey(record.sessionId))));
        const sessions = tuples.map(tuple => readTuple(tuple, this.config.rewardProtocolVersion === "task-rewards-v2"))
            .filter(row => row.started && row.beneficiary.toLowerCase() === user.walletAddress.toLowerCase());
        const unsettled = sessions.filter(row => !row.settled);
        const locked = unsettled.reduce((sum, row) => sum + row.stakedAmount, 0n);
        const pending = this.store.list(userId).find(row => row.stage && !['SETTLED', 'START_FAILED', 'INTENT_EXPIRED'].includes(row.stage));
        const block = this.provider.getBlock ? await this.provider.getBlock('latest') : null;
        const day = Math.floor((block?.timestamp || Date.now() / 1000) / 86400);
        const rewardPaidToday = sessions.filter(row => row.settled && Math.floor(Number(row.settledAt) / 86400) === day).reduce((sum, row) => sum + row.rewardPaid, 0n);
        const onChainRewardPaidToday = taskRewards
            ? await this.rewards.rewardsPaidByDay(user.walletAddress, day) : rewardPaidToday;
        const canEarnSafeReward = !taskRewards || (rewardPoolBalance >= safeReward && onChainRewardPaidToday + safeReward <= dailyRewardCap);
        const startReason = balance < stakeAmount ? "INSUFFICIENT_HRC_BALANCE"
            : allowance < stakeAmount ? "APPROVAL_REQUIRED"
            : unsettled.length || pending ? "SESSION_IN_PROGRESS"
            : taskRewards && rewardPoolBalance < safeReward ? "REWARD_POOL_EMPTY" : null;
        return {
            success: true, userId, address: user.walletAddress, walletAddress: user.walletAddress,
            chainId: Number(network.chainId), tokenAddress: this.config.tokenAddress,
            rewardsAddress: this.config.rewardAddress, rewardProtocolVersion: this.config.rewardProtocolVersion,
            symbol, decimals: Number(decimals),
            balance: ethers.formatUnits(balance, decimals), allowance: ethers.formatUnits(allowance, decimals),
            requiredAllowance: ethers.formatUnits(stakeAmount, decimals),
            stakeAmount: ethers.formatUnits(stakeAmount, decimals),
            rewardPoolBalance: taskRewards ? ethers.formatUnits(rewardPoolBalance, decimals) : null,
            safeReward: taskRewards ? ethers.formatUnits(safeReward, decimals) : null,
            dailyRewardCap: taskRewards ? ethers.formatUnits(dailyRewardCap, decimals) : null,
            rewardPaidToday: ethers.formatUnits(taskRewards ? onChainRewardPaidToday : rewardPaidToday, decimals),
            canEarnSafeReward,
            canStartReason: startReason,
            lockedBalance: ethers.formatUnits(locked, decimals), activeSessionCount: unsettled.length,
            pendingSessionId: pending?.sessionId || null,
            canStart: balance >= stakeAmount && allowance >= stakeAmount && !unsettled.length && !pending
                && (!taskRewards || rewardPoolBalance >= safeReward)
        };
    }

    _sessionId(userId, input = {}) {
        const supplied = input.sessionId;
        if (supplied) {
            const requested = identifier(supplied, "sessionId");
            const legacyOwner = legacySessionOwner(requested);
            if (legacyOwner) {
                if (legacyOwner !== userId) throw new ApiError(403, "SESSION_OWNER_MISMATCH", "sessionId belongs to another user");
                return requested;
            }
            if (requested.startsWith(SESSION_ID_PREFIX)) return requested;
            return newSessionId(userId, requested);
        }
        return newSessionId(userId, input.sessionRef);
    }

    legacySessionOwner(sessionId) { return legacySessionOwner(sessionId); }

    async _readLogs(address, topics) {
        const latest = await this.provider.getBlockNumber();
        const logs = [];
        for (let fromBlock = this.config.deploymentBlock; fromBlock <= latest; fromBlock += 10000) {
            logs.push(...await this.provider.getLogs({ address, topics, fromBlock, toBlock: Math.min(fromBlock + 9999, latest) }));
        }
        return logs;
    }

    _rewardTopics() {
        if (this.config.rewardProtocolVersion === "task-rewards-v2") return [
            ethers.id("SessionStarted(bytes32,bytes32,address,bytes32,uint256,uint256)"),
            ethers.id("SessionSettled(bytes32,bytes32,address,bytes32,uint8,uint256,uint256,uint256,bytes32,uint256,uint256,uint256)")
        ];
        return [
            ethers.id("SessionStarted(bytes32,address,uint256,uint256)"),
            ethers.id("SessionSettled(bytes32,address,uint8,uint8,uint256,uint256,uint256,bytes32,uint256,uint256)"),
            ethers.id("RewardPaid(bytes32,address,uint256,uint256)"),
            ethers.id("PenaltyCollected(bytes32,address,uint256,address)")
        ];
    }

    async _events(key) {
        const logs = await this._readLogs(this.config.rewardAddress, [this._rewardTopics(), key]);
        return logs.map(log => {
            try { return formatEvent(this.rewardInterface.parseLog({ topics: log.topics, data: log.data }), log,
                this.config.rewardProtocolVersion === "task-rewards-v2"); } catch { return null; }
        }).filter(Boolean);
    }

    _readSession(key) {
        return this.config.rewardProtocolVersion === "task-rewards-v2"
            ? this.rewards.sessions(key) : this.rewards.getSession(key);
    }

    async getTransactions(userId) {
        const user = this._user(userId);
        await this._requireConfiguredChain(userId);
        const ownerTopic = ethers.zeroPadValue(user.walletAddress, 32);
        const transferTopic = ethers.id("Transfer(address,address,uint256)");
        const rewardOwnerTopics = this.config.rewardProtocolVersion === "task-rewards-v2"
            ? [this._rewardTopics(), null, null, ownerTopic] : [this._rewardTopics(), null, ownerTopic];
        const groups = await Promise.all([
            this._readLogs(this.config.tokenAddress, [ethers.id("Approval(address,address,uint256)"), ownerTopic, ethers.zeroPadValue(this.config.rewardAddress, 32)]),
            this._readLogs(this.config.rewardAddress, rewardOwnerTopics),
            this._readLogs(this.config.tokenAddress, [transferTopic, ownerTopic]),
            this._readLogs(this.config.tokenAddress, [transferTopic, null, ownerTopic])
        ]);
        const unique = new Map();
        for (const log of groups.flat()) unique.set(log.transactionHash + ':' + (log.index ?? log.logIndex ?? log.topics.join(':')), log);
        const transactions = new Map();
        const refs = new Map(this.store.list(userId).map(record => [sessionKey(record.sessionId), record]));
        for (const log of [...unique.values()].sort((a, b) => a.blockNumber - b.blockNumber || (a.index ?? a.logIndex ?? 0) - (b.index ?? b.logIndex ?? 0))) {
            let event;
            try {
                if (log.address.toLowerCase() === this.config.tokenAddress.toLowerCase()) {
                    const parsed = this.tokenInterface.parseLog(log);
                    if (parsed?.name === 'Approval') {
                        if (parsed.args.owner.toLowerCase() !== user.walletAddress.toLowerCase() || parsed.args.spender.toLowerCase() !== this.config.rewardAddress.toLowerCase()) continue;
                        event = { name: 'Approval', owner: parsed.args.owner, spender: parsed.args.spender, amount: ethers.formatEther(parsed.args.value) };
                    } else if (parsed?.name === 'Transfer') {
                        const incoming = parsed.args.to.toLowerCase() === user.walletAddress.toLowerCase();
                        const outgoing = parsed.args.from.toLowerCase() === user.walletAddress.toLowerCase();
                        if (!incoming && !outgoing) continue;
                        event = { name: 'Transfer', from: parsed.args.from, to: parsed.args.to, amount: ethers.formatEther(parsed.args.value),
                            walletDelta: ethers.formatEther((incoming ? parsed.args.value : 0n) - (outgoing ? parsed.args.value : 0n)) };
                    } else continue;
                } else {
                    event = formatEvent(this.rewardInterface.parseLog(log), log,
                        this.config.rewardProtocolVersion === "task-rewards-v2");
                    if (!event || event.beneficiary.toLowerCase() !== user.walletAddress.toLowerCase()) continue;
                }
            } catch { continue; }
            if (!transactions.has(log.transactionHash)) transactions.set(log.transactionHash, { transactionHash: log.transactionHash, blockNumber: log.blockNumber, events: [] });
            transactions.get(log.transactionHash).events.push(event);
        }
        const rows = [...transactions.values()].map(transaction => {
            const start = transaction.events.find(event => event.name === 'SessionStarted');
            const settled = transaction.events.find(event => event.name === 'SessionSettled');
            const approval = transaction.events.find(event => event.name === 'Approval');
            const delta = transaction.events.filter(event => event.name === 'Transfer').reduce((sum, event) => sum + ethers.parseEther(event.walletDelta), 0n);
            const record = refs.get((settled || start)?.sessionKey);
            return { ...transaction, action: settled ? 'SETTLEMENT' : start ? 'STAKE' : approval ? 'APPROVE' : 'TRANSFER',
                sessionId: record?.sessionId || null, source: record?.source || 'legacy', walletDelta: ethers.formatEther(delta),
                lockedDelta: ethers.formatEther(start ? ethers.parseEther(start.amount) : settled ? -ethers.parseEther(settled.stakeReturned) - ethers.parseEther(settled.penaltyCollected) : 0n),
                stakeReturned: settled?.stakeReturned || '0.0', rewardPaid: settled?.rewardPaid || '0.0', penaltyCollected: settled?.penaltyCollected || '0.0',
                allowance: approval?.amount ?? null, outcomeCode: settled?.outcomeCode ?? null, settlementStatusCode: settled?.settlementStatusCode ?? null };
        }).sort((a, b) => b.blockNumber - a.blockNumber);
        return { success: true, userId, count: rows.length, transactions: rows };
    }

    _formatSession(record, tuple, events) {
        const row = readTuple(tuple, this.config.rewardProtocolVersion === "task-rewards-v2");
        const settledEvent = events.find(event => event.name === "SessionSettled");
        const startedEvent = events.find(event => event.name === "SessionStarted");
        return {
            sessionId: record.sessionId, sessionRef: record.sessionRef, sessionKey: sessionKey(record.sessionId),
            userId: record.userId, beneficiary: row.beneficiary,
            taskId: record.taskId || null, assignmentId: record.assignmentId || null,
            taskSnapshot: record.taskSnapshot || null, policySnapshot: record.policySnapshot || null,
            rewardProtocolVersion: this.config.rewardProtocolVersion || 'session-rewards-v1',
            started: row.started, settled: row.settled,
            startedAt: Number(row.startedAt), endedAt: Number(row.endedAt || settledEvent?.endedAt || 0), settledAt: Number(row.settledAt),
            stakedAmount: ethers.formatEther(row.stakedAmount), rewardPaid: ethers.formatEther(row.rewardPaid),
            penaltyCollected: ethers.formatEther(row.penaltyCollected),
            stakeReturned: row.settled ? ethers.formatEther(row.stakedAmount - row.penaltyCollected) : "0.0",
            outcomeCode: row.settled ? row.outcome : null, settlementStatusCode: row.settled ? row.settlementStatus : null,
            evidenceHash: row.settled ? row.evidenceHash : null, outcomeTimestamp: Number(row.outcomeTimestamp),
            transactionHash: settledEvent?.transactionHash || startedEvent?.transactionHash || null,
            blockNumber: settledEvent?.blockNumber ?? startedEvent?.blockNumber ?? null,
            events, source: record.source || 'legacy', stage: record.stage || (row.settled ? 'SETTLED' : 'LEGACY_UNLINKED'),
            iotSession: record.iotSession || null, endRequested: Boolean(record.endRequested),
            outcomeReason: record.evidence?.result?.reason || null, recoveryReason: record.recoveryReason || null,
            durationS: record.evidence?.result?.durationS ?? record.iotSession?.durationS ?? null,
            sampleCount: record.evidence?.result?.sampleCount ?? record.iotSession?.sample_count ?? 0,
            lastError: record.lastError || null, nextRetryAt: record.nextRetryAt || null
        };
    }

    async getSession(userId, inputSessionId) {
        const user = this._user(userId);
        const sessionId = identifier(inputSessionId, "sessionId");
        const record = this.store.get(sessionId);
        if (record) {
            if (record.userId !== user.userId) throw new ApiError(403, "SESSION_OWNER_MISMATCH", "session does not belong to this user");
        } else if (legacySessionOwner(sessionId) && legacySessionOwner(sessionId) !== user.userId) {
            throw new ApiError(403, "SESSION_OWNER_MISMATCH", "session does not belong to this user");
        }
        const fallback = record || { sessionId, sessionRef: null, userId: user.userId, createdAt: new Date(0).toISOString() };
        const key = sessionKey(sessionId);
        const tuple = await this._readSession(key);
        const row = readTuple(tuple, this.config.rewardProtocolVersion === "task-rewards-v2");
        if (!row.started && !record?.stage) throw new ApiError(404, "SESSION_NOT_FOUND", "session has not started on-chain");
        const events = row.started ? await this._events(key) : [];
        return { success: true, session: this._formatSession(fallback, tuple, events) };
    }

    async listSessions(userId) {
        this._user(userId);
        const records = this.store.list(userId);
        const sessions = [];
        for (const record of records) {
            try { sessions.push((await this.getSession(userId, record.sessionId)).session); }
            catch (error) { if (error.code !== "SESSION_NOT_FOUND") throw error; }
        }
        return { success: true, count: sessions.length, sessions };
    }


}

let singleton;
function getWalletService(env = process.env, identityStore = null) {
    if (!singleton) singleton = WalletService.fromEnv(env, identityStore);
    return singleton;
}
function resetWalletServiceForTests() { singleton = undefined; }

module.exports = { WalletService, getWalletService, resetWalletServiceForTests, normalizeSessionRef, legacySessionOwner, newSessionId };
