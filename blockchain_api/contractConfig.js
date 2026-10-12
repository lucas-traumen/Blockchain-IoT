require("dotenv").config();

const fs = require("node:fs");
const path = require("path");
const { ethers } = require("ethers");
const { configuredNetwork, chainIdForNetwork } = require("./networkConfig");

const RPC_URL =
    process.env.RPC_URL
    || "http://127.0.0.1:8545";

// Use a project-specific name. Generic NETWORK is commonly injected by hosts
// and previously made the API select an unrelated value such as a container
// network name.
const NETWORK = configuredNetwork(process.env);
const DEPLOYMENT_PATH = path.resolve(
    process.env.HRC_SAFETY_DEPLOYMENT
    || path.join(__dirname, "../smart_contracts/deployments", `${NETWORK}.json`)
);

// Ưu tiên cấu hình do scripts/deploy.js ghi ra: smart_contracts/deployments/<network>.json
// Nếu chưa có file đó thì dùng CONTRACT_ADDRESS trong .env.
function readDeployment() {
    try {
        return JSON.parse(fs.readFileSync(DEPLOYMENT_PATH, "utf8"));
    } catch {
        return undefined;
    }
}

const deployment = readDeployment();

if (deployment?.network && deployment.network !== NETWORK) {
    throw new Error(`Safety deployment network ${deployment.network} does not match ${NETWORK}`);
}
if (deployment?.chainId && Number(deployment.chainId) !== chainIdForNetwork(NETWORK)) {
    throw new Error(`Safety deployment chainId ${deployment.chainId} does not match ${NETWORK}`);
}

const configuredAddress =
    deployment?.address
    || process.env.CONTRACT_ADDRESS;

if (!ethers.isAddress(configuredAddress || "")) {
    throw new Error(
        "Safety contract address is not configured. Run ./scripts/run_local_metamask.sh or set HRC_SAFETY_DEPLOYMENT/CONTRACT_ADDRESS"
    );
}
const CONTRACT_ADDRESS = ethers.getAddress(configuredAddress);

const artifactPath =
    path.join(
        __dirname,
        "../smart_contracts/artifacts/contracts/HRCSafetyLog.sol/HRCSafetyLog.json"
    );

const contractArtifact =
    require(artifactPath);

const configuredDeploymentBlock =
    process.env.HRC_DEPLOYMENT_BLOCK
    || deployment?.deploymentBlock;

const DEPLOYMENT_BLOCK =
    configuredDeploymentBlock !== undefined
        ? Number(configuredDeploymentBlock)
        : NETWORK === "localhost"
            ? 0
            : 11852711;

if (!Number.isSafeInteger(DEPLOYMENT_BLOCK) || DEPLOYMENT_BLOCK < 0) {
    throw new Error("HRC_DEPLOYMENT_BLOCK must be a non-negative safe integer");
}

console.log(
    `Network: ${NETWORK} | Contract: ${CONTRACT_ADDRESS}`
);

module.exports = {
    RPC_URL,
    NETWORK,
    CONTRACT_ADDRESS,
    DEPLOYMENT_BLOCK,
    DEPLOYMENT_PATH,
    ABI: contractArtifact.abi
};
