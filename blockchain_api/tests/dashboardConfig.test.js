const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../../dashboard/app.js"), "utf8");
const configSource = source.slice(0, source.indexOf("window.hrcRuntimeConfig"));
const nginxConfig = fs.readFileSync(path.join(__dirname, "../../docker/nginx/dashboard.conf"), "utf8");
const demoScript = fs.readFileSync(path.join(__dirname, "../../scripts/run_local_demo.sh"), "utf8");

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

test("dashboard proxies return JSON when an upstream API is unreachable", () => {
    assert.match(nginxConfig, /location @chain_api_unavailable/);
    assert.match(nginxConfig, /"code":"CHAIN_API_UNAVAILABLE"/);
    assert.match(nginxConfig, /error_page 502 504 = @chain_api_unavailable/);
});

test("dashboard proxy sets CSP and browser security headers", () => {
    assert.match(nginxConfig, /Content-Security-Policy/);
    assert.match(source, /270 samples at 10 Hz/);
    assert.match(nginxConfig, /X-Frame-Options "DENY"/);
    assert.match(nginxConfig, /X-Content-Type-Options "nosniff"/);
    assert.match(nginxConfig, /Referrer-Policy "no-referrer"/);
});

test("local demo validates proxy JSON before announcing readiness", () => {
    assert.match(demoScript, /wait_for_dashboard_chain_proxy/);
    assert.match(demoScript, /JSON\.parse\(fs\.readFileSync/);
    assert.match(demoScript, /value\.success !== true/);
});
