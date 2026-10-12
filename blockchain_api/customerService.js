const crypto = require("node:crypto");
const path = require("node:path");
const { ethers } = require("ethers");
const { ApiError, identifier } = require("./eventPayload");
const { CustomerStore } = require("./customerStore");
const { configuredNetwork, chainIdForNetwork } = require("./networkConfig");
const { buildSiweMessage, parseSiweMessage } = require("./siweMessage");

const AUTH_MODES = new Set(["siwe"]);

function now() {
    return new Date().toISOString();
}

function hash(value) {
    return crypto.createHash("sha256").update(value).digest("hex");
}

function address(value) {
    if (!ethers.isAddress(value || "") || value === ethers.ZeroAddress) {
        throw new ApiError(400, "INVALID_WALLET", "walletAddress must be a non-zero EVM address");
    }
    return ethers.getAddress(value).toLowerCase();
}

function safeChainId(value) {
    const chainId = Number(value);
    if (!Number.isSafeInteger(chainId) || chainId < 1) {
        throw new ApiError(400, "INVALID_CHAIN_ID", "chainId must be a positive integer");
    }
    return chainId;
}

function parseLimit(value) {
    const limit = value === undefined ? 100 : Number(value);
    if (!Number.isSafeInteger(limit) || limit < 1 || limit > 500) {
        throw new ApiError(400, "INVALID_LIMIT", "limit must be an integer from 1 to 500");
    }
    return limit;
}

function positiveTtl(value, name) {
    if (!Number.isSafeInteger(value) || value < 1000 || value > 30 * 86400000) {
        throw new Error(name + " must be between 1 second and 30 days");
    }
    return value;
}

function canonical(value) {
    if (Array.isArray(value)) return value.map(canonical);
    if (value && typeof value === "object") {
        return Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])]));
    }
    return value;
}

function validateMetadata(value, depth = 0) {
    if (depth > 6) throw new ApiError(400, "INVALID_BEHAVIOR_METADATA", "metadata nesting exceeds 6 levels");
    if (value && typeof value === "object") {
        for (const [key, child] of Object.entries(value)) {
            if (["__proto__", "constructor", "prototype"].includes(key)) {
                throw new ApiError(400, "INVALID_BEHAVIOR_METADATA", "metadata contains a reserved key");
            }
            validateMetadata(child, depth + 1);
        }
    }
}

function publicUser(user) {
    return {
        userId: user.userId,
        walletAddress: ethers.getAddress(user.walletAddress),
        role: user.role,
        status: user.status,
        createdAt: user.createdAt,
        lastSeenAt: user.lastSeenAt,
        behaviorCount: user.behaviorCount,
        profile: user.profile
    };
}

class CustomerService {
    constructor({ store, chainId = 31337, challengeTtlMs = 300000, sessionTtlMs = 86400000, adminWallets = [], appOrigin = "http://localhost:8081", authMode = "siwe" } = {}) {
        this.store = store;
        this.chainId = safeChainId(chainId);
        this.challengeTtlMs = positiveTtl(challengeTtlMs, "challengeTtlMs");
        this.sessionTtlMs = positiveTtl(sessionTtlMs, "sessionTtlMs");
        if (!AUTH_MODES.has(authMode)) throw new Error("HRC_APP_AUTH_MODE must be siwe");
        this.authMode = authMode;
        const origin = new URL(appOrigin);
        if (!["http:", "https:"].includes(origin.protocol) || origin.username || origin.password) throw new Error("HRC_APP_ORIGIN must be an HTTP(S) origin");
        this.appOrigin = origin.origin;
        this.adminWallets = new Set(adminWallets.map(item => address(item)));
    }

    static fromEnv(env = process.env) {
        const file = env.HRC_CUSTOMER_STORE_PATH || path.join(__dirname, "data", "customers.json");
        const configuredAdmins = String(env.HRC_APP_ADMIN_WALLETS || "")
            .split(",").map(value => value.trim()).filter(Boolean);
        const network = configuredNetwork(env);
        const store = env.HRC_APP_STORE !== "json"
            ? (() => { const { SqliteCustomerStore } = require("./sqliteStores"); return new SqliteCustomerStore(env.HRC_APP_DB_PATH || file.replace(/\.json$/, ".db")); })()
            : new CustomerStore(file);
        const service = new CustomerService({
            store,
            chainId: Number(env.HRC_APP_CHAIN_ID || chainIdForNetwork(network)),
            challengeTtlMs: Number(env.HRC_AUTH_CHALLENGE_TTL_S || 300) * 1000,
            sessionTtlMs: Number(env.HRC_APP_SESSION_TTL_S || 86400) * 1000,
            adminWallets: configuredAdmins,
            appOrigin: env.HRC_APP_ORIGIN || "http://localhost:8081",
            authMode: env.HRC_APP_AUTH_MODE || "siwe"
        });
        process.once("exit", () => store.close());
        return service;
    }

    createChallenge(input) {
        const walletAddress = address(input?.walletAddress);
        const chainId = safeChainId(input?.chainId);
        if (chainId !== this.chainId) {
            throw new ApiError(400, "WRONG_NETWORK", `wallet must use chain ${this.chainId}`);
        }
        const issuedAt = now();
        const expiresAt = new Date(Date.now() + this.challengeTtlMs).toISOString();
        const nonce = crypto.randomBytes(24).toString("hex");
        const message = buildSiweMessage({
            domain: new URL(this.appOrigin).host, address: walletAddress, uri: this.appOrigin,
            chainId, nonce, issuedAt, expirationTime: expiresAt
        });
        this.store.saveChallenge({ walletAddress, chainId, nonce, message, issuedAt, expiresAt, used: false });
        return { success: true, mode: this.authMode, walletAddress: ethers.getAddress(walletAddress), chainId, message, expiresAt };
    }

    authenticateWallet(input) {
        const walletAddress = address(input?.walletAddress);
        const chainId = safeChainId(input?.chainId);
        const message = input?.message;
        const signature = input?.signature;
        if (typeof message !== "string" || message.length < 40 || message.length > 2000) {
            throw new ApiError(400, "INVALID_MESSAGE", "message is invalid");
        }
        if (typeof signature !== "string" || !signature.startsWith("0x")) {
            throw new ApiError(400, "INVALID_SIGNATURE", "signature is required");
        }
        if (chainId !== this.chainId) throw new ApiError(400, "WRONG_NETWORK", `wallet must use chain ${this.chainId}`);
        const challenge = this.store.getChallenge(walletAddress, message);
        if (!challenge || challenge.used || challenge.message !== message) {
            throw new ApiError(401, "CHALLENGE_INVALID", "wallet challenge is missing, used or does not match");
        }
        if (Date.parse(challenge.expiresAt) <= Date.now()) {
            throw new ApiError(401, "CHALLENGE_EXPIRED", "wallet challenge has expired");
        }
        this.verifySiweChallenge(message, challenge, walletAddress, chainId);
        let recovered;
        try { recovered = ethers.verifyMessage(message, signature).toLowerCase(); }
        catch { throw new ApiError(401, "SIGNATURE_INVALID", "wallet signature could not be verified"); }
        if (recovered !== walletAddress) throw new ApiError(401, "SIGNATURE_INVALID", "wallet signature does not match walletAddress");
        const existingUser = this.store.findUserByWallet(walletAddress);
        if (existingUser && existingUser.status !== "active") throw new ApiError(403, "USER_INACTIVE", "Application account is inactive");
        this.store.markChallengeUsed({ ...challenge, authenticatedAt: now() });

        const timestamp = now();
        const userId = "usr_" + hash(walletAddress).slice(0, 24);
        const existing = this.store.getUser(userId);
        const expiresAt = new Date(Date.now() + this.sessionTtlMs).toISOString();
        const user = {
            userId,
            walletAddress,
            role: this.adminWallets.has(walletAddress) ? "admin" : "customer",
            status: existing?.status || "active",
            createdAt: existing?.createdAt || timestamp,
            lastSeenAt: timestamp,
            behaviorCount: existing?.behaviorCount || 0,
            profile: existing?.profile || { displayName: null }
        };
        this.store.upsertUser(user);
        const token = crypto.randomBytes(32).toString("base64url");
        this.store.saveSession({ tokenHash: hash(token), userId, createdAt: timestamp,
            expiresAt });
        return { success: true, mode: this.authMode, token, expiresAt, user: publicUser(user) };
    }

    verifySiweChallenge(message, challenge, walletAddress, chainId) {
        const parsed = parseSiweMessage(message);
        if (parsed.address.toLowerCase() !== walletAddress) {
            throw new ApiError(401, "SIGNATURE_INVALID", "SIWE address does not match walletAddress");
        }
        if (parsed.chainId !== chainId) throw new ApiError(400, "WRONG_NETWORK", `wallet must use chain ${this.chainId}`);
        if (parsed.domain !== new URL(this.appOrigin).host) {
            throw new ApiError(401, "CHALLENGE_INVALID", "SIWE domain does not match this application origin");
        }
        if (parsed.uri !== this.appOrigin) {
            throw new ApiError(401, "CHALLENGE_INVALID", "SIWE URI does not match this application origin");
        }
        if (parsed.nonce !== challenge.nonce) {
            throw new ApiError(401, "CHALLENGE_INVALID", "SIWE nonce does not match the issued challenge");
        }
        if (Date.parse(parsed.issuedAt) !== Date.parse(challenge.issuedAt)) {
            throw new ApiError(401, "CHALLENGE_INVALID", "SIWE issued-at does not match the issued challenge");
        }
        if (parsed.expirationTime && Date.parse(parsed.expirationTime) <= Date.now()) {
            throw new ApiError(401, "CHALLENGE_EXPIRED", "SIWE message has expired");
        }
    }

    authenticateToken(token) {
        if (typeof token !== "string" || token.length < 20) return null;
        const session = this.store.getSession(hash(token));
        if (!session || Date.parse(session.expiresAt) <= Date.now()) return null;
        const user = this.store.getUser(session.userId);
        if (!user || user.status !== "active") return null;
        user.role = this.adminWallets.has(user.walletAddress) ? "admin" : "customer";
        return { session, user };
    }

    logout(session) {
        this.store.revokeSession(session.tokenHash);
        return { success: true };
    }

    updateProfile(user, input) {
        const displayName = input?.displayName;
        if (typeof displayName !== "string" || displayName.trim().length < 1 || displayName.trim().length > 80 || /[\u0000-\u001f]/.test(displayName)) {
            throw new ApiError(400, "INVALID_PROFILE", "displayName must contain 1-80 printable characters");
        }
        const updated = { ...this.store.getUser(user.userId), profile: { displayName: displayName.trim() } };
        this.store.upsertUser(updated);
        return { success: true, user: publicUser({ ...updated, role: user.role }) };
    }

    getMe(user) {
        return { success: true, user: publicUser(user) };
    }

    recordBehavior(user, input) {
        const eventType = String(input?.eventType || "").trim();
        if (!/^[a-z][a-z0-9_.-]{1,63}$/.test(eventType)) {
            throw new ApiError(400, "INVALID_BEHAVIOR_TYPE", "eventType must contain 2-64 lowercase characters");
        }
        const idempotencyKey = String(input?.idempotencyKey || "").trim();
        if (!/^[A-Za-z0-9._:-]{8,160}$/.test(idempotencyKey)) {
            throw new ApiError(400, "INVALID_IDEMPOTENCY_KEY", "idempotencyKey must contain 8-160 safe characters");
        }
        const metadata = input?.metadata === undefined ? {} : input.metadata;
        if (!metadata || typeof metadata !== "object" || Array.isArray(metadata)) {
            throw new ApiError(400, "INVALID_BEHAVIOR_METADATA", "metadata must be an object");
        }
        validateMetadata(metadata);
        if (Buffer.byteLength(JSON.stringify(metadata), "utf8") > 8000) throw new ApiError(413, "BEHAVIOR_METADATA_TOO_LARGE", "metadata exceeds 8 KiB");
        const occurredAt = input?.occurredAt;
        if (occurredAt !== undefined && (typeof occurredAt !== "string" || !Number.isFinite(Date.parse(occurredAt)))) {
            throw new ApiError(400, "INVALID_OCCURRED_AT", "occurredAt must be a valid timestamp");
        }
        const payload = { eventType, page: typeof input?.page === "string" ? input.page.slice(0, 160) : null,
            metadata, occurredAt: occurredAt === undefined ? null : new Date(occurredAt).toISOString() };
        const payloadHash = hash(JSON.stringify(canonical(payload)));
        const duplicate = this.store.behaviorByIdempotency(user.userId, idempotencyKey);
        if (duplicate) {
            if (duplicate.payloadHash && duplicate.payloadHash !== payloadHash) {
                throw new ApiError(409, "BEHAVIOR_CONFLICT", "idempotencyKey was already used with another payload");
            }
            return { success: true, duplicate: true, event: duplicate };
        }
        const event = {
            eventId: "beh_" + crypto.randomUUID(),
            userId: user.userId,
            walletAddress: user.walletAddress,
            ...payload,
            idempotencyKey,
            payloadHash,
            occurredAt: payload.occurredAt || now(),
            recordedAt: now()
        };
        const currentUser = this.store.getUser(user.userId);
        this.store.appendBehavior(event, { ...currentUser, lastSeenAt: event.recordedAt, behaviorCount: (currentUser.behaviorCount || 0) + 1 });
        return { success: true, duplicate: false, event };
    }

    getBehavior(userId, limit) {
        const events = this.store.listBehavior(userId, parseLimit(limit));
        return { success: true, count: events.length, events };
    }

    listCustomers(limit = 500) {
        const users = this.store.listUsers().sort((a, b) => String(b.lastSeenAt).localeCompare(String(a.lastSeenAt))).slice(0, parseLimit(limit));
        return { success: true, count: users.length, customers: users.map(user => publicUser({ ...user, role: this.adminWallets.has(user.walletAddress) ? "admin" : "customer" })) };
    }

    getCustomerBehavior(userId, limit) {
        identifier(userId, "userId");
        if (!this.store.getUser(userId)) throw new ApiError(404, "CUSTOMER_NOT_FOUND", "Customer does not exist");
        return { success: true, userId, ...this.getBehavior(userId, limit) };
    }
}

let singleton;
function getCustomerService(env = process.env) {
    if (!singleton) singleton = CustomerService.fromEnv(env);
    return singleton;
}

module.exports = { CustomerService, getCustomerService, publicUser };
