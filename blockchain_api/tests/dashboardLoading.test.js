const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

for (const missingMessage of [false, true]) {
    test(`dashboard loads while transaction API waits, including cached HTML=${missingMessage}`, async () => {
        const source = fs.readFileSync(path.join(__dirname, "../../dashboard/app.js"), "utf8");
        const dashboard = source.slice(source.indexOf("async function loadDashboard()"), source.indexOf("function showDetails("));
        const transactions = source.slice(source.indexOf("async function loadTransactions()"), source.indexOf('document.querySelectorAll("[data-refresh]")', source.indexOf("async function loadTransactions()")));
        class Element {
            constructor() { this.children = []; this.textContent = ""; }
            appendChild(child) { this.children.push(child); }
            replaceChildren() { this.children = []; }
        }
        const elements = new Map();
        const byId = id => {
            if (missingMessage && id === "transactionsMessage") return null;
            if (!elements.has(id)) elements.set(id, new Element());
            return elements.get(id);
        };
        let rejectTransactions;
        const pending = new Promise((resolve, reject) => { rejectTransactions = reject; });
        const context = vm.createContext({
            Date, console, ROBOT_ID: "ROBOT-ARM-01", loading: false, unlocking: false, robotLocked: null,
            backendConnected: null, transactionsLoading: false, transactionRows: [], transactionMessage: "",
            byId, refreshBtn: new Element(), unlockBtn: new Element(),
            document: { querySelector: () => new Element(), createElement: () => new Element() },
            setState: (id, state) => { byId(id).textContent = state; },
            renderRobot: locked => { byId("robotStatus").textContent = locked ? "LOCKED" : "UNKNOWN"; },
            renderEvents: events => { byId("eventCount").textContent = String(events.length); },
            tableMessage() {},
            updateAdminControls() {},
            request: async url => url === "/api/transactions" ? pending
                : url === "/api/events" ? { success: true, events: [{}] }
                : { success: true, robotLocked: true }
        });
        vm.runInContext(dashboard + transactions + "\nfunction syncSidebarViews() { renderTransactions(); }", context);
        await vm.runInContext("loadDashboard()", context);
        assert.equal(byId("eventCount").textContent, "1");
        assert.equal(byId("robotStatus").textContent, "LOCKED");
        assert.equal(context.loading, false);
        assert.equal(context.refreshBtn.disabled, false);
        assert.equal(context.transactionsLoading, true);
        rejectTransactions(new Error("Transaction API timeout"));
        await new Promise(resolve => setImmediate(resolve));
        assert.equal(context.transactionsLoading, false);
        assert.match(context.transactionMessage, /timeout/);
        assert.equal(byId("eventCount").textContent, "1");
        assert.equal(byId("robotStatus").textContent, "LOCKED");
    });
}
