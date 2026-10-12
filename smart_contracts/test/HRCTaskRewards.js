const { expect } = require("chai");
const { ethers } = require("hardhat");
const { time } = require("@nomicfoundation/hardhat-toolbox/network-helpers");

const coin = ethers.parseEther;
const taskHash = ethers.id("task:demo");
const policyHash = ethers.id("policy:v3");
const evidenceHash = ethers.id("evidence:closed");

async function deploy(rewardPool = coin("100")) {
    const [owner, operator, user, treasury, other] = await ethers.getSigners();
    const Token = await ethers.getContractFactory("HRCRewardToken");
    const token = await Token.deploy("HRC", "HRC", coin("10000"), owner.address);
    const Rewards = await ethers.getContractFactory("HRCTaskRewards");
    const rewards = await Rewards.deploy(await token.getAddress(), treasury.address);
    await token.transfer(user.address, coin("2"));
    await token.approve(await rewards.getAddress(), coin("100"));
    if (rewardPool > 0n) await rewards.fundRewardPool(rewardPool);
    await token.connect(user).approve(await rewards.getAddress(), coin("2"));
    await rewards.addOperator(operator.address);
    return { owner, operator, user, treasury, other, token, rewards };
}

async function start(rewards, user, id = ethers.id("session"), fullRewardDuration = 180) {
    await rewards.connect(user).startSession(id, taskHash, policyHash, fullRewardDuration);
    const session = await rewards.sessions(id);
    return { id, startedAt: Number(session.startedAt), fullRewardDuration: Number(session.fullRewardDuration) };
}

describe("HRCTaskRewards V2", function () {
    it("snapshots identifiers and pays the base reward at the 30 second minimum", async function () {
        const { rewards, token, user } = await deploy();
        const session = await start(rewards, user);
        await time.increaseTo(session.startedAt + 30);
        await rewards.submitOutcome(session.id, user.address, 0, evidenceHash, session.startedAt + 30);
        expect(await token.balanceOf(user.address)).to.equal(coin("2.001"));
        expect(await rewards.rewardPoolBalance()).to.equal(coin("99.999"));
        const stored = await rewards.sessions(session.id);
        expect(stored.taskIdHash).to.equal(taskHash);
        expect(stored.policyHash).to.equal(policyHash);
        expect(stored.fullRewardDuration).to.equal(180);
        expect(stored.endedAt).to.equal(session.startedAt + 30);
        expect(await rewards.accountingInvariant()).to.equal(true);
    });

    it("rejects unsafe duration for a safe outcome but permits a refund outcome", async function () {
        const { rewards, user } = await deploy();
        const session = await start(rewards, user, ethers.id("short"));
        await time.increaseTo(session.startedAt + 29);
        await expect(rewards.submitOutcome(session.id, user.address, 0, evidenceHash, session.startedAt + 29))
            .to.be.revertedWithCustomError(rewards, "SafeDurationTooShort");
        await rewards.submitOutcome(session.id, user.address, 3, evidenceHash, session.startedAt + 29);
        expect((await rewards.sessions(session.id)).settled).to.equal(true);
    });

    it("freezes a valid full-reward duration and rejects invalid bounds", async function () {
        const { rewards, token, user } = await deploy();
        await expect(rewards.connect(user).startSession(ethers.id("too-short"), taskHash, policyHash, 29))
            .to.be.revertedWithCustomError(rewards, "InvalidRewardDuration");
        await expect(rewards.connect(user).startSession(ethers.id("too-large"), taskHash, policyHash, 2n ** 64n))
            .to.be.revertedWithCustomError(rewards, "InvalidRewardDuration");
        const session = await start(rewards, user, ethers.id("valid-bound"), 30);
        expect(session.fullRewardDuration).to.equal(30);
        expect(await rewards.rewardAmountForDuration(30, 30)).to.equal(coin("0.002"));
        await time.increaseTo(session.startedAt + 30);
        await rewards.submitOutcome(session.id, user.address, 0, evidenceHash, session.startedAt + 30);
        expect((await rewards.sessions(session.id)).rewardPaid).to.equal(coin("0.002"));
        expect(await token.balanceOf(user.address)).to.equal(coin("2.002"));
    });

    it("keeps the sessions getter wire-compatible with the flat ABI and duration at index 13", async function () {
        const { rewards, user } = await deploy();
        const id = ethers.id("flat-abi");
        await start(rewards, user, id, 180);
        const flat = new ethers.Contract(await rewards.getAddress(), [
            "function sessions(bytes32) view returns (address,bytes32,bytes32,bytes32,uint64,uint64,uint64,uint128,uint128,uint128,uint8,bool,bool,uint64)"
        ], ethers.provider);
        const tuple = await flat.sessions(id);
        expect(tuple).to.have.length(14);
        expect(tuple[0]).to.equal(user.address);
        expect(tuple[13]).to.equal(180);
    });

    it("increases linearly to the maximum reward and caps longer sessions", async function () {
        const { rewards, user } = await deploy();
        expect(await rewards.rewardAmountForDuration(29, 180)).to.equal(0);
        expect(await rewards.rewardAmountForDuration(30, 180)).to.equal(coin("0.001"));
        expect(await rewards.rewardAmountForDuration(30, 30)).to.equal(coin("0.002"));
        expect(await rewards.rewardAmountForDuration(105, 180)).to.equal(coin("0.0015"));
        expect(await rewards.rewardAmountForDuration(180, 180)).to.equal(coin("0.002"));
        expect(await rewards.rewardAmountForDuration(181, 180)).to.equal(coin("0.002"));

        const midpoint = await start(rewards, user, ethers.id("midpoint"));
        await time.increaseTo(midpoint.startedAt + 105);
        await rewards.submitOutcome(midpoint.id, user.address, 0, ethers.id("midpoint-evidence"), midpoint.startedAt + 105);
        expect((await rewards.sessions(midpoint.id)).rewardPaid).to.equal(coin("0.0015"));

        const threshold = await start(rewards, user, ethers.id("threshold"));
        await time.increaseTo(threshold.startedAt + 180);
        await rewards.submitOutcome(threshold.id, user.address, 0, ethers.id("threshold-evidence"), threshold.startedAt + 180);
        expect((await rewards.sessions(threshold.id)).rewardPaid).to.equal(coin("0.002"));

        const longer = await start(rewards, user, ethers.id("longer"));
        await time.increaseTo(longer.startedAt + 181);
        await rewards.submitOutcome(longer.id, user.address, 0, ethers.id("longer-evidence"), longer.startedAt + 181);
        expect((await rewards.sessions(longer.id)).rewardPaid).to.equal(coin("0.002"));
    });

    it("pays a variable reward only when the pool can cover that exact amount", async function () {
        const exact = await deploy(coin("0.0015"));
        const exactSession = await start(exact.rewards, exact.user, ethers.id("exact-pool"));
        await time.increaseTo(exactSession.startedAt + 105);
        await exact.rewards.submitOutcome(exactSession.id, exact.user.address, 0,
            ethers.id("exact-pool-evidence"), exactSession.startedAt + 105);
        expect((await exact.rewards.sessions(exactSession.id)).rewardPaid).to.equal(coin("0.0015"));
        expect(await exact.rewards.rewardPoolBalance()).to.equal(0);

        const short = await deploy(coin("0.0015") - 1n);
        const shortSession = await start(short.rewards, short.user, ethers.id("short-pool"));
        await time.increaseTo(shortSession.startedAt + 105);
        await short.rewards.submitOutcome(shortSession.id, short.user.address, 0,
            ethers.id("short-pool-evidence"), shortSession.startedAt + 105);
        expect((await short.rewards.sessions(shortSession.id)).rewardPaid).to.equal(0);
        expect(await short.rewards.rewardPoolBalance()).to.equal(coin("0.0015") - 1n);
        expect(await short.token.balanceOf(short.user.address)).to.equal(coin("2"));
    });

    it("uses the frozen sensor end time even when settlement is delayed", async function () {
        const { rewards, token, user } = await deploy();
        const session = await start(rewards, user, ethers.id("delayed"));
        const endedAt = session.startedAt + 180;
        await time.increaseTo(endedAt + 600);
        await rewards.submitOutcome(session.id, user.address, 0, evidenceHash, endedAt);
        const settled = await rewards.sessions(session.id);
        expect(settled.endedAt).to.equal(endedAt);
        expect(settled.settledAt).to.be.greaterThan(settled.endedAt);
        expect(await token.balanceOf(user.address)).to.equal(coin("2.002"));
    });

    it("returns stake and applies the exact confirmed-violation penalty", async function () {
        const { rewards, token, user, treasury } = await deploy();
        const session = await start(rewards, user, ethers.id("violation"));
        await rewards.submitOutcome(session.id, user.address, 1, evidenceHash, session.startedAt + 1);
        expect(await token.balanceOf(user.address)).to.equal(coin("1.990"));
        expect(await token.balanceOf(treasury.address)).to.equal(coin("0.010"));
        expect(await rewards.accountingInvariant()).to.equal(true);
    });

    it("requires operator authorization, a single settlement, and valid end timestamp", async function () {
        const { rewards, user, other } = await deploy();
        const session = await start(rewards, user, ethers.id("guards"));
        await expect(rewards.connect(other).submitOutcome(session.id, user.address, 3, evidenceHash, session.startedAt))
            .to.be.revertedWithCustomError(rewards, "InvalidAddress");
        await expect(rewards.submitOutcome(session.id, user.address, 3, evidenceHash, session.startedAt - 1))
            .to.be.revertedWithCustomError(rewards, "InvalidEndTime");
        const latest = await time.latest();
        await expect(rewards.submitOutcome(session.id, user.address, 3, evidenceHash, latest + 100))
            .to.be.revertedWithCustomError(rewards, "InvalidEndTime");
        await rewards.submitOutcome(session.id, user.address, 3, evidenceHash, session.startedAt);
        await expect(rewards.submitOutcome(session.id, user.address, 3, evidenceHash, session.startedAt))
            .to.be.revertedWithCustomError(rewards, "SessionAlreadySettled");
    });

    it("keeps daily rewards under the wallet cap and protects pool plus escrow accounting", async function () {
        const { rewards, user, token } = await deploy();
        for (let i = 0; i < 6; i += 1) {
            const session = await start(rewards, user, ethers.id(`daily:${i}`));
            await time.increaseTo(session.startedAt + 180);
            await rewards.submitOutcome(session.id, user.address, 0, ethers.id(`e:${i}`), session.startedAt + 180);
        }
        expect(await rewards.rewardsPaidByDay(user.address, Math.floor((await time.latest()) / 86400))).to.equal(coin("0.010"));
        expect(await rewards.accountingInvariant()).to.equal(true);
        expect(await token.balanceOf(await rewards.getAddress())).to.equal(
            await rewards.rewardPoolBalance() + await rewards.escrowedStakeTotal()
        );
    });
});
