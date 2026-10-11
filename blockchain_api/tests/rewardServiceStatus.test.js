const test = require("node:test");
const assert = require("node:assert/strict");
const { ethers } = require("ethers");
const { RewardService } = require("../rewardService");

// Regression: the live admin/operator status routes derive their HTTP code from
// `result.success`. The fake service used by adminAudit.test.js always set it,
// which masked the fact that the real RewardService omitted it and therefore
// answered 503 with an otherwise valid body.
const OPERATOR = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266";
const TREASURY = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8";
const TOKEN = "0xA51c1fc2f0D1a1b8494Ed1FE312d7C3a78Ed91C0";

function fakeProvider() {
    const balances = { [TREASURY.toLowerCase()]: 2n * 10n ** 18n, "0x5fbdb2315678afecb367f032d93f642f64180aa3": 1000n * 10n ** 18n };
    return {
        // token.balanceOf(holder) — the holder is the last 20 bytes of the calldata.
        async call({ data }) {
            const holder = "0x" + String(data).slice(-40);
            const value = balances[holder.toLowerCase()] ?? 0n;
            return ethers.AbiCoder.defaultAbiCoder().encode(["uint256"], [value]);
        }
    };
}

function fakeWallet() {
    return { async getAddress() { return OPERATOR; } };
}

function fakeContract() {
    return {
        async owner() { return OPERATOR; },
        async paused() { return false; },
        async rewardPoolBalance() { return 1000n * 10n ** 18n; },
        async maxRewardPool() { return 100000n * 10n ** 18n; },
        async penaltyTreasury() { return TREASURY; },
        async token() { return TOKEN; },
        async getAddress() { return "0x5FbDB2315678afecb367f032d93F642f64180aa3"; }
    };
}

test("getOperatorStatus and getAdminStatus report success so the routes answer 200", async () => {
    const service = new RewardService({ provider: fakeProvider(), wallet: fakeWallet(), contract: fakeContract() });

    const operator = await service.getOperatorStatus();
    assert.equal(operator.success, true, "operator status must set success for the route contract");
    assert.equal(operator.operator, OPERATOR);
    assert.equal(operator.isOwner, true);
    assert.equal(operator.maxRewardPool, "100000.0");

    const admin = await service.getAdminStatus();
    assert.equal(admin.success, true, "admin status must set success for the route contract");
    assert.equal(admin.penaltyTreasury, TREASURY);
    assert.equal(admin.token, TOKEN);
    assert.equal(admin.treasuryBalance, "2.0");
    assert.equal(admin.contractTokenBalance, "1000.0");
});
