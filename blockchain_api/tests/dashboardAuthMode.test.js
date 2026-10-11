const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../../dashboard/app.js"), "utf8");
const block = source.slice(
    source.indexOf("function walletFirstMode()"),
    source.indexOf("async function loadDashboard()")
);

function harness({ authMode = "demo", trigger = null } = {}) {
    const elements = new Map();
    const byId = id => {
        if (!elements.has(id)) elements.set(id, { textContent: "", hidden: false, disabled: false, classList: { contains: () => true } });
        return elements.get(id);
    };
    const demoButtons = [{ hidden: false, disabled: false }, { hidden: false, disabled: false }];
    const calls = { demoModal: 0, walletConnect: 0 };
    const context = vm.createContext({
        console,
        appAuthMode: authMode,
        byId,
        text: (id, value) => { const element = byId(id); if (element) element.textContent = String(value ?? "--"); },
        document: { querySelectorAll: selector => (selector === "[data-demo-account-open]" ? demoButtons : []) },
        openDemoAccountModal: () => { calls.demoModal += 1; },
        closeDemoAccountModal: () => { calls.demoModal -= 1; },
        window: trigger ? { hrcWallet: { connect: () => { calls.walletConnect += 1; } } } : {}
    });
    vm.runInContext(block, context);
    return { context, byId, demoButtons, calls };
}

test("demo mode keeps the fixed-account picker as the entry point", () => {
    const h = harness({ authMode: "demo" });
    vm.runInContext("applyAuthMode()", h.context);
    assert.equal(h.byId("accountTriggerLabel").textContent, "DEMO ACCOUNT");
    assert.deepEqual(h.demoButtons.map(button => button.hidden), [false, false]);
    assert.equal(vm.runInContext("walletFirstMode()", h.context), false);
    vm.runInContext("onAccountTrigger()", h.context);
    assert.equal(h.calls.demoModal, 1);
    assert.equal(h.calls.walletConnect, 0);
});

test("wallet mode turns the trigger into a wallet connect action", () => {
    const h = harness({ authMode: "wallet", trigger: true });
    vm.runInContext("applyAuthMode()", h.context);
    assert.equal(h.byId("accountTriggerLabel").textContent, "WALLET");
    assert.equal(h.byId("demoAccountButtonLabel").textContent, "Connect wallet");
    assert.deepEqual(h.demoButtons.map(button => button.hidden), [true, true]);
    assert.deepEqual(h.demoButtons.map(button => button.disabled), [true, true]);
    assert.equal(vm.runInContext("walletFirstMode()", h.context), true);
    vm.runInContext("onAccountTrigger()", h.context);
    assert.equal(h.calls.walletConnect, 1);
    assert.equal(h.calls.demoModal, 0);
});

test("siwe mode labels the trigger and explains the signature flow", () => {
    const h = harness({ authMode: "siwe", trigger: true });
    vm.runInContext("applyAuthMode()", h.context);
    assert.equal(h.byId("accountTriggerLabel").textContent, "SIWE LOGIN");
    assert.match(h.byId("walletAccessMessage").textContent, /SIWE/);
});

test("wallet mode without an injected wallet reports the missing extension", () => {
    const h = harness({ authMode: "wallet" });
    vm.runInContext("applyAuthMode()", h.context);
    vm.runInContext("onAccountTrigger()", h.context);
    assert.equal(h.calls.walletConnect, 0);
    assert.match(h.byId("walletAccessMessage").textContent, /No EVM wallet detected/);
});
