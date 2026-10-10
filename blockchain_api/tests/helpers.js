const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { ethers } = require("ethers");
const { EventStore } = require("../eventStore");
const { EventService } = require("../eventService");

const ADDRESS = "0xF6CD683a1E46357Ae9A6e8e68D7d0094bfacd49C";
const CHAIN_ID = 11155111;
const ABI = [
    "function recordSafetyEvent(string,string,uint256,bytes32,uint8)",
    "function recordSafetyEventSigned(string,string,uint256,bytes32,uint8,bytes)",
    "function unlockRobot(string)",
    "event EmergencyStopTriggered(string,string,uint256,address)"
];
function payload(overrides = {}) {
    return { deviceId: "ROBOT-ARM-01", timestamp: 1790996000, sessionId: "TEST-SESSION", sequence: 1,
        sensorData: { distance: 0.18, speed: 2.1, force: 15.2 },
        aiResult: { riskLevel: 3, state: "VIOLATION", model: "LSTM" }, ...overrides };
}
function fixture(t) {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "hrc-blockchain_api-test-"));
    const owner = ethers.Wallet.createRandom();
    const iface = new ethers.Interface(ABI);
    const state = { events: new Map(), receipts: new Map(), devices: new Map(), locked: new Map(),
        broadcasts: [], signed: [], nonce: 0, chainId: CHAIN_ID, confirm: true,
        loseBroadcastResponse: false, broadcastFails: false, receiptFails: false, revert: false };
    const provider = {
        async getNetwork() { return { chainId: state.chainId }; },
        async getTransactionCount() { return state.nonce; },
        async getTransactionReceipt(hash) {
            if (state.receiptFails) throw new Error("RPC unavailable");
            return state.receipts.get(hash) ?? null;
        },
        async waitForTransaction(hash) { return state.receipts.get(hash) ?? null; },
        async broadcastTransaction(raw) {
            state.broadcasts.push(raw);
            if (state.broadcastFails) throw new Error("Disconnected before send");
            const tx = ethers.Transaction.from(raw);
            if (state.receipts.has(tx.hash)) return { hash: tx.hash };
            state.nonce = Math.max(state.nonce, tx.nonce + 1);
            const call = iface.parseTransaction({ data: tx.data });
            if (state.confirm) {
                const logs = [];
                if (!state.revert && call.name.startsWith("recordSafetyEvent")) {
                    const [eventId, deviceId, timestamp, hash, risk] = call.args;
                    state.events.set(eventId, [eventId, deviceId, timestamp, hash, risk, 1790996001n]);
                    if (risk === 3n && !state.locked.get(deviceId)) {
                        state.locked.set(deviceId, true);
                        const log = iface.encodeEventLog(iface.getEvent("EmergencyStopTriggered"), [deviceId, eventId, 1790996001, owner.address]);
                        logs.push({ ...log, address: ADDRESS });
                    }
                } else if (!state.revert && call.name === "unlockRobot") state.locked.set(call.args[0], false);
                state.receipts.set(tx.hash, { hash: tx.hash, status: state.revert ? 0 : 1, blockNumber: 11853429, logs });
            }
            if (state.loseBroadcastResponse) throw new Error("Disconnected after send");
            return { hash: tx.hash };
        }
    };
    const wallet = {
        async getAddress() { return owner.address; },
        async populateTransaction(tx) { return { ...tx, chainId: CHAIN_ID, gasLimit: 500000n, gasPrice: 1n }; },
        async signTransaction(tx) { const raw = await owner.signTransaction(tx); state.signed.push(raw); return raw; }
    };
    const contract = {
        interface: iface,
        async getAddress() { return ADDRESS; },
        async getDevice(deviceId) { return state.devices.get(deviceId) || [ethers.ZeroAddress, false]; },
        async getSafetyEvent(id) {
            if (!state.events.has(id)) throw Object.assign(new Error("missing"), { revert: { name: "EventNotFound" } });
            return state.events.get(id);
        },
        async isRobotLocked(id) { return state.locked.get(id) || false; },
        async getLockInfo(id) { return [state.locked.get(id) || false, "LOCK-EVENT", owner.address, 1790996001n]; },
        async owner() { return owner.address; }
    };
    for (const name of ["recordSafetyEvent", "recordSafetyEventSigned", "unlockRobot"]) {
        contract[name] = { async populateTransaction(...args) {
            return { to: ADDRESS, data: iface.encodeFunctionData(name, args) };
        } };
    }
    let store = new EventStore(directory);
    const createService = () => new EventService({ provider, wallet, contract, store, chainId: CHAIN_ID, contractAddress: ADDRESS, waitMs: 0 });
    const service = createService();
    t.after(() => { store.close(); fs.rmSync(directory, { recursive: true, force: true }); });
    return { state, store, service, owner, provider, contract, directory,
        reopen() { store.close(); store = new EventStore(directory); return createService(); } };
}
module.exports = { fixture, payload, ADDRESS, CHAIN_ID };
