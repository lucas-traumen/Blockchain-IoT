const test = require("node:test");
const assert = require("node:assert/strict");
const { getTransactionHistory } = require("../transactionHistory");
const { createApp } = require("../server");

test("history includes historical unlocks, deduplicates hashes and queries bounded blocks", async () => {
    const calls = [];
    const recorded = { transactionHash: "0xrecord", blockNumber: 12,
        args: { deviceId: "R1", eventId: "E1", riskLevel: 3n } };
    const unlocked = { transactionHash: "0xunlock", blockNumber: 13, args: { deviceId: "R1" } };
    const locked = { transactionHash: "0xlock", blockNumber: 14, args: { deviceId: "R1", eventId: "E1" } };
    const autoLock = { ...locked, transactionHash: "0xrecord", blockNumber: 12 };
    const contract = {
        filters: { SafetyEventRecorded: () => "record", RobotUnlocked: () => "unlock", EmergencyStopTriggered: () => "lock" },
        async queryFilter(type, from, to) {
            calls.push([type, from, to]);
            return (type === "record" ? [recorded, recorded] : type === "unlock" ? [unlocked] : [locked, autoLock]).filter(log => log.blockNumber >= from && log.blockNumber <= to);
        }
    };
    const result = await getTransactionHistory({ contract, provider: { getBlockNumber: async () => 14 }, deploymentBlock: 10, chunkSize: 2 });
    assert.equal(result.count, 3);
    assert.equal(result.transactions[0].eventId, "E1");
    assert.deepEqual(result.transactions[0].actions, ["ROBOT_LOCK"]);
    assert.deepEqual(result.transactions[1], { transactionHash: "0xunlock", blockNumber: 13,
        actions: ["ROBOT_UNLOCK"], deviceId: "R1", eventId: null, riskLevel: null });
    assert.equal(result.transactions[2].riskLevel, 3);
    assert.deepEqual(result.transactions[2].actions, ["SAFETY_EVENT", "ROBOT_LOCK"]);
    assert.equal(calls.length, 9);
    assert(calls.every(([, from, to]) => from >= 10 && to <= 14 && to - from < 2));
});
test("public transaction route returns history without write authorization", async t => {
    const expected = { success: true, count: 1, transactions: [{ actions: ["ROBOT_UNLOCK"], deviceId: "R1" }] };
    const server = createApp({ service: { getAllTransactions: async () => expected }, tokens: {} }).listen(0, "127.0.0.1");
    await new Promise((resolve, reject) => { server.once("listening", resolve); server.once("error", reject); });
    t.after(() => new Promise(resolve => server.close(resolve)));
    const response = await fetch(`http://127.0.0.1:${server.address().port}/api/transactions`);
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), expected);
});
