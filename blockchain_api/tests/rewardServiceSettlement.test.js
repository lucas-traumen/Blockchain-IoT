const test = require("node:test");
const assert = require("node:assert/strict");
const { ethers } = require("ethers");
const { RewardService } = require("../rewardService");
const { ApiError } = require("../eventPayload");

const BENEFICIARY = "0x0000000000000000000000000000000000000011";
const SESSION_KEY = ethers.id("reward-service-ended-at");
const EVIDENCE = ethers.id("frozen-evidence");

function fixture() {
    const state = {
        beneficiary: BENEFICIARY,
        startedAt: 100n,
        settledAt: 0n,
        stakedAmount: ethers.parseEther("2"),
        rewardPaid: 0n,
        penaltyCollected: 0n,
        outcome: 0,
        settlementStatus: 0,
        evidenceHash: ethers.ZeroHash,
        outcomeTimestamp: 0n,
        started: true,
        settled: false,
        endedAt: 0n
    };
    const calls = [];
    const contract = {
        async getSession() { return { ...state }; },
        async getSessionEndTime() { return state.endedAt; },
        async submitSessionOutcome(...args) {
            calls.push(args);
            state.endedAt = BigInt(args[5]);
            state.settledAt = BigInt(args[4]);
            state.outcome = args[2];
            state.evidenceHash = args[3];
            state.outcomeTimestamp = BigInt(args[4]);
            state.rewardPaid = ethers.parseEther("3");
            state.settlementStatus = 0;
            state.settled = true;
            return { hash: ethers.id("settlement-tx"), wait: async () => ({ blockNumber: 12 }) };
        }
    };
    const service = new RewardService({
        wallet: { getAddress: async () => "0x0000000000000000000000000000000000000055" },
        contract,
        waitConfirmations: 1
    });
    return { service, state, calls };
}

test("settlement passes the frozen Unix end time to the new contract ABI and retry is idempotent", async () => {
    const { service, state, calls } = fixture();
    const input = {
        sessionId: "reward-service-ended-at",
        beneficiary: BENEFICIARY,
        outcome: "VALID_NO_VIOLATION",
        evidenceHash: EVIDENCE,
        timestamp: 1_791_590_500,
        endedAt: 130
    };

    const settled = await service.processSessionOutcome(input);
    assert.equal(calls.length, 1);
    assert.deepEqual(calls[0], [SESSION_KEY, BENEFICIARY, 0, EVIDENCE, input.timestamp, input.endedAt]);
    assert.equal(settled.endedAt, input.endedAt);
    assert.equal(state.endedAt, BigInt(input.endedAt));
    assert.equal(state.outcome, 0);
    assert.equal(state.evidenceHash, EVIDENCE);
    assert.equal(state.settled, true);
    const retried = await service.processSessionOutcome(input);
    assert.equal(retried.duplicate, true);
    assert.equal(retried.endedAt, input.endedAt);
    assert.equal(calls.length, 1);
    await assert.rejects(service.processSessionOutcome({ ...input, endedAt: 131 }),
        (error) => error.code === "SETTLEMENT_CONFLICT");
    assert.equal(calls.length, 1);
});

test("settlement rejects a missing or non-integer end time before querying chain state", async () => {
    const { service, calls } = fixture();
    await assert.rejects(service.processSessionOutcome({
        sessionId: "reward-service-ended-at", outcome: "INCOMPLETE", timestamp: 1_791_590_500
    }), (error) => error instanceof ApiError && error.code === "INVALID_PAYLOAD");
    await assert.rejects(service.processSessionOutcome({
        sessionId: "reward-service-ended-at", outcome: "INCOMPLETE", timestamp: 1_791_590_500, endedAt: 130.5
    }), (error) => error instanceof ApiError && error.code === "INVALID_PAYLOAD");
    assert.equal(calls.length, 0);
});
