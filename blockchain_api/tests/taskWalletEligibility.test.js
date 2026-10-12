const test = require("node:test");
const assert = require("node:assert/strict");
const { ethers } = require("ethers");
const { WalletService } = require("../walletService");

function service({ rewardPoolBalance, rewardPaidToday = 0n }) {
    const owner = ethers.Wallet.createRandom();
    const stake = ethers.parseEther("0.2");
    const baseReward = ethers.parseEther("0.001");
    const safeReward = ethers.parseEther("0.002");
    const cap = ethers.parseEther("0.01");
    const walletService = new WalletService({
        provider: { getNetwork: async () => ({ chainId: 31337 }), getCode: async () => "0x01",
            getBlock: async () => ({ timestamp: 8640000 }), getBalance: async () => ethers.parseEther("1") },
        token: { balanceOf: async () => ethers.parseEther("20"), allowance: async () => stake,
            symbol: async () => "HRC", decimals: async () => 18 },
        rewards: { STAKE_AMOUNT: async () => stake, BASE_REWARD: async () => baseReward, SAFE_REWARD: async () => safeReward,
            DAILY_REWARD_CAP: async () => cap, rewardPoolBalance: async () => rewardPoolBalance,
            rewardsPaidByDay: async () => rewardPaidToday, sessions: async () => { throw new Error("unexpected"); } },
        config: { chainId: 31337, tokenAddress: "0x0000000000000000000000000000000000000003",
            rewardAddress: "0x0000000000000000000000000000000000000004", rewardProtocolVersion: "task-rewards-v2", deploymentTransactions: {} },
        store: { list: () => [], listTransactions: () => [] }
    });
    const user = walletService.walletUser(owner.address);
    return { walletService, user };
}

test("V2 wallet eligibility blocks an empty pool and exposes exhausted daily cap", async () => {
    const { walletService: emptyPoolService, user: emptyPoolUser } = service({ rewardPoolBalance: 0n });
    const emptyPool = await emptyPoolService.getWallet(emptyPoolUser.userId);
    assert.equal(emptyPool.canStart, false);
    assert.equal(emptyPool.canStartReason, "REWARD_POOL_EMPTY");

    const { walletService: cappedService, user: cappedUser } = service({ rewardPoolBalance: ethers.parseEther("1"), rewardPaidToday: ethers.parseEther("0.01") });
    const capped = await cappedService.getWallet(cappedUser.userId);
    assert.equal(capped.canStart, true);
    assert.equal(capped.canEarnSafeReward, false);
    assert.equal(capped.rewardPaidToday, "0.01");
});

test("V2 wallet eligibility allows a start when only the base reward is available", async () => {
    const { walletService, user } = service({
        rewardPoolBalance: ethers.parseEther("0.0015"),
        rewardPaidToday: ethers.parseEther("0.009")
    });
    const wallet = await walletService.getWallet(user.userId);
    assert.equal(wallet.baseReward, "0.001");
    assert.equal(wallet.safeReward, "0.002");
    assert.equal(wallet.canEarnSafeReward, true);
    assert.equal(wallet.canStartReason, null);
    assert.equal(wallet.canStart, true);
});
