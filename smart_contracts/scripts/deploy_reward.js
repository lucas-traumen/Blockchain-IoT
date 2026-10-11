const hre = require("hardhat");
const fs = require("fs");
const path = require("path");
const { DEMO_WALLETS, HARDHAT_MNEMONIC } = require("../../demo_wallets");

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

    // Fund exactly the two local demo users. Their addresses are derived from
    // Hardhat's deterministic mnemonic and are exposed as public identities;
    // their private keys remain backend-only.
    const demoInitialBalance = hre.ethers.parseUnits(
        process.env.HRC_DEMO_INITIAL_BALANCE || "20",
        tokenDecimals
    );
    const demoWallets = DEMO_WALLETS.map(spec => ({
        ...spec,
        address: hre.ethers.HDNodeWallet.fromPhrase(
            process.env.HRC_DEMO_MNEMONIC || HARDHAT_MNEMONIC,
            undefined,
            `m/44'/60'/0'/0/${spec.derivationIndex}`
        ).address
    }));
    for (const demo of demoWallets) {
        await (await token.transfer(demo.address, demoInitialBalance)).wait();
        console.log(`Seeded ${demo.userId}: ${demo.address}`);
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
            demoWallets: demoWallets.map(demo => ({
                userId: demo.userId,
                address: demo.address,
                initialBalance: demoInitialBalance.toString()
            })),
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
