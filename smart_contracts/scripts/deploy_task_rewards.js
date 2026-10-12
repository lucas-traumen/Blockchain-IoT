const hre = require("hardhat");
const fs = require("node:fs");
const path = require("node:path");

async function main() {
    const [deployer, treasurySigner] = await hre.ethers.getSigners();
    let tokenAddress = process.env.HRC_TASK_TOKEN_ADDRESS;
    let baseManifest = null;
    if (!tokenAddress) {
        baseManifest = process.env.HRC_REWARD_DEPLOYMENT
            ? JSON.parse(fs.readFileSync(path.resolve(process.env.HRC_REWARD_DEPLOYMENT), "utf8"))
            : null;
        tokenAddress = baseManifest?.tokenAddress;
    }
    if (!hre.ethers.isAddress(tokenAddress || "")) throw new Error("HRC_TASK_TOKEN_ADDRESS or a reward deployment with tokenAddress is required");
    const treasury = process.env.HRC_TASK_PENALTY_TREASURY || treasurySigner.address;
    if (!hre.ethers.isAddress(treasury)) throw new Error("Invalid HRC_TASK_PENALTY_TREASURY");

    const Rewards = await hre.ethers.getContractFactory("HRCTaskRewards");
    const rewards = await Rewards.deploy(tokenAddress, treasury);
    await rewards.waitForDeployment();
    const deploymentReceipt = await rewards.deploymentTransaction().wait();
    const poolAmount = hre.ethers.parseUnits(process.env.HRC_TASK_REWARD_POOL_AMOUNT || "1000", 18);
    const token = new hre.ethers.Contract(tokenAddress, [
        "function approve(address,uint256) returns (bool)",
        "function transferFrom(address,address,uint256) returns (bool)"
    ], deployer);
    await (await token.approve(await rewards.getAddress(), poolAmount)).wait();
    const poolTx = await rewards.fundRewardPool(poolAmount);
    const poolReceipt = await poolTx.wait();
    const network = await hre.ethers.provider.getNetwork();
    const address = await rewards.getAddress();
    const deploymentTransactions = {
        token: baseManifest?.deployTxHashes?.token || baseManifest?.deploymentTransactions?.token || null,
        rewards: rewards.deploymentTransaction()?.hash || null
    };
    const deployment = {
        version: "task-rewards-v2",
        network: hre.network.name,
        chainId: Number(network.chainId),
        tokenAddress,
        rewardsAddress: address,
        penaltyTreasury: treasury,
        owner: deployer.address,
        policy: {
            stakeAmount: (await rewards.STAKE_AMOUNT()).toString(),
            safeReward: (await rewards.SAFE_REWARD()).toString(),
            violationPenalty: (await rewards.VIOLATION_PENALTY()).toString(),
            dailyRewardCap: (await rewards.DAILY_REWARD_CAP()).toString(),
            minimumSafeDuration: Number(await rewards.MIN_SAFE_DURATION())
        },
        deployTxHash: rewards.deploymentTransaction()?.hash || null,
        deploymentBlock: deploymentReceipt.blockNumber,
        deploymentTransactions: Object.fromEntries(Object.entries(deploymentTransactions).filter(([, hash]) => hash)),
        deployTxHashes: Object.fromEntries(Object.entries(deploymentTransactions).filter(([, hash]) => hash)),
        rewardPoolBalance: (await rewards.rewardPoolBalance()).toString(),
        poolFundTxHash: poolTx.hash,
        poolFundBlock: poolReceipt.blockNumber,
        deployedAt: new Date().toISOString()
    };
    if (hre.network.name === "localhost") {
        const dir = path.resolve(process.env.HRC_DEPLOYMENT_DIR || path.join(__dirname, "../deployments"));
        fs.mkdirSync(dir, { recursive: true });
        const output = path.join(dir, "task-rewards-localhost.json");
        fs.writeFileSync(output, JSON.stringify(deployment, null, 2));
        console.log(`Saved: ${output}`);
    }
    console.log(JSON.stringify(deployment, null, 2));
}

main().catch(error => { console.error(error); process.exitCode = 1; });
