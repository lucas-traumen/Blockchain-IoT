const express = require("express");
const cors = require("cors");
const { requireToken } = require("./apiAuth");
const { ApiError, identifier } = require("./eventPayload");

function createApp({ service, tokens = process.env } = {}) {
    service ||= require("./safetyService");
    const app = express();
    const allowedOrigins = String(tokens.HRC_CORS_ORIGINS || "http://localhost:8080,http://127.0.0.1:8080")
        .split(",").map(value => value.trim().replace(/\/$/, "")).filter(Boolean);
    if (allowedOrigins.includes("*")) throw new Error("HRC_CORS_ORIGINS must be an explicit allowlist");
    app.use(cors({ origin(origin, callback) {
        if (!origin || allowedOrigins.includes(origin.replace(/\/$/, ""))) return callback(null, true);
        return callback(null, false);
    }}));
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
            rewardTransactions: "GET /api/rewards/transactions", publicConfig: "GET /api/config", adminAccess: "GET /api/admin/access (admin)", archivedPayload: "GET /api/events/:eventId/payload (admin)" }
    }));
    app.get("/api/config", async (req, res) => {
        const read = service.getPublicConfig || (() => require("./publicConfig").getPublicConfig(tokens));
        await respond(res, () => read());
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
    createApp().listen(port, () => console.log(`HRC Safety Log Backend running at http://localhost:${port}`));
}
module.exports = { createApp };
