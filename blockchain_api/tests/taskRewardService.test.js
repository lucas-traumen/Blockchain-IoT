const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { ethers } = require("ethers");
const { TaskRewardService } = require("../taskRewardService");

function session({ settled = false, outcome = 0, evidenceHash, endedAt = 0 } = {}) {
    return { beneficiary: "0x0000000000000000000000000000000000000001", taskIdHash: ethers.id("task"),
        policyHash: ethers.id("policy"), evidenceHash: evidenceHash || ethers.ZeroHash, startedAt: 1000n,
        endedAt: BigInt(endedAt), settledAt: settled ? 1700n : 0n, stakedAmount: ethers.parseEther("0.2"),
        rewardPaid: ethers.parseEther("0.002"), penaltyCollected: 0n, outcome, started: true, settled,
        fullRewardDuration: 180n };
}

test("task settlement freezes endedAt and idempotent retry does not submit another transaction", async () => {
    const wallet = { getAddress: async () => "0x0000000000000000000000000000000000000002" };
    let stored = session();
    let sends = 0;
    const contract = { sessions: async () => stored };
    const transactionSender = { send: async (key, signer, target, method, args) => {
        sends += 1;
        assert.equal(key, "settlement:session-1");
        assert.equal(method, "submitOutcome");
        assert.equal(args[4], 1450);
        stored = session({ settled: true, outcome: args[2], evidenceHash: args[3], endedAt: args[4] });
        return { hash: "0x" + "11".repeat(32), receipt: { blockNumber: 55 } };
    } };
    const service = new TaskRewardService({ wallet, contract, transactionSender });
    const payload = { sessionId: "session-1", beneficiary: stored.beneficiary, outcome: "COMPLETED_SAFE",
        evidenceHash: ethers.id("frozen-document"), endedAt: 1450 };
    const first = await service.processSessionOutcome(payload);
    const retry = await service.processSessionOutcome(payload);
    assert.equal(first.endedAt, 1450);
    assert.equal(first.settledAt, 1700);
    assert.equal(first.fullRewardDuration, 180);
    assert.equal(retry.duplicate, true);
    assert.equal(sends, 1);
    await assert.rejects(() => service.processSessionOutcome({ ...payload, endedAt: 1500 }), { code: "SETTLEMENT_CONFLICT" });
    assert.equal(sends, 1);
});

test("task settlement refuses to report success when confirmed chain state differs", async () => {
    const stored = session();
    const service = new TaskRewardService({
        wallet: { getAddress: async () => stored.beneficiary },
        contract: { sessions: async () => stored },
        transactionSender: { send: async () => ({ hash: "0x" + "22".repeat(32), receipt: { blockNumber: 56 } }) }
    });
    await assert.rejects(() => service.processSessionOutcome({ sessionId: "session-2", beneficiary: stored.beneficiary,
        outcome: "COMPLETED_SAFE", evidenceHash: ethers.id("frozen"), endedAt: 1450 }), { code: "SETTLEMENT_STATE_MISMATCH" });
});

test("task settlement service refuses a pre-curve V2 deployment manifest", t => {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "hrc-task-reward-config-"));
    t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
    const deployment = path.join(directory, "deployment.json");
    fs.writeFileSync(deployment, JSON.stringify({ version: "task-rewards-v2", network: "localhost", chainId: 31337,
        tokenAddress: "0x0000000000000000000000000000000000000003",
        rewardsAddress: "0x0000000000000000000000000000000000000004" }));
    assert.throws(() => TaskRewardService.fromEnv({ HRC_REWARD_DEPLOYMENT: deployment }),
        { code: "REWARD_ABI_MISMATCH" });
});
