const test = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const { ethers } = require("ethers");
const { createRpcProvider } = require("../rpcProvider");

test("concurrent RPC reads work when endpoint omits batch responses", async (t) => {
    const requests = [];
    const server = http.createServer(async (req, res) => {
        let body = "";
        for await (const chunk of req) body += chunk;
        const input = JSON.parse(body);
        requests.push(input);
        const reply = (item) => ({
            jsonrpc: "2.0", id: item.id,
            result: item.method === "eth_chainId" ? "0xaa36a7"
                : item.method === "eth_blockNumber" ? "0x123" : "0x00"
        });
        res.setHeader("Content-Type", "application/json");
        // Reproduce an RPC that returns only one item from a batch.
        res.end(JSON.stringify(Array.isArray(input) ? [reply(input[0])] : reply(input)));
    });
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    t.after(() => new Promise((resolve) => server.close(resolve)));
    const url = `http://127.0.0.1:${server.address().port}`;
    const call = { to: "0x0000000000000000000000000000000000000001", data: "0x" };

    const original = new ethers.JsonRpcProvider(url);
    t.after(() => original.destroy());
    await original.getNetwork();
    const failed = await Promise.allSettled([
        original.send("eth_blockNumber", []),
        original.send("eth_call", [call, "latest"])
    ]);
    assert.ok(failed.some((result) => result.status === "rejected"
        && result.reason.shortMessage === "missing response for request"));
    original.destroy();

    requests.length = 0;
    const provider = createRpcProvider(url);
    t.after(() => provider.destroy());
    await provider.getNetwork();
    const results = await Promise.all([
        provider.send("eth_blockNumber", []),
        provider.send("eth_call", [call, "latest"]),
        provider.send("eth_call", [call, "latest"])
    ]);
    assert.deepEqual(results, ["0x123", "0x00", "0x00"]);
    assert.ok(requests.length >= 3);
    assert.ok(requests.every((request) => !Array.isArray(request)));
});
