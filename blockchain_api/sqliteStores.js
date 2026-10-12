const fs = require("node:fs");
const path = require("node:path");
const { DatabaseSync } = require("node:sqlite");

function clone(value) { return value === undefined || value === null ? value : structuredClone(value); }
function parse(value) { return value === null || value === undefined ? null : JSON.parse(value); }

// SQLite-backed persistence for the application identity layer and the live
// session reference store. It implements the same public surface as the JSON
// stores so the services above it stay unchanged. Enable it with
// HRC_APP_STORE=sqlite and point HRC_APP_DB_PATH at the database file.
class SqliteCustomerStore {
    constructor(filePath) {
        this.filePath = path.resolve(filePath);
        fs.mkdirSync(path.dirname(this.filePath), { recursive: true, mode: 0o700 });
        this.db = new DatabaseSync(this.filePath);
        this.db.exec("PRAGMA journal_mode = WAL");
        this.db.exec(`
            CREATE TABLE IF NOT EXISTS users (userId TEXT PRIMARY KEY, walletAddress TEXT, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS users_wallet ON users (walletAddress);
            CREATE TABLE IF NOT EXISTS challenges (key TEXT PRIMARY KEY, walletAddress TEXT NOT NULL, issuedAt TEXT, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS challenges_wallet ON challenges (walletAddress, issuedAt);
            CREATE TABLE IF NOT EXISTS sessions (tokenHash TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS behavior (id INTEGER PRIMARY KEY AUTOINCREMENT, userId TEXT NOT NULL, idempotencyKey TEXT, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS behavior_user ON behavior (userId, id);
            CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT, action TEXT NOT NULL, data TEXT NOT NULL);
        `);
    }

    getUser(userId) { return parse(this.db.prepare("SELECT data FROM users WHERE userId = ?").get(userId)?.data ?? null); }
    findUserByWallet(walletAddress) {
        return parse(this.db.prepare("SELECT data FROM users WHERE walletAddress = ? LIMIT 1").get(String(walletAddress).toLowerCase())?.data ?? null);
    }
    listUsers() { return this.db.prepare("SELECT data FROM users ORDER BY userId").all().map(row => parse(row.data)); }
    upsertUser(user) {
        const wallet = user.walletAddress === undefined || user.walletAddress === null
            ? null : String(user.walletAddress).toLowerCase();
        this.db.prepare("INSERT INTO users (userId, walletAddress, data) VALUES (?, ?, ?) ON CONFLICT(userId) DO UPDATE SET walletAddress = excluded.walletAddress, data = excluded.data")
            .run(user.userId, wallet, JSON.stringify(user));
        return clone(user);
    }

    getChallenge(walletAddress, message) {
        const address = String(walletAddress).toLowerCase();
        const rows = this.db.prepare("SELECT data FROM challenges WHERE walletAddress = ? ORDER BY issuedAt DESC").all(address);
        const match = rows.map(row => parse(row.data)).find(challenge => !message || challenge.message === message);
        if (match) return match;
        // Read compatibility for the first MVP format, which keyed the single
        // challenge by wallet address instead of nonce.
        const legacy = parse(this.db.prepare("SELECT data FROM challenges WHERE key = ?").get(address)?.data ?? null);
        return legacy && (!message || legacy.message === message) ? legacy : null;
    }
    saveChallenge(challenge) {
        this.db.prepare("INSERT INTO challenges (key, walletAddress, issuedAt, data) VALUES (?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET walletAddress = excluded.walletAddress, issuedAt = excluded.issuedAt, data = excluded.data")
            .run(challenge.nonce, String(challenge.walletAddress).toLowerCase(), challenge.issuedAt ?? null, JSON.stringify(challenge));
        return clone(challenge);
    }
    markChallengeUsed(challenge) {
        const used = { ...clone(challenge), used: true };
        const byNonce = this.db.prepare("SELECT key FROM challenges WHERE key = ?").get(challenge.nonce);
        const key = byNonce ? challenge.nonce : String(challenge.walletAddress).toLowerCase();
        this.db.prepare("UPDATE challenges SET data = ? WHERE key = ?").run(JSON.stringify(used), key);
        return used;
    }

    getSession(tokenHash) { return parse(this.db.prepare("SELECT data FROM sessions WHERE tokenHash = ?").get(tokenHash)?.data ?? null); }
    saveSession(session) {
        this.db.prepare("INSERT INTO sessions (tokenHash, data) VALUES (?, ?) ON CONFLICT(tokenHash) DO UPDATE SET data = excluded.data")
            .run(session.tokenHash, JSON.stringify(session));
        return clone(session);
    }
    revokeSession(tokenHash) { return this.db.prepare("DELETE FROM sessions WHERE tokenHash = ?").run(tokenHash).changes > 0; }

    behaviorByIdempotency(userId, idempotencyKey) {
        const row = this.db.prepare("SELECT data FROM behavior WHERE userId = ? AND idempotencyKey = ? ORDER BY id DESC LIMIT 1").get(userId, idempotencyKey);
        return parse(row?.data ?? null);
    }
    appendBehavior(event, updatedUser) {
        const write = () => {
            this.db.prepare("INSERT INTO behavior (userId, idempotencyKey, data) VALUES (?, ?, ?)")
                .run(event.userId, event.idempotencyKey ?? null, JSON.stringify(event));
            if (updatedUser) this.upsertUser(updatedUser);
        };
        this._transaction(write);
        return clone(event);
    }
    listBehavior(userId, limit = 100) {
        const rows = userId
            ? this.db.prepare("SELECT data FROM behavior WHERE userId = ? ORDER BY id DESC LIMIT ?").all(userId, limit)
            : this.db.prepare("SELECT data FROM behavior ORDER BY id DESC LIMIT ?").all(limit);
        return rows.map(row => parse(row.data));
    }

    appendAudit(entry) {
        this.db.prepare("INSERT INTO audit (at, actor, action, data) VALUES (?, ?, ?, ?)")
            .run(entry.at, entry.actor ?? null, entry.action, JSON.stringify(entry));
        return clone(entry);
    }
    listAudit(limit = 100) { return this.db.prepare("SELECT data FROM audit ORDER BY id DESC LIMIT ?").all(limit).map(row => parse(row.data)); }

    _transaction(fn) {
        this.db.exec("BEGIN IMMEDIATE");
        try { fn(); this.db.exec("COMMIT"); }
        catch (error) { try { this.db.exec("ROLLBACK"); } catch { /* the rollback error must not mask the cause */ } throw error; }
    }

    close() { try { this.db.close(); } catch { /* already closed */ } }
}

class SqliteDemoSessionStore {
    constructor(filePath) {
        this.filePath = path.resolve(filePath);
        fs.mkdirSync(path.dirname(this.filePath), { recursive: true, mode: 0o700 });
        this.db = new DatabaseSync(this.filePath);
        this.db.exec("PRAGMA journal_mode = WAL");
        this.db.exec(`
            CREATE TABLE IF NOT EXISTS demo_sessions (sessionId TEXT PRIMARY KEY, userId TEXT NOT NULL, createdAt TEXT, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS demo_sessions_user ON demo_sessions (userId, createdAt);
            CREATE TABLE IF NOT EXISTS demo_transactions (key TEXT PRIMARY KEY, data TEXT NOT NULL);
        `);
    }

    save(record) {
        const previous = this.get(record.sessionId);
        if (previous && previous.userId !== record.userId) throw new Error("Cannot change the owner of a demo session");
        const next = { ...previous, ...record };
        this.db.prepare("INSERT INTO demo_sessions (sessionId, userId, createdAt, data) VALUES (?, ?, ?, ?) ON CONFLICT(sessionId) DO UPDATE SET userId = excluded.userId, createdAt = excluded.createdAt, data = excluded.data")
            .run(next.sessionId, next.userId, next.createdAt ?? null, JSON.stringify(next));
        return clone(next);
    }
    update(sessionId, fields) {
        const record = this.get(sessionId);
        if (!record) throw new Error("Demo session reference does not exist");
        return this.save({ ...record, ...fields });
    }
    get(sessionId) { return parse(this.db.prepare("SELECT data FROM demo_sessions WHERE sessionId = ?").get(sessionId)?.data ?? null); }
    list(userId) {
        const rows = userId
            ? this.db.prepare("SELECT data FROM demo_sessions WHERE userId = ? ORDER BY createdAt DESC").all(userId)
            : this.db.prepare("SELECT data FROM demo_sessions ORDER BY createdAt DESC").all();
        return rows.map(row => parse(row.data));
    }
    reassignOwner(fromUserId, toUserId) {
        const records = this.list(fromUserId);
        if (!records.length) return false;
        this.db.exec("BEGIN IMMEDIATE");
        try {
            const update = this.db.prepare("UPDATE demo_sessions SET userId = ?, data = ? WHERE sessionId = ? AND userId = ?");
            for (const record of records) {
                update.run(toUserId, JSON.stringify({ ...record, userId: toUserId }), record.sessionId, fromUserId);
            }
            this.db.exec("COMMIT");
        } catch (error) {
            try { this.db.exec("ROLLBACK"); } catch { /* Preserve the original failure. */ }
            throw error;
        }
        return true;
    }
    getTransaction(key) { return parse(this.db.prepare("SELECT data FROM demo_transactions WHERE key = ?").get(key)?.data ?? null); }
    saveTransaction(record) {
        const next = { ...this.getTransaction(record.key), ...record };
        this.db.prepare("INSERT INTO demo_transactions (key, data) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET data = excluded.data")
            .run(record.key, JSON.stringify(next));
        return clone(next);
    }
    listTransactions() { return this.db.prepare("SELECT data FROM demo_transactions").all().map(row => parse(row.data)); }
    close() { try { this.db.close(); } catch { /* already closed */ } }
}

// One-time import of the JSON snapshots produced before the SQLite store
// existed. It is additive: an already-populated database is left untouched.
function migrateJsonToSqlite({ customerJsonPath, sessionJsonPath, dbPath, sessionDbPath }) {
    const database = path.resolve(dbPath);
    const sessionDatabase = path.resolve(sessionDbPath || dbPath);
    const store = new SqliteCustomerStore(database);
    const sessions = new SqliteDemoSessionStore(sessionDatabase);
    const report = { users: 0, challenges: 0, behavior: 0, sessions: 0, transactions: 0 };
    try {
        if (customerJsonPath && fs.existsSync(customerJsonPath)) {
            const state = JSON.parse(fs.readFileSync(customerJsonPath, "utf8"));
            for (const user of Object.values(state.users || {})) { store.upsertUser(user); report.users += 1; }
            for (const challenge of Object.values(state.challenges || {})) { store.saveChallenge(challenge); report.challenges += 1; }
            for (const behavior of state.behavior || []) { store.appendBehavior(behavior); report.behavior += 1; }
        }
        if (sessionJsonPath && fs.existsSync(sessionJsonPath)) {
            const state = JSON.parse(fs.readFileSync(sessionJsonPath, "utf8"));
            for (const record of Object.values(state.sessions || {})) { sessions.save(record); report.sessions += 1; }
            for (const record of Object.values(state.transactions || {})) { sessions.saveTransaction(record); report.transactions += 1; }
        }
    } finally {
        store.close();
        sessions.close();
    }
    return { success: true, database, sessionDatabase, imported: report };
}

module.exports = { SqliteCustomerStore, SqliteDemoSessionStore, migrateJsonToSqlite };
