const { expect } = require("chai");
const { ethers } = require("hardhat");
const { time } = require("@nomicfoundation/hardhat-toolbox/network-helpers");

const coin = ethers.parseEther;
const taskHash = ethers.id("task:demo");
const policyHash = ethers.id("policy:v3");
const evidenceHash = ethers.id("evidence:closed");

async function deploy() {
    const [owner, operator, user, treasury, other] = await ethers.getSigners();
    const Token = await ethers.getContractFactory("HRCRewardToken");
    const token = await Token.deploy("HRC", "HRC", coin("10000"), owner.address);
    const Rewards = await ethers.getContractFactory("HRCTaskRewards");
    const rewards = await Rewards.deploy(await token.getAddress(), treasury.address);
    await token.transfer(user.address, coin("2"));
    await token.approve(await rewards.getAddress(), coin("100"));
    await rewards.fundRewardPool(coin("100"));
    await token.connect(user).approve(await rewards.getAddress(), coin("2"));
    await rewards.addOperator(operator.address);
    return { owner, operator, user, treasury, other, token, rewards };
}

async function start(rewards, user, id = ethers.id("session")) {
    await rewards.connect(user).startSession(id, taskHash, policyHash);
    const session = await rewards.sessions(id);
    return { id, startedAt: Number(session.startedAt) };
}

describe("HRCTaskRewards V2", function () {
    it("snapshots identifiers and pays approved safe economics after minimum task duration", async function () {
        const { rewards, token, user } = await deploy();
        const session = await start(rewards, user);
        await time.increaseTo(session.startedAt + 120);
        await rewards.submitOutcome(session.id, user.address, 0, evidenceHash, session.startedAt + 120);
        expect(await token.balanceOf(user.address)).to.equal(coin("2.002"));
        expect(await rewards.rewardPoolBalance()).to.equal(coin("99.998"));
        const stored = await rewards.sessions(session.id);
        expect(stored.taskIdHash).to.equal(taskHash);
        expect(stored.policyHash).to.equal(policyHash);
        expect(stored.endedAt).to.equal(session.startedAt + 120);
        expect(await rewards.accountingInvariant()).to.equal(true);
    });

    it("rejects unsafe duration for a safe outcome but permits a refund outcome", async function () {
        const { rewards, user } = await deploy();
        const session = await start(rewards, user, ethers.id("short"));
        await time.increaseTo(session.startedAt + 30);
        await expect(rewards.submitOutcome(session.id, user.address, 0, evidenceHash, session.startedAt + 30))
            .to.be.revertedWithCustomError(rewards, "SafeDurationTooShort");
        await rewards.submitOutcome(session.id, user.address, 3, evidenceHash, session.startedAt + 30);
        expect((await rewards.sessions(session.id)).settled).to.equal(true);
    });

    it("uses the frozen sensor end time even when settlement is delayed", async function () {
        const { rewards, token, user } = await deploy();
        const session = await start(rewards, user, ethers.id("delayed"));
        const endedAt = session.startedAt + 120;
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
            await time.increaseTo(session.startedAt + 120);
            await rewards.submitOutcome(session.id, user.address, 0, ethers.id(`e:${i}`), session.startedAt + 120);
        }
        expect(await rewards.rewardsPaidByDay(user.address, Math.floor((await time.latest()) / 86400))).to.equal(coin("0.010"));
        expect(await rewards.accountingInvariant()).to.equal(true);
        expect(await token.balanceOf(await rewards.getAddress())).to.equal(
            await rewards.rewardPoolBalance() + await rewards.escrowedStakeTotal()
        );
    });
});
