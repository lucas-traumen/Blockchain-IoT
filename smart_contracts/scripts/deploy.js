const hre = require("hardhat");
const fs = require("fs");
const path = require("path");

async function main() {
    const [deployer] = await hre.ethers.getSigners();

    console.log("Deploying with account:");
    console.log(deployer.address);

    const HRCSafetyLog =
        await hre.ethers.getContractFactory("HRCSafetyLog");

    const contract = await HRCSafetyLog.deploy();

    await contract.waitForDeployment();

    const address = await contract.getAddress();
    const { chainId } = await hre.ethers.provider.getNetwork();
    const networkName = hre.network.name;

    console.log("HRCSafetyLog deployed to:");
    console.log(address);

    console.log("Owner:");
    console.log(await contract.owner());

    // Ghi địa chỉ ra file để blockchain_api tự đọc (không phải sửa .env sau mỗi lần deploy)
    const dir = path.join(__dirname, "..", "deployments");
    fs.mkdirSync(dir, { recursive: true });

    const info = {
        network: networkName,
        chainId: Number(chainId),
        address,
        owner: deployer.address,
        deployTxHash: contract.deploymentTransaction()?.hash || null,
        deployedAt: new Date().toISOString()
    };

    fs.writeFileSync(
        path.join(dir, `${networkName}.json`),
        JSON.stringify(info, null, 2)
    );

    console.log(`Saved: deployments/${networkName}.json`);

    if (networkName !== "hardhat" && networkName !== "localhost") {
        console.log("\nVerify on Etherscan:");
        console.log(`npx hardhat verify --network ${networkName} ${address}`);
    }
}

main().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
