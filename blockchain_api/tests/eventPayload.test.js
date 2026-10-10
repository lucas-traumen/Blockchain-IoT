const test = require("node:test");
const assert = require("node:assert/strict");
const { preparePayload, canonicalJson } = require("../eventPayload");
const { payload } = require("./helpers");

test("canonical hash ignores object key order, preserves array order", () => {
    const original = preparePayload(payload());
    const reordered = preparePayload(payload({ sensorData: { force: 15.2, speed: 2.1, distance: 0.18 } }));
    assert.equal(original.dataHash, reordered.dataHash);
    assert.equal(original.payload.eventId, reordered.payload.eventId);
    assert.notEqual(canonicalJson([1, 2]), canonicalJson([2, 1]));
});
test("identity depends on source key, not timestamp or sensor values", () => {
    const first = preparePayload(payload());
    const changed = preparePayload(payload({ timestamp: 1790996001, sensorData: { distance: 0.1 } }));
    assert.equal(first.payload.eventId, changed.payload.eventId);
    assert.notEqual(first.dataHash, changed.dataHash);
    assert.notEqual(first.payload.eventId, preparePayload(payload({ sequence: 2 })).payload.eventId);
    assert.notEqual(first.payload.eventId, preparePayload(payload({ bootId: "second-boot" })).payload.eventId);
});
for (const riskLevel of [null, "", "3", true, -1, 4, 1.5]) {
    test(`reject non-numeric or invalid risk ${JSON.stringify(riskLevel)}`, () => {
        assert.throws(() => preparePayload(payload({ aiResult: { riskLevel } })), /riskLevel/);
    });
}
test("reject bad timestamps, sequence, identity, sensor and JSON shape", () => {
    for (const change of [{ timestamp: "1790996000" }, { timestamp: 0 }, { timestamp: Date.now() },
        { sequence: null }, { sequence: 0 }, { sequence: 1.1 }, { deviceId: "<script>" },
        { sensorData: [] }, { sensorData: {} }, { sensorData: { distance: "0.1" } },
        { sensorData: { distance: -1 } }, { sensorData: { distance: Infinity } },
        { sessionId: undefined, sequence: undefined }, { extra: "unsupported" }]) {
        assert.throws(() => preparePayload(payload(change)));
    }
    assert.throws(() => canonicalJson(JSON.parse('{"__proto__":{}}')), /Reserved/);
    assert.throws(() => canonicalJson({ x: undefined }));
});
test("UNKNOWN and invalid sensor must not silently become safe", () => {
    assert.throws(() => preparePayload(payload({ aiResult: { riskLevel: 0, state: "UNKNOWN" } })), /UNKNOWN/);
    assert.throws(() => preparePayload(payload({ sensorData: { distance: null, valid: false }, aiResult: { riskLevel: 0 } })), /UNKNOWN/);
    assert.equal(preparePayload(payload({ sensorData: { distance: null, valid: false }, aiResult: { riskLevel: 3, state: "UNKNOWN" } })).riskLevel, 3);
    assert.throws(() => preparePayload(payload({ aiResult: { riskLevel: 0, state: "VIOLATION" } })), /conflicts/);
});
