const fs = require("node:fs");
const path = require("node:path");
const { CONTRACT_ADDRESS, RPC_URL, NETWORK, DEPLOYMENT_BLOCK } = require("./contractConfig");

function readJson(file) {
    try { return JSON.parse(fs.readFileSync(file, "utf8")); }
    catch { return {}; }
}

function getPublicConfig(env = process.env) {
    const deployment = readJson(env.HRC_REWARD_DEPLOYMENT || path.join(__dirname, "../smart_contracts/deployments/reward-localhost.json"));
    const chainId = Number(deployment.chainId || (NETWORK === "sepolia" ? 11155111 : NETWORK === "amoy" ? 80002 : 31337));
    return {
        success: true,
        network: NETWORK,
        chainId,
        rpcUrl: RPC_URL,
        safety: { address: CONTRACT_ADDRESS, deploymentBlock: DEPLOYMENT_BLOCK },
        rewards: {
            tokenAddress: env.HRC_REWARD_TOKEN_ADDRESS || deployment.tokenAddress || null,
            address: env.HRC_REWARD_ADDRESS || deployment.rewardsAddress || null,
            deploymentBlock: Number(env.HRC_REWARD_DEPLOYMENT_BLOCK ?? deployment.deploymentBlock ?? 0)
        }
    };
}

module.exports = { getPublicConfig };
