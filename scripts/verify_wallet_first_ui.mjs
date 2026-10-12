// Browser regression for the wallet-first entry point. Uses a disposable test
// wallet to sign SIWE through a CDP binding; no wallet key is sent to the page.
// Reads the running local chain and API. Does not broadcast token transactions.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
const require = createRequire(new URL('../blockchain_api/package.json', import.meta.url));
const { ethers } = require('ethers');
const base = process.env.HRC_DASHBOARD_URL || 'http://localhost:8081/';
const configResponse = await fetch(new URL('/api/chain/api/config', base));
assert.equal(configResponse.status, 200, 'dashboard proxy returns config');
const config = await configResponse.json();
assert.equal(config.app?.authMode, 'siwe', 'SIWE is the only authentication mode');
assert.equal(config.chainId, 31337, 'this verifier uses only local Hardhat');
const wallet = ethers.Wallet.createRandom();
const provider = new ethers.JsonRpcProvider(config.rpcUrl, undefined, { cacheTimeout: -1 });
const profile = await mkdtemp(join(tmpdir(), 'hrc-wallet-first-'));
const evidence = resolve(process.env.HRC_UI_EVIDENCE_DIR || 'data/results/wallet-first-connect');
const chrome = spawn(process.env.HRC_CHROME || '/usr/bin/google-chrome', [
    '--headless=new', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
    '--disable-background-networking', '--disable-extensions', '--no-first-run',
    `--user-data-dir=${profile}`, '--remote-debugging-port=0', 'about:blank'
], { stdio: 'ignore' });
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const pending = new Map(), exceptions = [], results = [];
let ws, call, sequence = 0, scriptId;
try {
    await mkdir(evidence, { recursive: true });
    let port;
    for (let attempt = 0; attempt < 80; attempt++) {
        try { port = Number((await readFile(join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0]); break; }
        catch { await delay(100); }
    }
    assert.ok(port, 'Chrome starts');
    const version = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
    ws = new WebSocket(version.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
        ws.addEventListener('open', resolve, { once: true });
        ws.addEventListener('error', reject, { once: true });
    });
    ws.addEventListener('message', ({ data }) => {
        const message = JSON.parse(data);
        if (message.id && pending.has(message.id)) {
            const item = pending.get(message.id); pending.delete(message.id);
            message.error ? item.reject(new Error(message.error.message)) : item.resolve(message.result);
        }
        if (message.method === 'Runtime.exceptionThrown') {
            exceptions.push(message.params.exceptionDetails.exception?.description || message.params.exceptionDetails.text);
        }
        if (message.method === 'Runtime.bindingCalled' && message.params.name === '__walletBridge') {
            void handleWalletRequest(JSON.parse(message.params.payload)).catch(error => exceptions.push(error.message));
        }
    });
    const send = (method, params = {}, sessionId) => new Promise((resolve, reject) => {
        const id = ++sequence; pending.set(id, { resolve, reject });
        ws.send(JSON.stringify({ id, method, params, sessionId }));
    });
    const target = await send('Target.createTarget', { url: 'about:blank' });
    const { sessionId } = await send('Target.attachToTarget', { targetId: target.targetId, flatten: true });
    call = (method, params = {}) => send(method, params, sessionId);
    await call('Page.enable');
    await call('Runtime.enable');
    await call('Runtime.addBinding', { name: '__walletBridge' });
    const evaluate = async expression => {
        const result = await call('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
        if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
        return result.result.value;
    };
    const wait = async expression => {
        const deadline = Date.now() + 15000;
        while (Date.now() < deadline) {
            if (await evaluate(expression)) return;
            await delay(50);
        }
        throw new Error('UI timed out: ' + expression);
    };
    async function handleWalletRequest({ id, method, params }) {
        let result, error;
        try {
            if (method === 'personal_sign') {
                assert.equal(params[1].toLowerCase(), wallet.address.toLowerCase());
                result = await wallet.signMessage(params[0].startsWith('0x') ? ethers.getBytes(params[0]) : params[0]);
            } else if (method === 'eth_call') result = await provider.call(params[0]);
            else throw new Error('Unexpected wallet write or request: ' + method);
        } catch (failure) { error = { message: failure.message, code: failure.code }; }
        // Only the signature/chain response goes to the browser, never the key.
        await evaluate(`window.__walletReply(${JSON.stringify({ id, result, error })})`);
    }
    const injection = scenario => `
        window.__walletScenario = ${JSON.stringify(scenario)};
        window.__walletCalls = [];
        window.__walletAuthorized = false;
        window.__walletChain = ${JSON.stringify(scenario === 'wrong-network' ? '0x1' : '0x7a69')};
        window.__walletChainAdded = false;
        const promises = new Map(); let nextId = 0;
        window.__walletReply = response => {
            const item = promises.get(response.id); if (!item) return;
            promises.delete(response.id);
            response.error ? item.reject(Object.assign(new Error(response.error.message), response.error)) : item.resolve(response.result);
        };
        window.__installTestWallet = () => {
            window.ethereum = { isMetaMask: true, on() {}, request: async ({ method, params }) => {
                window.__walletCalls.push(method);
                if (method === 'eth_accounts') return window.__walletAuthorized ? [${JSON.stringify(wallet.address)}] : [];
                if (method === 'eth_requestAccounts') {
                    if (window.__walletScenario === 'cancel-connect') throw Object.assign(new Error('Cancelled'), { code: 4001 });
                    window.__walletAuthorized = true; return [${JSON.stringify(wallet.address)}];
                }
                if (method === 'eth_chainId') return window.__walletChain;
                if (method === 'wallet_switchEthereumChain') {
                    if (window.__walletScenario === 'wrong-network' && !window.__walletChainAdded) {
                        throw Object.assign(new Error('Unknown chain'), { code: 4902 });
                    }
                    window.__walletChain = params[0].chainId; return null;
                }
                if (method === 'wallet_addEthereumChain') {
                    window.__walletChainAdded = true;
                    window.__walletChain = params[0].chainId;
                    return null;
                }
                if (method === 'personal_sign' && window.__walletScenario === 'cancel-sign') throw Object.assign(new Error('Cancelled'), { code: 4001 });
                return new Promise((resolve, reject) => {
                    const id = ++nextId; promises.set(id, { resolve, reject });
                    window.__walletBridge(JSON.stringify({ id, method, params }));
                });
            } };
        };
        if (!['missing', 'late'].includes(window.__walletScenario)) window.__installTestWallet();
    `;
    const navigate = async (scenario, mobile = false) => {
        if (scriptId) await call('Page.removeScriptToEvaluateOnNewDocument', { identifier: scriptId });
        scriptId = (await call('Page.addScriptToEvaluateOnNewDocument', { source: injection(scenario) })).identifier;
        await call('Emulation.setDeviceMetricsOverride', { width: mobile ? 390 : 1440, height: mobile ? 844 : 1000, deviceScaleFactor: 1, mobile });
        await call('Page.navigate', { url: base });
        await wait('typeof window.hrcWallet === "object" && document.getElementById("walletFirstConnectBtn")?.hidden === false');
        await evaluate('sessionStorage.clear()');
    };
    const click = async selector => {
        const point = await evaluate(`(() => {
            const button = document.querySelector(${JSON.stringify(selector)});
            if (!button || button.disabled || button.hidden) throw new Error('Button unavailable: ' + ${JSON.stringify(selector)});
            button.scrollIntoView({ block: 'center', behavior: 'instant' });
            const r = button.getBoundingClientRect(); const x = r.x + r.width / 2, y = r.y + r.height / 2;
            if (!button.contains(document.elementFromPoint(x, y))) throw new Error('Button covered');
            return { x, y };
        })()`);
        await call('Input.dispatchMouseEvent', { type: 'mousePressed', ...point, button: 'left', clickCount: 1 });
        await call('Input.dispatchMouseEvent', { type: 'mouseReleased', ...point, button: 'left', clickCount: 1 });
    };
    const check = async name => {
        assert.equal(await evaluate('document.documentElement.scrollWidth <= window.innerWidth'), true, 'no horizontal overflow');
        const image = await call('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
        await writeFile(join(evidence, name + '.png'), Buffer.from(image.data, 'base64'));
        results.push(name); console.log('PASS: ' + name);
    };
    for (const mobile of [false, true]) {
        await navigate('missing', mobile);
        await click('#walletFirstConnectBtn');
        await wait('/MetaMask is not available/.test(document.getElementById("walletFirstStatus").textContent)');
        assert.equal(await evaluate('document.getElementById("walletAccountButtonLabel").textContent'), 'Connect wallet');
        assert.equal(await evaluate('document.getElementById("walletFirstStatus").getBoundingClientRect().height > 0'), true);
        await check('missing-wallet-' + (mobile ? 'mobile' : 'desktop'));
    }
    for (const scenario of ['cancel-connect', 'wrong-network', 'cancel-sign']) {
        await navigate(scenario);
        await click('#walletFirstConnectBtn');
        if (scenario === 'wrong-network') {
            await wait('document.getElementById("walletFirstConnectBtn").hidden && !document.getElementById("walletPanel").hidden');
            assert.ok((await evaluate('window.__walletCalls')).includes('wallet_addEthereumChain'));
            await check('wrong-network-add-chain');
        } else {
            const message = scenario === 'cancel-sign' ? 'Application login was cancelled' : 'MetaMask request cancelled';
            await wait(`document.getElementById('walletFirstStatus').textContent.includes(${JSON.stringify(message)}) && !document.getElementById('walletFirstConnectBtn').disabled`);
            assert.equal(await evaluate('document.getElementById("walletFirstConnectBtn").hidden'), false);
            await check(scenario);
        }
        if (scenario === 'cancel-sign') {
            await evaluate('window.__walletScenario = "success"');
            await click('#walletFirstConnectBtn');
            await wait('document.getElementById("walletFirstConnectBtn").hidden && !document.getElementById("walletPanel").hidden');
            await check('retry-sign-in');
        }
    }
    await navigate('late', true);
    await wait('/not available/.test(document.getElementById("walletFirstStatus").textContent)');
    await evaluate('window.__installTestWallet()');
    await click('#walletAccountTrigger');
    await wait('document.getElementById("walletFirstConnectBtn").hidden && !document.getElementById("walletPanel").hidden');
    assert.equal(await evaluate('window.hrcWallet.state.account'), wallet.address);
    assert.equal(await evaluate('document.getElementById("accountTriggerLabel").textContent'), 'SIGNED IN');
    assert.ok((await evaluate('window.__walletCalls')).includes('personal_sign'));
    await check('late-wallet-siwe-mobile');
    // Deterministic rendering fixtures only: no approval or stake broadcasts.
    // Verify both viewport sizes so blocked actions remain discoverable.
    await evaluate(`window.__walletUiSnapshot = {wallet, walletTasks, walletAssignments, selectedTaskId, walletSessions};`);
    for (const mobile of [true, false]) {
        await call('Emulation.setDeviceMetricsOverride', {width: mobile ? 390 : 1440, height: mobile ? 844 : 1000, deviceScaleFactor: 1, mobile});
        await evaluate(`wallet = {...window.__walletUiSnapshot.wallet, allowance:'0.2', requiredAllowance:'0.2', stakeAmount:'0.2',
            rewardProtocolVersion:'task-rewards-v2', canStart:true, canStartReason:null, device:{ready:true, operatorReady:true}};
            walletTasks = []; walletAssignments = []; selectedTaskId = null; walletSessions = []; walletTaskError = null;
            renderWallet(); document.getElementById('walletStartBtn').scrollIntoView({block:'center'});`);
        assert.equal(await evaluate("document.getElementById('walletStartBtn').hidden"), false);
        assert.equal(await evaluate("document.getElementById('walletStartBtn').disabled"), true);
        assert.equal(await evaluate("document.getElementById('walletApproveBtn').disabled"), true);
        assert.equal(await evaluate("document.getElementById('walletApproveBtn').textContent"), 'Approved 0.2 HRC');
        assert.match(await evaluate("document.getElementById('walletStartStatus').textContent"), /admin must create and publish/i);
        assert.equal(await evaluate("document.getElementById('walletStartBtn').getAttribute('aria-describedby')"), 'walletStartStatus');
        await check('approved-no-task-' + (mobile ? 'mobile' : 'desktop'));
        await evaluate(`walletTasks = [{taskId:'ui-task', title:'Browser layout fixture', description:'Live safety monitoring', completionRule:{requiredCycles:1, minimumDurationS:120}, status:'OPEN'}];
            walletAssignments = [{taskId:'ui-task', status:'ASSIGNED'}]; selectWalletTask('ui-task');`);
        assert.equal(await evaluate("document.getElementById('walletStartBtn').disabled"), false);
        assert.match(await evaluate("document.getElementById('walletStartStatus').textContent"), /separate MetaMask transaction.*0.2 HRC/);
        await check('selected-task-ready-' + (mobile ? 'mobile' : 'desktop'));
        await evaluate(`wallet.canStart = false; wallet.device = {ready:false, operatorReady:true, reason:'No recent sensor telemetry is available'};
            renderWallet();`);
        assert.equal(await evaluate("document.getElementById('walletStartBtn').hidden"), false);
        assert.equal(await evaluate("document.getElementById('walletStartBtn').disabled"), true);
        assert.match(await evaluate("document.getElementById('walletStartStatus').textContent"), /Sensor unavailable: No recent sensor telemetry/);
        await check('selected-task-sensor-unavailable-' + (mobile ? 'mobile' : 'desktop'));
    }
    await evaluate(`({wallet, walletTasks, walletAssignments, selectedTaskId, walletSessions} = window.__walletUiSnapshot);
        delete window.__walletUiSnapshot; renderWallet();`);
    await call('Emulation.setDeviceMetricsOverride', {width:390, height:844, deviceScaleFactor:1, mobile:true});
    // Model a running session only for timer rendering/layout. No stake or End
    // transaction is submitted by this connection verifier.
    await evaluate(`walletSessions = [{sessionId: 'timer-ui-fixture', stage: 'RUNNING', settled: false,
        sampleCount: 1095, stakedAmount: '2', iotSession: {started_at: new Date(Date.now() - 109000).toISOString()}}];
        renderWallet(); document.getElementById('walletSessionTimer').scrollIntoView({block: 'center'});`);
    await wait("document.getElementById('walletSessionTimer').textContent === '00:01:50'");
    assert.equal(await evaluate("document.getElementById('walletEndBtn').hidden"), false);
    assert.equal(await evaluate("document.getElementById('walletStartBtn').disabled"), true);
    await check('session-timer-mobile');
    await call('Emulation.setDeviceMetricsOverride', {width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false});
    await check('session-timer-desktop');
    await evaluate(`walletSessions[0].stage = 'SETTLEMENT_PENDING';
        walletSessions[0].iotSession.ended_at = new Date(Date.parse(walletSessions[0].iotSession.started_at) + 110000).toISOString();
        renderWallet();`);
    assert.equal(await evaluate("document.getElementById('walletSessionTimer').textContent"), '00:01:50');
    assert.match(await evaluate("document.getElementById('walletSessionTimerDetail').textContent"), /Stopped/);
    assert.deepEqual(exceptions, [], 'no JavaScript exceptions');
    await writeFile(join(evidence, 'verification.json'), JSON.stringify({ scenarios: results, authMode: config.app.authMode, chainId: config.chainId, walletAddress: wallet.address, broadcasts: 0, exceptions }, null, 2));
    console.log('Evidence: ' + evidence);
} finally {
    ws?.close();
    provider.destroy();
    chrome.kill();
    await new Promise(resolve => { if (chrome.exitCode !== null) resolve(); else chrome.once('exit', resolve); });
    await rm(profile, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
}
