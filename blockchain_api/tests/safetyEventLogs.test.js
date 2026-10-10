const test = require("node:test");
const assert = require("node:assert/strict");
const { getSafetyEventLogs } = require("../safetyEventLogs");

test("events remain readable after RPC history exceeds 10000 blocks without gaps or duplicates", async () => {
    const start = 11852711, latest = 11868826;
    const calls = [];
    const expected = [start, start + 1999, start + 2000, latest].map((blockNumber, index) => ({ blockNumber, eventId: "E" + index }));
    const contract = {
        filters: { SafetyEventRecorded: () => "safety" },
        async queryFilter(filter, from, to) {
            assert.equal(filter, "safety");
            if (to - from > 10000) throw new Error("range exceeds limit of 10000");
            calls.push([from, to]);
            return expected.filter(log => log.blockNumber >= from && log.blockNumber <= to);
        }
    };
    await assert.rejects(contract.queryFilter("safety", start, latest), /range exceeds/);
    assert.deepEqual(await getSafetyEventLogs({ contract, latestBlock: latest }), expected);
    assert.equal(calls.length, 9);
    assert.equal(calls[0][0], start);
    assert.equal(calls.at(-1)[1], latest);
    calls.forEach(([from, to], i) => {
        assert.ok(to - from + 1 <= 2000);
        if (i) assert.equal(from, calls[i - 1][1] + 1);
    });
});

test("empty history and RPC failures do not masquerade as partial event lists", async () => {
    const contract = { filters: { SafetyEventRecorded: () => "safety" }, queryFilter: async () => [] };
    assert.deepEqual(await getSafetyEventLogs({ contract, latestBlock: 11852710 }), []);
    assert.deepEqual(await getSafetyEventLogs({ contract, latestBlock: 11852711 }), []);
    let calls = 0;
    contract.queryFilter = async () => { if (++calls === 2) throw new Error("RPC unavailable"); return [{}]; };
    await assert.rejects(getSafetyEventLogs({ contract, latestBlock: 11856711 }), /RPC unavailable/);
    await assert.rejects(getSafetyEventLogs({ contract, latestBlock: 11856711, chunkSize: 0 }), /chunk size/);
});
