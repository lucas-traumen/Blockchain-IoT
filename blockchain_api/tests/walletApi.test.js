const test = require("node:test");
const assert = require("node:assert/strict");
const { createApp } = require("../server");

test("demo login and backend-signed approval routes have been removed", async t => {
    let authCalls = 0;
    const app = createApp({ service: {}, customerService: { authenticateToken() { authCalls++; return null; } }, tokens: {} });
    const server = app.listen(0, "127.0.0.1");
    await new Promise(resolve => server.once("listening", resolve));
    t.after(() => new Promise(resolve => server.close(resolve)));
    const base = `http://127.0.0.1:${server.address().port}`;
    for (const [path, method] of [["/api/demo/accounts", "GET"], ["/api/demo/login", "POST"], ["/api/auth/demo", "POST"],
        ["/api/demo/logout", "POST"], ["/api/demo/admin/overview", "GET"], ["/api/me/wallet/approve", "POST"]]) {
        assert.equal((await fetch(base + path, { method, headers: { authorization: "Bearer user_1-token" } })).status, 404);
    }
    assert.equal(authCalls, 0);
    for (const path of ["/api/me/wallet", "/api/me/sessions", "/api/me/wallet/transactions", "/api/me/wallet/prepare/approve"]) {
        assert.equal((await fetch(base + path, { headers: { authorization: "Bearer user_1-token" } })).status, 401);
    }
});
