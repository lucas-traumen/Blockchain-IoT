require("dotenv").config();

const path = require("path");

const RPC_URL =
    process.env.RPC_URL
    || "http://127.0.0.1:8545";

// Mạng đang dùng: localhost (mặc định) hoặc sepolia / amoy
const NETWORK =
    process.env.NETWORK
    || "localhost";

// Ưu tiên cấu hình do scripts/deploy.js ghi ra: smart_contracts/deployments/<network>.json
// Nếu chưa có file đó thì dùng CONTRACT_ADDRESS trong .env.
function readDeployment() {

    try {

        const deployment =
            require(
                path.join(
                    __dirname,
                    "../smart_contracts/deployments",
                    `${NETWORK}.json`
                )
            );

        return deployment;

    } catch (error) {

        return undefined;
    }
}

const deployment = readDeployment();

const CONTRACT_ADDRESS =
    deployment?.address
    || process.env.CONTRACT_ADDRESS;

if (!CONTRACT_ADDRESS) {
    throw new Error(
        "Contract address not found. Run scripts/deploy.js or set CONTRACT_ADDRESS in .env"
    );
}

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
    ABI: contractArtifact.abi
};
