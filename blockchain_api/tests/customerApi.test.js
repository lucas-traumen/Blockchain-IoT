const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { ethers } = require("ethers");
const { createApp } = require("../server");
const { CustomerStore } = require("../customerStore");
const { CustomerService } = require("../customerService");

async function fixture(t, { adminWallets = [], walletAuth = true } = {}) {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "hrc-customer-api-"));
    const store = new CustomerStore(path.join(directory, "customers.json"));
    const customerService = new CustomerService({ store, chainId: 31337, adminWallets });
    const server = createApp({ service: {}, customerService, tokens: {
        HRC_INGEST_API_TOKEN: "i".repeat(40), HRC_ADMIN_API_TOKEN: "a".repeat(40),
        ...(walletAuth ? { HRC_APP_AUTH_MODE: "siwe" } : {})
    }}).listen(0, "127.0.0.1");
    await new Promise((resolve, reject) => { server.once("listening", resolve); server.once("error", reject); });
    t.after(() => {
        server.close();
        store.close();
        fs.rmSync(directory, { recursive: true, force: true });
    });
    const base = `http://127.0.0.1:${server.address().port}`;
    const call = async (route, { method = "GET", body, token } = {}) => {
        const headers = {};
        if (body !== undefined) headers["Content-Type"] = "application/json";
        if (token) headers.Authorization = "Bearer " + token;
        const response = await fetch(base + route, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
        return { status: response.status, body: await response.json() };
    };
    return { customerService, call };
}

async function login(call, wallet) {
    const challenge = await call("/api/auth/challenge", { method: "POST", body: { walletAddress: wallet.address, chainId: 31337 } });
    assert.equal(challenge.status, 200);
    const signature = await wallet.signMessage(challenge.body.message);
    const response = await call("/api/auth/wallet", { method: "POST", body: {
        walletAddress: wallet.address, chainId: 31337, message: challenge.body.message, signature
    }});
    assert.equal(response.status, 200);
    return response.body;
}

test("wallet challenge login creates a customer session and behavior is idempotent", async t => {
    const f = await fixture(t);
    const wallet = ethers.Wallet.createRandom();
    assert.equal((await f.call("/api/me")).status, 401);
    assert.equal((await f.call("/api/auth/challenge", { method: "POST", body: { walletAddress: wallet.address, chainId: 1 } })).status, 400);
    const loginResult = await login(f.call, wallet);
    assert.equal(loginResult.user.role, "customer");
    assert.equal(loginResult.user.walletAddress, wallet.address);

    const event = await f.call("/api/behavior", { method: "POST", token: loginResult.token, body: {
        eventType: "wallet_connected", page: "/", idempotencyKey: "wallet-connected-1", metadata: { chainId: 31337 }
    }});
    assert.equal(event.status, 200);
    assert.equal(event.body.duplicate, false);
    const duplicate = await f.call("/api/behavior", { method: "POST", token: loginResult.token, body: {
        eventType: "wallet_connected", page: "/", idempotencyKey: "wallet-connected-1", metadata: { chainId: 31337 }
    }});
    assert.equal(duplicate.status, 200);
    assert.equal(duplicate.body.duplicate, true);
    assert.equal(duplicate.body.event.eventId, event.body.event.eventId);

    const me = await f.call("/api/me", { token: loginResult.token });
    assert.equal(me.status, 200);
    assert.equal(me.body.user.behaviorCount, 1);
    const behavior = await f.call("/api/me/behavior", { token: loginResult.token });
    assert.equal(behavior.status, 200);
    assert.equal(behavior.body.count, 1);
    assert.equal(behavior.body.events[0].eventType, "wallet_connected");
});

test("wallet signatures are one-time and customer directory requires an admin wallet", async t => {
    const admin = ethers.Wallet.createRandom();
    const customer = ethers.Wallet.createRandom();
    const f = await fixture(t, { adminWallets: [admin.address] });
    const challenge = await f.call("/api/auth/challenge", { method: "POST", body: { walletAddress: customer.address, chainId: 31337 } });
    const signature = await admin.signMessage(challenge.body.message);
    assert.equal((await f.call("/api/auth/wallet", { method: "POST", body: {
        walletAddress: customer.address, chainId: 31337, message: challenge.body.message, signature
    }})).status, 401);
    const signed = await customer.signMessage(challenge.body.message);
    const loggedIn = await f.call("/api/auth/wallet", { method: "POST", body: {
        walletAddress: customer.address, chainId: 31337, message: challenge.body.message, signature: signed
    }});
    assert.equal(loggedIn.status, 200);
    assert.equal((await f.call("/api/auth/wallet", { method: "POST", body: {
        walletAddress: customer.address, chainId: 31337, message: challenge.body.message, signature: signed
    }})).status, 401);
    assert.equal((await f.call("/api/customers", { token: loggedIn.body.token })).status, 403);

    const adminLogin = await login(f.call, admin);
    assert.equal(adminLogin.user.role, "admin");
    assert.equal((await f.call("/api/customers", { token: adminLogin.token })).body.count, 2);
});

test("concurrent challenges remain independently valid and idempotency conflicts are rejected", async t => {
    const f = await fixture(t);
    const wallet = ethers.Wallet.createRandom();
    const first = await f.call("/api/auth/challenge", { method: "POST", body: { walletAddress: wallet.address, chainId: 31337 } });
    const second = await f.call("/api/auth/challenge", { method: "POST", body: { walletAddress: wallet.address, chainId: 31337 } });
    const firstLogin = await f.call("/api/auth/wallet", { method: "POST", body: {
        walletAddress: wallet.address, chainId: 31337, message: first.body.message,
        signature: await wallet.signMessage(first.body.message)
    }});
    const secondLogin = await f.call("/api/auth/wallet", { method: "POST", body: {
        walletAddress: wallet.address, chainId: 31337, message: second.body.message,
        signature: await wallet.signMessage(second.body.message)
    }});
    assert.equal(firstLogin.status, 200);
    assert.equal(secondLogin.status, 200);
    const eventBody = { eventType: "page_view", page: "/user", idempotencyKey: "stable-key-1", metadata: { view: "user" } };
    assert.equal((await f.call("/api/behavior", { method: "POST", token: secondLogin.body.token, body: eventBody })).status, 200);
    const conflict = await f.call("/api/behavior", { method: "POST", token: secondLogin.body.token, body: {
        ...eventBody, metadata: { view: "customers" }
    }});
    assert.equal(conflict.status, 409);
    assert.equal(conflict.body.code, "BEHAVIOR_CONFLICT");
});

test("customer store survives reopen and recovers a dead writer lock", () => {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "hrc-customer-store-"));
    const file = path.join(directory, "customers.json");
    const first = new CustomerStore(file);
    first.upsertUser({ userId: "usr_test", walletAddress: "0x0000000000000000000000000000000000000001", role: "customer", status: "active", createdAt: "now", lastSeenAt: "now", behaviorCount: 0, profile: { displayName: null } });
    first.close();
    fs.writeFileSync(file + ".lock", String(2147483647));
    const reopened = new CustomerStore(file);
    assert.equal(reopened.getUser("usr_test").userId, "usr_test");
    reopened.close();
    fs.rmSync(directory, { recursive: true, force: true });
});

test("profile update, logout and inactive accounts are enforced", async t => {
    const wallet = ethers.Wallet.createRandom();
    const f = await fixture(t);
    const loginResult = await login(f.call, wallet);
    const profile = await f.call("/api/me/profile", { method: "PATCH", token: loginResult.token, body: { displayName: "Alice" } });
    assert.equal(profile.status, 200);
    assert.equal(profile.body.user.profile.displayName, "Alice");
    const logout = await f.call("/api/auth/logout", { method: "POST", token: loginResult.token });
    assert.equal(logout.status, 200);
    assert.equal((await f.call("/api/me", { token: loginResult.token })).status, 401);
    const secondLogin = await login(f.call, wallet);
    f.customerService.store.upsertUser({ ...f.customerService.store.getUser(secondLogin.user.userId), status: "inactive" });
    const challenge = await f.call("/api/auth/challenge", { method: "POST", body: { walletAddress: wallet.address, chainId: 31337 } });
    const inactive = await f.call("/api/auth/wallet", { method: "POST", body: {
        walletAddress: wallet.address, chainId: 31337, message: challenge.body.message,
        signature: await wallet.signMessage(challenge.body.message)
    }});
    assert.equal(inactive.status, 403);
});


test("default mode creates a wallet identity only after a valid SIWE signature", async t => {
    const f = await fixture(t, { walletAuth: false });
    const wallet = ethers.Wallet.createRandom();
    const body = { walletAddress: wallet.address, chainId: 31337 };
    const challenge = await f.call("/api/auth/challenge", { method: "POST", body });
    assert.equal(challenge.body.mode, "siwe");
    assert.equal(f.customerService.store.listUsers().length, 0);
    assert.equal((await f.call("/api/auth/wallet", { method: "POST", body })).status, 400);
    const result = await login(f.call, wallet);
    assert.equal(result.mode, "siwe");
    assert.equal(f.customerService.store.listUsers().length, 1);
});
