const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

test("dashboard permits only owner lock/unlock and chooses the right robot event", async () => {
    const source = fs.readFileSync(path.join(__dirname, "../../dashboard/app.js"), "utf8");
    const controls = source.slice(source.indexOf("function lockCandidate()"), source.indexOf("function safetyState("));
    const action = source.slice(source.indexOf("async function controlRobot("), source.indexOf('refreshBtn.addEventListener'));
    const elements = new Map();
    const byId = id => { if (!elements.has(id)) elements.set(id, { textContent: "" }); return elements.get(id); };
    const calls = [];
    const wallet = { isOwner: false, state: {},
        async lock(device, event) { calls.push(["lock", device, event]); return { pending: true, hash: "0xtest" }; },
        async unlock(device) { calls.push(["unlock", device]); return { pending: true, hash: "0xtest" }; } };
    const context = vm.createContext({
        ROBOT_ID: "ROBOT-ARM-01", robotLocked: false, unlocking: false, loading: false,
        sidebarEvents: [{ deviceId: "OTHER", riskLevel: 3, blockNumber: 20, eventId: "wrong" },
            { deviceId: "ROBOT-ARM-01", riskLevel: 2, blockNumber: 10, eventId: "warning" }],
        byId, unlockBtn: {}, refreshBtn: {}, window: { hrcWallet: wallet, confirm: () => true },
        syncSidebarViews() {}, loadTransactions() {}, loadDashboard() {},
        renderRobot(value) { context.robotLocked = value; }
    });
    vm.runInContext(controls + action, context);
    vm.runInContext("updateAdminControls()", context);
    assert.equal(byId("lockRobotBtn").disabled, true);
    await vm.runInContext("lockRobot()", context);
    assert.equal(calls.length, 0);
    wallet.isOwner = true;
    vm.runInContext("updateAdminControls()", context);
    assert.equal(byId("lockRobotBtn").disabled, false);
    assert.equal(context.unlockBtn.disabled, true);
    await vm.runInContext("lockRobot()", context);
    assert.deepEqual(calls[0], ["lock", "ROBOT-ARM-01", "warning"]);
    context.robotLocked = true;
    vm.runInContext("updateAdminControls()", context);
    assert.equal(byId("lockRobotBtn").disabled, true);
    assert.equal(context.unlockBtn.disabled, false);
    await vm.runInContext("unlockRobot()", context);
    assert.deepEqual(calls[1], ["unlock", "ROBOT-ARM-01"]);
    context.robotLocked = false;
    context.sidebarEvents = [];
    vm.runInContext("updateAdminControls()", context);
    assert.equal(byId("lockRobotBtn").disabled, true);
});
