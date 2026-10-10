#!/usr/bin/env node
/* Local Hardhat smoke: token funding, user stake, RewardService outcomes. */
const fs = require("node:fs");
const path = require("node:path");
const { ethers } = require("ethers");
const { RewardService } = require("../blockchain_api/rewardService");

const ROOT = path.resolve(__dirname, "..");
const deployment = JSON.parse(fs.readFileSync(
    path.join(ROOT, "smart_contracts/deployments/reward-localhost.json"), "utf8"
));
const tokenArtifact = JSON.parse(fs.readFileSync(
    path.join(ROOT, "smart_contracts/artifacts/contracts/HRCRewardToken.sol/HRCRewardToken.json"), "utf8"
));
const rewardArtifact = JSON.parse(fs.readFileSync(
    path.join(ROOT, "smart_contracts/artifacts/contracts/HRCSessionRewards.sol/HRCSessionRewards.json"), "utf8"
));

const rpcUrl = process.env.RPC_URL || "http://127.0.0.1:8545";
const operatorKey = process.env.PRIVATE_KEY;
const beneficiaryKey = process.env.HRC_BENEFICIARY_PRIVATE_KEY;
if (!operatorKey || !beneficiaryKey) {
    throw new Error("PRIVATE_KEY and HRC_BENEFICIARY_PRIVATE_KEY are required");
}

const provider = new ethers.JsonRpcProvider(rpcUrl);
const operatorWallet = new ethers.Wallet(operatorKey, provider);
// Keep explicit nonce managers for both accounts. Hardhat automines each
// transaction, while a reused Wallet can retain a stale nonce between the
// funding transaction and RewardService settlement.
const operator = new ethers.NonceManager(operatorWallet);
const beneficiaryWallet = new ethers.Wallet(beneficiaryKey, provider);
// Keep one nonce manager for approve and startSession. A newly-created Wallet
// can otherwise reuse a stale cached nonce on automining local nodes.
const beneficiary = new ethers.NonceManager(beneficiaryWallet);
const token = new ethers.Contract(deployment.tokenAddress, tokenArtifact.abi, operator);
function sessionKey(sessionId) { return ethers.id(sessionId); }
function evidence(label) { return ethers.keccak256(ethers.toUtf8Bytes(`smoke:${label}`)); }
function coins(value) { return ethers.formatEther(value); }

async function main() {
    const beneficiaryAddress = await beneficiary.getAddress();
    const initialTransfer = ethers.parseEther("20");
    await (await token.transfer(beneficiaryAddress, initialTransfer)).wait();
    await (await token.connect(beneficiary).approve(deployment.rewardsAddress, ethers.MaxUint256)).wait();
    const rewardsForBeneficiary = new ethers.Contract(
        deployment.rewardsAddress, rewardArtifact.abi, beneficiary
    );

    const service = new RewardService({
        provider,
        wallet: operator,
        contract: new ethers.Contract(
            deployment.rewardsAddress,
            rewardArtifact.abi,
            operator
        ),
        waitConfirmations: 1,
    });

    const outcomes = [
        { id: "smoke-valid-1", outcome: "VALID_NO_VIOLATION" },
        { id: "smoke-valid-2-daily-limit", outcome: "VALID_NO_VIOLATION" },
        { id: "smoke-violation", outcome: "VIOLATION_CONFIRMED" },
        { id: "smoke-sensor-fault", outcome: "SENSOR_FAULT" },
    ];
    const rows = [];
    const timestamp = Math.floor(Date.now() / 1000);
    for (const item of outcomes) {
        await (await rewardsForBeneficiary.startSession(sessionKey(item.id))).wait();
        const result = await service.processSessionOutcome({
            sessionId: item.id,
            beneficiary: beneficiaryAddress,
            outcome: item.outcome,
            evidenceHash: evidence(item.id),
            timestamp,
        });
        rows.push(result);
    }

    const duplicate = await service.processSessionOutcome({
        sessionId: outcomes[0].id,
        beneficiary: beneficiaryAddress,
        outcome: outcomes[0].outcome,
        evidenceHash: evidence(outcomes[0].id),
        timestamp,
    });

    const userBalance = await token.balanceOf(beneficiaryAddress);
    const poolBalance = await new ethers.Contract(
        deployment.rewardsAddress, rewardArtifact.abi, provider
    ).rewardPoolBalance();
    const output = {
        network: "localhost",
        chainId: Number((await provider.getNetwork()).chainId),
        tokenAddress: deployment.tokenAddress,
        rewardsAddress: deployment.rewardsAddress,
        beneficiary: beneficiaryAddress,
        policy: { stake: "2", reward: "3", penalty: "2", dailyRewardLimit: 1, rewardPool: "1000" },
        settlements: rows,
        duplicateRetry: duplicate,
        finalBeneficiaryBalance: coins(userBalance),
        finalRewardPoolBalance: coins(poolBalance),
    };
    const out = process.env.OUT || path.join(ROOT, "data/results/esp32-lstm-20261009/reward-smoke.json");
    fs.mkdirSync(path.dirname(out), { recursive: true });
    fs.writeFileSync(out, JSON.stringify(output, null, 2) + "\n");
    console.log(JSON.stringify(output, null, 2));
}

main().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
