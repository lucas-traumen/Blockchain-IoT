const test = require("node:test");
const assert = require("node:assert/strict");
const { ethers } = require("ethers");
const { configuredNetwork, chainIdForNetwork } = require("../networkConfig");
const { operatorPrivateKey } = require("../operatorKey");

test("blockchain network ignores an unrelated generic NETWORK variable", () => {
    assert.equal(configuredNetwork({ NETWORK: "caas_ingress_only" }), "localhost");
    assert.equal(configuredNetwork({ NETWORK: "wrong", HRC_BLOCKCHAIN_NETWORK: "sepolia" }), "sepolia");
    assert.equal(chainIdForNetwork("sepolia"), 11155111);
    assert.throws(
        () => configuredNetwork({ HRC_BLOCKCHAIN_NETWORK: "unknown" }),
        /HRC_BLOCKCHAIN_NETWORK/
    );
});

test("local operator key is derived automatically but live networks require an explicit key", () => {
    const key = operatorPrivateKey({}, "localhost");
    assert.equal(new ethers.Wallet(key).address, "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266");
    assert.throws(() => operatorPrivateKey({}, "sepolia"), /required/);
    assert.throws(() => operatorPrivateKey({}, "amoy"), /required/);
    assert.throws(
        () => operatorPrivateKey({ PRIVATE_KEY: "YOUR_HARDHAT_PRIVATE_KEY" }, "localhost"),
        /32-byte/
    );
    const custom = operatorPrivateKey(
        { HRC_LOCAL_CHAIN_MNEMONIC: "test test test test test test test test test test test junk" },
        "localhost"
    );
    assert.equal(new ethers.Wallet(custom).address, "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266");
});
