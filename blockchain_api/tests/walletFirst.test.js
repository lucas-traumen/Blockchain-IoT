const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { ethers } = require("ethers");
const { createApp } = require("../server");
const { CustomerStore } = require("../customerStore");
const { CustomerService } = require("../customerService");
const { DemoWalletService } = require("../demoWalletService");
const { DemoSessionStore } = require("../demoSessionStore");
const { buildWalletFirstService } = require("../walletFirstService");

const TOKENS = { HRC_INGEST_API_TOKEN: "i".repeat(40), HRC_ADMIN_API_TOKEN: "a".repeat(40), HRC_APP_AUTH_MODE: "siwe" };

async function fixture(t, { authMode = "siwe" } = {}) {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "hrc-wallet-first-"));
    const store = new CustomerStore(path.join(directory, "customers.json"));
    const customerService = new CustomerService({ store, chainId: 31337, authMode, appOrigin: "http://localhost:8080" });
    const demoService = new DemoWalletService({
        users: new Map([["user_1", { userId: "user_1", label: "Demo user 1", role: "customer", wallet: null, walletAddress: "0x90F79bf6EB2c4f870365E785982E1f101E93b906" }]]),
        store: new DemoSessionStore(path.join(directory, "demo-sessions.json")),
        config: { chainId: 31337 },
        tokens: { ...TOKENS, HRC_APP_AUTH_MODE: authMode }
    });
    // Keep the wallet chain reads deterministic and offline for this test.
    demoService.getWallet = async () => ({
        walletAddress: "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC", chainId: 31337,
        rewardsAddress: "0x9fE46736679d2D9a65F0992F2272dE9f3c7fa6e0", tokenAddress: "0xe7f1725E7734CE288F8367e1Bb143E90bb3F0512",
        symbol: "HRC", decimals: 18, stakeAmount: "2.0", requiredAllowance: "2.0"
    });
    const walletFirst = buildWalletFirstService(customerService, () => demoService);
    const liveService = {
        async sessions(userId) { return { success: true, userId, count: 0, sessions: [] }; },
        async session(userId, sessionId) { return { success: true, session: { userId, sessionId } }; },
        async start() { return { success: true, mode: "backend" }; },
        async end() { return { success: true, settled: true }; }
    };
    const server = createApp({ service: {}, customerService, demoService, liveService, walletFirst, tokens: { ...TOKENS, HRC_APP_AUTH_MODE: authMode } }).listen(0, "127.0.0.1");
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
    return { call, login, demoService };
}

test("wallet login issues an app token and a session token", async t => {
    const f = await fixture(t);
    const login = await f.login();
    assert.equal(login.status, 200);
    assert.equal(login.body.mode, "siwe");
    assert.ok(login.body.token, "application token");
    assert.ok(login.body.sessionToken, "reward session token");
    assert.match(login.body.user.userId, /^wallet_/);
    assert.equal(login.body.user.walletFirst, true);

    assert.equal((await f.call("/api/me", { token: login.body.token })).status, 200);
    assert.equal((await f.call("/api/me/sessions", { token: login.body.sessionToken })).status, 200);
    // Tokens are not interchangeable across the two surfaces.
    assert.equal((await f.call("/api/me/sessions", { token: login.body.token })).status, 401);
});

test("wallet-first sessions are prepared for the user to sign, never broadcast", async t => {
    const f = await fixture(t);
    const login = await f.login();
    const token = login.body.sessionToken;

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

    const end = await f.call(`/api/me/sessions/${start.body.sessionId}/end`, { method: "POST", token, body: {} });
    assert.equal(end.status, 200);
    assert.equal(end.body.operatorOnly, true);
    assert.equal(end.body.data, null);
    assert.match(end.body.description, /operator/i);
});

test("a wallet cannot prepare a session for another wallet's session id", async t => {
    const f = await fixture(t);
    const first = await f.login();
    const second = await f.login();
    const start = await f.call("/api/me/sessions/start", { method: "POST", token: first.body.sessionToken, body: { sessionRef: "mine" } });
    // Record the session for the first wallet through the live session service path.
    f.demoService.store.save({
        sessionId: start.body.sessionId, userId: first.body.user.userId, sessionRef: "mine",
        createdAt: new Date().toISOString(), stage: "RUNNING"
    });
    const attack = await f.call(`/api/me/sessions/${start.body.sessionId}/end`, { method: "POST", token: second.body.sessionToken, body: {} });
    assert.equal(attack.status, 403);
    assert.equal(attack.body.code, "SESSION_OWNER_MISMATCH");
});

test("wallet mode keeps the same prepared-transaction flow", async t => {
    const f = await fixture(t, { authMode: "wallet" });
    const login = await f.login();
    assert.equal(login.body.mode, "wallet");
    const start = await f.call("/api/me/sessions/start", { method: "POST", token: login.body.sessionToken, body: { sessionRef: "legacy-mode" } });
    assert.equal(start.status, 200);
    assert.equal(start.body.mode, "wallet-first");
});

test("the backend-managed demo users still broadcast their own transactions", async t => {
    const f = await fixture(t);
    assert.equal(f.demoService.isWalletFirst("user_1"), false);
    assert.equal(f.demoService.isWalletFirst("wallet_deadbeef"), false);
});
