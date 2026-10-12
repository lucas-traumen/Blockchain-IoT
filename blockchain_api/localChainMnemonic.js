// Local Hardhat development mnemonic only. Never use these accounts on a real
// network: they are publicly known and carry no value.
const LOCAL_HARDHAT_MNEMONIC =
    "test test test test test test test test test test test junk";

function localChainMnemonic(env = process.env) {
    const override =
        typeof env.HRC_LOCAL_CHAIN_MNEMONIC === "string"
            ? env.HRC_LOCAL_CHAIN_MNEMONIC.trim()
            : "";
    return override || LOCAL_HARDHAT_MNEMONIC;
}

module.exports = { LOCAL_HARDHAT_MNEMONIC, localChainMnemonic };
