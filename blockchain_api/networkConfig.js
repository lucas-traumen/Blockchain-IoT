const NETWORK_CHAIN_IDS = Object.freeze({
    localhost: 31337,
    sepolia: 11155111,
    amoy: 80002
});

function configuredNetwork(env = process.env, fallback = "localhost") {
    const value = String(env.HRC_BLOCKCHAIN_NETWORK || fallback).trim().toLowerCase();
    if (!Object.hasOwn(NETWORK_CHAIN_IDS, value)) {
        throw new Error("HRC_BLOCKCHAIN_NETWORK must be localhost, sepolia or amoy");
    }
    return value;
}

function chainIdForNetwork(network) {
    if (!Object.hasOwn(NETWORK_CHAIN_IDS, network)) {
        throw new Error(`Unsupported blockchain network: ${network}`);
    }
    return NETWORK_CHAIN_IDS[network];
}

module.exports = { NETWORK_CHAIN_IDS, configuredNetwork, chainIdForNetwork };
