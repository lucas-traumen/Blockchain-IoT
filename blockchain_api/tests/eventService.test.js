const test = require("node:test");
const assert = require("node:assert/strict");
const { ethers } = require("ethers");
const { preparePayload, TYPES } = require("../eventPayload");
const { EventStore } = require("../eventStore");
const { fixture, payload, ADDRESS, CHAIN_ID } = require("./helpers");

test("risk 0–3, hash archive, current lock and E-Stop receipt", async t => {
    const f = fixture(t);
    for (const riskLevel of [0, 1, 2, 3]) {
        const input = payload({ deviceId: `ROBOT-${riskLevel}`, aiResult: { riskLevel } });
        const result = await f.service.processSafetyEvent(input);
        assert.equal(result.success, true);
        assert.equal(result.transactionStatus, "confirmed");
        assert.equal(result.robotLocked, riskLevel === 3);
        assert.equal(result.emergencyStopTriggered, riskLevel === 3);
        const archived = f.service.getStoredPayload(result.eventId);
        assert.equal(ethers.keccak256(ethers.toUtf8Bytes(archived.canonicalPayload)), result.dataHash);
        assert.equal(archived.payload.aiResult.riskLevel, riskLevel);
        assert(!Object.hasOwn(archived, "rawTransaction"));
    }
});
test("concurrent duplicate requests sign once; changed payload returns 409", async t => {
    const f = fixture(t);
    const results = await Promise.all(Array.from({ length: 8 }, () => f.service.processSafetyEvent(payload())));
    assert(results.every(result => result.success));
    assert.equal(new Set(results.map(result => result.recordTransactionHash)).size, 1);
    assert.equal(f.state.signed.length, 1);
    const conflict = await f.service.processSafetyEvent(payload({ timestamp: 1790996001 }));
    assert.equal(conflict.httpStatus, 409);
    assert.equal(conflict.code, "IDEMPOTENCY_CONFLICT");
});
test("pending transaction survives restart and rebroadcasts identical bytes", async t => {
    const f = fixture(t);
    f.state.confirm = false;
    const first = await f.service.processSafetyEvent(payload());
    assert.equal(first.httpStatus, 202);
    assert.equal(first.robotLocked, null);
    const service = f.reopen();
    f.state.confirm = true;
    const retry = await service.processSafetyEvent(payload());
    assert.equal(retry.recordTransactionHash, first.recordTransactionHash);
    assert.equal(retry.transactionStatus, "confirmed");
    assert.equal(f.state.signed.length, 1);
    assert.equal(new Set(f.state.broadcasts).size, 1);
});
test("crash/timeout after signing before send does not lose signed bytes", async t => {
    const f = fixture(t);
    f.state.broadcastFails = true;
    const first = await f.service.processSafetyEvent(payload());
    assert.equal(first.httpStatus, 202);
    const record = f.store.list()[0];
    assert.equal(ethers.keccak256(record.rawTransaction), first.recordTransactionHash);
    const service = f.reopen();
    f.state.broadcastFails = false;
    assert.equal((await service.processSafetyEvent(payload())).recordTransactionHash, first.recordTransactionHash);
    assert.equal(f.state.signed.length, 1);
});
test("lost broadcast response is reconciled from receipt", async t => {
    const f = fixture(t);
    f.state.loseBroadcastResponse = true;
    assert.equal((await f.service.processSafetyEvent(payload())).transactionStatus, "confirmed");
    assert.equal(f.state.signed.length, 1);
});
test("unresolved transaction blocks another event nonce; receipt permits next", async t => {
    const f = fixture(t);
    f.state.confirm = false;
    await f.service.processSafetyEvent(payload());
    const blocked = await f.service.processSafetyEvent(payload({ sequence: 2 }));
    assert.equal(blocked.code, "TRANSACTION_IN_PROGRESS");
    assert.equal(f.state.signed.length, 1);
    f.state.confirm = true;
    await f.service.processSafetyEvent(payload());
    assert.equal((await f.service.processSafetyEvent(payload({ sequence: 2 }))).success, true);
    assert.equal(ethers.Transaction.from(f.state.signed[1]).nonce, 1);
});
test("RPC failure after broadcast can recover without a second signature", async t => {
    const f = fixture(t);
    const original = f.provider.getTransactionReceipt;
    let count = 0;
    f.provider.getTransactionReceipt = async hash => { if (++count === 2) throw new Error("RPC timeout"); return original(hash); };
    const first = await f.service.processSafetyEvent(payload());
    assert.equal(first.success, false);
    f.provider.getTransactionReceipt = original;
    assert.equal((await f.service.processSafetyEvent(payload())).transactionStatus, "confirmed");
    assert.equal(f.state.signed.length, 1);
});
test("reverted receipt is persisted and never silently resent", async t => {
    const f = fixture(t);
    f.state.revert = true;
    assert.equal((await f.service.processSafetyEvent(payload())).code, "TRANSACTION_REVERTED");
    assert.equal((await f.service.processSafetyEvent(payload())).code, "TRANSACTION_REVERTED");
    assert.equal(f.state.broadcasts.length, 1);
    assert.equal(f.store.list()[0].transactionStatus, "failed");
});
test("EIP-712 checks registered signer, tamper, domain, missing signature and revocation", async t => {
    const f = fixture(t);
    const device = ethers.Wallet.createRandom();
    f.state.devices.set("ROBOT-ARM-01", [device.address, false]);
    const input = payload();
    const prepared = preparePayload(input);
    const domain = { name: "HRCSafetyLog", version: "1", chainId: CHAIN_ID, verifyingContract: ADDRESS };
    const value = { eventId: prepared.payload.eventId, deviceId: input.deviceId, timestamp: input.timestamp, dataHash: prepared.dataHash, riskLevel: 3 };
    const signature = await device.signTypedData(domain, TYPES, value);
    assert.equal((await f.service.processSafetyEvent(input)).code, "SIGNATURE_REQUIRED");
    assert.equal((await f.service.processSafetyEvent({ ...input, sensorData: { distance: 0.2 }, signature })).code, "INVALID_SIGNATURE");
    const wrong = await device.signTypedData({ ...domain, chainId: 1 }, TYPES, value);
    assert.equal((await f.service.processSafetyEvent({ ...input, signature: wrong })).code, "INVALID_SIGNATURE");
    const result = await f.service.processSafetyEvent({ ...input, signature });
    assert.equal(result.success, true);
    const iface = f.contract.interface;
    assert.equal(iface.parseTransaction({ data: ethers.Transaction.from(f.state.signed[0]).data }).name, "recordSafetyEventSigned");
    f.state.devices.set(input.deviceId, [device.address, true]);
    assert.equal((await f.service.processSafetyEvent({ ...input, signature })).code, "DEVICE_REVOKED");
});
test("wrong chain and pre-existing on-chain identity fail before sending", async t => {
    const f = fixture(t);
    f.state.chainId = 1;
    assert.equal((await f.service.processSafetyEvent(payload())).code, "NETWORK_MISMATCH");
    f.state.chainId = CHAIN_ID;
    f.state.events.set(preparePayload(payload()).payload.eventId, []);
    assert.equal((await f.service.processSafetyEvent(payload())).code, "ONCHAIN_EVENT_EXISTS");
    assert.equal(f.state.signed.length, 0);
});
test("archive detects tampering and rejects unknown historical payload", async t => {
    const f = fixture(t);
    const result = await f.service.processSafetyEvent(payload());
    const record = f.store.list()[0];
    record.payload.sensorData.distance = 99;
    f.store.save(record);
    assert.throws(() => f.service.getStoredPayload(result.eventId), /integrity/);
    assert.throws(() => f.service.getStoredPayload("historical-event"), /historical/);
});
test("store refuses a second running writer", t => {
    const f = fixture(t);
    assert.throws(() => new EventStore(f.directory), /running writer/);
});
test("recovery refuses replacement payload and unexpected on-chain data", async t => {
    const f = fixture(t);
    const result = await f.service.processSafetyEvent(payload());
    const event = f.state.events.get(result.eventId);
    event[3] = "0x" + "1".repeat(64);
    assert.equal((await f.service.processSafetyEvent(payload())).code, "ONCHAIN_CONFLICT");
    const record = f.store.list()[0];
    record.payload.sensorData.distance = 12;
    f.store.save(record);
    assert.equal((await f.service.processSafetyEvent(payload())).code, "STORE_INTEGRITY_ERROR");
    assert.equal(f.state.signed.length, 1);
});
test("raw transaction corruption never broadcasts", async t => {
    const f = fixture(t);
    f.state.confirm = false;
    await f.service.processSafetyEvent(payload());
    const record = f.store.list()[0];
    record.rawTransaction = "0x1234";
    f.store.save(record);
    assert.equal((await f.service.processSafetyEvent(payload())).code, "STORE_INTEGRITY_ERROR");
    assert.equal(f.state.broadcasts.length, 1);
});
test("non-owner admin blockchain_api cannot unlock and unsigned gateway cannot claim device signature", async t => {
    const f = fixture(t);
    f.state.locked.set("ROBOT-ARM-01", true);
    f.contract.owner = async () => ethers.Wallet.createRandom().address;
    assert.equal((await f.service.unlockRobot("ROBOT-ARM-01")).code, "NOT_OWNER");
    assert.equal((await f.service.processSafetyEvent(payload({ signature: "0x" + "1".repeat(130) }))).code, "DEVICE_NOT_REGISTERED");
    assert.equal(f.state.signed.length, 0);
});
test("unlock uses owner wallet and retries the same lock epoch once", async t => {
    const f = fixture(t);
    await f.service.processSafetyEvent(payload());
    const first = await f.service.unlockRobot("ROBOT-ARM-01");
    assert.equal(first.success, true);
    assert.equal(first.robotLocked, false);
    const retry = await f.service.unlockRobot("ROBOT-ARM-01");
    assert.equal(retry.transactionHash, first.transactionHash);
    assert.equal(f.state.signed.length, 2);
});
