const test = require("node:test");
const assert = require("node:assert/strict");
const { ethers } = require("ethers");
const { AMOUNTS, OUTCOMES, normalizeOutcome, sessionKey, evidenceHash, statusName } = require("../rewardPolicy");

test("reward policy maps fixed outcome names to contract enum values", () => {
    assert.equal(normalizeOutcome("VALID_NO_VIOLATION").code, OUTCOMES.VALID_NO_VIOLATION);
    assert.equal(normalizeOutcome("VIOLATION_CONFIRMED").code, OUTCOMES.VIOLATION_CONFIRMED);
    assert.throws(() => normalizeOutcome("CLEAR"), /outcome must be/);
});

test("session IDs are hashed to bytes32 and evidence is strict non-zero bytes32", () => {
    assert.equal(sessionKey("run_1"), ethers.id("run_1"));
    assert.equal(evidenceHash("0x" + "1".repeat(64)), "0x" + "1".repeat(64));
    assert.throws(() => sessionKey("bad session"), /sessionId/);
    assert.throws(() => evidenceHash(ethers.ZeroHash), /non-zero/);
    assert.equal(statusName(0), "REWARDED");
    assert.deepEqual(AMOUNTS, {
        stake: "2",
        reward: "3",
        maxReward: "6",
        penalty: "2",
        minDurationSeconds: 30,
        fullBonusDurationSeconds: 60,
        maxBonusPercent: 100
    });
});
