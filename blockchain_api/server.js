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

function requireDemoSession(demoService, role) {
    return (req, res, next) => {
        const authenticated = demoService().authenticateToken(appToken(req));
        if (!authenticated) return res.status(401).json({ success: false, code: "UNAUTHORIZED", error: "Choose a demo account first" });
        if (role && authenticated.user.role !== role) {
            return res.status(403).json({ success: false, code: "FORBIDDEN", error: "This demo account is read-only for the requested operation" });
        }
        req.demoUser = authenticated.user;
        req.demoSession = authenticated.session;
        next();
    };
}

function createApp({ service, customerService, demoService, liveService, tokens = process.env } = {}) {
    service ||= require("./safetyService");
    let resolvedCustomerService = customerService;
    const getCustomerService = () => resolvedCustomerService || (resolvedCustomerService = require("./customerService").getCustomerService(tokens));
    let resolvedDemoService = demoService;
    const getDemoService = () => resolvedDemoService || (resolvedDemoService = require("./demoWalletService").getDemoWalletService(tokens));
    let resolvedLiveService = liveService;
    const getLiveService = () => resolvedLiveService || (resolvedLiveService = require('./liveSessionService').buildLiveSessionService(getDemoService(), tokens));
    const app = express();
    app.locals.startLiveRecovery = () => getLiveService().startRecovery();
    app.locals.stopLiveRecovery = () => resolvedLiveService?.stopRecovery();
    const allowedOrigins = String(tokens.HRC_CORS_ORIGINS || "http://localhost:8080,http://127.0.0.1:8080")
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
        if (/^\/api\/(auth|demo|me|behavior|customers|tasks)(\/|$)/.test(req.path)) res.set("Cache-Control", "no-store");
        next();
    });
    app.use(express.json({ limit: "64kb", strict: true }));
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
            demoAccounts: "GET /api/demo/accounts", demoLogin: "POST /api/demo/login", demoWallet: "GET /api/me/wallet",
            demoEnd: "POST /api/me/sessions/:sessionId/end", demoApprove: "POST /api/me/wallet/approve", demoStart: "POST /api/me/sessions/start", demoSessions: "GET /api/me/sessions",
            demoAdminOverview: "GET /api/demo/admin/overview", demoWalletTransactions: "GET /api/me/wallet/transactions" }
    }));
    app.get("/api/config", async (req, res) => {
        const read = service.getPublicConfig || (() => require("./publicConfig").getPublicConfig(tokens));
        await respond(res, () => read());
    });
    const requireWalletLogin = (req, res, next) => {
        if (tokens.HRC_APP_AUTH_MODE !== "wallet") {
            return res.status(403).json({ success: false, code: "WALLET_LOGIN_DISABLED",
                error: "Wallet registration is disabled in the local demo. Choose admin, user_1 or user_2." });
        }
        next();
    };
    app.post("/api/auth/challenge", requireWalletLogin, async (req, res) => {
        await respond(res, () => getCustomerService().createChallenge(req.body));
    });
    app.post("/api/auth/wallet", requireWalletLogin, async (req, res) => {
        await respond(res, () => getCustomerService().authenticateWallet(req.body), 401);
    });
    app.get("/api/demo/accounts", async (req, res) => {
        await respond(res, () => ({ success: true, accounts: getDemoService().listAccounts() }));
    });
    app.post("/api/demo/login", async (req, res) => {
        await respond(res, () => getDemoService().login(req.body?.userId), 401);
    });
    // Alias kept short for the dashboard and local smoke scripts.
    app.post("/api/auth/demo", async (req, res) => {
        await respond(res, () => getDemoService().login(req.body?.userId), 401);
    });
    app.post("/api/demo/logout", requireDemoSession(getDemoService), async (req, res) => {
        await respond(res, () => getDemoService().logout(appToken(req)));
    });
    app.get("/api/me/wallet", requireDemoSession(getDemoService, "customer"), async (req, res) => {
        await respond(res, () => getLiveService().wallet(req.demoUser.userId));
    });
    app.get("/api/me/wallet/transactions", requireDemoSession(getDemoService, "customer"), async (req, res) => {
        await respond(res, () => getDemoService().getTransactions(req.demoUser.userId));
    });
    app.post("/api/me/wallet/approve", requireDemoSession(getDemoService, "customer"), async (req, res) => {
        await respond(res, () => getDemoService().approve(req.demoUser.userId));
    });
    app.get("/api/me/sessions", requireDemoSession(getDemoService, "customer"), async (req, res) => {
        await respond(res, () => getLiveService().sessions(req.demoUser.userId));
    });
    app.get("/api/me/sessions/:sessionId", requireDemoSession(getDemoService, "customer"), async (req, res) => {
        identifier(req.params.sessionId, "sessionId");
        if (!req.params.sessionId.startsWith(`demo-${req.demoUser.userId}-`)) {
            return res.status(403).json({ success: false, code: "SESSION_OWNER_MISMATCH", error: "session does not belong to this demo user" });
        }
        await respond(res, () => getLiveService().session(req.demoUser.userId, req.params.sessionId), 404);
    });
    app.post("/api/me/sessions/start", requireDemoSession(getDemoService, "customer"), async (req, res) => {
        await respond(res, () => getLiveService().start(req.demoUser.userId, req.body || {}));
    });
    app.post('/api/me/sessions/:sessionId/end', requireDemoSession(getDemoService, 'customer'), async (req, res) => {
        await respond(res, () => getLiveService().end(req.demoUser.userId, req.params.sessionId, req.body || {}));
    });
    app.get("/api/demo/admin/overview", requireDemoSession(getDemoService, "admin"), async (req, res) => {
        await respond(res, () => getLiveService().overview());
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
    app.get("/api/admin/access", requireToken("admin", tokens), async (req, res) => {
        res.set("Cache-Control", "no-store");
        await respond(res, () => service.getAdminAccess());
    });
    app.get("/api/events/:eventId/payload", requireToken("admin", tokens), async (req, res) => {
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
    app.post("/api/robots/:deviceId/unlock", requireToken("admin", tokens), async (req, res) => {
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
