const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../../dashboard/app.js"), "utf8");
const configSource = source.slice(0, source.indexOf("window.hrcRuntimeConfig"));

function urls(location, overrides = {}) {
    const context = vm.createContext({ URL, window: { location, ...overrides } });
    vm.runInContext(configSource, context);
    return {
        chain: vm.runInContext("API_URL", context),
        iot: vm.runInContext("IOT_API_URL", context)
    };
}

test("hosted dashboard uses same-origin reverse proxies for both APIs", () => {
    assert.deepEqual(
        urls({ protocol: "http:", href: "http://192.168.1.10:8080/#rewards" }),
        { chain: "/api/chain", iot: "/api/iot" }
    );
});

test("an explicit blockchain API URL still overrides the proxy", () => {
    assert.equal(
        urls(
            { protocol: "http:", href: "http://localhost:8080/" },
            { HRC_CHAIN_API_URL: "http://127.0.0.1:3001" }
        ).chain,
        "http://127.0.0.1:3001"
    );
});
