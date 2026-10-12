const { ethers } = require("ethers");
const { withSignerWrite } = require("./signerQueue");

const TARGET_NATIVE = ethers.parseEther("1");
const TARGET_HRC = ethers.parseUnits("20", 18);
const TOKEN_ABI = ["function balanceOf(address) view returns (uint256)", "function transfer(address,uint256) returns (bool)"];

class LocalFundingService {
    constructor({ walletService, privateKey, enabled = false, network = "", clock = () => new Date() } = {}) {
        this.walletService = walletService;
        this.enabled = enabled === true || enabled === "true";
        this.network = network;
        this.clock = clock;
        this.privateKey = privateKey || "";
    }
    static fromEnv(walletService, env = process.env) {
        return new LocalFundingService({ walletService, privateKey: env.HRC_LOCAL_FAUCET_PRIVATE_KEY,
            enabled: env.HRC_LOCAL_ONBOARDING_FUNDING, network: env.HRC_BLOCKCHAIN_NETWORK || "", clock: () => new Date() });
    }
    configuration() {
        const config = this.walletService.config;
        if (!config || !ethers.isAddress(config.rewardAddress || "") || !ethers.isAddress(config.tokenAddress || "")
            || !this.enabled || this.network !== "localhost" || Number(config.chainId) !== 31337 || !this.privateKey) return null;
        return { config, deploymentId: `${config.chainId}:${config.rewardAddress.toLowerCase()}:${config.tokenAddress.toLowerCase()}` };
    }
    status(walletAddress) {
        const configured = this.configuration();
        if (!configured) return { enabled: false, status: "DISABLED", reason: "Local onboarding funding is not enabled for this deployment" };
        const row = this.walletService.identityStore?.getFunding?.(walletAddress.toLowerCase(), configured.deploymentId);
        return row || { enabled: true, status: "AVAILABLE", nativeTarget: "1", hrcTarget: "20" };
    }
    async fund(walletAddress) {
        const configured = this.configuration();
        if (!configured) return this.status(walletAddress);
        const address = ethers.getAddress(walletAddress);
        const store = this.walletService.identityStore;
        if (typeof store?.claimFunding !== "function") return { enabled: true, status: "UNAVAILABLE", reason: "Durable SQLite funding ledger is required" };
        const { config, deploymentId } = configured;
        const claimed = store.claimFunding(address.toLowerCase(), deploymentId, this.clock().toISOString());
        if (!claimed.claimed) return claimed.record;
        const hashes = [];
        try {
            const network = await this.walletService.provider.getNetwork();
            if (Number(network.chainId) !== 31337) throw new Error("Funding is restricted to chain 31337");
            const signer = new ethers.Wallet(this.privateKey, this.walletService.provider);
            const [nativeBalance, tokenBalance] = await Promise.all([
                this.walletService.provider.getBalance(address),
                this.walletService.token.balanceOf(address)
            ]);
            const nativeAmount = nativeBalance < TARGET_NATIVE ? TARGET_NATIVE - nativeBalance : 0n;
            const tokenAmount = tokenBalance < TARGET_HRC ? TARGET_HRC - tokenBalance : 0n;
            const token = new ethers.Contract(config.tokenAddress, TOKEN_ABI, signer);
            await withSignerWrite(await signer.getAddress(), async () => {
                if (nativeAmount > 0n) {
                    const tx = await signer.sendTransaction({ to: address, value: nativeAmount });
                    hashes.push(tx.hash);
                    await tx.wait(1);
                }
                if (tokenAmount > 0n) {
                    const tx = await token.transfer(address, tokenAmount);
                    hashes.push(tx.hash);
                    await tx.wait(1);
                }
            });
            return store.updateFunding(address.toLowerCase(), deploymentId, {
                enabled: true, status: "FUNDED", transactionHashes: hashes,
                fundedNative: nativeAmount.toString(), fundedHrc: tokenAmount.toString(),
                completedAt: this.clock().toISOString()
            });
        } catch (error) {
            return store.updateFunding(address.toLowerCase(), deploymentId, {
                enabled: true, status: "FAILED", transactionHashes: hashes,
                reason: error.shortMessage || error.message || "Local funding failed"
            });
        }
    }
}

module.exports = { LocalFundingService, TARGET_NATIVE, TARGET_HRC };
