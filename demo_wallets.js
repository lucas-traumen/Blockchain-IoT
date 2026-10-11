// Hardhat's documented local mnemonic is deterministic and is used only by the
// local demo. These keys never leave blockchain_api and are never sent to the
// browser.
const HARDHAT_MNEMONIC = "test test test test test test test test test test test junk";

const DEMO_WALLETS = Object.freeze([
    Object.freeze({ userId: "user_1", label: "User 1", derivationIndex: 2, initialBalance: "20" }),
    Object.freeze({ userId: "user_2", label: "User 2", derivationIndex: 3, initialBalance: "20" })
]);

module.exports = { HARDHAT_MNEMONIC, DEMO_WALLETS };
