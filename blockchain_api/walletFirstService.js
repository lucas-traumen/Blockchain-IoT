const { ethers } = require("ethers");
const { ApiError, identifier } = require("./eventPayload");
const { sessionKey } = require("./rewardPolicy");

// Wallet-first onboarding: the user keeps their own key, the backend stays the
// oracle/operator that confirms sensor data. This service links a signed SIWE
// login (or legacy wallet login) to the reward-session layer so a connected
// wallet can read its own history and prepare transactions to sign locally.
class WalletFirstService {
    constructor({ customerService, demoService, sessionKey }) {
        this.customerService = customerService;
        this.demoService = demoService;
        this.sessionKey = sessionKey;
    }

    /// @dev The demo/session layer is optional: a deployment can run the app
    ///      identity alone (for example a pure SIWE gate) without the reward
    ///      session store. Login must keep working in that case.
    _demo() {
        if (typeof this.demoService === "function") return this.demoService();
        return this.demoService || null;
    }

    /// @notice Verify the login message, then bind the verified wallet to a
    ///         session identity. The challenge nonce is single-use, so a replayed
    ///         signature is rejected by the customer service.
    login(input) {
        const authenticated = this.customerService.authenticateWallet(input);
        const demo = this._demo();
        if (!demo) {
            return {
                success: true,
                token: authenticated.token,
                mode: authenticated.mode,
                expiresAt: authenticated.expiresAt,
                user: authenticated.user
            };
        }
        const user = demo.loginWithWallet(authenticated.user.walletAddress);
        return {
            success: true,
            // token: application identity (used by /api/me and the behavior API).
            // sessionToken: reward-session identity for the /api/me/sessions routes.
            token: authenticated.token,
            sessionToken: user.token,
            mode: authenticated.mode,
            expiresAt: authenticated.expiresAt,
            user: user.user,
            appUser: authenticated.user
        };
    }

    _requireDemo() {
        const demo = this._demo();
        if (!demo) throw new ApiError(503, "DEMO_SERVICE_UNAVAILABLE", "The reward session layer is not configured");
        return demo;
    }

    /// @notice Transaction the wallet must sign locally to approve the stake. The
    ///         backend never signs on behalf of a wallet-first user.
    async prepareApproval(userId) {
        const demo = this._requireDemo();
        const wallet = await demo.getWallet(userId);
        const user = demo._user(userId);
        if (!user.walletFirst) throw new ApiError(400, "NOT_WALLET_FIRST", "This account is managed by the backend");
        const amount = ethers.parseUnits(wallet.stakeAmount, wallet.decimals);
        return {
            success: true,
            mode: "wallet-first",
            chainId: wallet.chainId,
            from: wallet.walletAddress,
            to: wallet.tokenAddress,
            value: "0",
            data: new ethers.Interface(["function approve(address spender, uint256 amount) returns (bool)"])
                .encodeFunctionData("approve", [wallet.rewardsAddress, amount]),
            description: `Approve ${wallet.stakeAmount} ${wallet.symbol} stake for the reward contract`
        };
    }

    /// @notice Transaction the wallet must sign locally to start a session.
    async prepareStartSession(userId, input = {}) {
        const demo = this._requireDemo();
        const user = demo._user(userId);
        if (!user.walletFirst) throw new ApiError(400, "NOT_WALLET_FIRST", "This account is managed by the backend");
        const sessionRef = input.sessionRef === undefined || input.sessionRef === null || input.sessionRef === ""
            ? "default" : String(input.sessionRef);
        const sessionId = demo._sessionId(userId, { sessionRef });
        const wallet = await demo.getWallet(userId);
        return {
            success: true,
            mode: "wallet-first",
            sessionId,
            chainId: wallet.chainId,
            from: wallet.walletAddress,
            to: wallet.rewardsAddress,
            value: "0",
            data: new ethers.Interface(["function startSession(bytes32 sessionId)"])
                .encodeFunctionData("startSession", [this.sessionKey(sessionId)]),
            description: "Start a reward session with the staked amount"
        };
    }

    /// @notice Transaction the wallet must sign locally to end a session. The
    ///         outcome itself is decided by the backend oracle from sensor data,
    ///         never by the caller, so a user cannot self-report a valid session.
    async prepareEndSession(userId, sessionId) {
        const demo = this._requireDemo();
        const user = demo._user(userId);
        if (!user.walletFirst) throw new ApiError(400, "NOT_WALLET_FIRST", "This account is managed by the backend");
        const id = identifier(sessionId, "sessionId");
        const record = demo.store.get(id);
        if (record && record.userId !== userId) {
            throw new ApiError(403, "SESSION_OWNER_MISMATCH", "This session belongs to another user");
        }
        const wallet = await demo.getWallet(userId);
        return {
            success: true,
            mode: "wallet-first",
            sessionId: id,
            chainId: wallet.chainId,
            from: wallet.walletAddress,
            to: wallet.rewardsAddress,
            value: "0",
            // endSession is operator-only by design; the user signs nothing that
            // could fabricate an outcome.
            data: null,
            operatorOnly: true,
            description: "Session outcome is settled by the backend operator from confirmed sensor data"
        };
    }
}

function buildWalletFirstService(customerService, demoService) {
    return new WalletFirstService({ customerService, demoService, sessionKey });
}

module.exports = { WalletFirstService, buildWalletFirstService };
