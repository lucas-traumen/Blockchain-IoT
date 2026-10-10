const test = require("node:test");
const assert = require("node:assert/strict");
const { ethers } = require("ethers");
const { createWalletAccess } = require("../../dashboard/wallet");
const owner = "0x0000000000000000000000000000000000000001";
const other = "0x0000000000000000000000000000000000000002";
const abi = new ethers.Interface(["function owner() view returns(address)", "function isRobotLocked(string) view returns(bool)", "function unlockRobot(string)", "function operators(address) view returns(bool)", "function getSafetyEvent(string) view returns(string eventId,string deviceId,uint256 timestamp,bytes32 dataHash,uint8 riskLevel,uint256 recordedAt)", "function triggerEmergencyStop(string,string)"]);
function fixture() {
    const calls = [], listeners = {};
    const state = { account: owner, chain: "0x1", locked: true, operator: true, risk: 3, device: "ROBOT-ARM-01", reject: false, receiptFails: false };
    const provider = {
        on(name, callback) { listeners[name] = callback; },
        async request({ method, params }) {
            calls.push({ method, params });
            if (method === "eth_requestAccounts" && state.reject) throw Object.assign(new Error("Rejected"), { code: 4001 });
            if (method === "eth_requestAccounts" || method === "eth_accounts") return state.account ? [state.account] : [];
            if (method === "eth_chainId") return state.chain;
            if (method === "wallet_switchEthereumChain") { state.chain = params[0].chainId; return null; }
            if (method === "eth_call") {
                const parsed = abi.parseTransaction({ data: params[0].data });
                if (parsed.name === "operators") return abi.encodeFunctionResult(parsed.name, [state.operator]);
                if (parsed.name === "getSafetyEvent") return abi.encodeFunctionResult(parsed.name, [parsed.args[0], state.device, 1, "0x" + "0".repeat(64), state.risk, 1]);
                return abi.encodeFunctionResult(parsed.name, parsed.name === "owner" ? [owner] : [state.locked]);
            }
            if (method === "eth_sendTransaction") return "0x" + "a".repeat(64);
            if (method === "eth_getTransactionReceipt") {
                if (state.receiptFails) throw new Error("RPC unavailable");
                return { status: "0x1" };
            }
            throw new Error("Unexpected method " + method);
        }
    };
    return { calls, listeners, state, wallet: createWalletAccess(provider, ethers) };
}
test("MetaMask connection switches Hardhat Local and owner signs the existing unlock method", async () => {
    const f = fixture();
    await f.wallet.connect();
    assert.equal(f.wallet.isOwner, true);
    assert.equal(f.state.chain, "0x7a69");
    const result = await f.wallet.unlock("ROBOT-ARM-01");
    assert.equal(result.pending, false);
    const sent = f.calls.find(call => call.method === "eth_sendTransaction").params[0];
    assert.equal(sent.from, owner);
    assert.equal(sent.to, "0x5FbDB2315678afecb367f032d93F642f64180aa3");
    assert.equal(abi.parseTransaction(sent).args[0], "ROBOT-ARM-01");
});
test("owner lock validates existing event and operator permission before signing", async () => {
    const f = fixture();
    await f.wallet.connect();
    f.state.locked = false;
    f.state.account = other;
    await assert.rejects(f.wallet.lock("ROBOT-ARM-01", "event"), /owner wallet/);
    f.state.account = owner;
    f.state.operator = false;
    await assert.rejects(f.wallet.lock("ROBOT-ARM-01", "event"), /operator permission/);
    f.state.operator = true;
    f.state.risk = 1;
    await assert.rejects(f.wallet.lock("ROBOT-ARM-01", "event"), /WARNING/);
    f.state.risk = 3;
    f.state.device = "OTHER";
    await assert.rejects(f.wallet.lock("ROBOT-ARM-01", "event"), /WARNING/);
    assert.equal(f.calls.some(call => call.method === "eth_sendTransaction"), false);
    f.state.device = "ROBOT-ARM-01";
    assert.equal((await f.wallet.lock("ROBOT-ARM-01", "event")).pending, false);
    const sent = f.calls.find(call => call.method === "eth_sendTransaction").params[0];
    assert.equal(abi.parseTransaction(sent).name, "triggerEmergencyStop");
    assert.equal(abi.parseTransaction(sent).args[1], "event");
});
test("non-owner, unlocked robot and changed network cannot submit unlock", async () => {
    const f = fixture();
    await f.wallet.connect();
    f.state.account = other;
    f.listeners.accountsChanged([other]);
    assert.equal(f.wallet.isOwner, false);
    await assert.rejects(f.wallet.unlock("ROBOT-ARM-01"), /owner wallet/);
    f.state.account = owner;
    f.state.locked = false;
    await assert.rejects(f.wallet.unlock("ROBOT-ARM-01"), /already unlocked/);
    f.state.locked = true;
    f.state.chain = "0x1";
    await assert.rejects(f.wallet.unlock("ROBOT-ARM-01"), /owner wallet/);
    assert.equal(f.calls.some(call => call.method === "eth_sendTransaction"), false);
});
test("missing extension, cancelled connection, disconnect and lost receipt show safe states", async () => {
    const missing = createWalletAccess(undefined, ethers);
    await missing.connect();
    assert.match(missing.state.message, /not available/);
    const f = fixture();
    f.state.reject = true;
    await f.wallet.connect();
    assert.equal(f.wallet.isOwner, false);
    assert.match(f.wallet.state.message, /cancelled/);
    f.state.reject = false;
    await f.wallet.connect();
    f.state.receiptFails = true;
    assert.equal((await f.wallet.unlock("ROBOT-ARM-01")).pending, true);
    f.listeners.disconnect();
    assert.equal(f.wallet.isOwner, false);
    assert.equal(f.wallet.state.account, null);
});
