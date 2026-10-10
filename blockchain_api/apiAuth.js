const { createHash, timingSafeEqual } = require("node:crypto");

function requireToken(role, tokens = process.env) {
    return (req, res, next) => {
        const expected = tokens[role === "admin" ? "HRC_ADMIN_API_TOKEN" : "HRC_INGEST_API_TOKEN"];
        if (typeof expected !== "string" || expected.length < 32) {
            return res.status(503).json({ success: false, code: "AUTH_NOT_CONFIGURED", error: `${role} API token is not configured (minimum 32 characters)` });
        }
        if (tokens.HRC_INGEST_API_TOKEN && tokens.HRC_INGEST_API_TOKEN === tokens.HRC_ADMIN_API_TOKEN) {
            return res.status(503).json({ success: false, code: "AUTH_NOT_CONFIGURED", error: "Ingestion and admin tokens must be different" });
        }
        const match = /^Bearer ([^\s]+)$/.exec(req.get("authorization") || "");
        const digest = value => createHash("sha256").update(value).digest();
        if (!match || !timingSafeEqual(digest(match[1]), digest(expected))) {
            return res.status(401).json({ success: false, code: "UNAUTHORIZED", error: "A valid Bearer token is required" });
        }
        next();
    };
}
module.exports = { requireToken };
