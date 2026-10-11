const { ApiError } = require("./eventPayload");

// Append-only operator audit trail. Every privileged action records who asked,
// what changed, the transaction hash and the resulting status so an incident can
// be reconstructed from the store alone.
class AuditLog {
    constructor({ store, clock = () => new Date() } = {}) {
        if (!store || typeof store.appendAudit !== "function") {
            throw new Error("AuditLog requires a store with appendAudit");
        }
        this.store = store;
        this.clock = clock;
    }

    async record({ actor, action, target = null, status, details = {}, transactionHash = null, blockNumber = null }) {
        if (!action || !status) throw new ApiError(500, "AUDIT_INVALID", "audit entries need an action and a status");
        const entry = {
            at: this.clock().toISOString(),
            actor: actor || "unknown",
            action,
            target,
            status,
            transactionHash,
            blockNumber,
            details
        };
        this.store.appendAudit(entry);
        return entry;
    }

    /// @dev Wraps a privileged call so both the success and the failure path land
    ///      in the audit trail. The original error is always re-thrown.
    async run({ actor, action, target = null, details = {} }, operation) {
        try {
            const result = await operation();
            await this.record({
                actor, action, target, details, status: "ok",
                transactionHash: result?.transactionHash ?? null,
                blockNumber: result?.blockNumber ?? null
            });
            return result;
        } catch (error) {
            await this.record({
                actor, action, target, status: "error",
                details: { ...details, code: error?.code ?? "UNKNOWN", error: error?.message ?? String(error) }
            });
            throw error;
        }
    }

    list(limit = 100) {
        const bounded = Math.min(Math.max(Number(limit) || 100, 1), 500);
        const entries = this.store.listAudit(bounded);
        return { success: true, count: entries.length, entries };
    }
}

module.exports = { AuditLog };
