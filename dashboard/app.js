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
const API_URL = window.HRC_CHAIN_API_URL || (
    window.location?.protocol === "http:" || window.location?.protocol === "https:"
        ? "/api/chain"
        : serviceOrigin(3000)
);
const IOT_API_URL = window.HRC_IOT_API_URL || (
    window.location?.protocol === "http:" || window.location?.protocol === "https:"
        ? "/api/iot"
        : serviceOrigin(8001)
);
const DIRECT_CHAIN_API_URL = serviceOrigin(3000);
const USE_CHAIN_PROXY_FALLBACK = !window.HRC_CHAIN_API_URL && API_URL === '/api/chain';
let activeChainApiUrl = API_URL;
window.hrcRuntimeConfig = window.hrcRuntimeConfig || {};
let liveState = {
    latest: null, history: [], source: "LIVE", sourceLabel: "UNKNOWN",
    sourceMode: "unknown", connected: false, stream: null, retryMs: 1000,
    lastHeartbeat: 0, loading: false, reconnectTimer: null,
};
let livePollTimer = null;
const ROBOT_ID = "ROBOT-ARM-01";
const byId = id => document.getElementById(id);
const refreshBtn = byId("dashboardRefreshBtn");
const unlockBtn = byId("unlockRobotBtn");
let loading = false;
let unlocking = false;
let robotLocked = null;
let sidebarEvents = [];
let backendConnected = null;
// "demo" keeps the fixed-account picker; "wallet"/"siwe" make the header trigger
// connect a real wallet and sign the application login message instead.
let appAuthMode = "demo";
let transactionRows = [];
let transactionMessage = "Loading transactions...";
let transactionsLoading = false;
let rewardConfig = null;
let rewardSummary = null;
let rewardSessions = [];
let rewardMessage = "Loading reward data...";
let rewardsLoading = false;
// sessionToken authenticates the reward-session routes for a wallet-first login;
// token authenticates the application identity routes.
let appSession = { token: null, sessionToken: null, user: null, key: null, inFlightKey: null, error: null };
// Local, dependency-free view of the wallet-first state so the reward panel can
// be exercised in isolation.
function walletFirstActive() { return Boolean(appSession.sessionToken) && (appAuthMode === "wallet" || appAuthMode === "siwe"); }
let appAuthGeneration = 0;
let pendingBehavior = [];
let behaviorFlushTimer = null;
let behaviorFlushInFlight = false;
let userBehavior = [];
let customerRows = [];
let customerMessage = "Connect an admin wallet to view customers.";
let demoAccounts = [];
let demoSession = { token: null, user: null };
let demoWallet = null;
let demoSessions = [];
let demoTransactions = [];
let demoAdminOverview = null;
let demoMessage = 'Choose user_1 or user_2 to open a local wallet.';
let demoSessionRef = null;
let demoBusy = null;
let demoReadTask = null;
let demoReceipt = null;
let demoLoginMessage = '';

function randomId() {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
    return Date.now().toString(36) + '-' + Math.random().toString(36).slice(2);
}

async function readJsonResponse(response, serviceName) {
    try {
        return await response.json();
    } catch (cause) {
        const status = response?.status ? ` (HTTP ${response.status})` : '';
        const contentType = response?.headers?.get?.('content-type') || '';
        const responseKind = contentType.toLowerCase().includes('text/html') ? 'HTML' : 'a non-JSON response';
        const error = new Error(
            `${serviceName} returned ${responseKind}${status}. `
            + 'Restart the local demo with ./scripts/run_local_demo.sh and keep that terminal open.'
        );
        error.status = response?.status;
        error.code = 'INVALID_API_RESPONSE';
        error.cause = cause;
        throw error;
    }
}

async function fetchChainJson(path, options) {
    const fetchFrom = async baseUrl => {
        const response = await fetch(baseUrl + path, options);
        const data = await readJsonResponse(response, 'Blockchain API');
        return { response, data };
    };
    const requestedBaseUrl = activeChainApiUrl;
    const canUseDirectFallback = USE_CHAIN_PROXY_FALLBACK && requestedBaseUrl !== DIRECT_CHAIN_API_URL;
    try {
        const result = await fetchFrom(requestedBaseUrl);
        if (!canUseDirectFallback || result.data?.code !== 'CHAIN_API_UNAVAILABLE') return result;
    } catch (error) {
        if (!canUseDirectFallback || error.code !== 'INVALID_API_RESPONSE') throw error;
    }
    try {
        const result = await fetchFrom(DIRECT_CHAIN_API_URL);
        activeChainApiUrl = DIRECT_CHAIN_API_URL;
        return result;
    } catch (cause) {
        if (cause.code === 'INVALID_API_RESPONSE') throw cause;
        const error = new Error(
            `Blockchain API is unreachable through the dashboard proxy and ${DIRECT_CHAIN_API_URL}. `
            + 'Run ./scripts/run_local_demo.sh and keep that terminal open.'
        );
        error.code = 'CHAIN_API_UNAVAILABLE';
        error.cause = cause;
        throw error;
    }
}

async function demoRequest(path, { method = 'GET', body, token = demoSession.token } = {}) {
    const headers = { Accept: 'application/json' };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (token) headers.Authorization = 'Bearer ' + token;
    const { response, data } = await fetchChainJson(path, {
        method, headers, body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(20000)
    });
    if (!response.ok || data.success !== true) {
        const error = new Error(data.error || `HTTP ${response.status}`);
        error.status = response.status;
        error.code = data.code;
        throw error;
    }
    return data;
}

function formatDemoAmount(value) {
    if (value === undefined || value === null || value === '') return '-- HRC';
    const number = Number(value);
    return Number.isFinite(number) ? number.toFixed(2).replace(/\.00$/, '') + ' HRC' : '-- HRC';
}

function demoOutcomeName(code) {
    return ['VALID / NO VIOLATION', 'VIOLATION CONFIRMED', 'SENSOR FAULT', 'INCOMPLETE'][Number(code)] || '--';
}

function demoSessionStage(session) {
    if (session.settled) return 'SETTLED';
    return ({ RUNNING: 'MEASURING', OPENING_SENSOR: 'OPENING SENSOR', START_PENDING: 'STAKE PENDING', STOPPING_SENSOR: 'ENDING SESSION',
        SETTLEMENT_PENDING: 'SETTLEMENT PENDING', COMPENSATING: 'CLOSING SENSOR', START_FAILED: 'START FAILED', RECOVERY_REQUIRED: 'NEEDS RECOVERY', LEGACY_UNLINKED: 'AWAITING RECOVERY' })[session.stage] || 'STAKE ACTIVE';
}

function demoResultLabel(session) {
    if (!session.settled) return 'PENDING SETTLEMENT';
    return ['REWARDED', 'DAILY LIMIT', 'POOL EMPTY', 'PENALTY', 'NO REWARD'][Number(session.settlementStatusCode)] || 'NO REWARD';
}

function signedDemoAmount(session) {
    if (!session?.settled) return '--';
    if (Number(session.rewardPaid) > 0) return '+' + formatDemoAmount(session.rewardPaid);
    if (Number(session.penaltyCollected) > 0) return '-' + formatDemoAmount(session.penaltyCollected);
    return '0 HRC';
}

function demoTableMessage(table, message, columns) {
    const row = document.createElement('tr'); row.className = 'empty-row';
    const cell = document.createElement('td'); cell.colSpan = columns; cell.textContent = message;
    row.appendChild(cell); table.appendChild(row);
}

function renderDemoSessionRows(target, sessions, includeUser = false) {
    const table = byId(target);
    if (!table) return;
    table.replaceChildren();
    if (!sessions.length) { demoTableMessage(table, 'No sessions recorded yet.', includeUser ? 9 : 8); return; }
    for (const session of sessions) {
        const row = document.createElement('tr');
        const eventName = session.settled ? 'SessionSettled' : 'SessionStarted';
        const event = session.events?.find(item => item.name === eventName);
        const values = [session.sessionId, demoSessionStage(session), session.settled ? demoOutcomeName(session.outcomeCode) : '--',
            session.settled ? demoResultLabel(session) + (session.outcomeReason ? ' · ' + session.outcomeReason : '') : session.lastError || 'Waiting for End Session', session.settled ? formatDemoAmount(session.stakeReturned) : formatDemoAmount(session.stakedAmount) + ' locked',
            signedDemoAmount(session), event?.blockNumber ?? session.blockNumber ?? '--'];
        if (includeUser) values.unshift(session.userId);
        values.forEach(value => { const cell = document.createElement('td'); cell.textContent = String(value ?? '--'); row.appendChild(cell); });
        const cell = document.createElement('td');
        const label = document.createElement('span'); label.className = 'demo-event-label'; label.textContent = eventName;
        const hash = document.createElement('span'); hash.className = 'mono'; hash.textContent = event?.transactionHash || session.transactionHash || '--';
        cell.append(label, hash); row.appendChild(cell); table.appendChild(row);
    }
}

function renderDemoSessions() {
    text('demoSessionCount', demoSessions.length);
    renderDemoSessionRows('demoSessionsTableBody', demoSessions);
}

function demoTransactionEvents(transaction) {
    return transaction.events?.map(event => event.name).join(' + ') || transaction.event?.name || 'Transaction';
}

function demoLedgerDescription(transaction) {
    const settled = transaction.events?.find(event => event.name === 'SessionSettled');
    const start = transaction.events?.find(event => event.name === 'SessionStarted');
    if (settled || transaction.action === 'SETTLEMENT') {
        const returned = transaction.stakeReturned ?? settled?.stakeReturned ?? '0';
        const reward = transaction.rewardPaid ?? settled?.rewardPaid ?? '0';
        const penalty = transaction.penaltyCollected ?? settled?.penaltyCollected ?? '0';
        if (Number(penalty) > 0) return 'Penalty ' + formatDemoAmount(penalty) + ' from locked stake · no additional wallet charge';
        const status = transaction.settlementStatusCode ?? settled?.settlementStatusCode;
        return 'Stake returned +' + formatDemoAmount(returned) + ' · Reward +' + formatDemoAmount(reward)
            + (Number(status) === 1 ? ' · Daily reward limit reached' : Number(status) === 2 ? ' · Reward pool empty' : Number(status) === 4 ? ' · No reward' : '');
    }
    if (start || transaction.action === 'STAKE') return 'Available −2 HRC · Locked +2 HRC · Live session started';
    if (transaction.action === 'APPROVE' || transaction.events?.some(event => event.name === 'Approval')) return 'Allowance granted · Balance unchanged';
    return 'Token transfer';
}

function demoDelta(value) { return Number(value) > 0 ? '+' + formatDemoAmount(value) : formatDemoAmount(value ?? 0); }

function renderDemoTransactions() {
    text('demoTransactionCount', demoTransactions.length);
    const recent = byId('demoRecentActivity');
    if (recent) {
        recent.replaceChildren();
        if (!demoTransactions.length) { const empty = document.createElement('p'); empty.textContent = 'No wallet activity yet.'; recent.appendChild(empty); }
        demoTransactions.slice(0, 5).forEach(transaction => {
            const entry = document.createElement('div'); entry.className = 'demo-ledger-entry';
            const title = document.createElement('strong'); title.textContent = transaction.action || demoTransactionEvents(transaction);
            const amount = document.createElement('span'); amount.textContent = demoDelta(transaction.walletDelta);
            amount.className = Number(transaction.walletDelta) > 0 ? 'ledger-positive' : Number(transaction.walletDelta) < 0 ? 'ledger-negative' : '';
            const detail = document.createElement('small'); detail.textContent = demoLedgerDescription(transaction);
            const receipt = document.createElement('small'); receipt.className = 'mono';
            receipt.textContent = 'Block ' + transaction.blockNumber + ' · ' + transaction.transactionHash + (transaction.sessionId ? ' · ' + transaction.sessionId : '');
            entry.append(title, amount, detail, receipt); recent.appendChild(entry);
        });
    }
    const table = byId('demoTransactionsTableBody');
    if (!table) return;
    table.replaceChildren();
    if (!demoTransactions.length) { demoTableMessage(table, 'No transactions recorded yet.', 5); return; }
    for (const transaction of demoTransactions) {
        const row = document.createElement('tr');
        [demoTransactionEvents(transaction) + ' · ' + demoLedgerDescription(transaction), demoDelta(transaction.walletDelta), demoDelta(transaction.lockedDelta), transaction.blockNumber, transaction.transactionHash].forEach((value, index) => {
            const cell = document.createElement('td'); cell.textContent = String(value ?? '--');
            if (index === 4) cell.className = 'mono';
            row.appendChild(cell);
        });
        table.appendChild(row);
    }
}

function renderDemoAdminOverview() {
    const users = demoAdminOverview?.users || [];
    const sessions = users.flatMap(user => (user.sessions || []).map(session => ({ ...session, userId: user.userId })))
        .sort((a, b) => Number(b.startedAt) - Number(a.startedAt));
    text('demoAdminMessage', demoAdminOverview ? 'Balances and session results read from the local chain. ' + demoMessage : demoMessage);
    text('demoAdminBalance', demoAdminOverview ? formatDemoAmount(users.reduce((sum, user) => sum + Number(user.wallet?.balance || 0), 0)) : '-- HRC');
    text('demoAdminSessionCount', demoAdminOverview ? sessions.filter(session => !session.settled).length + ' / ' + sessions.filter(session => session.settled).length : '-- / --');
    text('demoAdminPayout', demoAdminOverview ? '+' + formatDemoAmount(sessions.reduce((sum, session) => sum + Number(session.rewardPaid || 0), 0)) + ' / -' + formatDemoAmount(sessions.reduce((sum, session) => sum + Number(session.penaltyCollected || 0), 0)) : '-- / -- HRC');
    text('demoAdminHistoryCount', sessions.length);
    renderDemoSessionRows('demoAdminSessionsTableBody', sessions, true);
    const table = byId('demoAdminTableBody');
    if (!table) return;
    table.replaceChildren();
    if (!users.length) { demoTableMessage(table, demoMessage, 6); return; }
    users.forEach(user => {
        const row = document.createElement('tr');
        [user.userId, user.wallet?.address || '--', formatDemoAmount(user.wallet?.balance), formatDemoAmount(user.wallet?.allowance),
            user.sessions?.length || 0, user.sessions?.[0] ? demoResultLabel(user.sessions[0]) : 'No session'].forEach(value => {
            const cell = document.createElement('td'); cell.textContent = String(value ?? '--'); row.appendChild(cell);
        });
        table.appendChild(row);
    });
}

function updateDemoAccountPicker() {
    const busy = Boolean(demoBusy);
    const select = byId('demoAccountSelect');
    const button = byId('demoLoginBtn');
    if (select) select.disabled = busy;
    if (button) {
        button.disabled = busy || !select?.value;
        button.textContent = demoBusy === 'login' ? 'Opening...' : 'Open account';
    }
    text('demoAccountMessage', demoLoginMessage);
}

function renderDemoWallet() {
    const user = demoSession.user;
    const customer = user?.role === 'customer';
    const admin = user?.role === 'admin';
    const busy = Boolean(demoBusy);
    const walletFirst = appAuthMode === "wallet" || appAuthMode === "siwe";
    if (!walletFirst) text('demoAccountButtonLabel', user?.userId || 'Choose account');
    text('demoAccountStatus', user
        ? `${user.userId} · ${admin ? 'read-only report' : user.walletFirst ? 'your wallet · you sign every transaction' : 'local demo wallet'}`
        : walletFirstActive() ? 'Connect your wallet to start a session.' : 'Admin views both users. User accounts can approve and start sessions.');
    text('walletUserStatus', user ? `${user.role.toUpperCase()} · ${user.userId}` : 'Choose an account');
    text('connectedWalletAddress', user?.walletAddress || (admin ? 'Admin · read-only' : (walletFirst ? (window.hrcWallet?.state.account || 'Not connected') : 'No demo account')));
    text('walletRole', admin ? 'READ ONLY ADMIN' : customer ? (user?.walletFirst ? 'WALLET USER' : 'DEMO USER') : 'READ ONLY');
    text('demoWalletRole', user?.userId.toUpperCase() || 'CHOOSE ACCOUNT');
    text('demoWalletAddress', demoWallet?.address || user?.walletAddress || '--');
    text('demoWalletBalance', demoWallet ? formatDemoAmount(demoWallet.balance) : '-- HRC');
    text('demoWalletLocked', demoWallet ? formatDemoAmount(demoWallet.lockedBalance || 0) : '-- HRC');
    text('demoWalletAllowance', demoWallet ? formatDemoAmount(demoWallet.allowance) : '-- HRC');
    text('demoWalletChain', demoWallet ? 'CHAIN ' + demoWallet.chainId : 'CHAIN --');
    text('rewardBalance', demoWallet ? formatDemoAmount(demoWallet.balance) : '-- HRC');
    text('rewardSettlementStatus', demoSessions[0] ? demoResultLabel(demoSessions[0]) : 'No session');
    text('demoWalletMessage', demoMessage);
    for (const [id, visible] of [['demoWalletPanel', customer], ['demoAdminReport', admin], ['demoAccountEmpty', !user]]) {
        const element = byId(id); if (element) element.hidden = !visible;
    }
    const trigger = byId('demoAccountTrigger');
    if (trigger) trigger.disabled = busy || (walletFirst && Boolean(window.hrcWallet?.state.connecting || (appSession.inFlightKey && !appSession.key)));
    const approve = byId('demoApproveBtn');
    const start = byId('demoStartBtn');
    const ready = demoWallet && Number(demoWallet.allowance) >= Number(demoWallet.requiredAllowance);
    if (approve) {
        approve.hidden = !customer;
        approve.disabled = busy || !customer || !demoWallet || ready;
        approve.textContent = demoBusy === 'approve' ? 'Approving...' : ready ? 'Approved 2 HRC' : 'Approve 2 HRC';
    }
    if (start) {
        start.hidden = !customer;
        start.disabled = busy || !customer || !demoWallet || (!demoWallet.canStart && !demoSessionRef);
        start.textContent = demoBusy === 'start' ? 'Starting...' : demoSessionRef ? 'Retry Start Session' : 'Start Session';
    }
    const active = demoSessions.find(session => !session.settled && !['START_FAILED', 'SETTLED'].includes(session.stage));
    const end = byId('demoEndBtn');
    if (end) {
        end.hidden = !customer || !active;
        end.disabled = busy || !active || !['RUNNING', 'STOPPING_SENSOR', 'SETTLEMENT_PENDING'].includes(active.stage);
        end.textContent = demoBusy === 'end' ? 'Ending...' : active?.stage === 'SETTLEMENT_PENDING' ? 'Retry Settlement' : 'End Session';
    }
    const sensor = demoWallet?.device;
    text('demoSensorStatus', sensor ? sensor.ready ? `${sensor.deviceId} · ESP32 LIVE · Valid session: at least 30 seconds / 20 samples${sensor.busySessionId && sensor.busySessionId !== active?.sessionId ? ' · Device busy' : ''}` : 'Sensor unavailable: ' + sensor.reason : 'Checking live sensor...');
    const last = active || demoSessions[0];
    text('demoSessionState', last ? last.settled
        ? `${demoResultLabel(last)} · Stake returned +${formatDemoAmount(last.stakeReturned)} · Reward +${formatDemoAmount(last.rewardPaid)} · Penalty ${formatDemoAmount(last.penaltyCollected)}${last.outcomeReason ? ' · ' + last.outcomeReason : ''}`
        : `${demoSessionStage(last)} · ${formatDemoAmount(last.stakedAmount)} locked · ${Math.floor(last.iotSession?.durationS || last.durationS || 0)} seconds / ${last.sampleCount || 0} samples${last.lastError ? ' · ' + last.lastError : last.stage === 'RUNNING' ? ' · Press End Session to calculate the result' : ' · Recovering the same session'}`
        : demoWallet && !ready ? 'Step 1: approve 2 HRC. Approval does not change your balance.' : 'Ready to open a live sensor session and lock 2 HRC.');
    const latest = [demoReceipt, demoTransactions[0]].filter(Boolean).sort((a, b) => Number(b.blockNumber || 0) - Number(a.blockNumber || 0))[0];
    text('demoTransactionStatus', latest ? `${demoTransactionEvents(latest)} · block ${latest.blockNumber ?? '--'} · tx ${latest.transactionHash || '--'}` : 'No transaction recorded for this account.');
    updateDemoAccountPicker();
    renderDemoSessions();
    renderDemoTransactions();
    renderDemoAdminOverview();
}

async function loadDemoAccounts() {
    try {
        const data = await demoRequest('/api/demo/accounts', { token: null });
        const accountIds = ['admin', 'user_1', 'user_2'];
        demoAccounts = accountIds.map(id => (Array.isArray(data.accounts) ? data.accounts : []).find(account => account.userId === id)).filter(Boolean);
        const select = byId('demoAccountSelect');
        if (select) {
            select.replaceChildren();
            demoAccounts.forEach(account => {
                const option = document.createElement('option'); option.value = account.userId;
                option.textContent = account.userId; select.appendChild(option);
            });
            select.value = demoSession.user?.userId || 'admin';
        }
    } catch (error) { demoLoginMessage = 'Accounts unavailable: ' + error.message; }
    updateDemoAccountPicker();
}

function clearDemoSession() {
    demoSession = { token: null, user: null };
    demoWallet = null; demoSessions = []; demoTransactions = []; demoAdminOverview = null; demoReceipt = null; demoSessionRef = null;
    try { sessionStorage.removeItem('hrc-demo-session'); } catch { /* memory-only fallback */ }
}

function pendingDemoSession(userId) {
    try { return sessionStorage.getItem('hrc-demo-pending-' + userId) || null; } catch { return null; }
}

async function loginDemoAccount() {
    if (demoBusy) return;
    const userId = byId('demoAccountSelect')?.value;
    if (!['admin', 'user_1', 'user_2'].includes(userId)) { demoLoginMessage = 'Choose admin, user_1 or user_2.'; updateDemoAccountPicker(); return; }
    demoBusy = 'login'; demoLoginMessage = 'Opening ' + userId + '...'; renderDemoWallet();
    try {
        const data = await demoRequest('/api/demo/login', { method: 'POST', body: { userId }, token: null });
        clearDemoSession();
        demoSession = { token: data.token, user: data.user };
        demoSessionRef = pendingDemoSession(userId);
        try { sessionStorage.setItem('hrc-demo-session', JSON.stringify(demoSession)); } catch { /* memory-only fallback */ }
        demoMessage = userId === 'admin' ? 'Admin account is read-only.' : 'Wallet loaded. Approve the stake, then start a session.';
        renderDemoWallet();
        demoLoginMessage = ''; closeDemoAccountModal();
        await loadDemoAccountData();
    } catch (error) { demoLoginMessage = 'Unable to open account: ' + error.message; }
    finally { demoBusy = null; renderDemoWallet(); }
}

async function loadDemoAccountData({ fresh = false } = {}) {
    const session = demoSession;
    if (!session.token || !session.user) { renderDemoWallet(); return false; }
    if (demoReadTask?.session === session) {
        if (!fresh) return demoReadTask.promise;
        await demoReadTask.promise;
        if (session !== demoSession) return false;
    }
    const task = { session, promise: null };
    demoReadTask = task;
    task.promise = (async () => {
        try {
            const token = session.token;
            if (session.user.role === 'admin') {
                const data = await demoRequest('/api/demo/admin/overview', { token });
                if (session !== demoSession) return false;
                demoAdminOverview = data; demoWallet = null; demoSessions = []; demoTransactions = [];
            } else {
                const [wallet, sessions, transactions] = await Promise.all([
                    demoRequest('/api/me/wallet', { token }), demoRequest('/api/me/sessions', { token }), demoRequest('/api/me/wallet/transactions', { token })
                ]);
                if (session !== demoSession) return false;
                demoWallet = wallet; demoSessions = Array.isArray(sessions.sessions) ? sessions.sessions : [];
                demoTransactions = Array.isArray(transactions.transactions) ? transactions.transactions : [];
                const recoveredStart = demoSessionRef && demoSessions.find(row => row.sessionRef === demoSessionRef
                    && ['RUNNING', 'SETTLED', 'START_FAILED'].includes(row.stage));
                if (recoveredStart) {
                    demoSessionRef = null;
                    try { sessionStorage.removeItem('hrc-demo-pending-' + session.user.userId); } catch { /* in-memory reference is already cleared */ }
                }
                demoAdminOverview = null;
            }
            if (!demoBusy) {
                demoMessage = session.user.role === 'admin' ? 'Admin account is read-only.'
                    : demoSessions.some(row => row.stage === 'RUNNING') ? 'The sensor is measuring. End Session calculates the result.'
                    : 'Wallet loaded. Approve 2 HRC, start a sensor session, then end it to settle.';
            }
            renderDemoWallet(); return true;
        } catch (error) {
            if (session !== demoSession) return false;
            demoMessage = error.status === 401 ? 'Your demo login expired. Choose your account again.' : 'Unable to refresh account: ' + error.message;
            if (error.status === 401) clearDemoSession();
            renderDemoWallet(); return false;
        }
    })();
    try { return await task.promise; } finally { if (demoReadTask === task) demoReadTask = null; }
}

async function approveDemoWallet() {
    if (demoBusy || !demoSession.token || demoSession.user?.role !== 'customer' || !demoWallet || Number(demoWallet.allowance) >= Number(demoWallet.requiredAllowance)) return;
    const session = demoSession;
    demoBusy = 'approve'; demoMessage = 'Submitting HRC approval...'; renderDemoWallet();
    try {
        const result = (appSession.sessionToken && (appAuthMode === 'wallet' || appAuthMode === 'siwe'))
            ? await sendWalletPrepared(await demoRequest('/api/me/wallet/prepare/approve', { token: session.token }))
            : await demoRequest('/api/me/wallet/approve', { method: 'POST', token: session.token });
        if (session !== demoSession) return;
        if (result.transactionHash) demoReceipt = result;
        demoMessage = 'Approved ' + formatDemoAmount(demoWallet.requiredAllowance) + ' HRC allowance. Your balance is unchanged. Start opens the sensor session and locks ' + formatDemoAmount(demoWallet.stakeAmount) + ' HRC.';
        await loadDemoAccountData({ fresh: true });
    } catch (error) { if (session === demoSession) demoMessage = 'Approve failed: ' + error.message; }
    finally { demoBusy = null; renderDemoWallet(); }
}

// The backend prepares the calldata; the user's own wallet signs and broadcasts it.
async function sendWalletPrepared(prepared) {
    if (!window.hrcWallet?.sendPrepared) throw new Error("Connect a wallet that can sign transactions.");
    const hash = await window.hrcWallet.sendPrepared(prepared);
    return { ...prepared, transactionHash: hash, walletSigned: true };
}

async function startDemoSession() {
    if (demoBusy || !demoSession.token || demoSession.user?.role !== 'customer' || !demoWallet || (!demoWallet.canStart && !demoSessionRef)) return;
    const session = demoSession;
    if (!demoSessionRef) demoSessionRef = 'demo-' + randomId();
    try { sessionStorage.setItem('hrc-demo-pending-' + session.user.userId, demoSessionRef); } catch { /* same-page retries still keep their session id */ }
    demoBusy = 'start'; demoMessage = 'Submitting the 2 HRC session stake...'; renderDemoWallet();
    try {
        const prepared = await demoRequest('/api/me/sessions/start', { method: 'POST', token: session.token, body: { sessionRef: demoSessionRef } });
        const signed = (appSession.sessionToken && (appAuthMode === 'wallet' || appAuthMode === 'siwe')) ? await sendWalletPrepared(prepared) : prepared;
        const result = prepared.mode === 'wallet-first'
            ? await demoRequest('/api/me/sessions/' + encodeURIComponent(prepared.sessionId) + '/confirm-start', {
                method: 'POST', token: session.token, body: { transactionHash: signed.transactionHash }
            })
            : signed;
        if (session !== demoSession) return;
        demoReceipt = result;
        demoMessage = result.pending ? 'Opening the same sensor session. Transaction recovery is in progress.' : result.stage === 'START_FAILED' ? 'Start failed without moving HRC: ' + (result.lastError || 'Sensor session closed') : result.settled ? 'This session has already been settled.' : '2 HRC locked. The sensor is measuring. Press End Session to settle from its samples.';
        if (!result.pending) {
            demoSessionRef = null;
            try { sessionStorage.removeItem('hrc-demo-pending-' + session.user.userId); } catch { /* ignore storage policy */ }
        }
        await loadDemoAccountData({ fresh: true });
    } catch (error) { if (session === demoSession) demoMessage = 'Start failed: ' + error.message + ' Retry uses the same session; it will not lock a second stake.'; }
    finally { demoBusy = null; renderDemoWallet(); }
}

async function endDemoSession() {
    const active = demoSessions.find(session => !session.settled && !['START_FAILED', 'SETTLED'].includes(session.stage));
    if (demoBusy || demoSession.user?.role !== 'customer' || !active || !['RUNNING', 'STOPPING_SENSOR', 'SETTLEMENT_PENDING'].includes(active.stage)) return;
    const session = demoSession;
    demoBusy = 'end'; demoMessage = 'Closing the sensor session and calculating its result...'; renderDemoWallet();
    try {
        const result = await demoRequest('/api/me/sessions/' + encodeURIComponent(active.sessionId) + '/end', { method: 'POST', token: session.token, body: {} });
        if (session !== demoSession) return;
        demoMessage = result.operatorOnly
            ? result.description
            : result.pending ? 'Settlement is pending. The backend will retry with the same evidence.' : 'Session settled. Stake return, reward and penalty are shown in the activity log.';
        await loadDemoAccountData({ fresh: true });
    } catch (error) { if (session === demoSession) demoMessage = 'End failed: ' + error.message + ' Retry keeps the same session.'; }
    finally { demoBusy = null; renderDemoWallet(); }
}

function setDemoAccountModal(open) {
    const modal = byId('demoAccountModal');
    const trigger = byId('demoAccountTrigger');
    if (!modal) return;
    const wasOpen = !modal.classList.contains('hidden');
    modal.classList.toggle('hidden', !open);
    document.body.classList.toggle('demo-picker-open', open);
    if (trigger) trigger.setAttribute('aria-expanded', String(open));
    if (open) { const select = byId('demoAccountSelect'); if (select) select.value = demoSession.user?.userId || 'admin'; updateDemoAccountPicker(); window.setTimeout(() => byId('demoAccountSelect')?.focus(), 0); }
    else if (wasOpen) trigger?.focus();
}

function openDemoAccountModal() { if (!demoBusy) { demoLoginMessage = ''; setDemoAccountModal(true); } }
function closeDemoAccountModal() { setDemoAccountModal(false); }

function restoreDemoSession() {
    try {
        const stored = JSON.parse(sessionStorage.getItem('hrc-demo-session') || 'null');
        if (stored?.token && ['admin', 'user_1', 'user_2'].includes(stored.user?.userId)) {
            demoSession = stored; demoSessionRef = pendingDemoSession(stored.user.userId);
        }
    } catch { clearDemoSession(); }
}

async function appRequest(path, { method = "GET", body, token } = {}) {
    const headers = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (token) headers.Authorization = "Bearer " + token;
    const { response, data } = await fetchChainJson(path, {
        method, headers, body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(10000)
    });
    if (!response.ok || data.success !== true) {
        const error = new Error(data.error || `HTTP ${response.status}`);
        error.status = response.status;
        error.code = data.code;
        throw error;
    }
    return data;
}

function renderAppIdentity() {
    renderWalletConnection();
    const user = appSession.user;
    text("walletUserStatus", user ? `${user.role.toUpperCase()} · ${user.userId}` : (appSession.error || "Application login pending"));
    text("userPortalId", user?.userId || "Application login pending");
    text("userPortalWallet", user?.walletAddress || "Not connected");
    text("userPortalRole", user?.role?.toUpperCase() || "NOT AUTHENTICATED");
    text("userPortalStatus", user?.status?.toUpperCase() || "--");
    text("userPortalCreated", user?.createdAt ? new Date(user.createdAt).toLocaleString() : "--");
    text("userPortalLastSeen", user?.lastSeenAt ? new Date(user.lastSeenAt).toLocaleString() : "--");
    text("userPortalBehaviorCount", user ? String(user.behaviorCount || 0) : "--");
    text("userPortalAccessHint", user?.role === "admin" ? "Admin account is active." : user ? "Customer account is active." : "Connect Wallet to register or sign in.");
    text("userPortalAccessDetail", user?.role === "admin"
        ? "Customer Directory is available in the Account menu."
        : user
            ? "Your behavior is private to this wallet; admin access is configured by HRC_APP_ADMIN_WALLETS."
            : "A signed MetaMask message creates the application user and starts behavior tracking. Customer Directory appears after an allowlisted admin wallet signs in.");
    const customerLink = document.querySelector('[data-view="customers"]');
    if (customerLink) customerLink.hidden = user?.role !== "admin";
    const customerView = byId("view-customers");
    if (customerView && user?.role !== "admin" && !customerView.hidden) window.location.hash = "user";
}

function saveAppSession() {
    try { sessionStorage.setItem("hrc-app-session", JSON.stringify({ token: appSession.token, sessionToken: appSession.sessionToken, user: appSession.user })); }
    catch { /* Private browsing can disable sessionStorage; memory auth still works. */ }
}

function clearAppSession() {
    appAuthGeneration++;
    appSession = { token: null, sessionToken: null, user: null, key: null, inFlightKey: null, error: null };
    userBehavior = [];
    customerRows = [];
    customerMessage = "Connect an admin wallet to view customers.";
    try { sessionStorage.removeItem("hrc-app-session"); } catch { /* ignore storage policy */ }
    renderAppIdentity();
    renderUserPortal();
    renderCustomers();
}

function restoreAppSession() {
    try {
        const stored = JSON.parse(sessionStorage.getItem("hrc-app-session") || "null");
        if (stored?.token && stored?.user) appSession = { ...appSession, token: stored.token, sessionToken: stored.sessionToken || null, user: stored.user };
    } catch { clearAppSession(); }
}

function currentWalletAddress() {
    return window.hrcWallet?.state.account?.toLowerCase() || null;
}

function scheduleBehaviorRetry() {
    if (behaviorFlushTimer || !appSession.token || !pendingBehavior.length) return;
    behaviorFlushTimer = window.setTimeout(() => {
        behaviorFlushTimer = null;
        void flushBehavior();
    }, 1500);
}

async function flushBehavior() {
    if (behaviorFlushInFlight || !appSession.token || !pendingBehavior.length) return;
    const wallet = currentWalletAddress();
    const userWallet = appSession.user?.walletAddress?.toLowerCase();
    if (!wallet || wallet !== userWallet) return;
    behaviorFlushInFlight = true;
    // Events are tagged when queued. Drop events generated by another wallet
    // rather than attributing them to the currently signed-in account.
    const eligible = pendingBehavior.filter(event => event.walletAddress === userWallet);
    pendingBehavior = eligible;
    const queue = pendingBehavior.splice(0, 30);
    try {
        for (let index = 0; index < queue.length; index++) {
            const event = queue[index];
            try {
                await appRequest("/api/behavior", {
                    method: "POST", token: appSession.token,
                    body: { ...event, walletAddress: undefined }
                });
            } catch (error) {
                pendingBehavior.unshift(...queue.slice(index));
                if (error.status === 401) clearAppSession();
                break;
            }
        }
        if (appSession.token) void loadUserPortal();
    } finally {
        behaviorFlushInFlight = false;
        if (pendingBehavior.length && appSession.token) scheduleBehaviorRetry();
    }
}

async function trackBehavior(eventType, metadata = {}) {
    const walletAddress = currentWalletAddress();
    const event = { eventType, page: window.location.hash.slice(1) || "dashboard", metadata, idempotencyKey: randomId(), walletAddress };
    if (!appSession.token) {
        // There is no safe owner for anonymous events. Once a wallet is present,
        // retain them briefly and bind them to that exact wallet.
        if (walletAddress && pendingBehavior.length < 50) pendingBehavior.push(event);
        return;
    }
    try {
        await appRequest("/api/behavior", {
            method: "POST", token: appSession.token,
            body: { ...event, walletAddress: undefined }
        });
        void loadUserPortal();
    } catch (error) {
        if (error.status === 401) clearAppSession();
        else if (walletAddress === currentWalletAddress() && pendingBehavior.length < 50) {
            pendingBehavior.push(event);
            scheduleBehaviorRetry();
        }
    }
}

function authFlowIsCurrent(run, key) {
    const state = window.hrcWallet?.state;
    return run === appAuthGeneration && state?.account?.toLowerCase() === key.split(":")[0]
        && state.networkMatches === true && String(state.chainId) === key.split(":")[1];
}

async function syncWalletApplication(state) {
    if (!state.account || !state.networkMatches) {
        if (appSession.user || appSession.token || appSession.inFlightKey) clearAppSession();
        return;
    }
    const key = state.account.toLowerCase() + ":" + state.chainId;
    if (appSession.key && appSession.key !== key) clearAppSession();
    if (appSession.key === key && appSession.user) return;
    if (appSession.inFlightKey === key) return;
    const run = ++appAuthGeneration;
    appSession.inFlightKey = key;
    appSession.error = "Sign the application login message in MetaMask.";
    renderAppIdentity();
    try {
        if (appSession.token) {
            try {
                const current = await appRequest("/api/me", { token: appSession.token });
                if (!authFlowIsCurrent(run, key)) return;
                if (current.user.walletAddress.toLowerCase() === state.account.toLowerCase()) {
                    appSession.user = current.user;
                    appSession.key = key;
                    appSession.error = null;
                    saveAppSession();
                    renderAppIdentity();
                    await flushBehavior();
                    return;
                }
            } catch (error) {
                if (!authFlowIsCurrent(run, key)) return;
                if (error.status === 401) {
                    appSession = { token: null, sessionToken: null, user: null, key: null, inFlightKey: key, error: "Sign the application login message in MetaMask." };
                    renderAppIdentity();
                } else throw error;
            }
        }
        const challenge = await appRequest("/api/auth/challenge", { method: "POST", body: {
            walletAddress: state.account, chainId: state.chainId
        }});
        if (!authFlowIsCurrent(run, key)) return;
        const signature = await window.hrcWallet.signMessage(challenge.message);
        if (!authFlowIsCurrent(run, key)) return;
        const login = await appRequest("/api/auth/wallet", { method: "POST", body: {
            walletAddress: state.account, chainId: state.chainId,
            message: challenge.message, signature
        }});
        if (!authFlowIsCurrent(run, key)) return;
        appSession = { token: login.token, sessionToken: login.sessionToken || null, user: login.user, key, inFlightKey: key, error: null };
        saveAppSession();
        renderAppIdentity();
        if ((appSession.sessionToken && (appAuthMode === 'wallet' || appAuthMode === 'siwe'))) {
            // Wallet-first: the same signed login also authorizes the reward-session
            // routes, so the panel reads the wallet's own history.
            demoSession = {
                token: appSession.sessionToken,
                user: { userId: login.user.userId, role: "customer", walletAddress: login.user.walletAddress, walletFirst: true }
            };
            demoSessionRef = pendingDemoSession(login.user.userId);
        }
        await trackBehavior("wallet_connected", { chainId: state.chainId });
        await flushBehavior();
        if (walletFirstActive()) await loadDemoAccountData({ fresh: true });
        await loadRewards();
    } catch (error) {
        if (!authFlowIsCurrent(run, key)) return;
        const message = error.code === 4001 ? "Application login was cancelled in MetaMask." : "Application login failed: " + error.message;
        clearAppSession();
        appSession.error = message;
        appSession.inFlightKey = null;
        renderAppIdentity();
        renderUserPortal();
    } finally {
        if (run === appAuthGeneration && appSession.inFlightKey === key) {
            appSession.inFlightKey = null;
            renderAppIdentity();
        }
    }
}

async function loadUserPortal() {
    if (!appSession.token) { userBehavior = []; renderUserPortal(); return; }
    const token = appSession.token;
    try {
        const [me, history] = await Promise.all([
            appRequest("/api/me", { token }), appRequest("/api/me/behavior?limit=50", { token })
        ]);
        if (token !== appSession.token) return;
        if (currentWalletAddress() && me.user.walletAddress.toLowerCase() !== currentWalletAddress()) return;
        appSession.user = me.user;
        userBehavior = Array.isArray(history.events) ? history.events : [];
        saveAppSession();
        renderAppIdentity();
        renderUserPortal();
    } catch (error) {
        if (error.status === 401 && token === appSession.token) clearAppSession();
    }
}

function renderUserPortal() {
    const table = byId("userBehaviorTableBody");
    if (!table) return;
    table.replaceChildren();
    if (!appSession.user) {
        const row = document.createElement("tr");
        row.className = "empty-row";
        const cell = document.createElement("td");
        cell.colSpan = 4;
        cell.textContent = "Connect Wallet ở góc phải để đăng ký/đăng nhập và tải hoạt động của ví.";
        row.appendChild(cell);
        table.appendChild(row);
        return;
    }
    if (!userBehavior.length) {
        const row = document.createElement("tr");
        row.className = "empty-row";
        const cell = document.createElement("td");
        cell.colSpan = 4;
        cell.textContent = "No activity recorded yet.";
        row.appendChild(cell);
        table.appendChild(row);
        return;
    }
    userBehavior.forEach(event => {
        const row = document.createElement("tr");
        [new Date(event.recordedAt).toLocaleString(), event.eventType, event.page || "--", event.eventId]
            .forEach(value => { const cell = document.createElement("td"); cell.textContent = String(value ?? "--"); row.appendChild(cell); });
        table.appendChild(row);
    });
}

async function loadCustomers() {
    if (!appSession.token || appSession.user?.role !== "admin") {
        customerRows = [];
        customerMessage = "Connect an admin wallet to view customers.";
        renderCustomers();
        return;
    }
    customerMessage = "Loading customer activity...";
    renderCustomers();
    try {
        const data = await appRequest("/api/customers?limit=100", { token: appSession.token });
        customerRows = Array.isArray(data.customers) ? data.customers : [];
        customerMessage = "Updated: " + new Date().toLocaleString();
    } catch (error) {
        customerRows = [];
        customerMessage = "Unable to load customers: " + error.message;
        if (error.status === 401) clearAppSession();
    }
    renderCustomers();
}

function renderCustomers() {
    const message = byId("customersMessage");
    if (message) message.textContent = customerMessage;
    const table = byId("customersTableBody");
    if (!table) return;
    table.replaceChildren();
    if (!customerRows.length) {
        const row = document.createElement("tr"); row.className = "empty-row";
        const cell = document.createElement("td"); cell.colSpan = 6; cell.textContent = customerMessage; row.appendChild(cell); table.appendChild(row); return;
    }
    customerRows.forEach(customer => {
        const row = document.createElement("tr");
        [customer.walletAddress, customer.role, customer.status, customer.behaviorCount, new Date(customer.lastSeenAt).toLocaleString(), customer.userId]
            .forEach(value => { const cell = document.createElement("td"); cell.textContent = String(value ?? "--"); row.appendChild(cell); });
        table.appendChild(row);
    });
}
function lockCandidate() {
    return sidebarEvents.filter(event => event.deviceId === ROBOT_ID &&
        [2, 3].includes(Number(event.riskLevel))).sort((a, b) =>
        Number(b.blockNumber) - Number(a.blockNumber) || Number(b.timestamp) - Number(a.timestamp))[0] || null;
}
function updateAdminControls() {
    const owner = window.hrcWallet?.isOwner === true;
    if (unlockBtn) unlockBtn.disabled = !owner || robotLocked !== true || unlocking || loading;
    const lockBtn = byId("lockRobotBtn");
    if (lockBtn) lockBtn.disabled = !owner || robotLocked !== false || unlocking || loading || !lockCandidate();
    const lockMessage = byId("lockEventMessage");
    if (lockMessage) lockMessage.textContent = owner && lockCandidate()
        ? "Lock reference event: " + lockCandidate().eventId + " (risk " + lockCandidate().riskLevel + ")"
        : "Admin dashboard is read-only; safety actions are handled by the backend operator.";
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
    const { response, data } = await fetchChainJson(path, {
        ...options, signal: AbortSignal.timeout(["/api/transactions", "/api/rewards/sessions"].includes(path) ? 90000 : 20000)
    });
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
    const data = await readJsonResponse(response, 'IoT API');
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
        appAuthMode = config?.app?.authMode || "demo";
        applyAuthMode();
        renderRuntimeConfig(config);
        return config;
    } catch (error) {
        window.hrcRuntimeConfig = {};
        appAuthMode = "demo";
        applyAuthMode();
        renderRuntimeConfig({ network: "unavailable" });
        text("dashboardMessage", "Blockchain config unavailable: " + error.message);
        return null;
    }
}

function walletFirstMode() { return appAuthMode === "wallet" || appAuthMode === "siwe"; }

// Adapt the header entry point to the configured auth mode. The demo picker stays
// available in demo mode and is removed from the flow when the deployment is
// wallet-first.
function applyAuthMode() {
    const walletFirst = walletFirstMode();
    text("accountTriggerLabel", walletFirst ? (appAuthMode === "siwe" ? "SIWE LOGIN" : "WALLET") : "DEMO ACCOUNT");
    document.querySelectorAll("[data-demo-account-open]").forEach(button => {
        button.hidden = walletFirst;
        button.disabled = walletFirst;
    });
    const modal = byId("demoAccountModal");
    if (walletFirst && modal && !modal.classList.contains("hidden")) closeDemoAccountModal();
    if (walletFirst) {
        text("demoAccountButtonLabel", "Connect wallet");
        text("walletAccessMessage", appAuthMode === "siwe"
            ? "Connect a wallet and sign the SIWE message to start a session. The backend operator settles rewards."
            : "Connect a wallet and sign the login message to start a session. The backend operator settles rewards.");
        text("demoAccountEmptyHint", appAuthMode === "siwe"
            ? "Connect MetaMask and sign in. Then approve your HRC stake and start a sensor session."
            : "Connect MetaMask and sign in. Then approve your HRC stake and start a sensor session.");
    }
    // The main panel owns the visible CTA in wallet-first mode; the demo picker is hidden.
    const connectBtn = byId("walletFirstConnectBtn");
    if (connectBtn) connectBtn.hidden = !walletFirst;
    const status = byId("walletFirstStatus");
    if (status && !walletFirst) status.hidden = true;
}

// Keep wallet errors beside the connect action in the account panel.
function setWalletFirstStatus(message) {
    text("walletAccessMessage", message);
    const node = byId("walletFirstStatus");
    if (node) {
        node.hidden = false;
        node.textContent = String(message ?? "");
    }
}
function renderWalletConnection() {
    if (!walletFirstMode()) return;
    const state = window.hrcWallet?.state || {};
    const authenticated = walletFirstActive() && state.networkMatches
        && appSession.key === state.account?.toLowerCase() + ":" + state.chainId;
    const signing = Boolean(appSession.inFlightKey && !appSession.key);
    const short = state.account ? state.account.slice(0, 6) + "..." + state.account.slice(-4) : null;
    text("demoAccountButtonLabel", state.connecting ? "Connecting..." : short || "Connect wallet");
    text("accountTriggerLabel", authenticated ? "SIGNED IN" : "WALLET LOGIN");
    const message = state.connecting ? "Open MetaMask to connect or switch network."
        : !state.account || !state.networkMatches ? state.message
        : appSession.error || (authenticated ? "Signed in. Your HRC wallet and sessions are ready." : "Wallet connected. Sign the login message in MetaMask.");
    setWalletFirstStatus(message || "Connect MetaMask to sign in.");
    const button = byId("walletFirstConnectBtn");
    if (button) {
        button.hidden = Boolean(authenticated);
        button.disabled = Boolean(state.connecting || signing);
        button.textContent = state.connecting ? "Connecting..." : signing ? "Sign in MetaMask..."
            : state.account && !state.networkMatches ? "Switch network"
            : state.account ? "Retry sign in" : "Connect wallet";
    }
    const trigger = byId("demoAccountTrigger");
    if (trigger) trigger.disabled = Boolean(demoBusy || state.connecting || signing);
}
async function connectWalletFirst() {
    if (!window.hrcWallet || typeof window.hrcWallet.connect !== "function") {
        setWalletFirstStatus("No EVM wallet detected. Enable MetaMask and reload this page to sign in.");
        return;
    }
    try { await window.hrcWallet.connect(); }
    catch (error) { setWalletFirstStatus(error?.message || "Could not connect the wallet."); }
}

function onAccountTrigger() {
    if (!walletFirstMode()) { openDemoAccountModal(); return; }
    connectWalletFirst();
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
    renderDemoWallet();
}
async function loadRewards() {
    await loadDemoAccountData();
}
async function loadDashboard() {
    if (loading || unlocking) return;
    loading = true;
    if (typeof trackBehavior === "function") void trackBehavior("dashboard_refresh");
    refreshBtn.disabled = true;
    refreshBtn.textContent = "Loading...";
    byId("dashboardMessage").textContent = "Loading robot status and session rewards...";
    byId("robotStatusMessage").textContent = "Loading robot status...";
    ["aiSafetyState", "predictionState", "robotSafetyState"].forEach(id => setState(id, "UNKNOWN"));
    renderRobot(null);
    backendConnected = null;
    syncSidebarViews();
    if (typeof loadLiveRuntime === "function") void loadLiveRuntime();
    void loadTransactions();
    if (typeof loadDemoAccountData === "function") void loadDemoAccountData();
    const [robotResult] = await Promise.allSettled([
        request(`/api/robots/${encodeURIComponent(ROBOT_ID)}/status`)
    ]);
    backendConnected = robotResult.status === "fulfilled" && typeof robotResult.value.robotLocked === "boolean";
    const errors = [];
    try {
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
        if (typeof trackBehavior === "function") void trackBehavior(confirmed ? "robot_control_confirmed" : "robot_control_pending", { action: locking ? "lock" : "unlock", transactionHash: result.hash });
    } catch (error) {
        renderRobot(null);
        byId("robotStatusMessage").textContent = (error.code === 4001 ? "MetaMask request cancelled."
            : label + " could not be confirmed: " + (error.shortMessage || error.message)) + " Refresh status before retrying.";
        if (typeof trackBehavior === "function") void trackBehavior("robot_control_failed", { action: locking ? "lock" : "unlock", code: error.code || "UNKNOWN" });
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
    const setText = (id, value) => { const element = byId(id); if (element) element.textContent = value; };
    const unavailable = "MetaMask is not installed or the wallet script did not load. Refresh the page after enabling the extension.";
    if (typeof createWalletAccess === "function" && typeof ethers !== "undefined") {
        const injected = window.ethereum?.providers?.find(provider => provider.isMetaMask) || window.ethereum;
        window.hrcWallet = createWalletAccess(injected, ethers, state => {
            setText("connectedWalletAddress", state.account || "Not connected");
            setText("contractOwnerAddress", state.owner || "Not verified");
            setText("walletRole", state.isOwner ? "OWNER" : state.account && state.owner ? "READ ONLY" : "NOT VERIFIED");
            setText("walletSettingsState", state.account || "Not connected");
            setText("walletNetworkState", state.chainId
                ? String(state.chainId) + (state.networkMatches ? " · OK" : " · WRONG NETWORK") : "--");
            void syncWalletApplication(state);
            renderAppIdentity();
            updateAdminControls();
        }, window.hrcRuntimeConfig);
        void window.hrcWallet.refresh();
        return;
    }
    setWalletFirstStatus(unavailable);
}
unlockBtn?.addEventListener("click", unlockRobot);
byId("lockRobotBtn")?.addEventListener("click", lockRobot);
byId("demoAccountTrigger")?.addEventListener("click", onAccountTrigger);
byId("walletFirstConnectBtn")?.addEventListener("click", connectWalletFirst);
byId("demoAccountCloseBtn")?.addEventListener("click", closeDemoAccountModal);
byId("demoAccountCancelBtn")?.addEventListener("click", closeDemoAccountModal);
byId("demoAccountModal")?.querySelector("[data-demo-account-close]")?.addEventListener("click", closeDemoAccountModal);
document.addEventListener("keydown", event => {
    if (event.key !== "Escape") return;
    if (!byId("demoAccountModal")?.classList.contains("hidden")) closeDemoAccountModal();
});
byId("demoAccountModal")?.addEventListener("keydown", event => {
    if (event.key !== "Tab") return;
    const controls = Array.from(byId("demoAccountModal").querySelectorAll("button:not([disabled]), select:not([disabled])"));
    const first = controls[0], last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
});
const viewTitles = {
    dashboard: "Safety Control Center", "live-monitoring": "Live Monitoring", reports: "Reports"
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
    void trackBehavior("page_view", { view: current });
    if (current === "user") void loadUserPortal();
    if (current === "customers") void loadCustomers();
}
function syncSidebarViews() {
    document.querySelectorAll("[data-refresh]").forEach(button => {
        button.disabled = loading || unlocking;
        button.textContent = loading ? "Loading..." : "Refresh Data";
    });
    byId("liveConnection").textContent = loading ? "LOADING" : liveStatus();
    byId("liveTelemetryConnection").textContent = liveStatus();
    byId("robotViewStatus").textContent = byId("robotStatus").textContent;
    byId("robotViewStatus").style.color = byId("robotStatus").style.color;
    setState("robotViewSafety", byId("robotSafetyState").textContent);
    byId("robotViewMessage").textContent = byId("robotStatusMessage").textContent;

    renderTransactions();
    renderRewards();
    renderAppIdentity();
    renderUserPortal();
    renderCustomers();
    renderDemoWallet();
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
document.querySelectorAll("[data-user-refresh]").forEach(button => {
    button.addEventListener("click", () => { void loadUserPortal(); });
});
document.querySelectorAll("[data-customer-refresh]").forEach(button => {
    button.addEventListener("click", () => { void loadCustomers(); });
});
byId("demoAccountForm")?.addEventListener("submit", event => { event.preventDefault(); void loginDemoAccount(); });
byId("demoAccountSelect")?.addEventListener("change", updateDemoAccountPicker);
    document.querySelectorAll("[data-demo-account-open]").forEach(button => button.addEventListener("click", openDemoAccountModal));
document.querySelectorAll("[data-demo-refresh]").forEach(button => button.addEventListener("click", () => { void loadDemoAccountData(); }));
byId("demoApproveBtn")?.addEventListener("click", () => { void approveDemoWallet(); });
byId("demoStartBtn")?.addEventListener("click", () => { void startDemoSession(); });
byId('demoEndBtn')?.addEventListener('click', () => { void endDemoSession(); });
window.addEventListener("hashchange", navigate);
navigate();
async function bootstrap() {
    await loadRuntimeConfig();
    if (walletFirstMode()) {
        // Wallet-first: the header trigger connects a real wallet and signs the
        // application login message. The demo picker is not part of this flow.
        initWallet();
        await Promise.allSettled([loadDashboard(), loadLiveRuntime()]);
        connectLiveStream();
        return;
    }
    restoreDemoSession();
    await loadDemoAccounts();
    renderDemoWallet();
    await Promise.allSettled([loadDashboard(), loadLiveRuntime(), loadDemoAccountData()]);
    connectLiveStream();
    window.setInterval(() => {
        if (!document.hidden && !demoBusy) void loadDemoAccountData();
    }, 5000);
}
void bootstrap();
