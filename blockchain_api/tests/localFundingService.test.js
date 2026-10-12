const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { ethers } = require("ethers");
const { LocalFundingService } = require("../localFundingService");
const { SqliteCustomerStore } = require("../sqliteStores");

test("local funding is explicit and refuses any chain other than 31337 before transfer", async t => {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "hrc-funding-"));
    const store = new SqliteCustomerStore(path.join(directory, "funding.db"));
    t.after(() => { store.close(); fs.rmSync(directory, { recursive: true, force: true }); });
    const wallet = ethers.Wallet.createRandom();
    const service = new LocalFundingService({
        walletService: {
            config: { chainId: 31337, rewardAddress: "0x0000000000000000000000000000000000000002", tokenAddress: "0x0000000000000000000000000000000000000003" },
            identityStore: store,
            provider: { async getNetwork() { return { chainId: 11155111n }; } },
            token: { async balanceOf() { return 0n; } }
        },
        privateKey: ethers.Wallet.createRandom().privateKey,
        enabled: true,
        network: "localhost"
    });
    const failed = await service.fund(wallet.address);
    assert.equal(failed.status, "FAILED");
    assert.match(failed.reason, /chain 31337/);
    const retry = await service.fund(wallet.address);
    assert.equal(retry.status, "FAILED");
    assert.deepEqual(retry.transactionHashes, []);
});

test("funding stays disabled unless every local-only guard is enabled", async () => {
    const service = new LocalFundingService({ walletService: { config: { chainId: 31337 } }, enabled: true, network: "sepolia" });
    assert.equal((await service.fund("0x0000000000000000000000000000000000000001")).status, "DISABLED");
});
