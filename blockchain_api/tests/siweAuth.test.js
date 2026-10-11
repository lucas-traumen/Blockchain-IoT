const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { ethers } = require("ethers");
const { createApp } = require("../server");
const { CustomerStore } = require("../customerStore");
const { CustomerService } = require("../customerService");
const { buildSiweMessage, parseSiweMessage } = require("../siweMessage");

async function fixture(t, { authMode = "siwe" } = {}) {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "hrc-siwe-api-"));
    const store = new CustomerStore(path.join(directory, "customers.json"));
    const customerService = new CustomerService({ store, chainId: 31337, authMode, appOrigin: "http://localhost:8080" });
    const server = createApp({ service: {}, customerService, tokens: {
        HRC_INGEST_API_TOKEN: "i".repeat(40), HRC_ADMIN_API_TOKEN: "a".repeat(40), HRC_APP_AUTH_MODE: authMode
    }}).listen(0, "127.0.0.1");
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
    return { call, customerService };
}

test("siwe mode issues an EIP-4361 message and logs in with it", async t => {
    const f = await fixture(t);
    const wallet = ethers.Wallet.createRandom();
    const challenge = await f.call("/api/auth/challenge", { method: "POST", body: { walletAddress: wallet.address, chainId: 31337 } });
    assert.equal(challenge.status, 200);
    assert.equal(challenge.body.mode, "siwe");
    assert.match(challenge.body.message, /^localhost:8080 wants you to sign in with your Ethereum account:$/m);
    assert.match(challenge.body.message, /^Version: 1$/m);
    assert.match(challenge.body.message, /^Chain ID: 31337$/m);
    assert.match(challenge.body.message, /^Nonce: [A-Za-z0-9]{8,}$/m);
    assert.match(challenge.body.message, /^URI: http:\/\/localhost:8080$/m);
    assert.match(challenge.body.message, /^Issued At: .+$/m);
    assert.match(challenge.body.message, /^Expiration Time: .+$/m);

    const parsed = parseSiweMessage(challenge.body.message);
    assert.equal(parsed.address, wallet.address);
    assert.equal(parsed.domain, "localhost:8080");
    assert.equal(parsed.chainId, 31337);
    assert.equal(parsed.version, "1");

    const signature = await wallet.signMessage(challenge.body.message);
    const login = await f.call("/api/auth/wallet", { method: "POST", body: {
        walletAddress: wallet.address, chainId: 31337, message: challenge.body.message, signature
    }});
    assert.equal(login.status, 200);
    assert.equal(login.body.user.walletAddress, wallet.address);
    assert.equal((await f.call("/api/me", { token: login.body.token })).status, 200);
});

test("siwe login rejects a tampered nonce, domain, address and chain", async t => {
    const f = await fixture(t);
    const wallet = ethers.Wallet.createRandom();
    const other = ethers.Wallet.createRandom();

    const challenge = await f.call("/api/auth/challenge", { method: "POST", body: { walletAddress: wallet.address, chainId: 31337 } });
    const message = challenge.body.message;

    const attempts = [
        [message.replace(/^Nonce: (.+)$/m, "Nonce: forgednonce123456"), wallet],
        [message.replace(/^localhost:8080 wants/, "evil.example wants"), wallet],
        [message.replace(wallet.address, other.address), other],
        [message.replace(/^Chain ID: \d+$/m, "Chain ID: 1"), wallet],
        [message.replace(/^URI: .+$/m, "URI: http://evil.example"), wallet]
    ];
    for (const [tampered, signer] of attempts) {
        const response = await f.call("/api/auth/wallet", { method: "POST", body: {
            walletAddress: wallet.address, chainId: 31337, message: tampered, signature: await signer.signMessage(tampered)
        }});
        assert.equal(response.status, 401, "tampered SIWE message must be rejected");
    }
});

test("siwe field validation rejects a wrong chain and a stale expiration", async t => {
    const f = await fixture(t);
    const wallet = ethers.Wallet.createRandom();
    const challenge = await f.call("/api/auth/challenge", { method: "POST", body: { walletAddress: wallet.address, chainId: 31337 } });
    const stored = f.customerService.store.getChallenge(wallet.address, challenge.body.message);
    const wrongChain = challenge.body.message.replace(/^Chain ID: \d+$/m, "Chain ID: 1");
    assert.throws(() => f.customerService.verifySiweChallenge(wrongChain, { ...stored, nonce: stored.nonce, issuedAt: stored.issuedAt },
        wallet.address.toLowerCase(), 31337), error => error.statusCode === 400 && error.code === "WRONG_NETWORK");
    const expired = challenge.body.message.replace(/^Expiration Time: .+$/m, "Expiration Time: 2000-01-01T00:00:00.000Z");
    assert.throws(() => f.customerService.verifySiweChallenge(expired, { ...stored },
        wallet.address.toLowerCase(), 31337), error => error.statusCode === 401 && error.code === "CHALLENGE_EXPIRED");
});

test("siwe rejects a non-SIWE message and a wrong field version", async t => {
    const f = await fixture(t);
    const wallet = ethers.Wallet.createRandom();
    await f.call("/api/auth/challenge", { method: "POST", body: { walletAddress: wallet.address, chainId: 31337 } });
    const legacy = [
        "HRC Safety Log at http://localhost:8080 wants to verify ownership of this wallet.", "",
        `Wallet: ${wallet.address}`, "Chain ID: 31337", "Nonce: abcdefgh1234",
        "Issued At: " + new Date().toISOString(), "Expiration Time: " + new Date(Date.now() + 60000).toISOString()
    ].join("\n");
    const legacyAttempt = await f.call("/api/auth/wallet", { method: "POST", body: {
        walletAddress: wallet.address, chainId: 31337, message: legacy, signature: await wallet.signMessage(legacy)
    }});
    assert.equal(legacyAttempt.status, 401);

    const challenge = await f.call("/api/auth/challenge", { method: "POST", body: { walletAddress: wallet.address, chainId: 31337 } });
    const badVersion = challenge.body.message.replace(/^Version: 1$/m, "Version: 2");
    const badVersionAttempt = await f.call("/api/auth/wallet", { method: "POST", body: {
        walletAddress: wallet.address, chainId: 31337, message: badVersion, signature: await wallet.signMessage(badVersion)
    }});
    assert.equal(badVersionAttempt.status, 401);
});

test("wallet mode keeps the legacy EIP-191 message shape", async t => {
    const f = await fixture(t, { authMode: "wallet" });
    const wallet = ethers.Wallet.createRandom();
    const challenge = await f.call("/api/auth/challenge", { method: "POST", body: { walletAddress: wallet.address, chainId: 31337 } });
    assert.equal(challenge.body.mode, "wallet");
    assert.match(challenge.body.message, /^HRC Safety Log at http:\/\/localhost:8080 wants to verify ownership of this wallet\.$/m);
    const signature = await wallet.signMessage(challenge.body.message);
    const login = await f.call("/api/auth/wallet", { method: "POST", body: {
        walletAddress: wallet.address, chainId: 31337, message: challenge.body.message, signature
    }});
    assert.equal(login.status, 200);
});

test("buildSiweMessage rejects an invalid domain, uri, chain id and nonce", () => {
    const base = { address: ethers.Wallet.createRandom().address, chainId: 31337, nonce: "abcdefgh1234", issuedAt: new Date().toISOString() };
    assert.throws(() => buildSiweMessage({ ...base, domain: "bad domain", uri: "http://localhost:8080" }), /domain is invalid/);
    assert.throws(() => buildSiweMessage({ ...base, domain: "localhost", uri: "not-a-url" }), /uri/);
    assert.throws(() => buildSiweMessage({ ...base, domain: "localhost", uri: "http://localhost:8080", chainId: 0 }), /chainId/);
    assert.throws(() => buildSiweMessage({ ...base, domain: "localhost", uri: "http://localhost:8080", nonce: "short" }), /nonce/);
});
