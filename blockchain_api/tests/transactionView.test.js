const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

test("transaction view renders unlock, safe local transaction links, empty and error states", () => {
    const source = fs.readFileSync(path.join(__dirname, "../../dashboard/app.js"), "utf8");
    const start = source.indexOf("function renderTransactions()");
    const end = source.indexOf('document.querySelectorAll("[data-refresh]")', start);
    class Element {
        constructor() { this.children = []; }
        appendChild(child) { this.children.push(child); }
        replaceChildren() { this.children = []; }
    }
    const table = new Element(), message = new Element();
    const hash = "0x" + "a".repeat(64);
    const context = vm.createContext({
        byId: id => id === "transactionsTableBody" ? table : message,
        document: { createElement: () => new Element() },
        transactionRows: [{ transactionHash: hash, actions: ["ROBOT_UNLOCK"], blockNumber: 12, deviceId: "<script>" }],
        transactionMessage: "Updated: now"
    });
    vm.runInContext(source.slice(start, end) + "\nrenderTransactions();", context);
    assert.equal(table.children[0].children.length, 7);
    assert.equal(table.children[0].children[1].textContent, "ROBOT UNLOCK");
    assert.equal(table.children[0].children[3].textContent, "-");
    assert.equal(table.children[0].children[4].textContent, "<script>");
    assert.equal(table.children[0].children[6].children[0].href, "about:blank");
    context.transactionRows = [];
    vm.runInContext("renderTransactions()", context);
    assert.equal(table.children[0].children[0].textContent, "No transactions recorded.");
    context.transactionMessage = "Unable to load transactions: RPC error";
    vm.runInContext("renderTransactions()", context);
    assert.equal(table.children[0].children[0].textContent, context.transactionMessage);
});
