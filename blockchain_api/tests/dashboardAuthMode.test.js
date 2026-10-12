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

function harness({ authMode = "wallet", trigger = null } = {}) {
    const elements = new Map();
    const byId = id => {
        if (!elements.has(id)) elements.set(id, { textContent: "", hidden: false, disabled: false, classList: { contains: () => true } });
        return elements.get(id);
    };
    const walletButtons = [{ hidden: false, disabled: false }, { hidden: false, disabled: false }];
    const calls = { walletModal: 0, walletConnect: 0 };
    const context = vm.createContext({
        console,
        appAuthMode: authMode,
        byId,
        text: (id, value) => { const element = byId(id); if (element) element.textContent = String(value ?? "--"); },
        document: { querySelectorAll: selector => (selector === "[data-wallet-account-open]" ? walletButtons : []) },
        openWalletAccountModal: () => { calls.walletModal += 1; },
        closeWalletAccountModal: () => { calls.walletModal -= 1; },
        window: trigger ? { hrcWallet: { connect: () => { calls.walletConnect += 1; } } } : {}
    });
    vm.runInContext(block, context);
    return { context, byId, walletButtons, calls };
}

test("the header opens MetaMask and the fixed-account picker has been removed", () => {
    const h = harness({ authMode: "siwe", trigger: true });
    vm.runInContext("applyAuthMode(); onAccountTrigger()", h.context);
    assert.equal(h.byId("accountTriggerLabel").textContent, "SIWE LOGIN");
    assert.equal(h.byId("walletAccountButtonLabel").textContent, "Connect MetaMask");
    assert.equal(h.calls.walletConnect, 1);
    assert.equal(h.calls.walletModal, 0);
    const html = fs.readFileSync(path.join(__dirname, "../../dashboard/index.html"), "utf8");
    assert.doesNotMatch(html, /demoAccountModal|user_1|user_2|Choose account/);
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

test("wallet errors appear beside the visible connect action, including a rejected connection", async () => {
    const h = harness({ authMode: "siwe" });
    vm.runInContext("applyAuthMode(); onAccountTrigger()", h.context);
    assert.equal(h.byId("walletFirstConnectBtn").hidden, false);
    assert.equal(h.byId("walletFirstStatus").hidden, false);
    assert.match(h.byId("walletFirstStatus").textContent, /MetaMask/);
    h.context.window.hrcWallet = { connect: async () => { throw new Error("Connection rejected"); } };
    await vm.runInContext("connectWalletFirst()", h.context);
    assert.equal(h.byId("walletFirstStatus").textContent, "Connection rejected");
    assert.equal(h.byId("walletAccessMessage").textContent, "Connection rejected");
});

test("connected wallets keep a retry action after login failure or a wrong network", () => {
    const h = harness({ authMode: "siwe", trigger: true });
    const account = "0x1111111111111111111111111111111111111111";
    h.context.walletBusy = null;
    h.context.appSession = { error: "Application login was cancelled in MetaMask.", inFlightKey: null, key: null };
    h.context.walletFirstActive = () => false;
    h.context.window.hrcWallet.state = { account, chainId: 31337, networkMatches: true, connecting: false };
    vm.runInContext("renderWalletConnection()", h.context);
    assert.match(h.byId("walletFirstStatus").textContent, /cancelled/);
    assert.equal(h.byId("walletFirstConnectBtn").hidden, false);
    assert.equal(h.byId("walletFirstConnectBtn").disabled, false);
    assert.equal(h.byId("walletFirstConnectBtn").textContent, "Retry sign in");
    h.context.window.hrcWallet.state.networkMatches = false;
    h.context.window.hrcWallet.state.message = "Wrong network";
    vm.runInContext("renderWalletConnection()", h.context);
    assert.equal(h.byId("walletFirstStatus").textContent, "Wrong network");
    assert.equal(h.byId("walletFirstConnectBtn").textContent, "Switch network");
});
