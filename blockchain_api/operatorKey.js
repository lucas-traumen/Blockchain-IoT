const { ethers } = require("ethers");
const { localChainMnemonic } = require("./localChainMnemonic");

function operatorPrivateKey(env = process.env, network = "localhost") {
    const explicit = typeof env.PRIVATE_KEY === "string" ? env.PRIVATE_KEY.trim() : "";
    if (explicit) {
        if (!/^0x[0-9a-fA-F]{64}$/.test(explicit)) {
            throw new Error("PRIVATE_KEY must be a 32-byte 0x-prefixed hex key");
        }
        return explicit;
    }
    if (network !== "localhost") {
        throw new Error("PRIVATE_KEY is required for a live network");
    }
    return ethers.HDNodeWallet.fromPhrase(
        localChainMnemonic(env),
        undefined,
        "m/44'/60'/0'/0/0"
    ).privateKey;
}

module.exports = { operatorPrivateKey };
