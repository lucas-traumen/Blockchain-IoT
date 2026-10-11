require("dotenv").config();

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { ethers } = require("ethers");
const { ApiError, identifier } = require("./eventPayload");
const { createRpcProvider } = require("./rpcProvider");
const { sessionKey } = require("./rewardPolicy");
const { DemoSessionStore } = require("./demoSessionStore");
const { DurableContractWriter } = require("./durableContractWriter");
const { withSignerWrite } = require("./signerQueue");
const { DEMO_WALLETS, HARDHAT_MNEMONIC } = require("../demo_wallets");

const DEFAULT_DEPLOYMENT = path.join(__dirname, "../smart_contracts/deployments/reward-localhost.json");
const DEFAULT_TOKEN_ABI = path.join(__dirname, "../smart_contracts/artifacts/contracts/HRCRewardToken.sol/HRCRewardToken.json");
const DEFAULT_REWARD_ABI = path.join(__dirname, "../smart_contracts/artifacts/contracts/HRCSessionRewards.sol/HRCSessionRewards.json");
const ADMIN = Object.freeze({ userId: "admin", label: "Dashboard Admin", role: "admin" });
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
    "function startSession(bytes32)",
    "event SessionStarted(bytes32 indexed sessionId,address indexed beneficiary,uint256 amount,uint256 startedAt)",
    "event SessionSettled(bytes32 indexed sessionId,address indexed beneficiary,uint8 outcome,uint8 settlementStatus,uint256 stakeReturned,uint256 rewardPaid,uint256 penaltyCollected,bytes32 evidenceHash,uint256 timestamp)",
    "event RewardPaid(bytes32 indexed sessionId,address indexed beneficiary,uint256 amount,uint256 rewardDay)",
    "event PenaltyCollected(bytes32 indexed sessionId,address indexed beneficiary,uint256 amount,address indexed treasury)"
];

function readJson(file) {
    try { return JSON.parse(fs.readFileSync(file, "utf8")); } catch { return null; }
}

function deriveWallet(index, provider, env) {
    const mnemonic = env.HRC_DEMO_MNEMONIC || HARDHAT_MNEMONIC;
    return ethers.HDNodeWallet.fromPhrase(mnemonic, undefined, `m/44'/60'/0'/0/${index}`).connect(provider);
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

function readTuple(tuple) {
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

function formatEvent(parsed, log) {
    if (!parsed) return null;
    const args = parsed.args;
    const result = { name: parsed.name, transactionHash: log?.transactionHash || null, blockNumber: log?.blockNumber ?? null };
    if (parsed.name === "SessionStarted") {
        result.sessionKey = args.sessionId ?? args[0];
        result.beneficiary = args.beneficiary ?? args[1];
        result.amount = ethers.formatEther(args.amount ?? args[2]);
        result.startedAt = Number(args.startedAt ?? args[3]);
    } else if (parsed.name === "SessionSettled") {
        result.sessionKey = args.sessionId ?? args[0];
        result.beneficiary = args.beneficiary ?? args[1];
        result.outcomeCode = Number(args.outcome ?? args[2]);
        result.settlementStatusCode = Number(args.settlementStatus ?? args[3]);
        result.stakeReturned = ethers.formatEther(args.stakeReturned ?? args[4]);
        result.rewardPaid = ethers.formatEther(args.rewardPaid ?? args[5]);
        result.penaltyCollected = ethers.formatEther(args.penaltyCollected ?? args[6]);
        result.evidenceHash = args.evidenceHash ?? args[7];
        result.timestamp = Number(args.timestamp ?? args[8]);
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

class DemoWalletService {
    constructor({ provider, token, rewards, config, users, store, transactionSender = null, tokens = process.env } = {}) {
        this.provider = provider;
        this.token = token;
        this.rewards = rewards;
        this.config = config;
        this.users = users;
        this.store = store;
        this.transactionSender = transactionSender;
        this.tokens = tokens;
        this.rewardInterface = new ethers.Interface(REWARD_ABI);
        this.tokenInterface = new ethers.Interface(TOKEN_ABI);
        this.pendingWrites = new Map();
        this.sessionsByToken = new Map();
    }

    static fromEnv(env = process.env) {
        const deployment = readJson(env.HRC_REWARD_DEPLOYMENT || DEFAULT_DEPLOYMENT) || {};
        const tokenAddress = env.HRC_REWARD_TOKEN_ADDRESS || deployment.tokenAddress;
        const rewardAddress = env.HRC_REWARD_ADDRESS || deployment.rewardsAddress;
        if (!ethers.isAddress(tokenAddress) || !ethers.isAddress(rewardAddress)) {
            throw new ApiError(503, "REWARD_NOT_CONFIGURED", "HRC reward contract addresses are not configured");
        }
        const tokenArtifact = readJson(env.HRC_REWARD_TOKEN_ABI || DEFAULT_TOKEN_ABI);
        const rewardArtifact = readJson(env.HRC_REWARD_ABI || DEFAULT_REWARD_ABI);
        const provider = createRpcProvider(env.RPC_URL || "http://127.0.0.1:8545", { cacheTimeout: -1 });
        const users = new Map(DEMO_WALLETS.map(spec => {
            const wallet = deriveWallet(spec.derivationIndex, provider, env);
            return [spec.userId, { ...spec, role: "customer", wallet, walletAddress: wallet.address }];
        }));
        const config = {
            tokenAddress, rewardAddress,
            chainId: Number(deployment.chainId || env.HRC_APP_CHAIN_ID || 31337),
            deploymentTransactions: deployment.deployTxHashes || {},
            deploymentBlock: positiveSafeInteger(env.HRC_REWARD_DEPLOYMENT_BLOCK ?? deployment.deploymentBlock ?? 0, "HRC_REWARD_DEPLOYMENT_BLOCK"),
            tokenAbi: tokenArtifact?.abi || TOKEN_ABI,
            rewardAbi: rewardArtifact?.abi || REWARD_ABI
        };
        const token = new ethers.Contract(tokenAddress, config.tokenAbi, provider);
        const rewards = new ethers.Contract(rewardAddress, config.rewardAbi, provider);
        const store = new DemoSessionStore(env.HRC_DEMO_SESSION_STORE_PATH || path.join(__dirname, "data", "demo-sessions.json"));
        const transactionSender = new DurableContractWriter({ provider, store });
        return new DemoWalletService({ provider, token, rewards, config, users, store, transactionSender, tokens: env });
    }

    listAccounts() {
        return [ADMIN, ...[...this.users.values()].map(user => ({ userId: user.userId, label: user.label, role: user.role, walletAddress: user.walletAddress }))];
    }

    login(userId) {
        if (userId !== ADMIN.userId && !this.users.has(userId)) throw new ApiError(401, "UNKNOWN_DEMO_ACCOUNT", "Choose admin, user_1 or user_2");
        const user = userId === ADMIN.userId ? ADMIN : this.users.get(userId);
        const token = crypto.randomBytes(32).toString("base64url");
        this.sessionsByToken.set(token, { userId, createdAt: Date.now() });
        return { success: true, token, user: this.publicUser(user) };
    }

    logout(token) {
        this.sessionsByToken.delete(token);
        return { success: true };
    }

    authenticateToken(token) {
        const session = this.sessionsByToken.get(token);
        if (!session) return null;
        const user = session.userId === ADMIN.userId ? ADMIN : this.users.get(session.userId);
        return user ? { user: this.publicUser(user), session } : null;
    }

    publicUser(user) {
        return { userId: user.userId, label: user.label, role: user.role, walletAddress: user.walletAddress || null };
    }

    _user(userId) {
        const user = this.users.get(userId);
        if (!user) throw new ApiError(403, "DEMO_ACCOUNT_REQUIRED", "A demo user account is required");
        return user;
    }

    async _requireLocalChain() {
        let network;
        try { network = await this.provider.getNetwork(); }
        catch { throw new ApiError(503, "LOCAL_CHAIN_UNAVAILABLE", "Hardhat local RPC is unavailable. Transactions are paused; session references are preserved."); }
        if (Number(network.chainId) !== 31337) throw new ApiError(503, "LOCAL_CHAIN_REQUIRED", "Demo wallets require Hardhat local chain 31337");
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

    async _writeForUser(userId, operation) {
        this._user(userId);
        const previous = this.pendingWrites.get(userId) || Promise.resolve();
        const current = previous.catch(() => {}).then(async () => {
            await this._requireLocalChain();
            return withSignerWrite(this._user(userId).walletAddress, operation);
        });
        this.pendingWrites.set(userId, current);
        try { return await current; }
        finally { if (this.pendingWrites.get(userId) === current) this.pendingWrites.delete(userId); }
    }

    async getWallet(userId) {
        const user = this._user(userId);
        const [network, balance, allowance, stakeAmount, symbol, decimals] = await Promise.all([
            this._requireLocalChain(), this.token.balanceOf(user.walletAddress),
            this.token.allowance(user.walletAddress, this.config.rewardAddress),
            this.rewards.stakeAmount(), this.token.symbol(), this.token.decimals()
        ]);
        const tuples = await Promise.all(this.store.list(userId).map(record => this.rewards.getSession(sessionKey(record.sessionId))));
        const sessions = tuples.map(readTuple).filter(row => row.started && row.beneficiary.toLowerCase() === user.walletAddress.toLowerCase());
        const unsettled = sessions.filter(row => !row.settled);
        const locked = unsettled.reduce((sum, row) => sum + row.stakedAmount, 0n);
        const pending = this.store.list(userId).find(row => row.stage && !['SETTLED', 'START_FAILED'].includes(row.stage));
        const block = this.provider.getBlock ? await this.provider.getBlock('latest') : null;
        const day = Math.floor((block?.timestamp || Date.now() / 1000) / 86400);
        const rewardPaidToday = sessions.filter(row => row.settled && Math.floor(Number(row.settledAt) / 86400) === day).reduce((sum, row) => sum + row.rewardPaid, 0n);
        return {
            success: true, userId, address: user.walletAddress, walletAddress: user.walletAddress,
            chainId: Number(network.chainId), tokenAddress: this.config.tokenAddress,
            rewardsAddress: this.config.rewardAddress, symbol, decimals: Number(decimals),
            balance: ethers.formatUnits(balance, decimals), allowance: ethers.formatUnits(allowance, decimals),
            requiredAllowance: ethers.formatUnits(stakeAmount, decimals),
            stakeAmount: ethers.formatUnits(stakeAmount, decimals),
            lockedBalance: ethers.formatUnits(locked, decimals), activeSessionCount: unsettled.length,
            pendingSessionId: pending?.sessionId || null, rewardPaidToday: ethers.formatUnits(rewardPaidToday, decimals),
            canStart: balance >= stakeAmount && allowance >= stakeAmount && !unsettled.length && !pending
        };
    }

    approve(userId) { return this._writeForUser(userId, () => this._approve(userId)); }

    async _approve(userId) {
        const user = this._user(userId);
        if (this.transactionSender) await this.transactionSender.assertSignerReady(user.walletAddress);
        const stakeAmount = await this.rewards.stakeAmount();
        const allowance = await this.token.allowance(user.walletAddress, this.config.rewardAddress);
        if (allowance >= stakeAmount) {
            return { ...(await this.getWallet(userId)), approved: true, skipped: true, transactionHash: null, blockNumber: null, event: null };
        }
        const signerToken = this.token.connect(user.wallet);
        const transaction = await signerToken.approve(this.config.rewardAddress, stakeAmount);
        const receipt = await transaction.wait(1);
        const event = receipt.logs.map(log => {
            try { return log.address.toLowerCase() === this.config.tokenAddress.toLowerCase() ? log : null; } catch { return null; }
        }).filter(Boolean).map(log => {
            try {
                const parsed = signerToken.interface.parseLog({ topics: log.topics, data: log.data });
                return parsed?.name === "Approval" ? { name: parsed.name, owner: parsed.args.owner ?? parsed.args[0], spender: parsed.args.spender ?? parsed.args[1], amount: ethers.formatEther(parsed.args.amount ?? parsed.args[2]) } : null;
            } catch { return null; }
        }).find(Boolean) || null;
        return { ...(await this.getWallet(userId)), approved: true, skipped: false, transactionHash: transaction.hash, blockNumber: receipt.blockNumber, event };
    }

    _sessionId(userId, input = {}) {
        const supplied = input.sessionId;
        if (supplied) {
            const requested = identifier(supplied, "sessionId");
            if (requested.startsWith("demo-user_1-") || requested.startsWith("demo-user_2-")) {
                if (!requested.startsWith(`demo-${userId}-`)) throw new ApiError(403, "SESSION_OWNER_MISMATCH", "sessionId belongs to another demo user");
                return requested;
            }
            return `demo-${userId}-${normalizeSessionRef(requested)}`;
        }
        return `demo-${userId}-${normalizeSessionRef(input.sessionRef)}`;
    }

    async _readLogs(address, topics) {
        const latest = await this.provider.getBlockNumber();
        const logs = [];
        for (let fromBlock = this.config.deploymentBlock; fromBlock <= latest; fromBlock += 10000) {
            logs.push(...await this.provider.getLogs({ address, topics, fromBlock, toBlock: Math.min(fromBlock + 9999, latest) }));
        }
        return logs;
    }

    _rewardTopics() {
        return [
            ethers.id("SessionStarted(bytes32,address,uint256,uint256)"),
            ethers.id("SessionSettled(bytes32,address,uint8,uint8,uint256,uint256,uint256,bytes32,uint256)"),
            ethers.id("RewardPaid(bytes32,address,uint256,uint256)"),
            ethers.id("PenaltyCollected(bytes32,address,uint256,address)")
        ];
    }

    async _events(key) {
        const logs = await this._readLogs(this.config.rewardAddress, [this._rewardTopics(), key]);
        return logs.map(log => {
            try { return formatEvent(this.rewardInterface.parseLog({ topics: log.topics, data: log.data }), log); } catch { return null; }
        }).filter(Boolean);
    }

    async getTransactions(userId) {
        const user = this._user(userId);
        await this._requireLocalChain();
        const ownerTopic = ethers.zeroPadValue(user.walletAddress, 32);
        const transferTopic = ethers.id("Transfer(address,address,uint256)");
        const groups = await Promise.all([
            this._readLogs(this.config.tokenAddress, [ethers.id("Approval(address,address,uint256)"), ownerTopic, ethers.zeroPadValue(this.config.rewardAddress, 32)]),
            this._readLogs(this.config.rewardAddress, [this._rewardTopics(), null, ownerTopic]),
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
                    event = formatEvent(this.rewardInterface.parseLog(log), log);
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
        const row = readTuple(tuple);
        const settledEvent = events.find(event => event.name === "SessionSettled");
        const startedEvent = events.find(event => event.name === "SessionStarted");
        return {
            sessionId: record.sessionId, sessionRef: record.sessionRef, sessionKey: sessionKey(record.sessionId),
            userId: record.userId, beneficiary: row.beneficiary,
            started: row.started, settled: row.settled,
            startedAt: Number(row.startedAt), settledAt: Number(row.settledAt),
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
        if (!sessionId.startsWith(`demo-${userId}-`)) throw new ApiError(403, "SESSION_OWNER_MISMATCH", "session does not belong to this demo user");
        const record = this.store.get(sessionId);
        if (record && record.userId !== user.userId) throw new ApiError(403, "SESSION_OWNER_MISMATCH", "session does not belong to this demo user");
        const fallback = record || { sessionId, sessionRef: null, userId: user.userId, createdAt: new Date(0).toISOString() };
        const key = sessionKey(sessionId);
        const tuple = await this.rewards.getSession(key);
        if (!readTuple(tuple).started && !record?.stage) throw new ApiError(404, "SESSION_NOT_FOUND", "session has not started on-chain");
        const events = readTuple(tuple).started ? await this._events(key) : [];
        return { success: true, session: this._formatSession(fallback, tuple, events) };
    }

    startSession(userId, input = {}) { return this._writeForUser(userId, () => this._startSession(userId, input)); }

    async _startSession(userId, input = {}) {
        const user = this._user(userId);
        const sessionId = this._sessionId(userId, input);
        const key = sessionKey(sessionId);
        const tuple = readTuple(await this.rewards.getSession(key));
        if (tuple.started) {
            if (tuple.beneficiary.toLowerCase() !== user.walletAddress.toLowerCase()) throw new ApiError(409, "BENEFICIARY_MISMATCH", "session beneficiary does not match this demo user");
            const record = this.store.get(sessionId) || this.store.save({ sessionId, sessionRef: input.sessionRef || null, userId, createdAt: new Date().toISOString() });
            const events = await this._events(key);
            return { success: true, duplicate: true, ...this._formatSession(record, tuple, events), transactionHash: events.find(event => event.name === "SessionStarted")?.transactionHash || null };
        }
        const wallet = await this.getWallet(userId);
        if (Number(wallet.balance) < Number(wallet.stakeAmount)) throw new ApiError(409, "INSUFFICIENT_HRC_BALANCE", "demo wallet does not have enough HRC for the stake");
        if (Number(wallet.allowance) < Number(wallet.requiredAllowance)) throw new ApiError(409, "APPROVAL_REQUIRED", "approve the 2 HRC stake before starting a session");
        const record = this.store.get(sessionId) || this.store.save({ sessionId, sessionRef: input.sessionRef || null, userId, createdAt: new Date().toISOString() });
        let transaction;
        try {
            let receipt;
            if (this.transactionSender) {
                transaction = await this.transactionSender.send('stake:' + sessionId, user.wallet, this.rewards, 'startSession', [key]);
                if (transaction.pending) return { success: true, httpStatus: 202, pending: true, sessionId, transactionHash: transaction.hash, stage: 'START_PENDING' };
                receipt = transaction.receipt;
            } else {
                transaction = await this.rewards.connect(user.wallet).startSession(key);
                receipt = await transaction.wait(1);
            }
            const events = await this._events(key);
            const after = readTuple(await this.rewards.getSession(key));
            return { success: true, duplicate: false, ...this._formatSession(record, after, events), transactionHash: transaction.hash, blockNumber: receipt.blockNumber, event: events.find(event => event.name === "SessionStarted") || null };
        } catch (error) {
            if (error instanceof ApiError) throw error;
            throw new ApiError(503, "REWARD_SESSION_START_FAILED", error.shortMessage || error.message || "Reward session start failed");
        }
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

    async adminOverview() {
        const users = [];
        for (const spec of DEMO_WALLETS) {
            const wallet = await this.getWallet(spec.userId);
            const sessions = await this.listSessions(spec.userId);
            users.push({ userId: spec.userId, label: spec.label, role: "customer", wallet: { ...wallet, privateKey: undefined }, sessions: sessions.sessions });
        }
        return { success: true, chainId: this.config.chainId, users };
    }
}

let singleton;
function getDemoWalletService(env = process.env) {
    if (!singleton) singleton = DemoWalletService.fromEnv(env);
    return singleton;
}
function resetDemoWalletServiceForTests() { singleton = undefined; }

module.exports = { DemoWalletService, getDemoWalletService, resetDemoWalletServiceForTests, normalizeSessionRef };
