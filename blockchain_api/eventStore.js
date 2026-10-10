const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");

// Single writer per data directory; atomic snapshots persisted before broadcast.
class EventStore {
    constructor(directory) {
        this.directory = path.resolve(directory);
        fs.mkdirSync(this.directory, { recursive: true, mode: 0o700 });
        this.lockPath = path.join(this.directory, "writer.lock");
        try { this.lock = fs.openSync(this.lockPath, "wx", 0o600); }
        catch (error) {
            if (error.code !== "EEXIST") throw error;
            // Serialize stale-lock recovery too. A second recovering process
            // must never remove a lock that the first process just acquired.
            const recoveryPath = path.join(this.directory, "recovery.lock");
            let recovery;
            try { recovery = fs.openSync(recoveryPath, "wx", 0o600); }
            catch { throw new Error("Event store lock recovery is in progress; verify writers before removing a stale recovery.lock"); }
            try {
                const pid = Number(fs.readFileSync(this.lockPath, "utf8"));
                if (!Number.isInteger(pid) || pid < 1) throw new Error("Store lock is incomplete; verify no writer is running before removing writer.lock");
                try { process.kill(pid, 0); }
                catch (check) {
                    if (check.code !== "ESRCH") throw check;
                    fs.unlinkSync(this.lockPath);
                    this.lock = fs.openSync(this.lockPath, "wx", 0o600);
                }
                if (this.lock === undefined) throw new Error("Event store already has a running writer");
            } finally {
                fs.closeSync(recovery);
                fs.unlinkSync(recoveryPath);
            }
        }
        fs.writeFileSync(this.lock, String(process.pid));
        fs.fsyncSync(this.lock);
        this.records = new Map();
        try {
            for (const name of fs.readdirSync(this.directory).filter(name => name.endsWith(".json"))) {
                const record = JSON.parse(fs.readFileSync(path.join(this.directory, name), "utf8"));
                if (typeof record.key !== "string" || this.records.has(record.key)) throw new Error("Invalid event store record");
                this.records.set(record.key, record);
            }
        } catch (error) { this.close(); throw error; }
    }
    get(key) { const record = this.records.get(key); return record ? structuredClone(record) : undefined; }
    list() { return [...this.records.values()].map(record => structuredClone(record)); }
    save(record) {
        if (this.lock === undefined) throw new Error("Event store is closed");
        const name = crypto.createHash("sha256").update(record.key).digest("hex");
        const target = path.join(this.directory, name + ".json");
        const temp = path.join(this.directory, name + ".tmp");
        const fd = fs.openSync(temp, "w", 0o600);
        try { fs.writeFileSync(fd, JSON.stringify(record)); fs.fsyncSync(fd); }
        finally { fs.closeSync(fd); }
        fs.renameSync(temp, target);
        this.records.set(record.key, structuredClone(record));
    }
    close() {
        if (this.lock !== undefined) {
            fs.closeSync(this.lock);
            this.lock = undefined;
            fs.unlinkSync(this.lockPath);
        }
    }
}
module.exports = { EventStore };
