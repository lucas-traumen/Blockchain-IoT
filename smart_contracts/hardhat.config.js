require("@nomicfoundation/hardhat-toolbox");
require("dotenv").config();

const {
    SEPOLIA_RPC_URL,
    AMOY_RPC_URL,
    DEPLOYER_PRIVATE_KEY,
    ETHERSCAN_API_KEY
} = process.env;

const accounts = DEPLOYER_PRIVATE_KEY ? [DEPLOYER_PRIVATE_KEY] : [];

// Chỉ thêm testnet khi đã khai báo RPC trong .env (tránh lỗi cấu hình khi chạy local)
const networks = {
    localhost: { url: "http://127.0.0.1:8545", chainId: 31337 }
};

if (SEPOLIA_RPC_URL) {
    networks.sepolia = { url: SEPOLIA_RPC_URL, accounts };
}

if (AMOY_RPC_URL) {
    networks.amoy = { url: AMOY_RPC_URL, accounts };
}

/** @type import('hardhat/config').HardhatUserConfig */
module.exports = {
    solidity: {
        version: "0.8.27",
        settings: {
            optimizer: { enabled: true, runs: 200 }
        }
    },
    networks,
    etherscan: {
        apiKey: ETHERSCAN_API_KEY || ""
    },
    gasReporter: {
        enabled: process.env.REPORT_GAS === "true"
    }
};
