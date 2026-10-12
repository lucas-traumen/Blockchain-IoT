const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { SqliteCustomerStore, SqliteDemoSessionStore, migrateJsonToSqlite } = require("../sqliteStores");

function tempDir(t) {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "hrc-sqlite-"));
    t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
    return directory;
}

test("sqlite customer store keeps users, challenges, sessions and behavior", t => {
    const directory = tempDir(t);
    const store = new SqliteCustomerStore(path.join(directory, "app.db"));
    t.after(() => store.close());

    store.upsertUser({ userId: "user_1", walletAddress: "0xAbC", behaviorCount: 0 });
    store.upsertUser({ userId: "user_1", walletAddress: "0xAbC", behaviorCount: 3 });
    assert.equal(store.getUser("user_1").behaviorCount, 3);
    assert.equal(store.findUserByWallet("0xabc").userId, "user_1");
    assert.equal(store.findUserByWallet("0xmissing"), null);

    const challenge = { walletAddress: "0xabc", chainId: 31337, nonce: "nonce-1", message: "m1", issuedAt: "2026-01-01T00:00:00.000Z", expiresAt: "2026-01-01T00:05:00.000Z", used: false };
    store.saveChallenge(challenge);
    assert.equal(store.getChallenge("0xabc", "m1").nonce, "nonce-1");
    assert.equal(store.getChallenge("0xabc", "other"), null);
    assert.equal(store.markChallengeUsed(challenge).used, true);
    assert.equal(store.getChallenge("0xabc", "m1").used, true);

    store.saveSession({ tokenHash: "hash-1", userId: "user_1", expiresAt: "2030-01-01T00:00:00.000Z" });
    assert.equal(store.getSession("hash-1").userId, "user_1");
    assert.equal(store.revokeSession("hash-1"), true);
    assert.equal(store.revokeSession("hash-1"), false);

    store.appendBehavior({ userId: "user_1", idempotencyKey: "k1", action: "page_view", at: "2026-01-01T00:00:00.000Z" });
    store.appendBehavior({ userId: "user_1", idempotencyKey: "k2", action: "wallet_login", at: "2026-01-01T00:00:01.000Z" });
    assert.equal(store.behaviorByIdempotency("user_1", "k1").action, "page_view");
    assert.equal(store.listBehavior("user_1").length, 2);
    assert.equal(store.listBehavior("user_1", 1).length, 1);
});

test("sqlite session store keeps session ownership and transaction snapshots", t => {
    const directory = tempDir(t);
    const store = new SqliteDemoSessionStore(path.join(directory, "sessions.db"));
    t.after(() => store.close());

    store.save({ sessionId: "hrc-abc-demo", userId: "user_1", sessionRef: "demo", createdAt: "2026-01-01T00:00:00.000Z" });
    store.update("hrc-abc-demo", { ended: true });
    assert.equal(store.get("hrc-abc-demo").ended, true);
    assert.equal(store.list("user_1").length, 1);
    assert.equal(store.list("user_2").length, 0);
    assert.throws(() => store.save({ sessionId: "hrc-abc-demo", userId: "user_2" }), /Cannot change the owner/);
    assert.equal(store.reassignOwner("user_1", "usr_wallet"), true);
    assert.equal(store.list("user_1").length, 0);
    assert.equal(store.get("hrc-abc-demo").userId, "usr_wallet");

    store.saveTransaction({ key: "stake:hrc-abc-demo", transactionHash: "0x1" });
    store.saveTransaction({ key: "stake:hrc-abc-demo", blockNumber: 7 });
    assert.deepEqual(
        { hash: store.getTransaction("stake:hrc-abc-demo").transactionHash, block: store.getTransaction("stake:hrc-abc-demo").blockNumber },
        { hash: "0x1", block: 7 }
    );
    assert.equal(store.listTransactions().length, 1);
});

test("migration imports the JSON snapshots once", t => {
    const directory = tempDir(t);
    const customers = path.join(directory, "customers.json");
    const sessions = path.join(directory, "demo-sessions.json");
    fs.writeFileSync(customers, JSON.stringify({
        users: { user_1: { userId: "user_1", walletAddress: "0xabc", behaviorCount: 1 } },
        challenges: { "nonce-1": { walletAddress: "0xabc", nonce: "nonce-1", message: "m", issuedAt: "2026-01-01T00:00:00.000Z" } },
        sessions: {},
        behavior: [{ userId: "user_1", idempotencyKey: "k1", at: "2026-01-01T00:00:00.000Z" }]
    }));
    fs.writeFileSync(sessions, JSON.stringify({
        sessions: { "hrc-abc-demo": { sessionId: "hrc-abc-demo", userId: "user_1", createdAt: "2026-01-01T00:00:00.000Z" } },
        transactions: { "stake:hrc-abc-demo": { key: "stake:hrc-abc-demo", transactionHash: "0x1" } }
    }));

    const db = path.join(directory, "app.db");
    const report = migrateJsonToSqlite({ customerJsonPath: customers, sessionJsonPath: sessions, dbPath: db, sessionDbPath: db });
    assert.deepEqual(report.imported, { users: 1, challenges: 1, behavior: 1, sessions: 1, transactions: 1 });

    const store = new SqliteCustomerStore(db);
    assert.equal(store.getUser("user_1").walletAddress, "0xabc");
    assert.equal(store.listBehavior("user_1").length, 1);
    store.close();
    const sessionStore = new SqliteDemoSessionStore(db);
    assert.equal(sessionStore.get("hrc-abc-demo").userId, "user_1");
    assert.equal(sessionStore.getTransaction("stake:hrc-abc-demo").transactionHash, "0x1");
    sessionStore.close();

    // Re-running is additive and must not duplicate or fail.
    const again = migrateJsonToSqlite({ customerJsonPath: customers, sessionJsonPath: sessions, dbPath: db, sessionDbPath: db });
    assert.equal(again.imported.sessions, 1);
});
