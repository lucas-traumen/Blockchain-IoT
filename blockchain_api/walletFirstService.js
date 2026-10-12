const { ethers } = require("ethers");
const { ApiError, identifier } = require("./eventPayload");
const { sessionKey } = require("./rewardPolicy");

// Wallet-first onboarding: the user keeps their own key, the backend stays the
// oracle/operator that confirms sensor data. This service links a signed SIWE
// login to the reward-session layer so a connected
// wallet can read its own history and prepare transactions to sign locally.
class WalletFirstService {
    constructor({ customerService, walletService, sessionKey }) {
        this.customerService = customerService;
        this.walletService = walletService;
        this.sessionKey = sessionKey;
    }

    /// @dev The reward/session layer is optional: a deployment can run the app
    ///      identity alone (for example a pure SIWE gate) without the reward
    ///      session store. Login must keep working in that case.
    _wallet() {
        if (typeof this.walletService === "function") return this.walletService();
        return this.walletService || null;
    }

    /// @notice Verify the login message, then bind the verified wallet to a
    ///         session identity. The challenge nonce is single-use, so a replayed
    ///         signature is rejected by the customer service.
    login(input) {
        const authenticated = this.customerService.authenticateWallet(input);
        const walletService = this._wallet();
        if (!walletService) {
            return {
                success: true,
                token: authenticated.token,
                mode: authenticated.mode,
                expiresAt: authenticated.expiresAt,
                user: authenticated.user
            };
        }
        const user = walletService.loginWithWallet(authenticated.user.walletAddress);
        return {
            success: true,
            token: authenticated.token,
            mode: authenticated.mode,
            expiresAt: authenticated.expiresAt,
            user: authenticated.user,
            rewardUser: user.user
        };
    }

    _requireWallet() {
        const walletService = this._wallet();
        if (!walletService) throw new ApiError(503, "WALLET_SERVICE_UNAVAILABLE", "The reward session layer is not configured");
        return walletService;
    }

    /// @notice Transaction the wallet must sign locally to approve the stake. The
    ///         backend never signs on behalf of a wallet-first user.
    async prepareApproval(userId) {
        const walletService = this._requireWallet();
        const wallet = await walletService.getWallet(userId);
        const user = walletService._user(userId);
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
        const walletService = this._requireWallet();
        const user = walletService._user(userId);
        if (!user.walletFirst) throw new ApiError(400, "NOT_WALLET_FIRST", "This account is managed by the backend");
        const sessionRef = input.sessionRef === undefined || input.sessionRef === null || input.sessionRef === ""
            ? "default" : String(input.sessionRef);
        const sessionId = walletService._sessionId(userId, { sessionRef });
        const wallet = await walletService.getWallet(userId);
        let taskIdHash = null;
        let policyHash = null;
        let fullRewardDuration = null;
        let data;
        if (walletService.config.rewardProtocolVersion === "task-rewards-v2") {
            const task = input.taskSnapshot?.task;
            policyHash = input.taskSnapshot?.snapshotHash;
            if (!task?.taskId || !/^0x[0-9a-fA-F]{64}$/.test(String(policyHash || ""))) {
                throw new ApiError(400, "TASK_ASSIGNMENT_REQUIRED", "Accept a published task before starting a V2 reward session");
            }
            fullRewardDuration = Number(task.completionRule?.maximumDurationS);
            if (!Number.isSafeInteger(fullRewardDuration) || fullRewardDuration < 30) {
                throw new ApiError(400, "TASK_ASSIGNMENT_REQUIRED", "The accepted task must include a valid full-reward duration of at least 30 seconds");
            }
            taskIdHash = ethers.id(task.taskId);
            data = new ethers.Interface(["function startSession(bytes32 sessionId,bytes32 taskIdHash,bytes32 policyHash,uint256 fullRewardDuration)"])
                .encodeFunctionData("startSession", [this.sessionKey(sessionId), taskIdHash, policyHash, fullRewardDuration]);
        } else {
            data = new ethers.Interface(["function startSession(bytes32 sessionId)"])
                .encodeFunctionData("startSession", [this.sessionKey(sessionId)]);
        }
        return {
            success: true,
            mode: "wallet-first",
            sessionId,
            chainId: wallet.chainId,
            from: wallet.walletAddress,
            to: wallet.rewardsAddress,
            value: "0",
            taskId: input.taskSnapshot?.task?.taskId || null,
            taskIdHash,
            policyHash,
            fullRewardDuration,
            data,
            description: "Start a reward session with the staked amount"
        };
    }

    /// @notice Transaction the wallet must sign locally to end a session. The
    ///         outcome itself is decided by the backend oracle from sensor data,
    ///         never by the caller, so a user cannot self-report a valid session.
    async prepareEndSession(userId, sessionId) {
        const walletService = this._requireWallet();
        const user = walletService._user(userId);
        if (!user.walletFirst) throw new ApiError(400, "NOT_WALLET_FIRST", "This account is managed by the backend");
        const id = identifier(sessionId, "sessionId");
        const record = walletService.store.get(id);
        if (record && record.userId !== userId) {
            throw new ApiError(403, "SESSION_OWNER_MISMATCH", "This session belongs to another user");
        }
        const wallet = await walletService.getWallet(userId);
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

function buildWalletFirstService(customerService, walletService) {
    return new WalletFirstService({ customerService, walletService, sessionKey });
}

module.exports = { WalletFirstService, buildWalletFirstService };
