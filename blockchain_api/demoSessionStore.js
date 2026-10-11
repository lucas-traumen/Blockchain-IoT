const fs = require("node:fs");
const path = require("node:path");

function emptyState() { return { sessions: {}, transactions: {} }; }

class DemoSessionStore {
    constructor(filePath) {
        this.filePath = path.resolve(filePath);
        fs.mkdirSync(path.dirname(this.filePath), { recursive: true, mode: 0o700 });
        this.state = this._read();
    }

    _read() {
        if (!fs.existsSync(this.filePath)) return emptyState();
        const parsed = JSON.parse(fs.readFileSync(this.filePath, "utf8"));
        if (!parsed || typeof parsed !== "object" || !parsed.sessions || typeof parsed.sessions !== "object") throw new Error("Invalid demo session store");
        // Existing reference-only snapshots are compatible with the live workflow.
        parsed.transactions ||= {};
        return parsed;
    }

    _write() {
        const temp = this.filePath + ".tmp";
        const fd = fs.openSync(temp, "w", 0o600);
        try { fs.writeFileSync(fd, JSON.stringify(this.state, null, 2) + "\n"); fs.fsyncSync(fd); }
        finally { fs.closeSync(fd); }
        fs.renameSync(temp, this.filePath);
        const directory = fs.openSync(path.dirname(this.filePath), "r");
        try { fs.fsyncSync(directory); } finally { fs.closeSync(directory); }
    }

    save(record) {
        const previous = this.state.sessions[record.sessionId];
        if (previous && previous.userId !== record.userId) throw new Error("Cannot change the owner of a demo session");
        this.state.sessions[record.sessionId] = { ...previous, ...record };
        this._write();
        return this.get(record.sessionId);
    }

    update(sessionId, fields) {
        const record = this.get(sessionId);
        if (!record) throw new Error("Demo session reference does not exist");
        return this.save({ ...record, ...fields });
    }

    get(sessionId) { return this.state.sessions[sessionId] ? structuredClone(this.state.sessions[sessionId]) : null; }
    list(userId) {
        return Object.values(this.state.sessions).filter(record => !userId || record.userId === userId)
            .sort((a, b) => String(b.createdAt).localeCompare(String(a.createdAt))).map(record => structuredClone(record));
    }
    getTransaction(key) { return this.state.transactions[key] ? structuredClone(this.state.transactions[key]) : null; }
    saveTransaction(record) {
        this.state.transactions[record.key] = { ...this.state.transactions[record.key], ...record };
        this._write();
        return this.getTransaction(record.key);
    }
    listTransactions() { return Object.values(this.state.transactions).map(record => structuredClone(record)); }
}
module.exports = { DemoSessionStore };
