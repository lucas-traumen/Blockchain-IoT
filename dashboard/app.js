function serviceOrigin(port) {
    if (window.location?.protocol === "http:" || window.location?.protocol === "https:") {
        const url = new URL(window.location.href);
        url.port = String(port);
        url.pathname = "";
        url.search = "";
        url.hash = "";
        return url.origin;
    }
    return "http://127.0.0.1:" + port;
}
const API_URL = window.HRC_CHAIN_API_URL || serviceOrigin(3000);
const IOT_API_URL = window.HRC_IOT_API_URL || (
    window.location?.protocol === "http:" || window.location?.protocol === "https:"
        ? "/api/iot"
        : serviceOrigin(8001)
);
window.hrcRuntimeConfig = window.hrcRuntimeConfig || {};
let liveState = {
    latest: null, history: [], source: "LIVE", sourceLabel: "UNKNOWN",
    sourceMode: "unknown", connected: false, stream: null, retryMs: 1000,
    lastHeartbeat: 0, loading: false, reconnectTimer: null,
};
let livePollTimer = null;
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
async function requestIot(path, options = {}) {
    const response = await fetch(IOT_API_URL + path, {
        ...options,
        signal: AbortSignal.timeout(8000),
        headers: { Accept: "application/json", ...(options.headers || {}) },
    });
    const data = await response.json();
    if (!response.ok) {
        const error = new Error(data.detail || data.error || `HTTP ${response.status}`);
        error.status = response.status;
        throw error;
    }
    return data;
}
function text(id, value) {
    const element = byId(id);
    if (element) element.textContent = String(value ?? "--");
}
function formatMeters(value) {
    return Number.isFinite(Number(value)) ? Number(value).toFixed(3) + " m" : "-- m";
}
function formatAge(value) {
    if (!value) return "--";
    const age = Math.max(0, Date.now() - new Date(value).getTime());
    if (!Number.isFinite(age)) return "--";
    if (age < 1000) return "<1s ago";
    return Math.round(age / 1000) + "s ago";
}
function sampleKey(record) {
    const ref = record?.assessment?.sample_ref;
    return ref ? [ref.device_id, ref.boot_id, ref.sequence].join(":") : null;
}
function recordDistance(record) {
    const telemetry = record?.telemetry;
    if (telemetry?.valid === true && Number.isFinite(Number(telemetry.distance_mm))) {
        return Number(telemetry.distance_mm) / 1000;
    }
    const value = record?.assessment?.distance_m;
    return Number.isFinite(Number(value)) ? Number(value) : null;
}
function recordThreshold(record) {
    const value = record?.assessment?.safety?.sp_m;
    return Number.isFinite(Number(value)) ? Number(value) : null;
}
function recordPrediction(record) {
    const prediction = record?.assessment?.prediction || {};
    return Array.isArray(prediction.distance_m)
        ? prediction.distance_m.map(Number).filter(Number.isFinite) : [];
}
function recordTimestamp(record) {
    return record?.received_at || record?.assessment?.received_at || record?.assessment?.measured_at || null;
}
function liveStatus() {
    const timestamp = recordTimestamp(liveState.latest);
    if (!timestamp) return "DISCONNECTED";
    const age = Date.now() - new Date(timestamp).getTime();
    if (!Number.isFinite(age) || age > 3000) return "STALE";
    return liveState.connected ? "LIVE" : "DEGRADED";
}
function statusTone(status) {
    return status === "LIVE" ? "clear-text" : ["DEGRADED", "STALE"].includes(status) ? "warning-text" : "violation-text";
}
function setBadge(id, value) {
    const element = byId(id);
    if (!element) return;
    element.textContent = value;
    element.classList.remove("clear-text", "warning-text", "violation-text");
    const tone = statusTone(value);
    if (tone) element.classList.add(tone);
}
function renderRuntimeConfig(config) {
    const network = config?.network || "unknown";
    const chainId = config?.chainId ?? "--";
    const safety = config?.safety?.address || "--";
    const rewards = config?.rewards?.address || "--";
    text("networkEnvironment", String(network).toUpperCase());
    text("networkNameHeader", network);
    text("networkOverview", String(network).toUpperCase() + " · " + chainId);
    text("networkName", String(network).toUpperCase() + " · " + chainId);
    text("networkViewName", network);
    text("networkViewChainId", chainId);
    text("networkViewSafety", safety);
    text("networkViewReward", rewards);
    text("networkViewRpc", config?.rpcUrl || "--");
    text("footerRewardContract", rewards);
    text("settingsChainApi", API_URL);
    text("settingsIotApi", IOT_API_URL);
    text("settingsNetwork", String(network).toUpperCase() + " · " + chainId);
    text("transactionsDescription", "Safety and reward transactions recorded on " + network + " (chain " + chainId + ").");
}
async function loadRuntimeConfig() {
    try {
        const config = await request("/api/config");
        window.hrcRuntimeConfig = config;
        renderRuntimeConfig(config);
        return config;
    } catch (error) {
        window.hrcRuntimeConfig = {};
        renderRuntimeConfig({ network: "unavailable" });
        text("dashboardMessage", "Blockchain config unavailable: " + error.message);
        return null;
    }
}
function svgElement(name, attributes = {}) {
    const element = document.createElementNS("http://www.w3.org/2000/svg", name);
    Object.entries(attributes).forEach(([key, value]) => element.setAttribute(key, String(value)));
    return element;
}
function chartPath(points) {
    return points.map(([x, y], index) => (index ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1)).join(" ");
}
function renderLiveChart() {
    const svg = byId("liveChart");
    if (!svg) return;
    svg.replaceChildren();
    const records = liveState.history.filter(record => recordDistance(record) !== null);
    const latest = liveState.latest;
    const actual = records.map(recordDistance);
    const predictions = recordPrediction(latest);
    const threshold = recordThreshold(latest) ?? recordThreshold(records[records.length - 1]);
    if (!actual.length && !predictions.length) {
        byId("chartPlaceholder")?.removeAttribute("hidden");
        return;
    }
    byId("chartPlaceholder")?.setAttribute("hidden", "hidden");
    const width = 800;
    const height = 220;
    const forecastStart = actual.length > 1 ? 625 : 250;
    const maxValue = Math.max(2, threshold || 0, ...actual, ...predictions, 0.05);
    const y = value => height - (Math.max(0, value) / maxValue) * (height - 8);
    const xActual = index => actual.length > 1 ? (index / (actual.length - 1)) * forecastStart : forecastStart;
    const actualPoints = actual.map((value, index) => [xActual(index), y(value)]);
    if (threshold !== null) {
        const thresholdY = y(threshold);
        svg.appendChild(svgElement("rect", { class: "violation-zone", x: 0, y: thresholdY, width, height: height - thresholdY }));
        svg.appendChild(svgElement("path", { class: "threshold-line", d: `M0 ${thresholdY} L${width} ${thresholdY}` }));
    }
    if (actualPoints.length > 1) {
        svg.appendChild(svgElement("path", { class: "actual-line", d: chartPath(actualPoints) }));
    }
    if (predictions.length) {
        const last = actualPoints[actualPoints.length - 1] || [forecastStart, y(predictions[0])];
        const predictedPoints = [last, ...predictions.map((value, index) => [forecastStart + ((index + 1) * (width - forecastStart)) / predictions.length, y(value)])];
        svg.appendChild(svgElement("path", { class: "prediction-line", d: chartPath(predictedPoints) }));
        predictedPoints.slice(1).forEach(([x, value], index) => {
            const dot = svgElement("circle", { class: "forecast-dot", cx: x, cy: value, r: 4 });
            dot.setAttribute("aria-label", `${latest?.assessment?.prediction?.horizons_ms?.[index] || (index + 1) * 100} ms forecast`);
            svg.appendChild(dot);
        });
    }
    if (actualPoints.length) {
        const [x, value] = actualPoints[actualPoints.length - 1];
        svg.appendChild(svgElement("circle", { class: "actual-dot", cx: x, cy: value, r: 3.5 }));
    }
    if (predictions.length) {
        const label = svgElement("text", { class: "chart-label", x: forecastStart + 8, y: 16 });
        label.textContent = "FORECAST";
        svg.appendChild(label);
    }
}
function renderPrediction(record) {
    const prediction = record?.assessment?.prediction || {};
    const list = byId("predictionList");
    if (!list) return;
    list.replaceChildren();
    const distances = Array.isArray(prediction.distance_m) ? prediction.distance_m : [];
    const horizons = Array.isArray(prediction.horizons_ms) ? prediction.horizons_ms : [];
    const threshold = recordThreshold(record);
    if (!distances.length) {
        const message = document.createElement("p");
        message.textContent = prediction.status ? "Prediction status: " + prediction.status : "Waiting for a runtime assessment.";
        list.appendChild(message);
        return;
    }
    const scale = Math.max(0.05, threshold || 0, ...distances);
    distances.forEach((distance, index) => {
        const row = document.createElement("div");
        row.className = "prediction-row";
        const horizon = document.createElement("span");
        horizon.textContent = (horizons[index] || (index + 1) * 100) + " ms";
        const bar = document.createElement("div");
        bar.className = "prediction-bar";
        const fill = document.createElement("span");
        fill.style.width = Math.min(100, Math.max(2, distance / scale * 100)) + "%";
        if (threshold !== null && distance <= threshold) fill.style.background = "var(--violation)";
        bar.appendChild(fill);
        const value = document.createElement("strong");
        value.className = threshold !== null && distance <= threshold ? "violation-text" : "clear-text";
        value.textContent = formatMeters(distance);
        row.append(horizon, bar, value);
        list.appendChild(row);
    });
}
function renderLiveState() {
    const latest = liveState.latest;
    const assessment = latest?.assessment;
    const prediction = assessment?.prediction || {};
    const model = latest?.model || liveState.model || {};
    const provenance = latest?.provenance || {};
    const state = assessment?.safety?.status || "UNKNOWN";
    const status = liveStatus();
    setBadge("sourceBadge", (liveState.sourceLabel || "UNKNOWN") + " · " + status);
    setBadge("liveHealthBadge", status);
    text("liveConnection", status === "DISCONNECTED" ? "DISCONNECTED" : status);
    text("liveTelemetryConnection", status);
    text("currentDistance", formatMeters(recordDistance(latest)));
    text("safetyThreshold", formatMeters(recordThreshold(latest)));
    text("updateRate", updateRate());
    text("liveCurrentDistance", formatMeters(recordDistance(latest)));
    text("liveThreshold", formatMeters(recordThreshold(latest)));
    text("livePrediction", prediction.predicted_violation_ms ? prediction.predicted_violation_ms + " ms" : "No crossing predicted");
    text("liveLastUpdate", formatAge(recordTimestamp(latest)));
    text("liveProvenance", provenance.training_data === "synthetic-only" ? "synthetic-only · threshold unverified" : (provenance.training_data || "--"));
    text("liveModelStatus", model.status ? model.status + (model.promotion_status ? " · " + model.promotion_status : "") : "--");
    text("liveParity", provenance.parity_status ? provenance.parity_status + (provenance.edge_backend_max_abs_diff_m == null ? "" : " · " + (provenance.edge_backend_max_abs_diff_m * 1000).toFixed(2) + " mm") : "--");
    text("activeModelLabel", model.model_version || "MODEL UNAVAILABLE");
    text("modelProvenance", model.training_data === "synthetic-only" ? "synthetic-only" : (model.model_version || "--"));
    text("inferenceLatency", latest?.edge_prediction?.inference_us ? (Number(latest.edge_prediction.inference_us) / 1000).toFixed(1) + " ms edge" : "backend runtime");
    text("liveSourceDescription", liveState.sourceLabel === "SYNTHETIC STREAM"
        ? "Synthetic runtime stream. Threshold is a mathematical benchmark and is not hardware-validated."
        : "Experimental hardware runtime. Model remains synthetic-trained and is not hardware-validated.");
    ["aiSafetyState", "predictionState"].forEach(id => setState(id, state));
    renderPrediction(latest);
    renderLiveChart();
    const footer = byId("footerStatus");
    if (footer) footer.textContent = status === "LIVE" ? "● Live" : "● " + status;
}
function updateRate() {
    const timestamps = liveState.history.map(recordTimestamp).filter(Boolean).map(value => new Date(value).getTime()).filter(Number.isFinite);
    if (timestamps.length < 2) return "--";
    const deltas = timestamps.slice(1).map((value, index) => value - timestamps[index]).filter(value => value > 0);
    if (!deltas.length) return "--";
    const mean = deltas.reduce((sum, value) => sum + value, 0) / deltas.length;
    return (1000 / mean).toFixed(1) + " Hz";
}
function applyLiveSnapshot(snapshot) {
    if (!snapshot) return;
    liveState.source = snapshot.source || liveState.source;
    liveState.sourceMode = snapshot.source_mode || liveState.sourceMode;
    liveState.sourceLabel = snapshot.source_label || liveState.sourceLabel;
    liveState.model = snapshot.model || liveState.model;
    const record = snapshot.latest || (snapshot.assessment ? snapshot : null);
    if (record) {
        const key = sampleKey(record);
        const history = liveState.history.filter(item => sampleKey(item) !== key);
        history.push(record);
        liveState.history = history.sort((a, b) => new Date(recordTimestamp(a) || 0) - new Date(recordTimestamp(b) || 0)).slice(-600);
        liveState.latest = record;
    }
    renderLiveState();
}
async function loadLiveRuntime() {
    if (liveState.loading) return;
    liveState.loading = true;
    const [latestResult, historyResult, healthResult] = await Promise.allSettled([
        requestIot("/live/latest"), requestIot("/live/history?seconds=60&limit=600"), requestIot("/system/health"),
    ]);
    if (latestResult.status === "fulfilled") applyLiveSnapshot(latestResult.value);
    if (historyResult.status === "fulfilled") {
        const data = historyResult.value;
        liveState.history = Array.isArray(data.records) ? data.records : liveState.history;
        if (!liveState.latest && liveState.history.length) {
            liveState.latest = liveState.history[liveState.history.length - 1];
        }
        liveState.model = data.model || liveState.model;
        liveState.source = data.source || liveState.source;
        liveState.sourceLabel = data.source_label || liveState.sourceLabel;
        liveState.sourceMode = data.source_mode || liveState.sourceMode;
        renderLiveState();
    }
    if (healthResult.status === "fulfilled") {
        backendConnected = healthResult.value.status === "ok";
        liveState.health = healthResult.value;
    } else if (latestResult.status === "rejected" && historyResult.status === "rejected") {
        backendConnected = false;
    }
    renderLiveState();
    liveState.loading = false;
    syncSidebarViews();
}
function startPollingFallback() {
    if (livePollTimer) return;
    livePollTimer = window.setInterval(() => { void loadLiveRuntime(); }, 1000);
    renderLiveState();
}
function stopPollingFallback() {
    if (!livePollTimer) return;
    window.clearInterval(livePollTimer);
    livePollTimer = null;
}
function scheduleStreamReconnect() {
    if (liveState.reconnectTimer) return;
    startPollingFallback();
    const delay = liveState.retryMs;
    liveState.retryMs = Math.min(30000, liveState.retryMs * 2);
    liveState.reconnectTimer = window.setTimeout(() => {
        liveState.reconnectTimer = null;
        connectLiveStream();
    }, delay);
}
function connectLiveStream() {
    if (liveState.stream || typeof EventSource === "undefined") {
        if (typeof EventSource === "undefined") startPollingFallback();
        return;
    }
    const stream = new EventSource(IOT_API_URL + "/live/stream");
    liveState.stream = stream;
    stream.onopen = () => {
        liveState.connected = true;
        liveState.retryMs = 1000;
        stopPollingFallback();
        renderLiveState();
    };
    stream.addEventListener("runtime", event => {
        try {
            applyLiveSnapshot(JSON.parse(event.data));
            liveState.connected = true;
        } catch (error) {
            console.warn("Invalid live runtime event", error);
        }
    });
    stream.addEventListener("heartbeat", event => {
        liveState.lastHeartbeat = Date.now();
        if (event.data) liveState.connected = true;
        renderLiveState();
    });
    stream.onerror = () => {
        liveState.connected = false;
        stream.close();
        liveState.stream = null;
        scheduleStreamReconnect();
        renderLiveState();
    };
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
    if (typeof loadLiveRuntime === "function") void loadLiveRuntime();
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
function initWallet() {
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
        }, window.hrcRuntimeConfig);
        byId("connectWalletBtn").addEventListener("click", () => { void window.hrcWallet.connect(); });
    } else {
        byId("connectWalletBtn").addEventListener("click", () => {
            byId("walletAccessMessage").textContent = "Wallet scripts could not load. Refresh the page with Ctrl+F5.";
        });
    }
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
    byId("liveConnection").textContent = loading ? "LOADING" : liveStatus();
    byId("liveTelemetryConnection").textContent = liveStatus();
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
async function bootstrap() {
    await loadRuntimeConfig();
    initWallet();
    await Promise.allSettled([loadDashboard(), loadLiveRuntime()]);
    connectLiveStream();
}
void bootstrap();
