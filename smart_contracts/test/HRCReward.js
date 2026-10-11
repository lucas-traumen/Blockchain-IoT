const { expect } = require("chai");
const { ethers } = require("hardhat");
const {
    loadFixture,
    time
} = require("@nomicfoundation/hardhat-toolbox/network-helpers");
const {
    anyValue
} = require("@nomicfoundation/hardhat-chai-matchers/withArgs");

const COIN = ethers.parseEther;
const VALID = 0;
const VIOLATION = 1;
const SENSOR_FAULT = 2;
const INCOMPLETE = 3;

function sessionId(label) {
    return ethers.id(`session:${label}`);
}

function evidence(label) {
    return ethers.keccak256(ethers.toUtf8Bytes(`evidence:${label}`));
}

async function deploySystem(poolCoins = "1000") {
    const [owner, operator, user, otherUser, treasury, stranger] =
        await ethers.getSigners();

    const Token = await ethers.getContractFactory("HRCRewardToken");
    const token = await Token.deploy(
        "HRC Safety Coin",
        "HRC",
        COIN("10000"),
        owner.address
    );
    await token.waitForDeployment();

    const Rewards = await ethers.getContractFactory("HRCSessionRewards");
    const rewards = await Rewards.deploy(
        await token.getAddress(),
        treasury.address
    );
    await rewards.waitForDeployment();

    await token.transfer(user.address, COIN("100"));
    await token.transfer(otherUser.address, COIN("100"));

    if (poolCoins !== "0") {
        await token.approve(await rewards.getAddress(), COIN(poolCoins));
        await rewards.fundRewardPool(COIN(poolCoins));
    }

    await rewards.addOperator(operator.address);

    await token.connect(user).approve(await rewards.getAddress(), COIN("100"));
    await token.connect(otherUser).approve(await rewards.getAddress(), COIN("100"));

    return {
        token,
        rewards,
        owner,
        operator,
        user,
        otherUser,
        treasury,
        stranger
    };
}

async function deployLowRewardPoolSystem() {
    return deploySystem("2");
}

async function start(rewards, user, label) {
    return rewards.connect(user).startSession(sessionId(label));
}

async function settle(
    rewards,
    operator,
    label,
    beneficiary,
    outcome,
    labelSuffix = label
) {
    return rewards.connect(operator).submitSessionOutcome(
        sessionId(label),
        beneficiary.address,
        outcome,
        evidence(labelSuffix),
        1_791_532_800
    );
}

describe("HRCRewardToken", function () {
    it("mints the fixed supply once and exposes no mint function", async function () {
        const { token, owner } = await loadFixture(deploySystem);

        expect(await token.totalSupply()).to.equal(COIN("10000"));
        expect(await token.balanceOf(owner.address)).to.equal(COIN("8800"));

        const hasMint = token.interface.fragments.some(
            (fragment) => fragment.type === "function" && fragment.name === "mint"
        );
        expect(hasMint).to.equal(false);
    });

    it("supports standard ERC-20 transfer and allowance flows", async function () {
        const { token, user, otherUser } = await loadFixture(deploySystem);

        await token.connect(user).approve(otherUser.address, COIN("4"));
        await token.connect(otherUser).transferFrom(
            user.address,
            otherUser.address,
            COIN("4")
        );

        expect(await token.balanceOf(user.address)).to.equal(COIN("96"));
        expect(await token.balanceOf(otherUser.address)).to.equal(COIN("104"));
    });
});

describe("HRCSessionRewards policy", function () {
    it("exposes stable policy constants, outcome values, and addresses", async function () {
        const { token, rewards, treasury } = await loadFixture(deploySystem);

        expect(await rewards.stakeAmount()).to.equal(COIN("2"));
        expect(await rewards.rewardAmount()).to.equal(COIN("3"));
        expect(await rewards.MIN_REWARD_DURATION()).to.equal(30);
        expect(await rewards.FULL_BONUS_DURATION()).to.equal(60);
        expect(await rewards.MAX_REWARD_BONUS_PERCENT()).to.equal(100);
        expect(await rewards.rewardAmountForDuration(30)).to.equal(COIN("3"));
        expect(await rewards.rewardAmountForDuration(45)).to.equal(COIN("4.5"));
        expect(await rewards.rewardAmountForDuration(60)).to.equal(COIN("6"));
        expect(await rewards.rewardAmountForDuration(120)).to.equal(COIN("6"));
        expect(await rewards.penaltyAmount()).to.equal(COIN("2"));
        expect(await rewards.token()).to.equal(await token.getAddress());
        expect(await rewards.rewardPool()).to.equal(await rewards.getAddress());
        expect(await rewards.penaltyTreasury()).to.equal(treasury.address);
        expect(await rewards.OUTCOME_VALID_NO_VIOLATION()).to.equal(VALID);
        expect(await rewards.OUTCOME_VIOLATION_CONFIRMED()).to.equal(VIOLATION);
        expect(await rewards.OUTCOME_SENSOR_FAULT()).to.equal(SENSOR_FAULT);
        expect(await rewards.OUTCOME_INCOMPLETE()).to.equal(INCOMPLETE);
        expect(await rewards.outcomeName(VALID)).to.equal("VALID_NO_VIOLATION");
        expect(await rewards.outcomeName(VIOLATION)).to.equal("VIOLATION_CONFIRMED");
    });

    it("requires a two-coin stake and pays stake plus three-coin reward", async function () {
        const { token, rewards, operator, user } = await loadFixture(deploySystem);
        const id = sessionId("valid");
        const hash = evidence("valid");
        const userBefore = await token.balanceOf(user.address);

        await expect(start(rewards, user, "valid"))
            .to.emit(rewards, "SessionStarted")
            .withArgs(id, user.address, COIN("2"), anyValue);

        expect(await token.balanceOf(user.address)).to.equal(userBefore - COIN("2"));
        expect(await rewards.escrowedStakeTotal()).to.equal(COIN("2"));

        await expect(
            rewards.connect(operator).submitSessionOutcome(
                id,
                user.address,
                VALID,
                hash,
                1_791_532_800
            )
        )
            .to.emit(rewards, "RewardPaid")
            .withArgs(id, user.address, COIN("3"), anyValue)
            .and.to.emit(rewards, "SessionSettled")
            .withArgs(
                id,
                user.address,
                VALID,
                0,
                COIN("2"),
                COIN("3"),
                0,
                hash,
                1_791_532_800
            );

        expect(await token.balanceOf(user.address)).to.equal(userBefore + COIN("3"));
        expect(await rewards.rewardPoolBalance()).to.equal(COIN("997"));
        expect(await rewards.escrowedStakeTotal()).to.equal(0);
        expect(await rewards.hasSettled(id)).to.equal(true);
    });

    it("scales a valid reward from three to six coins and caps the bonus at 100 percent", async function () {
        const { token, rewards, operator, user } = await loadFixture(deploySystem);
        const id = sessionId("duration-bonus");
        const hash = evidence("duration-bonus");
        const userBefore = await token.balanceOf(user.address);

        await start(rewards, user, "duration-bonus");
        const started = await rewards.getSession(id);
        await time.increaseTo(Number(started.startedAt) + 44);

        await expect(
            rewards.connect(operator).submitSessionOutcome(
                id,
                user.address,
                VALID,
                hash,
                1_791_532_800
            )
        )
            .to.emit(rewards, "RewardPaid")
            .withArgs(id, user.address, COIN("4.5"), anyValue)
            .and.to.emit(rewards, "SessionSettled")
            .withArgs(id, user.address, VALID, 0, COIN("2"), COIN("4.5"), 0, hash, 1_791_532_800);

        expect(await token.balanceOf(user.address)).to.equal(userBefore + COIN("4.5"));
        expect(await rewards.rewardPoolBalance()).to.equal(COIN("995.5"));
    });

    it("only an operator can submit outcome and client cannot provide amounts", async function () {
        const { rewards, user, stranger } = await loadFixture(deploySystem);
        await start(rewards, user, "operator-only");

        await expect(
            settle(rewards, stranger, "operator-only", user, VALID)
        ).to.be.revertedWithCustomError(rewards, "NotOperator");

        const submit = rewards.interface.getFunction("submitSessionOutcome");
        expect(submit.inputs.map((input) => input.type)).to.deep.equal([
            "bytes32",
            "address",
            "uint8",
            "bytes32",
            "uint256"
        ]);
    });

    it("moves exactly the two-coin stake to the penalty treasury", async function () {
        const { token, rewards, operator, user, treasury } =
            await loadFixture(deploySystem);
        const treasuryBefore = await token.balanceOf(treasury.address);

        await start(rewards, user, "violation");

        await expect(
            settle(rewards, operator, "violation", user, VIOLATION)
        )
            .to.emit(rewards, "PenaltyCollected")
            .withArgs(
                sessionId("violation"),
                user.address,
                COIN("2"),
                treasury.address
            )
            .and.to.emit(rewards, "SessionSettled")
            .withArgs(
                sessionId("violation"),
                user.address,
                VIOLATION,
                3,
                0,
                0,
                COIN("2"),
                evidence("violation"),
                1_791_532_800
            );

        expect(await token.balanceOf(treasury.address)).to.equal(
            treasuryBefore + COIN("2")
        );
        expect(await rewards.rewardPoolBalance()).to.equal(COIN("1000"));
        expect(await rewards.escrowedStakeTotal()).to.equal(0);
    });

    it("returns stake without reward or penalty for sensor fault and incomplete sessions", async function () {
        const { token, rewards, operator, user, treasury } =
            await loadFixture(deploySystem);
        const userBefore = await token.balanceOf(user.address);
        const treasuryBefore = await token.balanceOf(treasury.address);

        await start(rewards, user, "sensor-fault");
        await settle(rewards, operator, "sensor-fault", user, SENSOR_FAULT);

        await start(rewards, user, "incomplete");
        await settle(rewards, operator, "incomplete", user, INCOMPLETE);

        expect(await token.balanceOf(user.address)).to.equal(userBefore);
        expect(await token.balanceOf(treasury.address)).to.equal(treasuryBefore);
        expect(await rewards.rewardPoolBalance()).to.equal(COIN("1000"));
    });

    it("settles a session only once", async function () {
        const { rewards, operator, user } = await loadFixture(deploySystem);
        await start(rewards, user, "duplicate");
        await settle(rewards, operator, "duplicate", user, VALID);

        await expect(
            settle(rewards, operator, "duplicate", user, VALID, "duplicate-retry")
        ).to.be.revertedWithCustomError(rewards, "SessionAlreadySettled");
    });

    it("pays only one reward per beneficiary per UTC day", async function () {
        const { token, rewards, operator, user } = await loadFixture(deploySystem);
        const userBefore = await token.balanceOf(user.address);

        await start(rewards, user, "daily-1");
        await settle(rewards, operator, "daily-1", user, VALID);

        await start(rewards, user, "daily-2");
        await expect(settle(rewards, operator, "daily-2", user, VALID))
            .to.emit(rewards, "SessionSettled")
            .withArgs(
                sessionId("daily-2"),
                user.address,
                VALID,
                1,
                COIN("2"),
                0,
                0,
                evidence("daily-2"),
                1_791_532_800
            );

        expect(await token.balanceOf(user.address)).to.equal(userBefore + COIN("3"));
        expect(await rewards.lastRewardDay(user.address)).to.equal(
            await rewards.currentRewardDay()
        );
    });

    it("returns stake and reports an empty reward pool", async function () {
        const { token, rewards, operator, user } = await loadFixture(
            deployLowRewardPoolSystem
        );
        const userBefore = await token.balanceOf(user.address);

        await start(rewards, user, "pool-empty");
        await expect(settle(rewards, operator, "pool-empty", user, VALID))
            .to.emit(rewards, "SessionSettled")
            .withArgs(
                sessionId("pool-empty"),
                user.address,
                VALID,
                2,
                COIN("2"),
                0,
                0,
                evidence("pool-empty"),
                1_791_532_800
            );

        expect(await token.balanceOf(user.address)).to.equal(userBefore);
        expect(await rewards.rewardPoolBalance()).to.equal(COIN("2"));
    });

    it("rejects invalid outcomes and preserves an unsettled session", async function () {
        const { rewards, operator, user } = await loadFixture(deploySystem);
        const id = sessionId("invalid-outcome");
        await start(rewards, user, "invalid-outcome");

        await expect(
            rewards.connect(operator).submitSessionOutcome(
                id,
                user.address,
                4,
                evidence("invalid-outcome"),
                1_791_532_800
            )
        ).to.be.revertedWithCustomError(rewards, "InvalidOutcome");

        const session = await rewards.getSession(id);
        expect(session.settled).to.equal(false);
        expect(await rewards.hasSettled(id)).to.equal(false);
    });

    it("preview reports the same fixed policy before settlement", async function () {
        const { rewards, user } = await loadFixture(deploySystem);
        const preview = await rewards.previewSettlement(
            sessionId("preview"),
            user.address,
            VALID
        );

        expect(preview.settlementStatus).to.equal(0);
        expect(preview.stakeReturned).to.equal(COIN("2"));
        expect(preview.rewardPaid).to.equal(COIN("3"));
        expect(preview.penaltyCollected).to.equal(0);
        expect(preview.dailyLimitReached).to.equal(false);
        expect(preview.poolEmpty).to.equal(false);
    });

    it("supports operator-assisted session start for a beneficiary", async function () {
        const { rewards, operator, user } = await loadFixture(deploySystem);
        const id = sessionId("operator-start");

        await expect(
            rewards.connect(operator).startSessionFor(id, user.address)
        )
            .to.emit(rewards, "SessionStarted")
            .withArgs(id, user.address, COIN("2"), anyValue);

        const session = await rewards.getSession(id);
        expect(session.beneficiary).to.equal(user.address);
        expect(session.stakedAmount).to.equal(COIN("2"));
    });

    it("does not change total supply during pool, stake, reward, or penalty flows", async function () {
        const { token, rewards, operator, user } = await loadFixture(deploySystem);
        const supplyBefore = await token.totalSupply();

        await start(rewards, user, "supply-valid");
        await settle(rewards, operator, "supply-valid", user, VALID);
        await start(rewards, user, "supply-violation");
        await settle(rewards, operator, "supply-violation", user, VIOLATION);

        expect(await token.totalSupply()).to.equal(supplyBefore);
    });

    it("stores evidence and timestamp without changing policy amounts", async function () {
        const { token, rewards, operator, user } = await loadFixture(deploySystem);
        const before = await token.balanceOf(user.address);

        await start(rewards, user, "evidence");
        await settle(rewards, operator, "evidence", user, VALID, "custom-evidence");

        expect(await token.balanceOf(user.address)).to.equal(before + COIN("3"));
        const stored = await rewards.getSession(sessionId("evidence"));
        expect(stored.evidenceHash).to.equal(evidence("custom-evidence"));
        expect(stored.outcomeTimestamp).to.equal(1_791_532_800);
    });
});

describe("HRCSessionRewards owner controls", function () {
    it("exposes a configurable policy that the owner can retune", async function () {
        const { rewards, owner, stranger } = await loadFixture(deploySystem);
        expect(await rewards.stakeAmount()).to.equal(COIN("2"));
        expect(await rewards.MIN_REWARD_DURATION()).to.equal(30);
        expect(await rewards.FULL_BONUS_DURATION()).to.equal(60);
        expect(await rewards.MAX_REWARD_BONUS_PERCENT()).to.equal(100);

        await expect(rewards.connect(stranger).setPolicy(
            COIN("4"), COIN("5"), COIN("1"), 10, 20, 50
        )).to.be.revertedWithCustomError(rewards, "OwnableUnauthorizedAccount");

        await expect(rewards.connect(owner).setPolicy(
            COIN("4"), COIN("5"), COIN("1"), 10, 20, 50
        )).to.emit(rewards, "PolicyUpdated");

        expect(await rewards.stakeAmount()).to.equal(COIN("4"));
        expect(await rewards.rewardAmount()).to.equal(COIN("5"));
        expect(await rewards.penaltyAmount()).to.equal(COIN("1"));
        expect(await rewards.MIN_REWARD_DURATION()).to.equal(10);
        expect(await rewards.FULL_BONUS_DURATION()).to.equal(20);
        expect(await rewards.MAX_REWARD_BONUS_PERCENT()).to.equal(50);
        // base reward below the threshold, half bonus at the midpoint, capped after
        expect(await rewards.rewardAmountForDuration(9)).to.equal(COIN("5"));
        expect(await rewards.rewardAmountForDuration(15)).to.equal(COIN("6.25"));
        expect(await rewards.rewardAmountForDuration(20)).to.equal(COIN("7.5"));
    });

    it("rejects an invalid policy", async function () {
        const { rewards, owner } = await loadFixture(deploySystem);
        const invalid = [
            [0, COIN("3"), COIN("2"), 30, 60, 100],
            [COIN("2"), 0, COIN("2"), 30, 60, 100],
            [COIN("2"), COIN("3"), 0, 30, 60, 100],
            [COIN("2"), COIN("3"), COIN("3"), 30, 60, 100],
            [COIN("2"), COIN("3"), COIN("2"), 0, 60, 100],
            [COIN("2"), COIN("3"), COIN("2"), 60, 60, 100],
            [COIN("2"), COIN("3"), COIN("2"), 30, 60, 1001]
        ];
        for (const args of invalid) {
            await expect(rewards.connect(owner).setPolicy(...args))
                .to.be.revertedWithCustomError(rewards, "InvalidPolicy");
        }
    });

    it("pause stops new stakes and funding but still allows settlement", async function () {
        const { token, rewards, owner, operator, user, stranger } = await loadFixture(deploySystem);
        await expect(rewards.connect(stranger).pause())
            .to.be.revertedWithCustomError(rewards, "OwnableUnauthorizedAccount");

        const open = await start(rewards, user, "pause-open");
        await expect(open.wait()).to.not.be.reverted;
        await expect(rewards.connect(owner).pause()).to.emit(rewards, "RewardPoolPaused");

        await expect(start(rewards, user, "pause-blocked"))
            .to.be.revertedWithCustomError(rewards, "EnforcedPause");
        await expect(rewards.connect(owner).fundRewardPool(COIN("1")))
            .to.be.revertedWithCustomError(rewards, "EnforcedPause");

        // Settlement must stay open so a paused pool cannot strand a stake.
        const before = await token.balanceOf(user.address);
        await settle(rewards, operator, "pause-open", user, VALID);
        expect(await token.balanceOf(user.address)).to.equal(before + COIN("2") + COIN("3"));

        await expect(rewards.connect(owner).unpause()).to.emit(rewards, "RewardPoolUnpaused");
        await expect(start(rewards, user, "pause-resumed")).to.not.be.reverted;
    });

    it("caps the reward pool and rejects a cap below the funded balance", async function () {
        const { token, rewards, owner, stranger } = await loadFixture(deploySystem);
        expect(await rewards.maxRewardPool()).to.equal(COIN("100000"));

        await expect(rewards.connect(stranger).setMaxRewardPool(COIN("5000")))
            .to.be.revertedWithCustomError(rewards, "OwnableUnauthorizedAccount");
        await expect(rewards.connect(owner).setMaxRewardPool(0))
            .to.be.revertedWithCustomError(rewards, "InvalidPolicy");

        const funded = await rewards.rewardPoolBalance();
        await expect(rewards.connect(owner).setMaxRewardPool(funded - 1n))
            .to.be.revertedWithCustomError(rewards, "InvalidPolicy");

        const cap = funded + COIN("10");
        await expect(rewards.connect(owner).setMaxRewardPool(cap))
            .to.emit(rewards, "MaxRewardPoolUpdated");
        expect(await rewards.maxRewardPool()).to.equal(cap);

        await token.connect(owner).approve(await rewards.getAddress(), COIN("11"));
        await expect(rewards.connect(owner).fundRewardPool(COIN("11")))
            .to.be.revertedWithCustomError(rewards, "RewardPoolCapExceeded");
        await expect(rewards.connect(owner).fundRewardPool(COIN("10"))).to.not.be.reverted;
        expect(await rewards.rewardPoolBalance()).to.equal(cap);
    });

    it("grants the operator flag to the accepted owner after a two-step transfer", async function () {
        const { rewards, owner, stranger } = await loadFixture(deploySystem);
        await rewards.connect(owner).transferOwnership(stranger.address);
        expect(await rewards.pendingOwner()).to.equal(stranger.address);
        await rewards.connect(stranger).acceptOwnership();

        expect(await rewards.owner()).to.equal(stranger.address);
        expect(await rewards.operators(stranger.address)).to.equal(true);
        expect(await rewards.pendingOwner()).to.equal(ethers.ZeroAddress);
    });

    it("sweeps only the surplus above the pool and escrowed stake", async function () {
        const { token, rewards, owner, user, stranger } = await loadFixture(deploySystem);
        const address = await rewards.getAddress();

        // The pool plus escrow is committed user collateral and reward liability.
        await expect(rewards.connect(owner).sweepExcessToken(stranger.address, COIN("1")))
            .to.be.revertedWithCustomError(rewards, "InsufficientSurplus");

        // A mistaken transfer creates a surplus that the owner may recover.
        await token.connect(user).transfer(address, COIN("3"));
        await expect(rewards.connect(stranger).sweepExcessToken(stranger.address, COIN("1")))
            .to.be.revertedWithCustomError(rewards, "OwnableUnauthorizedAccount");
        await expect(rewards.connect(owner).sweepExcessToken(stranger.address, COIN("4")))
            .to.be.revertedWithCustomError(rewards, "InsufficientSurplus");

        const before = await token.balanceOf(stranger.address);
        await expect(rewards.connect(owner).sweepExcessToken(stranger.address, COIN("3")))
            .to.emit(rewards, "ExcessTokenSwept").withArgs(stranger.address, COIN("3"));
        expect(await token.balanceOf(stranger.address)).to.equal(before + COIN("3"));

        // The committed balance is untouched by the sweep.
        expect(await rewards.rewardPoolBalance()).to.equal(COIN("1000"));
        expect(await rewards.escrowedStakeTotal()).to.equal(0);
        expect(await token.balanceOf(address)).to.equal(COIN("1000"));
    });

    it("refuses to sweep while paused", async function () {
        const { token, rewards, owner, user, stranger } = await loadFixture(deploySystem);
        await token.connect(user).transfer(await rewards.getAddress(), COIN("1"));
        await rewards.connect(owner).pause();
        await expect(rewards.connect(owner).sweepExcessToken(stranger.address, COIN("1")))
            .to.be.revertedWithCustomError(rewards, "EnforcedPause");
    });
});
