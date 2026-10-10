const API_URL = "http://localhost:3000";
const ROBOT_ID = "ROBOT-ARM-01";
const byId = id => document.getElementById(id);
const refreshBtn = byId("refreshBtn");
const tableBody = byId("eventsTableBody");
const unlockBtn = byId("unlockRobotBtn");
let loading = false;
let unlocking = false;
let robotLocked = null;
let selectedTransaction = null;
let sidebarEvents = [];
let backendConnected = null;
let transactionRows = [];
let transactionMessage = "Loading transactions...";
let transactionsLoading = false;
let rewardConfig = null;
let rewardSummary = null;
let rewardSessions = [];
let rewardMessage = "Loading reward data...";
let rewardsLoading = false;
function lockCandidate() {
    return sidebarEvents.filter(event => event.deviceId === ROBOT_ID &&
        [2, 3].includes(Number(event.riskLevel))).sort((a, b) =>
        Number(b.blockNumber) - Number(a.blockNumber) || Number(b.timestamp) - Number(a.timestamp))[0] || null;
}
function updateAdminControls() {
    const owner = window.hrcWallet?.isOwner === true;
    unlockBtn.disabled = !owner || robotLocked !== true || unlocking || loading;
    const lockBtn = byId("lockRobotBtn");
    lockBtn.disabled = !owner || robotLocked !== false || unlocking || loading || !lockCandidate();
    byId("lockEventMessage").textContent = lockCandidate()
        ? "Lock reference event: " + lockCandidate().eventId + " (risk " + lockCandidate().riskLevel + ")"
        : "Lock requires a recorded WARNING/VIOLATION event for " + ROBOT_ID + ".";
    byId("connectWalletBtn").disabled = unlocking || window.hrcWallet?.state.connecting === true;
}

function safetyState(event) {
    // Historical API returns risk only; these labels are derived, not AI output.
    const risk = event.riskLevel;
    if (risk === null || risk === undefined || risk === "") return "UNKNOWN";
    return ["CLEAR", "LOW", "WARNING", "VIOLATION"][Number(risk)] || "UNKNOWN";
}
function stateClass(state) {
    return state === "VIOLATION" ? "violation-text"
        : ["LOW", "WARNING"].includes(state) ? "warning-text"
        : state === "CLEAR" ? "clear-text" : "";
}
function formatTimestamp(value) {
    if (value === null || value === undefined || value === "") return "-";
    const date = new Date(Number(value) * 1000);
    return Number.isFinite(date.getTime()) ? date.toLocaleString() : "-";
}
function setState(id, state) {
    const element = byId(id);
    element.textContent = state;
    element.classList.remove("clear-text", "warning-text", "violation-text", "safety-clear");
    const color = stateClass(state);
    if (color) element.classList.add(color);
}
async function request(path, options = {}) {
    const response = await fetch(API_URL + path, {
        ...options, signal: AbortSignal.timeout(["/api/events", "/api/transactions", "/api/rewards/sessions"].includes(path) ? 90000 : 20000)
    });
    const data = await response.json();
    if (!response.ok || data.success !== true) {
        const error = new Error(data.error || `HTTP ${response.status}`);
        error.status = response.status;
        error.code = data.code;
        throw error;
    }
    return data;
}
function tableMessage(message) {
    sidebarEvents = [];
    const row = document.createElement("tr");
    row.className = "empty-row";
    const cell = document.createElement("td");
    cell.colSpan = 8;
    cell.textContent = message;
    row.appendChild(cell);
    tableBody.replaceChildren(row);
}
function renderEvents(events) {
    byId("eventCount").textContent = String(events.length);
    document.querySelector(".nav-badge").textContent = String(events.length);
    const sorted = [...events].sort((a, b) =>
        Number(b.blockNumber) - Number(a.blockNumber) ||
        Number(b.timestamp) - Number(a.timestamp));
    sidebarEvents = sorted;
    const latest = sorted.find(event => event.deviceId === ROBOT_ID);
    const state = latest ? safetyState(latest) : "UNKNOWN";
    ["aiSafetyState", "predictionState", "robotSafetyState"].forEach(id => setState(id, state));
    if (!events.length) {
        tableMessage("No safety events recorded.");
        return;
    }
    const fragment = document.createDocumentFragment();
    for (const event of sorted) {
        const row = document.createElement("tr");
        const state = safetyState(event);
        const values = [
            formatTimestamp(event.timestamp), event.eventId, event.deviceId,
            state, event.riskLevel, event.blockNumber, event.transactionHash
        ];
        values.forEach((value, index) => {
            const cell = document.createElement("td");
            cell.textContent = String(value ?? "-");
            if (index === 3) cell.className = stateClass(state);
            if (index === 6) {
                cell.className = "mono";
                cell.title = String(value ?? "");
            }
            row.appendChild(cell);
        });
        const action = document.createElement("td");
        const button = document.createElement("button");
        button.type = "button";
        button.className = "view-all-btn";
        button.textContent = "Details";
        button.addEventListener("click", () => showDetails(event));
        action.appendChild(button);
        row.appendChild(action);
        fragment.appendChild(row);
    }
    tableBody.replaceChildren(fragment);
}
function renderRobot(locked) {
    robotLocked = locked;
    const state = locked === true ? "LOCKED" : locked === false ? "UNLOCKED" : "UNKNOWN";
    for (const id of ["robotStatus", "robotAccessState"]) {
        const element = byId(id);
        element.textContent = state;
        element.style.color = locked === true ? "var(--violation)"
            : locked === false ? "var(--clear)" : "var(--text-muted)";
    }
    // This describes the current lock, not a reconstructed historical E-Stop log.
    byId("emergencyStopState").textContent = locked === true ? "ACTIVE (LOCKED)"
        : locked === false ? "INACTIVE (UNLOCKED)" : "UNKNOWN";
    const indicator = document.querySelector(".robot-status-indicator");
    indicator.classList.toggle("unlocked", locked === false);
    indicator.style.background = locked === true ? "var(--violation-bg)"
        : locked === false ? "var(--clear-bg)" : "var(--border)";
    indicator.querySelector("span").style.background = locked === true ? "var(--violation)"
        : locked === false ? "var(--clear)" : "var(--text-muted)";
    updateAdminControls();
}
function rewardStatusClass(status) {
    return status === "REWARDED" ? "clear-text"
        : ["PENALTY", "REWARD_POOL_EMPTY"].includes(status) ? "violation-text"
        : status ? "warning-text" : "";
}
function rewardCell(row, value, className = "") {
    const cell = document.createElement("td");
    cell.textContent = String(value ?? "-");
    if (className) cell.className = className;
    return cell;
}
function renderRewardRows(targetId, rows, compact = false) {
    const table = byId(targetId);
    if (!table) return;
    table.replaceChildren();
    if (!rows.length) {
        const row = document.createElement("tr");
        row.className = "empty-row";
        const cell = document.createElement("td");
        cell.colSpan = compact ? 7 : 8;
        cell.textContent = rewardMessage.startsWith("Updated:") ? "No settled reward sessions." : rewardMessage;
        row.appendChild(cell);
        table.appendChild(row);
        return;
    }
    for (const session of rows) {
        const row = document.createElement("tr");
        const values = compact
            ? [session.sessionKey, session.outcome, session.settlementStatus, session.rewardPaid + " HRC", session.penaltyCollected + " HRC", session.blockNumber, session.transactionHash]
            : [session.sessionKey, session.outcome, session.settlementStatus, session.stakeReturned + " HRC", session.rewardPaid + " HRC", session.penaltyCollected + " HRC", session.blockNumber, session.transactionHash];
        values.forEach((value, index) => {
            const cell = rewardCell(row, value);
            if ((compact && index === 2) || (!compact && index === 2)) cell.className = rewardStatusClass(session.settlementStatus);
            if (String(value || "").startsWith("0x") && String(value).length === 66) {
                cell.className = "mono";
                cell.title = String(value);
            }
            row.appendChild(cell);
        });
        table.appendChild(row);
    }
}
function renderRewards() {
    const summary = rewardSummary;
    const beneficiary = summary?.beneficiary || rewardConfig?.defaultBeneficiary || "-";
    const balance = summary ? summary.balance + " HRC" : "- HRC";
    const pool = summary ? summary.rewardPoolBalance + " HRC" : "- HRC";
    const status = rewardSessions[0]?.settlementStatus || (summary ? "No settled session" : "UNAVAILABLE");
    ["rewardBalance", "rewardBalancePanel", "rewardViewBalance"].forEach(id => {
        const element = byId(id);
        if (element) element.textContent = balance;
    });
    ["rewardPoolBalance", "rewardViewPool"].forEach(id => {
        const element = byId(id);
        if (element) element.textContent = pool;
    });
    ["rewardBeneficiary", "rewardViewBeneficiary"].forEach(id => {
        const element = byId(id);
        if (element) element.textContent = beneficiary;
    });
    const statusElement = byId("rewardSettlementStatus");
    if (statusElement) statusElement.textContent = status;
    const messageElement = byId("rewardMessage");
    if (messageElement) messageElement.textContent = rewardMessage;
    const viewMessage = byId("rewardViewMessage");
    if (viewMessage) viewMessage.textContent = rewardMessage;
    renderRewardRows("rewardSessionsTableBody", rewardSessions);
    renderRewardRows("rewardViewSessionsTableBody", rewardSessions, true);
}
async function loadRewards() {
    if (rewardsLoading) return;
    rewardsLoading = true;
    rewardMessage = "Loading reward data...";
    renderRewards();
    try {
        rewardConfig = await request("/api/rewards/config");
        if (!rewardConfig.defaultBeneficiary) throw new Error("No beneficiary has a settled on-chain session yet");
        const beneficiary = encodeURIComponent(rewardConfig.defaultBeneficiary);
        const [summary, sessions] = await Promise.all([
            request("/api/rewards/summary?beneficiary=" + beneficiary),
            request("/api/rewards/sessions?beneficiary=" + beneficiary + "&limit=20")
        ]);
        rewardSummary = summary;
        rewardSessions = Array.isArray(sessions.sessions) ? sessions.sessions : [];
        rewardMessage = "Updated: " + new Date().toLocaleString() + " · chain " + summary.chainId;
    } catch (error) {
        rewardSummary = null;
        rewardSessions = [];
        rewardMessage = "Unable to load rewards: " + error.message;
    } finally {
        rewardsLoading = false;
        renderRewards();
    }
}
async function loadDashboard() {
    if (loading || unlocking) return;
    loading = true;
    refreshBtn.disabled = true;
    refreshBtn.textContent = "Loading...";
    byId("dashboardMessage").textContent = "Loading events, robot status and session rewards...";
    byId("robotStatusMessage").textContent = "Loading robot status...";
    byId("eventCount").textContent = "-";
    document.querySelector(".nav-badge").textContent = "-";
    ["aiSafetyState", "predictionState", "robotSafetyState"].forEach(id => setState(id, "UNKNOWN"));
    renderRobot(null);
    tableMessage("Loading safety events...");
    backendConnected = null;
    syncSidebarViews();
    void loadTransactions();
    if (typeof loadRewards === "function") void loadRewards();
    const [eventsResult, robotResult] = await Promise.allSettled([
        request("/api/events"), request(`/api/robots/${encodeURIComponent(ROBOT_ID)}/status`)
    ]);
    backendConnected = robotResult.status === "fulfilled" && typeof robotResult.value.robotLocked === "boolean";
    const errors = [];
    try {
        if (eventsResult.status === "fulfilled" && Array.isArray(eventsResult.value.events)) {
            renderEvents(eventsResult.value.events);
        } else {
            const message = eventsResult.status === "rejected"
                ? eventsResult.reason.message : "Invalid events response";
            tableMessage("Unable to load events: " + message);
            errors.push("Events: " + message);
        }
        if (robotResult.status === "fulfilled" &&
            typeof robotResult.value.robotLocked === "boolean") {
            renderRobot(robotResult.value.robotLocked);
            byId("robotStatusMessage").textContent = "Robot status updated from blockchain_api.";
        } else {
            const message = robotResult.status === "rejected"
                ? robotResult.reason.message : "Invalid robot status response";
            renderRobot(null);
            byId("robotStatusMessage").textContent = "Unable to load robot status: " + message;
            errors.push("Robot: " + message);
        }
        byId("dashboardMessage").textContent = errors.length
            ? errors.join(" | ") : "Updated: " + new Date().toLocaleString();
    } finally {
        loading = false;
        refreshBtn.disabled = false;
        refreshBtn.textContent = "Refresh Data";
        updateAdminControls();
        syncSidebarViews();
    }
}
function showDetails(event) {
    const values = {
        detailEventId: event.eventId, detailDeviceId: event.deviceId,
        detailSafetyState: safetyState(event), detailRisk: event.riskLevel,
        detailTx: event.transactionHash, detailDataHash: event.dataHash
    };
    Object.entries(values).forEach(([id, value]) => {
        byId(id).textContent = String(value ?? "-");
    });
    selectedTransaction = /^0x[0-9a-fA-F]{64}$/.test(event.transactionHash || "")
        ? event.transactionHash : null;
    byId("etherscanBtn").disabled = !selectedTransaction;
    byId("eventModal").classList.remove("hidden");
}
function closeDetails() {
    byId("eventModal").classList.add("hidden");
}
async function controlRobot(action) {
    if (!window.hrcWallet?.isOwner || unlocking || loading) return;
    const locking = action === "lock";
    const event = lockCandidate();
    if (locking ? robotLocked !== false || !event : robotLocked !== true) return;
    const violation = !locking && byId("robotSafetyState").textContent === "VIOLATION"
        ? " Latest recorded state is still VIOLATION; unlocking does not clear this record." : "";
    if (!window.confirm(`${locking ? "Lock" : "Unlock"} ${ROBOT_ID} using MetaMask?${locking ? " Reference event: " + event.eventId : violation}`)) return;
    unlocking = true;
    updateAdminControls();
    refreshBtn.disabled = true;
    syncSidebarViews();
    let confirmed = false;
    const label = locking ? "Lock" : "Unlock";
    byId("robotStatusMessage").textContent = "Confirm " + label.toLowerCase() + " in MetaMask...";
    try {
        const submitted = hash => {
            byId("robotStatusMessage").textContent = label + " submitted: " + hash + ". Waiting for local confirmation...";
        };
        const result = locking ? await window.hrcWallet.lock(ROBOT_ID, event.eventId, submitted)
            : await window.hrcWallet.unlock(ROBOT_ID, submitted);
        confirmed = !result.pending;
        renderRobot(null);
        byId("robotStatusMessage").textContent = result.pending
            ? label + " pending: " + result.hash + ". Refresh status before retrying."
            : label + " confirmed: " + result.hash;
        void loadTransactions();
    } catch (error) {
        renderRobot(null);
        byId("robotStatusMessage").textContent = (error.code === 4001 ? "MetaMask request cancelled."
            : label + " could not be confirmed: " + (error.shortMessage || error.message)) + " Refresh status before retrying.";
    } finally {
        unlocking = false;
        refreshBtn.disabled = false;
        updateAdminControls();
        syncSidebarViews();
        if (confirmed) void loadDashboard();
    }
}
async function unlockRobot() { return controlRobot("unlock"); }
async function lockRobot() { return controlRobot("lock"); }
refreshBtn.addEventListener("click", loadDashboard);
if (typeof createWalletAccess === "function" && typeof ethers !== "undefined") {
    const injected = window.ethereum?.providers?.find(provider => provider.isMetaMask) || window.ethereum;
    window.hrcWallet = createWalletAccess(injected, ethers, state => {
        byId("walletButtonLabel").textContent = state.connecting ? "Connecting..."
            : state.account ? state.account.slice(0, 6) + "..." + state.account.slice(-4) : "Connect Wallet";
        byId("connectWalletBtn").disabled = state.connecting || unlocking;
        byId("walletAccessMessage").textContent = state.message;
        byId("connectedWalletAddress").textContent = state.account || "Not connected";
        byId("contractOwnerAddress").textContent = state.owner || "Not verified";
        byId("walletRole").textContent = state.isOwner ? "OWNER" : state.account && state.owner ? "READ ONLY" : "NOT VERIFIED";
        byId("walletSettingsState").textContent = state.account || "Not connected";
        updateAdminControls();
    });
    byId("connectWalletBtn").addEventListener("click", () => { void window.hrcWallet.connect(); });
} else {
    byId("connectWalletBtn").addEventListener("click", () => {
        byId("walletAccessMessage").textContent = "Wallet scripts could not load. Refresh the page with Ctrl+F5.";
    });
}
unlockBtn.addEventListener("click", unlockRobot);
byId("lockRobotBtn").addEventListener("click", lockRobot);
byId("closeModalBtn").addEventListener("click", closeDetails);
document.querySelector(".modal-overlay").addEventListener("click", closeDetails);
document.addEventListener("keydown", event => {
    if (event.key === "Escape") closeDetails();
});
byId("etherscanBtn").addEventListener("click", () => {
    if (selectedTransaction) window.open(
        "about:blank", "_blank", "noopener,noreferrer"
    );
});

const viewTitles = {
    dashboard: "Safety Control Center", "live-monitoring": "Live Monitoring",
    "safety-events": "Safety Events", blockchain: "Blockchain",
    transactions: "Transactions", rewards: "Session Rewards", robot: "Robot", settings: "Settings"
};
function navigate() {
    const requested = window.location.hash.slice(1);
    const current = Object.hasOwn(viewTitles, requested) ? requested : "dashboard";
    document.querySelectorAll(".sidebar-view").forEach(view => {
        view.hidden = view.id !== "view-" + current;
    });
    document.querySelectorAll(".sidebar-nav [data-view]").forEach(link => {
        const active = link.dataset.view === current;
        link.classList.toggle("active", active);
        if (active) link.setAttribute("aria-current", "page");
        else link.removeAttribute("aria-current");
    });
    document.querySelector(".page-heading h2").textContent = viewTitles[current];
    closeDetails();
}
function syncSidebarViews() {
    document.querySelectorAll("[data-refresh]").forEach(button => {
        button.disabled = loading || unlocking;
        button.textContent = loading ? "Loading..." : "Refresh Data";
    });
    document.querySelectorAll("[data-load-message]").forEach(element => {
        element.textContent = byId("dashboardMessage").textContent;
    });
    byId("liveConnection").textContent = loading ? "LOADING"
        : backendConnected === true ? "CONNECTED"
        : backendConnected === false ? "UNAVAILABLE" : "UNKNOWN";
    byId("robotViewStatus").textContent = byId("robotStatus").textContent;
    byId("robotViewStatus").style.color = byId("robotStatus").style.color;
    setState("robotViewSafety", byId("robotSafetyState").textContent);
    byId("robotViewMessage").textContent = byId("robotStatusMessage").textContent;

    // Reuse the dashboard's safely rendered rows and bind fresh listeners.
    const allEvents = byId("allEventsTableBody");
    allEvents.replaceChildren();
    Array.from(tableBody.children).forEach((row, index) => {
        const copy = row.cloneNode(true);
        const details = copy.querySelector("button");
        if (details && sidebarEvents[index]) {
            details.addEventListener("click", () => showDetails(sidebarEvents[index]));
        }
        allEvents.appendChild(copy);
    });
    renderTransactions();
    renderRewards();
}
async function loadTransactions() {
    if (transactionsLoading) return;
    transactionsLoading = true;
    transactionRows = [];
    transactionMessage = "Loading transactions...";
    try {
        renderTransactions();
        const data = await request("/api/transactions");
        if (!Array.isArray(data.transactions)) throw new Error("Invalid transactions response");
        transactionRows = data.transactions;
        transactionMessage = "Updated: " + new Date().toLocaleString();
    } catch (error) {
        transactionMessage = "Unable to load transactions: " + error.message;
    } finally {
        transactionsLoading = false;
        renderTransactions();
    }
}
function renderTransactions() {
    const message = byId("transactionsMessage");
    if (message) message.textContent = transactionMessage;
    const transactions = byId("transactionsTableBody");
    if (!transactions) return;
    transactions.replaceChildren();
    if (!transactionRows.length) {
        const row = document.createElement("tr");
        row.className = "empty-row";
        const cell = document.createElement("td");
        cell.colSpan = 7;
        cell.textContent = transactionMessage.startsWith("Updated:") ? "No transactions recorded." : transactionMessage;
        row.appendChild(cell);
        transactions.appendChild(row);
        return;
    }
    transactionRows.forEach(event => {
        const row = document.createElement("tr");
        [event.transactionHash, (event.actions || []).map(action => action === "ROBOT_UNLOCK" ? "ROBOT UNLOCK" : action === "ROBOT_LOCK" ? "ROBOT LOCK" : action === "SAFETY_EVENT" ? "SAFETY EVENT" : "UNKNOWN").join(" + "), event.blockNumber, event.eventId, event.deviceId, event.riskLevel]
            .forEach(value => {
                const cell = document.createElement("td");
                cell.textContent = String(value ?? "-");
                row.appendChild(cell);
            });
        const cell = document.createElement("td");
        if (/^0x[0-9a-fA-F]{64}$/.test(event.transactionHash || "")) {
            const link = document.createElement("a");
            link.textContent = "Local transaction";
            link.href = "about:blank";
            link.target = "_blank";
            link.rel = "noopener noreferrer";
            cell.appendChild(link);
        } else {
            cell.textContent = "Not available";
        }
        row.appendChild(cell);
        transactions.appendChild(row);
    });
}
document.querySelectorAll("[data-refresh]").forEach(button => {
    button.addEventListener("click", loadDashboard);
});
window.addEventListener("hashchange", navigate);
navigate();

loadDashboard();
