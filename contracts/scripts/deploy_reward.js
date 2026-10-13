const hre = require("hardhat");
const fs = require("fs");
const path = require("path");

async function main() {
    const [deployer, defaultPenaltyTreasury] = await hre.ethers.getSigners();
    const tokenDecimals = 18;
    const totalSupply = hre.ethers.parseUnits(
        process.env.REWARD_TOTAL_SUPPLY || "10000",
        tokenDecimals
    );
    const rewardPoolAmount = hre.ethers.parseUnits(
        process.env.REWARD_POOL_AMOUNT || "1000",
        tokenDecimals
    );
    const penaltyTreasury =
        process.env.PENALTY_TREASURY || defaultPenaltyTreasury.address;

    console.log("Deploying HRCRewardToken with account:");
    console.log(deployer.address);

    const Token = await hre.ethers.getContractFactory("HRCRewardToken");
    const token = await Token.deploy(
        "HRC Safety Coin",
        "HRC",
        totalSupply,
        deployer.address
    );
    await token.waitForDeployment();

    const Rewards = await hre.ethers.getContractFactory("HRCSessionRewards");
    const rewards = await Rewards.deploy(
        await token.getAddress(),
        penaltyTreasury
    );
    await rewards.waitForDeployment();

    await (await token.approve(await rewards.getAddress(), rewardPoolAmount)).wait();
    await (await rewards.fundRewardPool(rewardPoolAmount)).wait();

    // Local funding is explicit and independent of application identity/SIWE.
    const localTestWallet = process.env.HRC_LOCAL_TEST_WALLET_ADDRESS;
    const localTestBalance = hre.ethers.parseUnits(process.env.HRC_LOCAL_TEST_HRC || "20", tokenDecimals);
    if (localTestWallet) {
        if (hre.network.name !== "localhost") throw new Error("Local test wallet funding is only allowed on localhost");
        if (!hre.ethers.isAddress(localTestWallet)) throw new Error("Invalid HRC_LOCAL_TEST_WALLET_ADDRESS");
        await (await token.transfer(localTestWallet, localTestBalance)).wait();
        console.log(`Funded local MetaMask test wallet: ${localTestWallet}`);
    }

    const [network, tokenAddress, rewardsAddress] = await Promise.all([
        hre.ethers.provider.getNetwork(),
        token.getAddress(),
        rewards.getAddress()
    ]);

    console.log("HRCRewardToken deployed to:");
    console.log(tokenAddress);
    console.log("HRCSessionRewards deployed to:");
    console.log(rewardsAddress);
    console.log("Reward pool:");
    console.log(await rewards.rewardPool());
    console.log("Penalty treasury:");
    console.log(await rewards.penaltyTreasury());
    console.log("Reward pool balance:");
    console.log((await rewards.rewardPoolBalance()).toString());

    // Keep the requested localhost artifact deterministic. Other networks only
    // print addresses so a test run cannot overwrite deployment records.
    if (hre.network.name === "localhost") {
        const deploymentsDir = process.env.HRC_DEPLOYMENT_DIR
            ? path.resolve(process.env.HRC_DEPLOYMENT_DIR)
            : path.join(__dirname, "..", "deployments");
        fs.mkdirSync(deploymentsDir, { recursive: true });

        const info = {
            network: hre.network.name,
            chainId: Number(network.chainId),
            tokenAddress,
            rewardsAddress,
            rewardPool: await rewards.rewardPool(),
            penaltyTreasury: await rewards.penaltyTreasury(),
            owner: deployer.address,
            totalSupply: totalSupply.toString(),
            rewardPoolBalance: rewardPoolAmount.toString(),
            localTestWallet: localTestWallet ? { address: localTestWallet, initialBalance: localTestBalance.toString() } : null,
            deployTxHashes: {
                token: token.deploymentTransaction()?.hash || null,
                rewards: rewards.deploymentTransaction()?.hash || null
            },
            deployedAt: new Date().toISOString()
        };

        const output = path.join(deploymentsDir, "reward-localhost.json");
        fs.writeFileSync(output, JSON.stringify(info, null, 2));
        console.log(`Saved: ${output}`);
    }
}

main().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
