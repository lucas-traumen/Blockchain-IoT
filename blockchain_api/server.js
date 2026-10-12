const express = require("express");
const cors = require("cors");
const { requireToken } = require("./apiAuth");
const { ApiError, identifier } = require("./eventPayload");

function appToken(req) {
    const match = /^Bearer ([^\s]+)$/.exec(req.get("authorization") || "");
    return match?.[1] || null;
}

function requireUserSession(customerService) {
    return (req, res, next) => {
        const authenticated = customerService().authenticateToken(appToken(req));
        if (!authenticated) return res.status(401).json({ success: false, code: "UNAUTHORIZED", error: "A valid wallet session is required" });
        req.appUser = authenticated.user;
        req.appSession = authenticated.session;
        next();
    };
}

function requireUserRole(customerService, role) {
    return (req, res, next) => {
        const authenticated = customerService().authenticateToken(appToken(req));
        if (!authenticated) return res.status(401).json({ success: false, code: "UNAUTHORIZED", error: "A valid wallet session is required" });
        if (authenticated.user.role !== role) return res.status(403).json({ success: false, code: "FORBIDDEN", error: "This wallet does not have the required application role" });
        req.appUser = authenticated.user;
        req.appSession = authenticated.session;
        next();
    };
}

function rateLimit({ windowMs = 60000, max = 20 } = {}) {
    const clients = new Map();
    let requests = 0;
    return (req, res, next) => {
        const now = Date.now();
        const key = req.ip || req.socket.remoteAddress || "unknown";
        let client = clients.get(key);
        if (!client || client.resetAt <= now) client = { count: 0, resetAt: now + windowMs };
        client.count += 1;
        clients.set(key, client);
        requests += 1;
        if ((requests & 255) === 0) {
            for (const [address, entry] of clients) if (entry.resetAt <= now) clients.delete(address);
        }
        if (client.count > max) {
            res.set("Retry-After", String(Math.max(1, Math.ceil((client.resetAt - now) / 1000))));
            return res.status(429).json({ success: false, code: "RATE_LIMITED", error: "Too many requests; retry later" });
        }
        next();
    };
}

function requireWalletSession(customerService, rewardWalletService, role = "customer") {
    return (req, res, next) => {
        const authenticated = customerService().authenticateToken(appToken(req));
        if (!authenticated) return res.status(401).json({ success: false, code: "UNAUTHORIZED", error: "A valid SIWE wallet session is required" });
        if (role && authenticated.user.role !== role) {
            return res.status(403).json({ success: false, code: "FORBIDDEN", error: "This wallet does not have the required application role" });
        }
        req.appUser = authenticated.user;
        req.appSession = authenticated.session;
        req.walletUser = rewardWalletService().walletUser(authenticated.user.walletAddress);
        next();
    };
}

function createApp({ service, customerService, walletService, liveService, rewardService, auditLog, tokens = process.env } = {}) {
    const authMode = tokens.HRC_APP_AUTH_MODE || "siwe";
    if (authMode !== "siwe") {
        throw new Error("HRC_APP_AUTH_MODE must be siwe; demo and legacy wallet login are disabled");
    }
    service ||= require("./safetyService");
    let resolvedCustomerService = customerService;
    const getCustomerService = () => resolvedCustomerService || (resolvedCustomerService = require("./customerService").getCustomerService(tokens));
    let resolvedWalletService = walletService;
    const getWalletService = () => resolvedWalletService || (resolvedWalletService = require("./walletService").getWalletService(tokens, getCustomerService().store));
    let resolvedLiveService = liveService;
    const getLiveService = () => resolvedLiveService || (resolvedLiveService = require('./liveSessionService').buildLiveSessionService(getWalletService(), tokens));
    let resolvedRewardService = rewardService || null;
    const getRewardService = () => resolvedRewardService || (resolvedRewardService = require("./rewardService").getRewardService(tokens));
    let resolvedWalletFirst = null;
    const getWalletFirstService = () => resolvedWalletFirst || (resolvedWalletFirst = require("./walletFirstService")
        .buildWalletFirstService(getCustomerService(), () => { try { return getWalletService(); } catch { return null; } }));
    let resolvedAuditLog = auditLog || null;
    const getAuditLog = () => resolvedAuditLog || (resolvedAuditLog = new (require("./auditLog").AuditLog)({ store: getCustomerService().store }));
    const prepareWalletStart = async (userId, input) => {
        const prepared = await getWalletFirstService().prepareStartSession(userId, input);
        await getLiveService().reserveWalletStart(userId, prepared.sessionId);
        return { ...prepared, stage: "PREPARED", intentTtlMs: 120000 };
    };
    const adminActor = req => req.appUser.walletAddress;
    const requireExternalPolicyGovernance = (req, res, next) => {
        if (tokens.NODE_ENV === "production") {
            return res.status(501).json({ success: false, code: "POLICY_MULTISIG_REQUIRED",
                error: "Policy changes must be submitted by the configured contract multisig or timelock" });
        }
        next();
    };
    const requireAdmin = (req, res, next) => requireUserRole(getCustomerService, "admin")(req, res, next);
    const app = express();
    app.set("trust proxy", tokens.HRC_TRUST_PROXY === "true" ? 1 : false);
    app.locals.customerService = { authenticateToken: token => getCustomerService().authenticateToken(token) };
    app.locals.startLiveRecovery = () => getLiveService().startRecovery();
    app.locals.stopLiveRecovery = () => resolvedLiveService?.stopRecovery();
    const allowedOrigins = String(tokens.HRC_CORS_ORIGINS || "http://localhost:8081,http://127.0.0.1:8081")
        .split(",").map(value => value.trim().replace(/\/$/, "")).filter(Boolean);
    if (allowedOrigins.includes("*")) throw new Error("HRC_CORS_ORIGINS must be an explicit allowlist");
    app.use(cors({ origin(origin, callback) {
        if (!origin || allowedOrigins.includes(origin.replace(/\/$/, ""))) return callback(null, true);
        return callback(null, false);
    }}));
    app.use((req, res, next) => {
        const requestId = require("node:crypto").randomUUID();
        res.set("X-Request-ID", requestId);
        req.requestId = requestId;
        if (/^\/api\/(auth|me|behavior|customers|tasks)(\/|$)/.test(req.path)) res.set("Cache-Control", "no-store");
        next();
    });
    app.use((req, res, next) => {
        res.set({
            "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()"
        });
        if (tokens.NODE_ENV === "production" && req.secure) {
            res.set("Strict-Transport-Security", "max-age=31536000; includeSubDomains");
        }
        if (tokens.NODE_ENV === "production" && !req.secure) {
            return res.status(426).json({ success: false, code: "HTTPS_REQUIRED", error: "HTTPS is required" });
        }
        next();
    });
    app.use(express.json({ limit: "64kb", strict: true }));
    const authRateLimit = rateLimit({ windowMs: 60000, max: 20 });
    const adminRateLimit = rateLimit({ windowMs: 60000, max: 60 });
    const respond = async (res, operation, defaultFailure = 503) => {
        const result = await operation();
        const { httpStatus, ...body } = result;
        res.status(httpStatus || (result.success ? 200 : defaultFailure)).json(body);
    };
    app.get("/", (req, res) => res.json({
        message: "HRC Safety Log Backend is running", version: "1.0",
        endpoints: { submitSafetyEvent: "POST /api/safety-event", getEvents: "GET /api/events", getTransactions: "GET /api/transactions",
            getEvent: "GET /api/events/:eventId", robotStatus: "GET /api/robots/:deviceId/status",
            unlockRobot: "POST /api/robots/:deviceId/unlock", startRewardSession: "POST /api/session-start",
            settleRewardSession: "POST /api/session-outcome", rewardConfig: "GET /api/rewards/config",
            rewardSummary: "GET /api/rewards/summary?beneficiary=0x...", rewardSessions: "GET /api/rewards/sessions",
            rewardTransactions: "GET /api/rewards/transactions", publicConfig: "GET /api/config", adminAccess: "GET /api/admin/access (admin)", archivedPayload: "GET /api/events/:eventId/payload (admin)",
            walletChallenge: "POST /api/auth/challenge", walletLogin: "POST /api/auth/wallet", walletLogout: "POST /api/auth/logout",
            me: "GET /api/me", profile: "PATCH /api/me/profile", behavior: "POST /api/behavior",
            myBehavior: "GET /api/me/behavior", customers: "GET /api/customers (admin wallet)", customerBehavior: "GET /api/customers/:userId/behavior (admin wallet)",
            wallet: "GET /api/me/wallet",
            walletEnd: "POST /api/me/sessions/:sessionId/end", walletApprove: "GET /api/me/wallet/prepare/approve", walletStart: "POST /api/me/sessions/start", walletStartConfirm: "POST /api/me/sessions/:sessionId/confirm-start", walletSessions: "GET /api/me/sessions",
            walletTransactions: "GET /api/me/wallet/transactions" }
    }));
    app.get("/api/config", async (req, res) => {
        const read = service.getPublicConfig || (() => require("./publicConfig").getPublicConfig(tokens));
        await respond(res, () => read());
    });
    app.post("/api/auth/challenge", authRateLimit, async (req, res) => {
        await respond(res, () => getCustomerService().createChallenge(req.body));
    });
    app.post("/api/auth/wallet", authRateLimit, async (req, res) => {
        await respond(res, () => getWalletFirstService().login(req.body || {}), 401);
    });
    app.get("/api/me/wallet", requireWalletSession(getCustomerService, getWalletService), async (req, res) => {
        await respond(res, () => getLiveService().wallet(req.walletUser.userId));
    });
    app.get("/api/me/wallet/transactions", requireWalletSession(getCustomerService, getWalletService), async (req, res) => {
        await respond(res, () => getWalletService().getTransactions(req.walletUser.userId));
    });
    // Wallet-first: the user signs these locally. The backend only prepares them.
    app.get("/api/me/wallet/prepare/approve", requireWalletSession(getCustomerService, getWalletService), async (req, res) => {
        await respond(res, () => getWalletFirstService().prepareApproval(req.walletUser.userId), 400);
    });
    app.get("/api/me/wallet/prepare/start", requireWalletSession(getCustomerService, getWalletService), async (req, res) => {
        await respond(res, () => prepareWalletStart(req.walletUser.userId, req.query), 400);
    });
    app.get("/api/me/wallet/prepare/end/:sessionId", requireWalletSession(getCustomerService, getWalletService), async (req, res) => {
        await respond(res, () => getWalletFirstService().prepareEndSession(req.walletUser.userId, req.params.sessionId), 400);
    });
    app.get("/api/me/sessions", requireWalletSession(getCustomerService, getWalletService), async (req, res) => {
        await respond(res, () => getLiveService().sessions(req.walletUser.userId));
    });
    app.get("/api/me/sessions/:sessionId", requireWalletSession(getCustomerService, getWalletService), async (req, res) => {
        identifier(req.params.sessionId, "sessionId");
        await respond(res, () => getLiveService().session(req.walletUser.userId, req.params.sessionId), 404);
    });
    app.post("/api/me/sessions/start", requireWalletSession(getCustomerService, getWalletService), async (req, res) => {
        await respond(res, () => prepareWalletStart(req.walletUser.userId, req.body || {}));
    });
    app.post("/api/me/sessions/:sessionId/confirm-start", requireWalletSession(getCustomerService, getWalletService), async (req, res) => {
        await respond(res, () => getLiveService().confirmWalletStart(req.walletUser.userId, req.params.sessionId, req.body || {}), 400);
    });
    app.post('/api/me/sessions/:sessionId/end', requireWalletSession(getCustomerService, getWalletService), async (req, res) => {
        await respond(res, () => getLiveService().end(req.walletUser.userId, req.params.sessionId, req.body || {}));
    });
    app.post("/api/auth/logout", requireUserSession(getCustomerService), async (req, res) => {
        await respond(res, () => getCustomerService().logout(req.appSession));
    });
    app.patch("/api/me/profile", requireUserSession(getCustomerService), async (req, res) => {
        await respond(res, () => getCustomerService().updateProfile(req.appUser, req.body));
    });
    app.get("/api/me", requireUserSession(getCustomerService), async (req, res) => {
        await respond(res, () => getCustomerService().getMe(req.appUser));
    });
    app.post("/api/behavior", requireUserSession(getCustomerService), async (req, res) => {
        await respond(res, () => getCustomerService().recordBehavior(req.appUser, req.body));
    });
    app.get("/api/me/behavior", requireUserSession(getCustomerService), async (req, res) => {
        await respond(res, () => getCustomerService().getBehavior(req.appUser.userId, req.query.limit === undefined ? 100 : Number(req.query.limit)));
    });
    app.get("/api/customers", requireUserRole(getCustomerService, "admin"), async (req, res) => {
        await respond(res, () => getCustomerService().listCustomers(req.query.limit === undefined ? 500 : Number(req.query.limit)));
    });
    app.get("/api/customers/:userId/behavior", requireUserRole(getCustomerService, "admin"), async (req, res) => {
        identifier(req.params.userId, "userId");
        await respond(res, () => getCustomerService().getCustomerBehavior(req.params.userId, req.query.limit === undefined ? 100 : Number(req.query.limit)));
    });
    app.post("/api/safety-event", requireToken("ingest", tokens), async (req, res) => {
        await respond(res, () => service.processSafetyEvent(req.body));
    });
    app.post("/api/session-start", requireToken("ingest", tokens), async (req, res) => {
        const start = service.startSession || (() => require("./rewardService").getRewardService().startSession(req.body));
        await respond(res, () => start(req.body));
    });
    app.post("/api/session-outcome", requireToken("ingest", tokens), async (req, res) => {
        const settle = service.processSessionOutcome || (() => require("./rewardService").getRewardService().processSessionOutcome(req.body));
        await respond(res, () => settle(req.body));
    });
    app.get("/api/rewards/config", async (req, res) => {
        const read = service.getRewardConfig || (() => require("./rewardReadService").getRewardReadService().getConfig());
        await respond(res, () => read());
    });
    app.get("/api/rewards/summary", async (req, res) => {
        const read = service.getRewardSummary || (() => require("./rewardReadService").getRewardReadService().getSummary(req.query.beneficiary));
        await respond(res, () => read(req.query.beneficiary));
    });
    app.get("/api/rewards/sessions", async (req, res) => {
        const read = service.getRewardSessions || (() => require("./rewardReadService").getRewardReadService().getSessions({ beneficiary: req.query.beneficiary, limit: req.query.limit }));
        await respond(res, () => read({ beneficiary: req.query.beneficiary, limit: req.query.limit }));
    });
    app.get("/api/rewards/sessions/:sessionId", async (req, res) => {
        identifier(req.params.sessionId, "sessionId");
        const read = service.getRewardSession || (() => require("./rewardReadService").getRewardReadService().getSession(req.params.sessionId));
        await respond(res, () => read(req.params.sessionId), 404);
    });
    app.get("/api/rewards/transactions", async (req, res) => {
        const read = service.getRewardTransactions || (() => require("./rewardReadService").getRewardReadService().getTransactions({ beneficiary: req.query.beneficiary, limit: req.query.limit }));
        await respond(res, () => read({ beneficiary: req.query.beneficiary, limit: req.query.limit }));
    });
    app.get("/api/events", async (req, res) => {
        await respond(res, () => service.getAllSafetyEvents());
    });
    app.get("/api/transactions", async (req, res) => {
        await respond(res, () => service.getAllTransactions());
    });
    app.get("/api/admin/access", adminRateLimit, requireAdmin, async (req, res) => {
        res.set("Cache-Control", "no-store");
        await respond(res, () => service.getAdminAccess());
    });
    app.get("/api/admin/reward/status", adminRateLimit, requireAdmin, async (req, res) => {
        res.set("Cache-Control", "no-store");
        await respond(res, () => getRewardService().getAdminStatus());
    });
    app.post("/api/admin/reward/pause", adminRateLimit, requireAdmin, async (req, res) => {
        await respond(res, () => getAuditLog().run({ actor: adminActor(req), action: "reward.pause" },
            () => getRewardService().setPaused(true)));
    });
    app.post("/api/admin/reward/unpause", adminRateLimit, requireAdmin, async (req, res) => {
        await respond(res, () => getAuditLog().run({ actor: adminActor(req), action: "reward.unpause" },
            () => getRewardService().setPaused(false)));
    });
    app.post("/api/admin/reward/policy", adminRateLimit, requireAdmin, requireExternalPolicyGovernance, async (req, res) => {
        await respond(res, () => getAuditLog().run({ actor: adminActor(req), action: "reward.policy", details: req.body || {} },
            () => getRewardService().setPolicy(req.body || {})));
    });
    app.post("/api/admin/reward/pool/fund", adminRateLimit, requireAdmin, async (req, res) => {
        await respond(res, () => getAuditLog().run({ actor: adminActor(req), action: "reward.pool.fund", details: { amount: req.body?.amount } },
            () => getRewardService().fundRewardPool(req.body?.amount)));
    });
    app.post("/api/admin/reward/pool/sweep", adminRateLimit, requireAdmin, async (req, res) => {
        await respond(res, () => getAuditLog().run({ actor: adminActor(req), action: "reward.pool.sweep", target: req.body?.to, details: { amount: req.body?.amount } },
            () => getRewardService().sweepExcessToken(req.body?.to, req.body?.amount)));
    });
    app.post("/api/admin/reward/treasury", adminRateLimit, requireAdmin, async (req, res) => {
        await respond(res, () => getAuditLog().run({ actor: adminActor(req), action: "reward.treasury", target: req.body?.treasury },
            () => getRewardService().setPenaltyTreasury(req.body?.treasury)));
    });
    app.get("/api/admin/audit", adminRateLimit, requireAdmin, async (req, res) => {
        res.set("Cache-Control", "no-store");
        await respond(res, () => getAuditLog().list(req.query.limit));
    });
    app.get("/api/events/:eventId/payload", adminRateLimit, requireAdmin, async (req, res) => {
        identifier(req.params.eventId, "eventId");
        await respond(res, () => service.getStoredPayload(req.params.eventId));
    });
    app.get("/api/events/:eventId", async (req, res) => {
        identifier(req.params.eventId, "eventId");
        await respond(res, () => service.getSafetyEventById(req.params.eventId), 404);
    });
    app.get("/api/robots/:deviceId/status", async (req, res) => {
        identifier(req.params.deviceId, "deviceId");
        await respond(res, () => service.getRobotStatus(req.params.deviceId));
    });
    app.post("/api/robots/:deviceId/unlock", adminRateLimit, requireAdmin, async (req, res) => {
        identifier(req.params.deviceId, "deviceId");
        await respond(res, () => service.unlockRobot(req.params.deviceId));
    });
    app.use((error, req, res, next) => {
        if (res.headersSent) return next(error);
        const parseError = error.type === "entity.parse.failed";
        const tooLarge = error.type === "entity.too.large";
        res.status(error instanceof ApiError ? error.statusCode : tooLarge ? 413 : parseError ? 400 : 503).json({
            success: false,
            code: error instanceof ApiError ? error.code : tooLarge ? "PAYLOAD_TOO_LARGE" : parseError ? "INVALID_JSON" : "SERVICE_UNAVAILABLE",
            error: error instanceof ApiError ? error.message : tooLarge ? "Payload exceeds 64 KiB" : parseError ? "Invalid JSON body" : "Service unavailable; retry with the same event identity"
        });
    });
    return app;
}

if (require.main === module) {
    require("dotenv").config();
    const port = Number(process.env.PORT || 3000);
    const app = createApp();
    const server = app.listen(port, () => {
        console.log(`HRC Safety Log Backend running at http://localhost:${port}`);
        try { app.locals.startLiveRecovery(); }
        catch { console.error('Live session recovery is unavailable; check local reward configuration'); }
    });
    const shutdown = () => { app.locals.stopLiveRecovery(); server.close(() => process.exit(0)); };
    process.on('SIGINT', shutdown); process.on('SIGTERM', shutdown);
}
module.exports = { createApp };
