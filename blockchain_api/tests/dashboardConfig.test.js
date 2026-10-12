const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../../dashboard/app.js"), "utf8");
const configSource = source.slice(0, source.indexOf("window.hrcRuntimeConfig"));
const nginxConfig = fs.readFileSync(path.join(__dirname, "../../docker/nginx/dashboard.conf"), "utf8");
const walletScript = fs.readFileSync(path.join(__dirname, "../../scripts/run_local_metamask.sh"), "utf8");

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
    assert.match(source, /function walletSessionRequirements\(session\)/);
    assert.match(source, /durationS: 30, minimumSamples: 270/);
    assert.match(source, /Math\.ceil\(durationS \* 10 \* 0\.9\)/);
    assert.match(nginxConfig, /X-Frame-Options "DENY"/);
    assert.match(nginxConfig, /X-Content-Type-Options "nosniff"/);
    assert.match(nginxConfig, /Referrer-Policy "no-referrer"/);
});

test("V2 task reward UI documents the 30-second floor and full-reward threshold", () => {
    const html = fs.readFileSync(path.join(__dirname, "../../dashboard/index.html"), "utf8");
    assert.match(html, /Minimum seconds<input name="minimumDurationS" type="number" min="30"[^>]*value="30"/);
    assert.match(html, /Full reward seconds<input name="maximumDurationS" type="number" min="30"[^>]*value="180"/);
    assert.match(source, /full reward at \$\{task\.completionRule\?\.maximumDurationS \?\? 180\}s \(capped after\)/);
    assert.match(source, /remains eligible beyond it/);
});

test("local MetaMask launcher validates proxy JSON and defaults to the supported stack ports", () => {
    assert.match(walletScript, /wait_for_dashboard_chain_proxy/);
    assert.match(walletScript, /JSON\.parse\(fs\.readFileSync/);
    assert.match(walletScript, /value\.success !== true/);
    assert.match(walletScript, /HRC_DASHBOARD_HOST_PORT:-8081/);
    assert.match(walletScript, /HRC_API_HOST_PORT:-8002/);
    assert.match(walletScript, /HRC_MQTT_HOST_PORT:-1886/);
    assert.match(walletScript, /HRC_APP_AUTH_MODE=siwe/);
});
