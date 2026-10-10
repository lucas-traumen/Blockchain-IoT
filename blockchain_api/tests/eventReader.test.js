const test = require("node:test");
const assert = require("node:assert/strict");
const { createEventReader } = require("../eventReader");

test("concurrent dashboard requests share scan; refresh reads only tail and caches event details", async () => {
    let latest = 11869041, detailCalls = 0;
    const ranges = [];
    const logs = [{ blockNumber: 11853429, transactionHash: "0xold", args: { eventId: "E1" } }];
    const contract = {
        filters: { SafetyEventRecorded: () => "record" },
        async queryFilter(filter, from, to) {
            ranges.push([from, to]);
            await new Promise(resolve => setImmediate(resolve));
            return logs.filter(log => log.blockNumber >= from && log.blockNumber <= to);
        },
        async getSafetyEvent(id) { detailCalls++; return [id, "ROBOT-ARM-01", 1n, "0xhash", 3n, 2n]; }
    };
    const reader = createEventReader({ contract, provider: { getBlockNumber: async () => latest } });
    const first = await Promise.all([reader(), reader(), reader()]);
    assert.equal(ranges.length, 9);
    assert.equal(detailCalls, 1);
    assert.equal(first[0].events[0].riskLevel, 3);
    ranges.length = 0;
    latest += 2;
    logs.push({ blockNumber: latest, transactionHash: "0xnew", args: { eventId: "E2" } });
    assert.equal((await reader()).count, 2);
    assert.equal(ranges.length, 1);
    assert.equal(ranges[0][0], 11869041 - 12);
    assert.equal(detailCalls, 2);
});
