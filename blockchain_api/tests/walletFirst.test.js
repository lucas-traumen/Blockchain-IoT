const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { ethers } = require("ethers");
const { createApp } = require("../server");
const { CustomerStore } = require("../customerStore");
const { CustomerService } = require("../customerService");
const { WalletService } = require("../walletService");
const { JsonSessionStore } = require("../sessionStore");
const { buildWalletFirstService } = require("../walletFirstService");
const { LiveSessionService } = require("../liveSessionService");

const TOKENS = { HRC_INGEST_API_TOKEN: "i".repeat(40), HRC_ADMIN_API_TOKEN: "a".repeat(40), HRC_APP_AUTH_MODE: "siwe" };

async function fixture(t, { authMode = "siwe" } = {}) {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "hrc-wallet-first-"));
    const store = new CustomerStore(path.join(directory, "customers.json"));
    const customerService = new CustomerService({ store, chainId: 31337, authMode, appOrigin: "http://localhost:8080" });
    const walletService = new WalletService({
        users: new Map(),
        store: new JsonSessionStore(path.join(directory, "sessions.json")),
        config: { chainId: 31337 },
        tokens: { ...TOKENS, HRC_APP_AUTH_MODE: authMode }
    });
    // Keep the wallet chain reads deterministic and offline for this test.
    walletService.getWallet = async () => ({
        walletAddress: "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC", chainId: 31337,
        rewardsAddress: "0x9fE46736679d2D9a65F0992F2272dE9f3c7fa6e0", tokenAddress: "0xe7f1725E7734CE288F8367e1Bb143E90bb3F0512",
        symbol: "HRC", decimals: 18, stakeAmount: "2.0", requiredAllowance: "2.0"
    });
    const walletFirst = buildWalletFirstService(customerService, () => walletService);
    const endCalls = [];
    // Exercise the real End guards, ownership and duplicate handling at the HTTP seam.
    // Sensor/chain settlement itself is covered by liveSessionService.test.js.
    const lifecycle = new LiveSessionService({ walletService: walletService, iot: {}, settler: {}, logger: () => {} });
    lifecycle.response = async (record, extra = {}) => ({ success: true, sessionId: record.sessionId, stage: record.stage, settled: record.stage === "SETTLED", ...extra });
    lifecycle.advanceEnd = async record => {
        endCalls.push(record.sessionId);
        return lifecycle.response(walletService.store.update(record.sessionId, { stage: "SETTLED" }));
    };
    const liveService = {
        async wallet(userId) { return { success: true, userId }; },
        async sessions(userId) { return { success: true, userId, count: 0, sessions: [] }; },
        async session(userId, sessionId) { return { success: true, session: { userId, sessionId } }; },
        async start() { return { success: true, mode: "backend" }; },
        async confirmWalletStart(userId, sessionId, body) {
            walletService.store.save({ sessionId, userId, source: "esp32", stage: "RUNNING", createdAt: new Date().toISOString() });
            return { success: true, userId, sessionId, stage: "RUNNING", transactionHash: body.transactionHash };
        },
        async reserveWalletStart(userId, sessionId) {
            walletService.store.save({ sessionId, userId, source: "esp32", stage: "WALLET_INTENT", intentExpiresAt: Date.now() + 120000, createdAt: new Date().toISOString() });
        },
        end: lifecycle.end.bind(lifecycle)
    };
    const server = createApp({ service: {}, customerService, walletService, liveService, tokens: { ...TOKENS, HRC_APP_AUTH_MODE: authMode } }).listen(0, "127.0.0.1");
    await new Promise((resolve, reject) => { server.once("listening", resolve); server.once("error", reject); });
    t.after(() => { server.close(); store.close(); fs.rmSync(directory, { recursive: true, force: true }); });
    const base = `http://127.0.0.1:${server.address().port}`;
    const call = async (route, { method = "GET", body, token } = {}) => {
        const headers = {};
        if (body !== undefined) headers["Content-Type"] = "application/json";
        if (token) headers.Authorization = "Bearer " + token;
        const response = await fetch(base + route, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
        return { status: response.status, body: await response.json() };
    };
    const login = async () => {
        const wallet = ethers.Wallet.createRandom();
        const challenge = await call("/api/auth/challenge", { method: "POST", body: { walletAddress: wallet.address, chainId: 31337 } });
        const signature = await wallet.signMessage(challenge.body.message);
        const result = await call("/api/auth/wallet", { method: "POST", body: { walletAddress: wallet.address, chainId: 31337, message: challenge.body.message, signature } });
        return { wallet, ...result };
    };
    return { call, login, walletService, endCalls };
}

test("wallet login issues one durable app token for both application and reward routes", async t => {
    const f = await fixture(t);
    const login = await f.login();
    assert.equal(login.status, 200);
    assert.equal(login.body.mode, "siwe");
    assert.ok(login.body.token, "application token");
    assert.equal(login.body.sessionToken, undefined);
    assert.match(login.body.user.userId, /^usr_/);
    assert.equal(login.body.rewardUser.userId, login.body.user.userId);

    assert.equal((await f.call("/api/me", { token: login.body.token })).status, 200);
    assert.equal((await f.call("/api/me/sessions", { token: login.body.token })).status, 200);
    assert.equal((await f.call("/api/me/wallet", { token: login.body.token })).status, 200);
    const logout = await f.call("/api/auth/logout", { method: "POST", token: login.body.token });
    assert.equal(logout.status, 200);
    assert.equal((await f.call("/api/me/sessions", { token: login.body.token })).status, 401);
});

test("wallet login migrates legacy reward session ownership to the verified canonical wallet identity", async t => {
    const f = await fixture(t);
    const wallet = ethers.Wallet.createRandom();
    const legacyUserId = "wallet_" + wallet.address.slice(2).toLowerCase();
    f.walletService.store.save({ sessionId: "legacy-session", userId: legacyUserId, stage: "SETTLED", createdAt: new Date().toISOString() });
    const challenge = await f.call("/api/auth/challenge", { method: "POST", body: { walletAddress: wallet.address, chainId: 31337 } });
    const signature = await wallet.signMessage(challenge.body.message);
    const login = await f.call("/api/auth/wallet", { method: "POST", body: { walletAddress: wallet.address, chainId: 31337, message: challenge.body.message, signature } });
    assert.equal(f.walletService.store.get("legacy-session").userId, login.body.user.userId);
});

test("wallet-first Approve and Start prepare signatures; End executes the operator lifecycle", async t => {
    const f = await fixture(t);
    const login = await f.login();
    const token = login.body.token;

    const approve = await f.call("/api/me/wallet/prepare/approve", { token });
    assert.equal(approve.status, 200);
    assert.equal(approve.body.mode, "wallet-first");
    // approve() is called on the ERC-20 token, spending for the reward contract.
    assert.equal(approve.body.to, "0xe7f1725E7734CE288F8367e1Bb143E90bb3F0512");
    // approve(address,uint256) selector with the 2 HRC stake amount.
    assert.equal(approve.body.data.slice(0, 10), "0x095ea7b3");
    assert.equal(BigInt("0x" + approve.body.data.slice(-64)), 2000000000000000000n);

    const start = await f.call("/api/me/sessions/start", { method: "POST", token, body: { sessionRef: "wallet-first" } });
    assert.equal(start.status, 200);
    assert.equal(start.body.mode, "wallet-first");
    assert.match(start.body.sessionId, /^hrc-[0-9a-f]{8}-wallet-first$/);
    assert.match(start.body.data, /^0x[0-9a-f]{8}/);
    assert.equal(start.body.operatorOnly, undefined);

    const confirmed = await f.call(`/api/me/sessions/${start.body.sessionId}/confirm-start`, {
        method: "POST", token, body: { transactionHash: "0x" + "b".repeat(64) }
    });
    assert.equal(confirmed.status, 200);
    assert.equal(confirmed.body.stage, "RUNNING");

    const end = await f.call(`/api/me/sessions/${start.body.sessionId}/end`, { method: "POST", token, body: {} });
    assert.equal(end.status, 200);
    assert.equal(end.body.settled, true);
    assert.equal(end.body.stage, "SETTLED");
    assert.equal(end.body.operatorOnly, undefined);
    assert.deepEqual(f.endCalls, [start.body.sessionId]);
    const repeated = await f.call(`/api/me/sessions/${start.body.sessionId}/end`, { method: "POST", token, body: {} });
    assert.equal(repeated.status, 200);
    assert.equal(repeated.body.duplicate, true);
    assert.equal(f.endCalls.length, 1);
});

test("a wallet cannot End another wallet's session", async t => {
    const f = await fixture(t);
    const first = await f.login();
    const second = await f.login();
    const start = await f.call("/api/me/sessions/start", { method: "POST", token: first.body.token, body: { sessionRef: "mine" } });
    // Record the session for the first wallet through the live session service path.
    f.walletService.store.save({
        sessionId: start.body.sessionId, userId: first.body.user.userId, sessionRef: "mine",
        createdAt: new Date().toISOString(), stage: "RUNNING"
    });
    const attack = await f.call(`/api/me/sessions/${start.body.sessionId}/end`, { method: "POST", token: second.body.token, body: {} });
    assert.equal(attack.status, 403);
    assert.equal(attack.body.code, "SESSION_OWNER_MISMATCH");
    assert.equal(f.endCalls.length, 0);
});

test("fixed demo users and legacy wallet mode are unavailable", () => {
    assert.throws(() => createApp({ service: {}, tokens: { HRC_APP_AUTH_MODE: "demo" } }), /must be siwe/);
    assert.throws(() => createApp({ service: {}, tokens: { HRC_APP_AUTH_MODE: "wallet" } }), /must be siwe/);
});

test("wallet-first End rejects user-chosen outcomes and rewards", async t => {
    const f = await fixture(t);
    const login = await f.login();
    const result = await f.call("/api/me/sessions/any-session/end", {
        method: "POST", token: login.body.token, body: { outcome: "VALID_NO_VIOLATION", rewardPaid: "100" }
    });
    assert.equal(result.status, 400);
    assert.equal(result.body.code, "INVALID_PAYLOAD");
    assert.equal(f.endCalls.length, 0);
});
