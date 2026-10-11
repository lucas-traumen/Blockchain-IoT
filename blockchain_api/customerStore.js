const fs = require("node:fs");
const path = require("node:path");

function emptyState() {
    return { users: {}, challenges: {}, sessions: {}, behavior: [] };
}

function clone(value) {
    return structuredClone(value);
}

// A small durable store for the application identity layer. Blockchain event
// storage stays separate because wallet sessions and product analytics have a
// different retention and access policy.
class CustomerStore {
    constructor(filePath) {
        this.filePath = path.resolve(filePath);
        this.directory = path.dirname(this.filePath);
        this.lockPath = this.filePath + ".lock";
        this.recoveryPath = this.filePath + ".recovery.lock";
        fs.mkdirSync(this.directory, { recursive: true, mode: 0o700 });
        this.lock = this._acquireLock();
        try {
            this.state = fs.existsSync(this.filePath)
                ? JSON.parse(fs.readFileSync(this.filePath, "utf8"))
                : emptyState();
            this._validate();
        } catch (error) {
            this.close();
            throw error;
        }
    }

    _acquireLock() {
        try {
            const lock = fs.openSync(this.lockPath, "wx", 0o600);
            fs.writeFileSync(lock, String(process.pid));
            fs.fsyncSync(lock);
            return lock;
        } catch (error) {
            if (error.code !== "EEXIST") throw error;

            // Serialize stale-lock recovery. A second recovering process must
            // never remove a lock that the first process just acquired.
            let recovery;
            try { recovery = fs.openSync(this.recoveryPath, "wx", 0o600); }
            catch { throw new Error("Customer store lock recovery is in progress; verify writers before removing the stale lock"); }
            try {
                let pid;
                try { pid = Number(fs.readFileSync(this.lockPath, "utf8")); }
                catch (readError) { throw new Error("Customer store lock is incomplete; verify no writer is running before removing it", { cause: readError }); }
                if (!Number.isInteger(pid) || pid < 1 || pid > 2147483647) {
                    throw new Error("Customer store lock is incomplete; verify no writer is running before removing it");
                }
                try { process.kill(pid, 0); }
                catch (check) {
                    if (check.code !== "ESRCH") throw check;
                    try { fs.unlinkSync(this.lockPath); }
                    catch (unlinkError) {
                        if (unlinkError.code !== "ENOENT") throw unlinkError;
                    }
                }
                try {
                    const lock = fs.openSync(this.lockPath, "wx", 0o600);
                    fs.writeFileSync(lock, String(process.pid));
                    fs.fsyncSync(lock);
                    return lock;
                } catch (retryError) {
                    if (retryError.code === "EEXIST") throw new Error("Customer store is already open by another process");
                    throw retryError;
                }
            } finally {
                fs.closeSync(recovery);
                try { fs.unlinkSync(this.recoveryPath); } catch (cleanupError) {
                    if (cleanupError.code !== "ENOENT") throw cleanupError;
                }
            }
        }
    }

    _validate() {
        if (!this.state || typeof this.state !== "object" || Array.isArray(this.state)) {
            throw new Error("Invalid customer store state");
        }
        for (const key of ["users", "challenges", "sessions"]) {
            if (!this.state[key] || typeof this.state[key] !== "object" || Array.isArray(this.state[key])) {
                throw new Error("Invalid customer store " + key);
            }
        }
        if (!Array.isArray(this.state.behavior)) throw new Error("Invalid customer store behavior");
    }

    _writeState(nextState) {
        if (this.lock === undefined) throw new Error("Customer store is closed");
        const tempPath = this.filePath + ".tmp";
        const fd = fs.openSync(tempPath, "w", 0o600);
        try {
            fs.writeFileSync(fd, JSON.stringify(nextState));
            fs.fsyncSync(fd);
        } finally {
            fs.closeSync(fd);
        }
        try {
            fs.renameSync(tempPath, this.filePath);
            // Persist the directory entry where supported. This is harmless on
            // platforms that do not permit opening directories for fsync.
            try {
                const directoryFd = fs.openSync(this.directory, "r");
                try { fs.fsyncSync(directoryFd); } finally { fs.closeSync(directoryFd); }
            } catch { /* The file rename is still durable on supported filesystems. */ }
        } catch (error) {
            try { fs.unlinkSync(tempPath); } catch (cleanupError) {
                if (cleanupError.code !== "ENOENT") error.cleanupError = cleanupError;
            }
            throw error;
        }
        this.state = nextState;
    }

    _commit(mutator) {
        const nextState = clone(this.state);
        mutator(nextState);
        this._writeState(nextState);
    }

    getUser(userId) {
        const user = this.state.users[userId];
        return user ? clone(user) : null;
    }

    findUserByWallet(walletAddress) {
        const address = walletAddress.toLowerCase();
        const user = Object.values(this.state.users).find(item => item.walletAddress === address);
        return user ? clone(user) : null;
    }

    listUsers() {
        return Object.values(this.state.users).map(user => clone(user));
    }

    upsertUser(user) {
        this._commit(state => { state.users[user.userId] = clone(user); });
        return clone(user);
    }

    getChallenge(walletAddress, message) {
        const address = walletAddress.toLowerCase();
        const challenges = Object.values(this.state.challenges)
            .filter(challenge => challenge.walletAddress === address && (!message || challenge.message === message))
            .sort((a, b) => String(b.issuedAt).localeCompare(String(a.issuedAt)));
        if (challenges.length) return clone(challenges[0]);

        // Read compatibility for the first MVP format, which keyed the single
        // challenge by wallet address instead of nonce.
        const legacy = this.state.challenges[address];
        return legacy && (!message || legacy.message === message) ? clone(legacy) : null;
    }

    saveChallenge(challenge) {
        this._commit(state => {
            // New challenges use the nonce as the key, so two MetaMask tabs can
            // complete their own valid challenge without invalidating each other.
            state.challenges[challenge.nonce] = clone(challenge);
        });
        return clone(challenge);
    }

    markChallengeUsed(challenge) {
        this._commit(state => {
            const key = state.challenges[challenge.nonce] ? challenge.nonce : challenge.walletAddress;
            state.challenges[key] = { ...clone(challenge), used: true };
        });
        return clone({ ...challenge, used: true });
    }

    getSession(tokenHash) {
        const session = this.state.sessions[tokenHash];
        return session ? clone(session) : null;
    }

    saveSession(session) {
        this._commit(state => { state.sessions[session.tokenHash] = clone(session); });
        return clone(session);
    }

    revokeSession(tokenHash) {
        if (!this.state.sessions[tokenHash]) return false;
        this._commit(state => { delete state.sessions[tokenHash]; });
        return true;
    }

    behaviorByIdempotency(userId, idempotencyKey) {
        const event = this.state.behavior.find(item => item.userId === userId && item.idempotencyKey === idempotencyKey);
        return event ? clone(event) : null;
    }

    appendBehavior(event, updatedUser) {
        this._commit(state => {
            state.behavior.push(clone(event));
            if (updatedUser) state.users[updatedUser.userId] = clone(updatedUser);
        });
        return clone(event);
    }

    listBehavior(userId, limit = 100) {
        return this.state.behavior
            .filter(item => !userId || item.userId === userId)
            .slice(-limit)
            .reverse()
            .map(item => clone(item));
    }

    close() {
        if (this.lock !== undefined) {
            fs.closeSync(this.lock);
            this.lock = undefined;
            try { fs.unlinkSync(this.lockPath); } catch (error) {
                if (error.code !== "ENOENT") throw error;
            }
        }
    }
}

module.exports = { CustomerStore };
