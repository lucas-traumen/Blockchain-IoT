// Local ethers bundle is used only for ABI encoding; MetaMask owns signing.
function createWalletAccess(ethereum, ethers, changed = () => {}, runtimeConfig = {}) {
    // API config is authoritative; defaults only support the standalone legacy test.
    const legacyAddress = "0x5FbDB2315678afecb367f032d93F642f64180aa3";
    const contractAddress = () => runtimeConfig.safety?.address || runtimeConfig.safetyAddress || legacyAddress;
    const chain = () => "0x" + Number(runtimeConfig.chainId || 31337).toString(16);
    const abi = new ethers.Interface([
        "function owner() view returns (address)",
        "function isRobotLocked(string) view returns (bool)",
        "function unlockRobot(string)",
        "function operators(address) view returns (bool)",
        "function getSafetyEvent(string) view returns (string eventId,string deviceId,uint256 timestamp,bytes32 dataHash,uint8 riskLevel,uint256 recordedAt)",
        "function triggerEmergencyStop(string,string)"
    ]);
    const state = { account: null, owner: null, chainId: null, networkMatches: false, isOwner: false, connecting: false, message: "Connect MetaMask to verify owner access." };
    let generation = 0;
    const notify = () => changed(state);
    async function read(name, args = []) {
        const result = await ethereum.request({ method: "eth_call", params: [{ to: contractAddress(), data: abi.encodeFunctionData(name, args) }, "latest"] });
        const values = abi.decodeFunctionResult(name, result);
        return name === "getSafetyEvent" ? values : values[0];
    }
    async function refresh() {
        const version = ++generation;
        Object.assign(state, {
            account: null, owner: null, chainId: null, networkMatches: false, isOwner: false
        });
        notify();
        try {
            const accounts = await ethereum.request({ method: "eth_accounts" });
            if (version !== generation) return;
            state.account = accounts[0] || null;
            const network = await ethereum.request({ method: "eth_chainId" });
            if (version !== generation) return;
            state.chainId = Number.parseInt(network, 16);
            state.networkMatches = network.toLowerCase() === chain();
            if (!state.account) {
                state.chainId = null;
                state.networkMatches = false;
                state.message = "Wallet disconnected. Connect MetaMask.";
            } else if (!state.networkMatches) state.message = "Wrong network. Click Connect Wallet to switch to the configured network.";
            else {
                const owner = await read("owner");
                if (version !== generation) return;
                state.owner = owner;
                state.isOwner = state.account.toLowerCase() === owner.toLowerCase();
                state.message = state.isOwner ? "Owner verified on the configured network. Lock/Unlock requires MetaMask confirmation."
                    : "Read-only wallet. Contract owner: " + owner;
            }
        } catch (error) {
            if (version !== generation) return;
            state.isOwner = false;
            state.message = error.code === 4001 ? "MetaMask request cancelled." : "Wallet error: " + (error.shortMessage || error.message);
        }
        if (version === generation) notify();
    }
    async function connect() {
        if (state.connecting) return;
        state.connecting = true;
        notify();
        try {
            if (!ethereum) throw new Error("MetaMask is not available. Install or enable the extension in this browser.");
            await ethereum.request({ method: "eth_requestAccounts" });
            const network = await ethereum.request({ method: "eth_chainId" });
            if (network.toLowerCase() !== chain()) {
                await ethereum.request({ method: "wallet_switchEthereumChain", params: [{ chainId: chain() }] });
            }
            await refresh();
        } catch (error) {
            state.isOwner = false;
            state.message = error.code === 4001 ? "MetaMask request cancelled. Click Connect Wallet to retry."
                : error.code === -32002 ? "Open MetaMask: a connection request is already pending."
                : error.code === 4902 ? "Add the configured network to MetaMask, then reconnect."
                : error.message;
        } finally { state.connecting = false; notify(); }
    }
    async function signMessage(message) {
        if (!ethereum || !state.account) throw new Error("Connect a wallet before signing the application login message.");
        return ethereum.request({ method: "personal_sign", params: [message, state.account] });
    }
    async function control(deviceId, eventId, submitted = () => {}) {
        const locking = eventId !== null;
        await refresh();
        if (!state.isOwner) throw new Error("Connect the contract owner wallet on the configured network before controlling the robot.");
        const account = state.account;
        const version = generation;
        const locked = await read("isRobotLocked", [deviceId]);
        if (locking ? locked : !locked) throw new Error("Robot is already " + (locking ? "locked" : "unlocked") + ". Refresh status.");
        if (locking) {
            // Existing contract requires operator permission even for the owner.
            if (!await read("operators", [account])) throw new Error("Owner wallet lacks the operator permission required by the existing contract to lock.");
            const event = await read("getSafetyEvent", [eventId]);
            if (event.deviceId !== deviceId || Number(event.riskLevel) < 2) {
                throw new Error("Lock requires a WARNING/VIOLATION event for this robot.");
            }
        }
        const accounts = await ethereum.request({ method: "eth_accounts" });
        const network = await ethereum.request({ method: "eth_chainId" });
        if (version !== generation || accounts[0]?.toLowerCase() !== account.toLowerCase() || network.toLowerCase() !== chain()) {
            throw new Error("Wallet account or network changed. Reconnect before unlocking.");
        }
        const data = locking ? abi.encodeFunctionData("triggerEmergencyStop", [deviceId, eventId])
            : abi.encodeFunctionData("unlockRobot", [deviceId]);
        const hash = await ethereum.request({ method: "eth_sendTransaction", params: [{ from: account, to: contractAddress(), data }] });
        submitted(hash);
        const deadline = Date.now() + 90000;
        while (Date.now() < deadline) {
            if (version !== generation) return { pending: true, hash };
            let receipt;
            try { receipt = await ethereum.request({ method: "eth_getTransactionReceipt", params: [hash] }); }
            catch { return { pending: true, hash }; }
            if (receipt) {
                if (BigInt(receipt.status) !== 1n) throw new Error("Robot control transaction reverted: " + hash);
                return { pending: false, hash };
            }
            await new Promise(resolve => setTimeout(resolve, 2000));
        }
        return { pending: true, hash };
    }
    if (ethereum?.on) {
        ethereum.on("accountsChanged", () => { void refresh(); });
        ethereum.on("chainChanged", () => { void refresh(); });
        ethereum.on("disconnect", () => {
            generation++;
            Object.assign(state, { isOwner: false, account: null, owner: null, chainId: null, networkMatches: false, message: "Wallet disconnected." });
            notify();
        });
    }
    return { get isOwner() { return state.isOwner; }, state, connect, refresh, signMessage,
        unlock: (deviceId, submitted) => control(deviceId, null, submitted),
        lock: (deviceId, eventId, submitted) => {
            if (typeof eventId !== "string" || !eventId) throw new Error("Lock reference event is required");
            return control(deviceId, eventId, submitted);
        }
    };
}
if (typeof module !== "undefined") module.exports = { createWalletAccess };
