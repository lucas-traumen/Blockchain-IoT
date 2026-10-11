// Real Chromium clicks against the running local demo. By default this is read-only.
// --transact opens and ends one live sensor session through the user UI. Outcome
// is evaluated from that session by the backend; no operator token enters this script.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { randomUUID } from 'node:crypto';

const base = process.env.HRC_DASHBOARD_URL || 'http://127.0.0.1:8080/';
const api = process.env.HRC_CHAIN_API_URL || new URL(base).origin.replace(/:\d+$/, ':3000');
const evidence = resolve(process.env.HRC_UI_EVIDENCE_DIR || 'data/results/live-wallet-sessions-20261011');
const transact = process.argv.includes('--transact');
const profile = await mkdtemp(join(tmpdir(), 'hrc-wallet-browser-'));
const chrome = spawn(process.env.HRC_CHROME || '/usr/bin/google-chrome', ['--headless=new', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage', '--disable-background-networking', '--disable-extensions', '--no-first-run', `--user-data-dir=${profile}`, '--remote-debugging-port=0', 'about:blank'], { stdio: 'ignore' });
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const exceptions = [], walletWrites = [], pending = new Map();
let ws, sequence = 0, send;
try {
    await mkdir(evidence, { recursive: true });
    let port;
    for (let attempt = 0; attempt < 80; attempt++) {
        try { port = Number((await readFile(join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0]); break; }
        catch { await delay(100); }
    }
    assert.ok(port, 'Chrome remote debugging started');
    const version = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
    ws = new WebSocket(version.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => { ws.addEventListener('open', resolve, { once: true }); ws.addEventListener('error', reject, { once: true }); });
    ws.addEventListener('message', ({ data }) => {
        const message = JSON.parse(data);
        if (message.id && pending.has(message.id)) {
            const item = pending.get(message.id); pending.delete(message.id);
            message.error ? item.reject(new Error(message.error.message)) : item.resolve(message.result);
        }
        if (message.method === 'Network.requestWillBeSent' && message.params.request.method === 'POST' && /\/api\/me\/(wallet\/approve|sessions\/start|sessions\/[^/]+\/end)$/.test(message.params.request.url)) walletWrites.push(message.params.request.url);
        if (message.method === 'Runtime.exceptionThrown') exceptions.push(message.params.exceptionDetails.exception?.description || message.params.exceptionDetails.text);
    });
    send = (method, params = {}, sessionId) => new Promise((resolve, reject) => {
        const id = ++sequence; pending.set(id, { resolve, reject }); ws.send(JSON.stringify({ id, method, params, sessionId }));
    });
    const target = await send('Target.createTarget', { url: 'about:blank' });
    const { sessionId } = await send('Target.attachToTarget', { targetId: target.targetId, flatten: true });
    const call = (method, params = {}) => send(method, params, sessionId);
    const evaluate = async expression => {
        const result = await call('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
        if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
        return result.result.value;
    };
    const wait = async (expression, timeout = 15000) => {
        const deadline = Date.now() + timeout;
        while (Date.now() < deadline) { if (await evaluate(expression)) return; await delay(80); }
        throw new Error('UI timed out: ' + expression);
    };
    const click = async selector => {
        // Smooth scrolling otherwise leaves the old coordinates under a different
        // button and causes the test harness to click the wrong action.
        const point = await evaluate(`(() => { const element = document.querySelector(${JSON.stringify(selector)}); element.scrollIntoView({ block: 'center', behavior: 'instant' }); const r = element.getBoundingClientRect(); const x = r.x + r.width / 2, y = r.y + r.height / 2; if (!element.contains(document.elementFromPoint(x, y))) throw new Error('Click target is covered: ' + ${JSON.stringify(selector)}); window.__hrcExpectedClick = element; window.__hrcClickCorrect = false; return { x, y }; })()`);
        await call('Input.dispatchMouseEvent', { type: 'mousePressed', ...point, button: 'left', clickCount: 1 });
        await call('Input.dispatchMouseEvent', { type: 'mouseReleased', ...point, button: 'left', clickCount: 1 });
        assert.equal(await evaluate('window.__hrcClickCorrect'), true, 'Native click reached ' + selector);
    };
    const screenshot = async name => {
        await evaluate('window.scrollTo({ top: 0, behavior: "instant" })');
        await evaluate('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())))');
        const image = await call('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
        await writeFile(join(evidence, name + '.png'), Buffer.from(image.data, 'base64'));
    };
    const check = (name, passed) => { assert.ok(passed, name); console.log('PASS: ' + name); };
    const login = async userId => {
        await click('#demoAccountTrigger');
        await wait('!document.getElementById("demoAccountModal").classList.contains("hidden")');
        const index = await evaluate(`Array.from(document.getElementById('demoAccountSelect').options).findIndex(option => option.value === ${JSON.stringify(userId)})`);
        assert.ok(index >= 0, 'Fixed account is available in the picker');
        await click('#demoAccountSelect');
        const press = async (key, code, keyCode) => {
            const params = { key, code, windowsVirtualKeyCode: keyCode, nativeVirtualKeyCode: keyCode };
            await call('Input.dispatchKeyEvent', { type: 'keyDown', ...params });
            await call('Input.dispatchKeyEvent', { type: 'keyUp', ...params });
        };
        await press('Home', 'Home', 36);
        for (let option = 0; option < index; option++) await press('ArrowDown', 'ArrowDown', 40);
        await press('Enter', 'Enter', 13);
        await wait(`document.getElementById('demoAccountSelect').value === ${JSON.stringify(userId)} && !document.getElementById('demoLoginBtn').disabled`);
        await click('#demoLoginBtn');
        await wait(`document.getElementById('demoAccountButtonLabel').textContent === ${JSON.stringify(userId)} && document.getElementById('demoAccountModal').classList.contains('hidden') && !document.getElementById('demoAccountTrigger').disabled`);
    };
    const request = async (path, token, body) => {
        const response = await fetch(api + path, { method: body === undefined ? 'GET' : 'POST', headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token }, body: body === undefined ? undefined : JSON.stringify(body) });
        const data = await response.json();
        assert.equal(response.status, 200, `${path}: ${data.error || response.status}`);
        return data;
    };
    await call('Page.enable');
    if (process.env.HRC_CHAIN_API_URL) {
        await call('Page.addScriptToEvaluateOnNewDocument', { source: `window.HRC_CHAIN_API_URL = ${JSON.stringify(api)};` });
    }
    await call('Runtime.enable');
    await call('Network.enable');
    await call('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
    await call('Page.navigate', { url: base });
    await wait('document.readyState === "complete" && typeof openDemoAccountModal === "function" && !document.getElementById("refreshBtn").disabled');
    const browserApi = await evaluate('API_URL');
    assert.equal(browserApi, api, 'The browser and receipt verifier must use the same blockchain API');
    await evaluate(`document.addEventListener('click', event => { window.__hrcClickCorrect = window.__hrcExpectedClick?.contains(event.target) === true; }, true);`);
    await click('#demoAccountTrigger');
    await wait('!document.getElementById("demoAccountModal").classList.contains("hidden")');
    check('Account opens a small picker', await evaluate('document.querySelector(".demo-account-modal-content").getBoundingClientRect().width <= 410'));
    check('Picker has exactly admin/user_1/user_2 and Open defaults to admin', await evaluate('JSON.stringify(Array.from(document.getElementById("demoAccountSelect").options).map(option => option.value)) === JSON.stringify(["admin","user_1","user_2"]) && document.getElementById("demoAccountSelect").value === "admin" && !document.getElementById("demoLoginBtn").disabled'));
    await screenshot('account-picker-desktop');
    await call('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape' });
    await call('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape' });
    await wait('document.getElementById("demoAccountModal").classList.contains("hidden")');
    check('Escape closes the picker and restores focus', await evaluate('document.activeElement.id === "demoAccountTrigger"'));
    await login('user_1');
    check('Open loads the fixed user_1 wallet', await evaluate('document.getElementById("demoWalletAddress").textContent.startsWith("0x3C44")'));
    const before = await evaluate('document.getElementById("demoWalletBalance").textContent');
    await click('#refreshBtn');
    await wait('!document.getElementById("refreshBtn").disabled');
    check('Refresh preserves the selected user balance', await evaluate(`document.getElementById('demoWalletBalance').textContent === ${JSON.stringify(before)} && document.getElementById('rewardBalance').textContent === ${JSON.stringify(before)}`));
    await login('user_2');
    check('user_2 sees its own wallet and sessions', await evaluate('document.getElementById("demoWalletAddress").textContent.startsWith("0x90F7") && !document.getElementById("demoSessionsTableBody").textContent.includes("demo-user_1-")'));
    check('Reward and penalty policy is next to user actions', await evaluate('document.getElementById("demoApproveBtn").getClientRects().length > 0 && document.getElementById("policyHeading").getClientRects().length > 0'));

    check('Available balance, locked stake and recent activity are visible', await evaluate('document.getElementById("demoWalletLocked").getClientRects().length > 0 && document.getElementById("demoLedgerHeading").getClientRects().length > 0'));
    if (transact) {
        const token = await evaluate('demoSession.token');
        const initial = await request('/api/me/wallet', token);
        const existingId = process.env.HRC_UI_SESSION_ID || initial.activeSessionId;
        let session, staked;
        if (existingId) {
            session = (await request('/api/me/sessions/' + existingId, token)).session;
            check('Retry resumes the existing ESP32 session', session.source === 'esp32' && session.started);
            check('The existing stake has one SessionStarted event and exactly 2 HRC', session.events.filter(event => event.name === 'SessionStarted').length === 1 && Number(session.stakedAmount) === 2);
            staked = initial;
        } else {
            check('The live sensor is ready for a new session', initial.device?.ready && !initial.activeSessionId && !initial.device?.busySessionId);
            if (Number(initial.allowance) < 2) {
                await click('#demoApproveBtn');
                await wait('!demoBusy && Number(demoWallet?.allowance) >= 2');
            }
            check('Approve does not change wallet balance', Number((await request('/api/me/wallet', token)).balance) === Number(initial.balance));
            const ref = 'live-' + randomUUID();
            await evaluate(`demoSessionRef = ${JSON.stringify(ref)}`);
            await click('#demoStartBtn');
            await wait(`!demoBusy && demoSessions.some(session => session.sessionRef === ${JSON.stringify(ref)} && session.stage === 'RUNNING')`, 25000);
            session = await evaluate(`demoSessions.find(session => session.sessionRef === ${JSON.stringify(ref)})`);
            staked = await request('/api/me/wallet', token);
            check('Start locks exactly 2 HRC and opens the matching sensor session', Number(initial.balance) - Number(staked.balance) === 2 && Number(staked.lockedBalance) === 2 && session.iotSession?.session_id === session.sessionId);
            const duplicate = await request('/api/me/sessions/start', token, { sessionRef: ref });
            check('Start retry returns the same session without staking again', duplicate.duplicate === true && Number((await request('/api/me/wallet', token)).balance) === Number(staked.balance));
        }
        const denied = await fetch(api + '/api/session-outcome', { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token }, body: JSON.stringify({ sessionId: session.sessionId, outcome: 'VALID_NO_VIOLATION' }) });
        check('User cannot submit the settlement outcome', denied.status === 401);
        // Wait only while measuring. A previously closed incomplete session must
        // retain its actual short duration instead of waiting for fabricated data.
        if (!session.settled && session.iotSession?.status === 'running') {
            await wait(`demoSessions.find(session => session.sessionId === ${JSON.stringify(session.sessionId)})?.iotSession?.durationS >= 31`, 55000);
        }
        const measured = await request('/api/me/sessions/' + session.sessionId, token);
        check('Live session contains sensor samples', measured.session.sampleCount > 0);
        const alreadySettled = measured.session.settled;
        if (!alreadySettled) {
            staked = await request('/api/me/wallet', token);
            await click('#demoEndBtn');
            await wait(`!demoBusy && demoSessions.find(session => session.sessionId === ${JSON.stringify(session.sessionId)})?.settled === true`, 25000);
        }
        const readback = (await request('/api/me/sessions/' + session.sessionId, token)).session;
        const after = await request('/api/me/wallet', token);
        const history = await request('/api/me/wallet/transactions', token);
        const settlement = history.transactions.find(row => row.action === 'SETTLEMENT' && row.sessionId === session.sessionId);
        check('End returns the measured outcome and one on-chain settlement', readback.source === 'esp32' && readback.events.filter(event => event.name === 'SessionSettled').length === 1 && readback.outcomeReason);
        if (!alreadySettled) {
            check('Settlement balance matches returned stake and reward', Number(after.balance) === Number(staked.balance) + Number(readback.stakeReturned) + Number(readback.rewardPaid));
        }
        check('Ledger reports the real token transfer and unlocks the stake', Number(settlement.walletDelta) === Number(readback.stakeReturned) + Number(readback.rewardPaid) && Number(after.lockedBalance) === 0);
        const repeated = await request('/api/me/sessions/' + session.sessionId + '/end', token, {});
        check('Repeated End does not settle twice', repeated.duplicate === true && Number((await request('/api/me/wallet', token)).balance) === Number(after.balance));
        await writeFile(join(evidence, 'live-sensor-settlement.json'), JSON.stringify({ source: 'live-esp32-session', chain: process.env.HRC_UI_CHAIN_LABEL || 'existing-local-demo', apiUrl: api, resumed: Boolean(existingId), alreadySettled, initialBalance: initial.balance, balanceBeforeEnd: staked.balance, after: after.balance, measured: measured.session, readback, settlement }, null, 2) + '\n');
        await call('Page.reload');
        await wait('document.readyState === "complete" && demoSession.user?.userId === "user_2" && demoTransactions.length > 0 && !document.getElementById("refreshBtn").disabled');
        await evaluate(`document.addEventListener('click', event => { window.__hrcClickCorrect = window.__hrcExpectedClick?.contains(event.target) === true; }, true);`);
        check('Reload restores settlement and visible amount history', await evaluate('demoTransactions.some(transaction => transaction.action === "SETTLEMENT") && document.getElementById("demoRecentActivity").children.length > 0'));
    }
    await screenshot('user-wallet-desktop');
    await login('admin');
    check('Admin sees both users', await evaluate('document.getElementById("demoAdminTableBody").textContent.includes("user_1") && document.getElementById("demoAdminTableBody").textContent.includes("user_2")'));
    check('Admin has no visible contract transaction buttons', await evaluate('!document.getElementById("demoApproveBtn").getClientRects().length && !document.getElementById("demoStartBtn").getClientRects().length && !document.getElementById("demoEndBtn").getClientRects().length'));
    await screenshot('admin-dashboard-desktop');
    await click('[data-view="reports"]');
    await wait('document.getElementById("demoAccountWorkspace").parentElement.id === "demoReportsSlot"');
    check('Admin report is accessible through Reports', await evaluate('document.getElementById("demoAdminReport").getClientRects().length > 0'));
    await screenshot('admin-reports-desktop');
    await click('[data-view="dashboard"]');
    await login('user_2');
    await call('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
    check('Mobile has no page overflow', await evaluate('document.documentElement.scrollWidth <= window.innerWidth'));
    await screenshot('user-wallet-mobile');
    await click('#demoAccountTrigger');
    await screenshot('account-picker-mobile');
    check('No JavaScript exceptions', exceptions.length === 0);
    if (!transact) check('Read-only browser verification submitted no wallet transactions', walletWrites.length === 0);
    await writeFile(join(evidence, transact ? 'browser-transactions-result.json' : 'browser-result.json'), JSON.stringify({ success: true, transact, source: process.env.HRC_UI_CHAIN_LABEL || 'local-hardhat-demo', apiUrl: api, url: base, screenshots: 6 }, null, 2) + '\n');
    console.log('Browser evidence: ' + evidence);
} finally {
    if (ws?.readyState === WebSocket.OPEN) { await send('Browser.close').catch(() => {}); ws.close(); }
    chrome.kill('SIGTERM');
    await new Promise(resolve => { if (chrome.exitCode !== null) resolve(); else { chrome.once('exit', resolve); setTimeout(resolve, 1000).unref(); } });
    await rm(profile, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 });
}
