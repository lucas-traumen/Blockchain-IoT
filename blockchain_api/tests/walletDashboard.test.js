const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../../dashboard/app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../../dashboard/index.html'), 'utf8');
const users = {
    user_1: { userId: 'user_1', role: 'customer', walletAddress: '0x1111' },
    user_2: { userId: 'user_2', role: 'customer', walletAddress: '0x2222' },
    admin: { userId: 'admin', role: 'admin', walletAddress: null }
};
const wallet = id => ({ success: true, address: users[id].walletAddress, balance: '20', allowance: '2', requiredAllowance: '2', stakeAmount: '2', chainId: 31337, canStart: true });
const reply = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });
const htmlReply = (status = 200) => ({
    ok: status < 400,
    status,
    headers: { get: name => name.toLowerCase() === 'content-type' ? 'text/html; charset=utf-8' : null },
    json: async () => { throw new SyntaxError("Unexpected token '<'"); }
});
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };

function fixture(fetchImpl = async () => reply({ success: true, sessions: [], transactions: [] }), storage = new Map()) {
    class Element {
        constructor() { this.children = []; this.textContent = ''; this.disabled = false; this.hidden = false; this.value = ''; this.style = {}; this.dataset = {}; this.attrs = new Map(); this.classes = new Set(); this.listeners = new Map();
            this.classList = { toggle: (name, on) => on ? this.classes.add(name) : this.classes.delete(name), contains: name => this.classes.has(name), add: name => this.classes.add(name), remove: name => this.classes.delete(name) }; }
        appendChild(child) { this.children.push(child); return child; }
        append(...children) { this.children.push(...children); }
        replaceChildren(...children) { this.children = children; }
        setAttribute(name, value) { this.attrs.set(name, value); }
        removeAttribute(name) { this.attrs.delete(name); }
        addEventListener(name, callback) { this.listeners.set(name, callback); }
        focus() {}
        querySelector() { return null; }
        cloneNode() { return new Element(); }
    }
    const elements = new Map([...html.matchAll(/id="([^"]+)"/g)].map(match => [match[1], new Element()]));
    const document = { getElementById: id => elements.get(id) || null, createElement: () => new Element(), createTextNode: value => ({ textContent: value }), querySelector: () => new Element(), querySelectorAll: () => [], body: new Element() };
    const context = vm.createContext({
        console, URL, AbortSignal, fetch: fetchImpl, document,
        window: { hrcWallet: { state: {}, sendPrepared: async () => "0xabc" }, location: { protocol: 'http:', href: 'http://localhost:8080/', hash: '' }, setTimeout: () => 1 },
        sessionStorage: { getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) },
        text: (id, value) => { const el = elements.get(id); if (el) el.textContent = String(value ?? '--'); },
        clearAppSession() { vm.runInContext('clearWalletSession(); renderWallet(); sessionStorage.removeItem("hrc-app-session")', context); },
        liveStatus: () => 'LIVE', setState() {}, renderTransactions() {}, renderAppIdentity() {}, renderUserPortal() {}, renderCustomers() {}
    });
    vm.runInContext(source.slice(0, source.indexOf('async function appRequest(')), context);
    // Exercise the actual refresh-to-render call chain; a legacy render used to overwrite wallet balances.
    vm.runInContext(source.slice(source.indexOf('function rewardStatusClass('), source.indexOf('async function loadRewards(')), context);
    vm.runInContext(source.slice(source.indexOf('function syncSidebarViews('), source.indexOf('async function loadTransactions(')), context);
    const run = code => vm.runInContext(code, context);
    const select = id => { context.loginUser = users[id]; run('bindWalletSession(loginUser, loginUser.userId + "-token");'); };
    const setWallet = id => { context.walletData = wallet(id); run('wallet = walletData; renderWallet();'); };
    return { context, elements, run, select, setWallet, storage };
}

test('dashboard refresh preserves the signed-in wallet identity and balance', () => {
    const f = fixture(); f.select('user_1'); f.setWallet('user_1');
    const before = f.elements.get('rewardBalance').textContent;
    f.run('syncSidebarViews()');
    assert.equal(f.elements.get('rewardBalance').textContent, before);
    assert.match(f.elements.get('walletUserStatus').textContent, /user_1/);
});

test('admin has reports but no transaction controls; users have their own actions', () => {
    const f = fixture(); f.select('admin'); f.run('renderWallet()');
    assert.equal(f.elements.get('walletApproveBtn').hidden, true);
    assert.equal(f.elements.get('walletStartBtn').hidden, true);
    f.select('user_2'); f.setWallet('user_2');
    assert.equal(f.elements.get('walletApproveBtn').hidden, false);
    assert.equal(f.elements.get('walletStartBtn').hidden, false);
});

test('a delayed read for the previous account cannot replace the current user wallet', async () => {
    const old = deferred();
    const f = fixture(async (url, options) => {
        const id = options.headers.Authorization.includes('user_1') ? 'user_1' : 'user_2';
        if (id === 'user_1') await old.promise;
        return reply(url.endsWith('/wallet') ? wallet(id) : { success: true, sessions: [], transactions: [] });
    });
    f.select('user_1'); const pending = f.run('loadWalletAccountData()');
    f.select('user_2'); await f.run('loadWalletAccountData()');
    old.resolve(); await pending;
    assert.equal(f.elements.get('walletAddress').textContent, users.user_2.walletAddress);
});

test('double-clicking Start submits once and leaves the consumed allowance disabled', async () => {
    const hold = deferred(); let starts = 0;
    const f = fixture(async url => {
        if (url.endsWith('/start')) { starts++; await hold.promise; return reply({ success: true, started: true, sessionId: 'wallet-user_1-run', transactionHash: '0xabc', blockNumber: 12, event: { name: 'SessionStarted' } }); }
        if (url.endsWith('/confirm-start')) return reply({ success: true, stage: 'RUNNING' });
        if (url.endsWith('/wallet')) return reply({ ...wallet('user_1'), allowance: '0', canStart: false });
        return reply({ success: true, sessions: [], transactions: [] });
    });
    f.select('user_1'); f.setWallet('user_1');
    const first = f.run('startWalletSession()'); const second = f.run('startWalletSession()');
    await new Promise(resolve => setImmediate(resolve));
    const sent = starts;
    hold.resolve(); await Promise.all([first, second]);
    assert.equal(sent, 1);
    assert.equal(f.elements.get('walletStartBtn').disabled, true);
    assert.equal(f.elements.get('walletApproveBtn').disabled, false);
});

test('an expired wallet login clears previous wallet data and session storage', async () => {
    const f = fixture(async () => reply({ success: false, error: 'Expired' }, 401));
    f.select('user_1'); f.setWallet('user_1');
    f.storage.set('hrc-app-session', JSON.stringify({ token: 'user_1-token', user: users.user_1 }));
    await f.run('loadWalletAccountData()');
    assert.equal(f.elements.get('walletAddress').textContent, '--');
    assert.equal(f.elements.get('walletAccountEmpty').hidden, false);
    assert.equal(f.storage.has('hrc-app-session'), false);
});

test('a lost start response keeps the same session reference after a page reload, even with no remaining allowance', async () => {
    const storage = new Map(); let firstBody;
    const first = fixture(async (url, options) => {
        if (url.endsWith('/start')) { firstBody = JSON.parse(options.body); throw new Error('Response lost after broadcast'); }
        return reply({ success: true });
    }, storage);
    storage.set('hrc-app-session', JSON.stringify({ token: 'user_1-token', user: users.user_1 }));
    first.select('user_1'); first.setWallet('user_1');
    await first.run('startWalletSession()');
    assert.equal(storage.get('hrc-wallet-pending-user_1'), firstBody.sessionRef);
    let secondBody;
    const reloaded = fixture(async (url, options) => {
        if (url.endsWith('/start')) { secondBody = JSON.parse(options.body); return reply({ success: true, started: true, duplicate: true, sessionId: 'wallet-user_1-retry', events: [{ name: 'SessionStarted' }] }); }
        if (url.endsWith('/confirm-start')) return reply({ success: true, stage: 'RUNNING' });
        return reply(url.endsWith('/wallet') ? { ...wallet('user_1'), allowance: '0', canStart: false } : { success: true, sessions: [], transactions: [] });
    }, storage);
    reloaded.select('user_1');
    await reloaded.run('loadWalletAccountData()');
    assert.equal(reloaded.elements.get('walletStartBtn').disabled, false);
    assert.match(reloaded.elements.get('walletStartBtn').textContent, /Retry/);
    await reloaded.run('startWalletSession()');
    assert.equal(secondBody.sessionRef, firstBody.sessionRef);
    assert.equal(storage.has('hrc-wallet-pending-user_1'), false);
});


test('approve refreshes the allowance after an older wallet read finishes', async () => {
    const oldRead = deferred(); const approved = deferred();
    let allowance = '0'; let walletReads = 0;
    const f = fixture(async url => {
        if (url.endsWith('/approve')) {
            allowance = '2'; approved.resolve();
            return reply({ success: true, transactionHash: '0xapproval', blockNumber: 14, event: { name: 'Approval' } });
        }
        if (url.endsWith('/wallet')) {
            walletReads++;
            const snapshot = { ...wallet('user_1'), allowance, canStart: allowance === '2' };
            if (walletReads === 1) await oldRead.promise;
            return reply(snapshot);
        }
        return reply({ success: true, sessions: [], transactions: [] });
    });
    f.select('user_1');
    f.context.walletData = { ...wallet('user_1'), allowance: '0', canStart: false };
    f.run('wallet = walletData; renderWallet()');
    const reading = f.run('loadWalletAccountData()');
    const approving = f.run('approveWallet()');
    await approved.promise;
    oldRead.resolve(); await Promise.all([reading, approving]);
    assert.equal(walletReads, 2);
    assert.equal(f.elements.get('walletAllowance').textContent, '2 HRC');
    assert.equal(f.elements.get('walletApproveBtn').disabled, true);
    assert.equal(f.elements.get('walletStartBtn').disabled, false);
});

test('End sends only the owning session reference and displays refunded stake, reward and wallet activity', async () => {
    let endRequests=0,body;
    const id='wallet-user_1-sensor';
    const settled={sessionId:id,started:true,settled:true,stage:'SETTLED',stakedAmount:'2',stakeReturned:'2',rewardPaid:'3',penaltyCollected:'0',settlementStatusCode:0,outcomeCode:0};
    const f=fixture(async(url,options)=>{
        if(url.endsWith('/end')){endRequests++;body=JSON.parse(options.body);return reply({success:true,...settled});}
        if(url.endsWith('/wallet'))return reply({...wallet('user_1'),balance:'23',lockedBalance:'0',canStart:false});
        if(url.endsWith('/transactions'))return reply({success:true,transactions:[{action:'SETTLEMENT',walletDelta:'5',lockedDelta:'-2',stakeReturned:'2',rewardPaid:'3',penaltyCollected:'0',blockNumber:14,transactionHash:'0xsettle',events:[{name:'SessionSettled'}]}]});
        return reply({success:true,sessions:[settled]});
    });
    f.select('user_1');f.setWallet('user_1');
    f.context.active={sessionId:id,started:true,settled:false,stage:'RUNNING',stakedAmount:'2',sampleCount:40,iotSession:{durationS:30}};
    f.run('walletSessions=[active]; renderWallet()');
    assert.equal(f.elements.get('walletEndBtn').hidden,false);
    await f.run('endWalletSession()');
    assert.equal(endRequests,1);assert.deepEqual(body,{});
    assert.equal(f.elements.get('walletBalance').textContent,'23 HRC');
    assert.match(f.elements.get('walletSessionState').textContent,/Stake returned \+2 HRC/);
    const entry=f.elements.get('walletRecentActivity').children[0];
    assert.equal(entry.children[1].textContent,'+5 HRC');
    assert.match(entry.children[2].textContent,/Stake returned \+2 HRC · Reward \+3 HRC/);
});

test('penalty ledger reports loss of locked stake without an additional wallet debit', () => {
    const f=fixture();f.context.penalty={action:'SETTLEMENT',walletDelta:'0',lockedDelta:'-2',penaltyCollected:'2',events:[{name:'SessionSettled'}]};
    assert.match(f.run('walletLedgerDescription(penalty)'),/Penalty 2 HRC from locked stake · no additional wallet charge/);
    f.select('admin');f.run('renderWallet()');
    assert.equal(f.elements.get('walletEndBtn').hidden,true);
});


test('an HTML proxy response is reported in the wallet workspace', async () => {
    const f = fixture(async () => htmlReply(502));
    f.select('user_2');
    await f.run('loadWalletAccountData()');
    assert.match(f.elements.get('walletMessage').textContent, /Blockchain API returned HTML \(HTTP 502\)/);
    assert.doesNotMatch(f.elements.get('walletMessage').textContent, /Unexpected token/);
});

test('a dashboard served without nginx falls back from its HTML 404 page to the direct blockchain API', async () => {
    const urls = [];
    const f = fixture(async url => {
        urls.push(url);
        if (url.startsWith('/api/chain/')) return htmlReply(404);
        if (url === 'http://localhost:3000/api/me/wallet') return reply(wallet('user_2'));
        return reply({success:true});
    });
    f.select('user_2');
    const result = await f.run("walletRequest('/api/me/wallet')");
    assert.equal(result.address, users.user_2.walletAddress);
    assert.deepEqual(urls, ['/api/chain/api/me/wallet', 'http://localhost:3000/api/me/wallet']);
});

test('concurrent account reads all complete when the static dashboard discovers the direct API', async () => {
    const f = fixture(async url => {
        if (url.startsWith('/api/chain/')) return htmlReply(404);
        if (url.endsWith('/wallet')) return reply(wallet('user_1'));
        if (url.endsWith('/sessions')) return reply({success:true,sessions:[]});
        if (url.endsWith('/transactions')) return reply({success:true,transactions:[]});
        return reply({success:true});
    });
    f.select('user_1');
    await f.run('loadWalletAccountData()');
    assert.equal(f.elements.get('walletBalance').textContent, '20 HRC');
    assert.doesNotMatch(f.elements.get('walletMessage').textContent, /Unable to/);
});

test('a recovered Start clears its pending reference after refresh without another stake request', async () => {
    const f=fixture(async url => {
        if(url.endsWith('/wallet')) return reply({...wallet('user_1'),allowance:'0',canStart:false});
        if(url.endsWith('/sessions')) return reply({success:true,sessions:[{sessionId:'wallet-user_1-recovered',sessionRef:'recovered',stage:'RUNNING',started:true,settled:false,source:'esp32'}]});
        return reply({success:true,transactions:[]});
    });
    f.select('user_1');
    f.storage.set('hrc-wallet-pending-user_1','recovered');
    f.run('walletSessionRef="recovered"');
    await f.run('loadWalletAccountData()');
    assert.equal(f.storage.has('hrc-wallet-pending-user_1'),false);
    assert.equal(f.elements.get('walletStartBtn').disabled,true);
    assert.equal(f.elements.get('walletStartBtn').textContent,'Start Session');
});


test('a restored wallet and a successful retry replace the stale choose-account or read-error message', async () => {
    let available = true;
    const storage = new Map([['hrc-app-session', JSON.stringify({ token: 'user_2-token', user: users.user_2 })]]);
    const f = fixture(async url => {
        if (!available) return reply({ success: false, error: 'RPC unavailable' }, 503);
        return reply(url.endsWith('/wallet') ? wallet('user_2') : { success: true, sessions: [], transactions: [] });
    }, storage);
    f.select('user_2');
    await f.run('loadWalletAccountData()');
    assert.doesNotMatch(f.elements.get('walletMessage').textContent, /Choose user_/);
    available = false;
    await f.run('loadWalletAccountData({ fresh: true })');
    assert.match(f.elements.get('walletMessage').textContent, /Unable to refresh/);
    available = true;
    await f.run('loadWalletAccountData({ fresh: true })');
    assert.doesNotMatch(f.elements.get('walletMessage').textContent, /Unable to refresh/);
});

test('anonymous wallet mode preserves the connect label across dashboard refreshes', () => {
    const f = fixture();
    f.run('appAuthMode = "siwe"');
    f.elements.get('walletAccountButtonLabel').textContent = 'Connect wallet';
    f.run('syncSidebarViews()');
    assert.equal(f.elements.get('walletAccountButtonLabel').textContent, 'Connect wallet');
    assert.equal(f.elements.get('connectedWalletAddress').textContent, 'Not connected');
});


test('session timer ticks from sensor start, freezes when closed and clears on account change', () => {
    const f = fixture();
    f.select('user_1');
    f.context.now = Date.parse('2026-10-12T10:01:49Z');
    f.run('Date.now = () => now');
    f.context.active = { sessionId: 'timer-session', stage: 'RUNNING', settled: false, sampleCount: 1095,
        iotSession: { started_at: '2026-10-12T10:00:00Z', ended_at: null, durationS: 109 } };
    f.run('walletSessions = [active]; renderWallet()');
    assert.equal(f.elements.get('walletSessionTimer').textContent, '00:01:49');
    f.context.now += 1000;
    f.run('renderWalletSessionTimer()');
    assert.equal(f.elements.get('walletSessionTimer').textContent, '00:01:50');
    assert.match(f.elements.get('walletSessionTimerDetail').textContent, /1,095 samples/);
    f.run("active.stage = 'SETTLEMENT_PENDING'; active.iotSession.ended_at = '2026-10-12T10:01:50Z'; renderWalletSessionTimer()");
    f.context.now += 60000;
    f.run('renderWalletSessionTimer()');
    assert.equal(f.elements.get('walletSessionTimer').textContent, '00:01:50');
    assert.match(f.elements.get('walletSessionTimerDetail').textContent, /Stopped/);
    f.run("active.stage = 'SETTLED'; active.settled = true; renderWalletSessionTimer()");
    assert.equal(f.elements.get('walletSessionTimer').textContent, '00:01:50');
    f.run('clearWalletSession(); renderWallet()');
    assert.equal(f.elements.get('walletSessionTimer').textContent, '--:--:--');
});

test('session timer never extrapolates missing or malformed sensor timestamps', () => {
    const f = fixture();
    f.select('user_1');
    f.run("walletSessions = [{stage: 'RUNNING', settled: false, durationS: 45}]; renderWallet()");
    assert.equal(f.elements.get('walletSessionTimer').textContent, '00:00:45');
    f.run("walletSessions[0].iotSession = {started_at: 'invalid', durationS: NaN}; walletSessions[0].durationS = null; renderWalletSessionTimer()");
    assert.equal(f.elements.get('walletSessionTimer').textContent, '--:--:--');
});

test('V2 task session guidance uses its snapshotted duration and 10 Hz sample threshold', () => {
    const f = fixture();
    f.select('user_1');
    f.run(`wallet = { success: true, address: '0x1111', balance: '20', allowance: '2',
            requiredAllowance: '2', stakeAmount: '0.2', chainId: 31337, canStart: true,
            rewardProtocolVersion: 'task-rewards-v2', device: { ready: true, deviceId: 'esp32_01' } };
        walletSessions = [{ sessionId: 'task-session', taskId: 'task-1', assignmentId: 'asg-1',
            taskSnapshot: { task: { completionRule: { minimumDurationS: 120 } } },
            stage: 'RUNNING', settled: false, sampleCount: 900,
            iotSession: { started_at: '2026-10-12T10:00:00Z', ended_at: null } }];
        renderWallet()`);
    assert.match(f.elements.get('walletSessionTimerDetail').textContent, /Minimum 120s/);
    assert.match(f.elements.get('walletSessionTimerDetail').textContent, /about 1,080 samples at 10 Hz/);
    assert.match(f.elements.get('walletSensorStatus').textContent, /120 seconds \/ 1,080 samples/);
});

test('V2 outcome labels use actual payout and do not misreport refunds as rewarded', () => {
    const f = fixture();
    for (const [outcomeCode, rewardPaid, penaltyCollected, expected] of [
        [0, '0.002', '0', 'REWARDED'], [0, '0', '0', 'COMPLETED · NO REWARD'],
        [1, '0', '0.010', 'PENALTY'], [2, '0', '0', 'REFUNDED · NO REWARD'],
        [3, '0', '0', 'REFUNDED · NO REWARD'], [4, '0', '0', 'REFUNDED · NO REWARD'],
        [5, '0', '0', 'REFUNDED · NO REWARD']
    ]) {
        f.context.labelSession = { settled: true, rewardProtocolVersion: 'task-rewards-v2',
            settlementStatusCode: null, outcomeCode, rewardPaid, penaltyCollected };
        assert.equal(f.run('walletResultLabel(labelSession)'), expected);
    }
    assert.equal(f.run('walletOutcomeName(4)'), 'OWNER CANCELLED');
    assert.equal(f.run('walletOutcomeName(5)'), 'SYSTEM CANCELLED');
    assert.equal(f.run('walletResultLabel({settled:true,settlementStatusCode:null})'), 'NO REWARD');
    assert.equal(f.run('walletResultLabel({settled:true,settlementStatusCode:1})'), 'DAILY LIMIT');
});

function setTaskWallet(f, overrides = {}) {
    f.context.taskWalletData = { ...wallet('user_1'), allowance: '0.2', requiredAllowance: '0.2', stakeAmount: '0.2',
        rewardProtocolVersion: 'task-rewards-v2', device: { ready: true, operatorReady: true }, ...overrides };
    f.run('wallet = taskWalletData; renderWallet()');
}

function selectAssignedTask(f) {
    f.run(`walletTasks = [{taskId: 'task-local', title: 'Local task', status: 'OPEN'}];
        walletAssignments = [{taskId: 'task-local', status: 'ASSIGNED'}];
        selectWalletTask('task-local')`);
}

test('approved V2 wallet keeps Start visible with actionable empty-task and allowance explanations', async () => {
    let writes = 0;
    const f = fixture(async () => { writes++; return reply({success:true}); });
    f.select('user_1'); setTaskWallet(f);
    const start = f.elements.get('walletStartBtn');
    assert.equal(start.hidden, false);
    assert.equal(start.disabled, true);
    assert.equal(start.textContent, 'Start Task Session');
    assert.equal(start.attrs.get('aria-describedby'), 'walletStartStatus');
    assert.match(f.elements.get('walletStartStatus').textContent, /admin must create and publish/i);
    assert.match(f.elements.get('walletTaskList').children[0].textContent, /admin must create and publish/i);
    assert.equal(f.elements.get('walletApproveBtn').disabled, true);
    assert.equal(f.elements.get('walletApproveBtn').textContent, 'Approved 0.2 HRC');
    assert.match(f.elements.get('walletApprovalStatus').textContent, /No further approval is needed/);
    await f.run('startWalletSession()');
    await f.run('approveWallet()');
    assert.equal(writes, 0);
    assert.equal(f.run('walletSessionRef'), null);
    assert.equal(f.storage.has('hrc-wallet-pending-user_1'), false);
});

test('Start requires a selected usable assignment, not just an open task or a stale task id', async () => {
    const f = fixture(); f.select('user_1'); setTaskWallet(f);
    f.run("walletTasks = [{taskId: 'task-local', title: 'Task', status: 'OPEN'}]; selectedTaskId = 'stale-task'; renderWallet()");
    assert.equal(f.elements.get('walletStartBtn').hidden, false);
    assert.equal(f.elements.get('walletStartBtn').disabled, true);
    assert.match(f.elements.get('walletStartStatus').textContent, /Accept a task and select it/);
    selectAssignedTask(f);
    assert.equal(f.elements.get('walletStartBtn').disabled, false);
    assert.match(f.elements.get('walletStartStatus').textContent, /separate MetaMask transaction.*0.2 HRC/);
    f.run("walletAssignments[0].status = 'COMPLETED'; renderWallet()");
    assert.equal(f.elements.get('walletStartBtn').disabled, true);
});

test('V2 Accept selects the server assignment and enables Start only when wallet/sensor are ready', async () => {
    let accepts = 0;
    const f = fixture(async url => {
        assert.match(url, /\/tasks\/task-local\/accept$/); accepts++;
        return reply({success:true, assignment:{taskId:'task-local', status:'ASSIGNED'}});
    });
    f.select('user_1'); setTaskWallet(f);
    f.run("walletTasks = [{taskId:'task-local', title:'Task', status:'OPEN'}]; renderWallet()");
    const accept = f.elements.get('walletTaskList').children[0].children[1];
    assert.equal(accept.textContent, 'Accept task');
    await accept.listeners.get('click')();
    assert.equal(accepts, 1);
    assert.equal(f.run('selectedTaskId'), 'task-local');
    assert.equal(f.elements.get('walletStartBtn').disabled, false);
    assert.equal(f.storage.get('hrc-selected-task-user_1'), 'task-local');
    assert.match(f.elements.get('walletMessage').textContent, /0.2 HRC/);
});

test('selected tasks display concrete approval, balance, pool, operator and sensor blockers', () => {
    const f = fixture(); f.select('user_1'); setTaskWallet(f); selectAssignedTask(f);
    for (const [overrides, message] of [
        [{allowance:'0', canStart:false, canStartReason:'APPROVAL_REQUIRED'}, /Approve 0.2 HRC/],
        [{canStart:false, canStartReason:'INSUFFICIENT_HRC_BALANCE'}, /Not enough HRC/],
        [{canStart:false, canStartReason:'REWARD_POOL_EMPTY'}, /Reward pool is empty/],
        [{canStart:false, device:{ready:true, operatorReady:false}}, /operator is not ready/],
        [{canStart:false, device:{ready:true, busySessionId:'other'}}, /reserved by another/],
        [{canStart:false, device:{ready:false, reason:'Telemetry is stale'}}, /Sensor unavailable: Telemetry is stale/],
        [{canStart:false}, /Refresh to check/]
    ]) {
        setTaskWallet(f, overrides);
        assert.equal(f.elements.get('walletStartBtn').hidden, false);
        assert.equal(f.elements.get('walletStartBtn').disabled, true);
        assert.match(f.elements.get('walletStartStatus').textContent, message);
    }
});

test('failed task refresh clears stale assignments and stays blocked with the original error', async () => {
    let fail = true;
    const f = fixture(async url => {
        if (url.endsWith('/wallet')) return reply({...wallet('user_1'), allowance:'0.2', requiredAllowance:'0.2', stakeAmount:'0.2', rewardProtocolVersion:'task-rewards-v2'});
        if (url.endsWith('/api/tasks')) return fail ? reply({success:false, error:'Task database unavailable'}, 503)
            : reply({success:true, tasks:[{taskId:'task-local', title:'Task', status:'OPEN'}]});
        if (url.endsWith('/assignments')) return reply({success:true, assignments:[{taskId:'task-local', status:'ASSIGNED'}]});
        return reply({success:true, sessions:[], transactions:[]});
    });
    f.select('user_1'); setTaskWallet(f); selectAssignedTask(f);
    await f.run('loadWalletAccountData({fresh:true})');
    assert.equal(f.run('walletAssignments.length'), 0);
    assert.equal(f.run('selectedTaskId'), null);
    assert.equal(f.elements.get('walletStartBtn').disabled, true);
    assert.match(f.elements.get('walletStartStatus').textContent, /Task service unavailable: Task database unavailable/);
    assert.match(f.elements.get('walletMessage').textContent, /Task database unavailable/);
    fail = false;
    await f.run('loadWalletAccountData({fresh:true})');
    assert.equal(f.elements.get('walletStartBtn').disabled, false);
    assert.equal(f.run('walletTaskError'), null);
    assert.doesNotMatch(f.elements.get('walletMessage').textContent, /Approve 2 HRC/);
    assert.match(f.elements.get('walletMessage').textContent, /0.2 HRC/);
    f.run("walletTaskError = 'old error'; clearWalletSession()");
    assert.equal(f.run('walletTaskError'), null);
});

test('V2 pending retry keeps the same reference but cannot open a second active session', () => {
    const f = fixture(); f.select('user_1'); setTaskWallet(f); selectAssignedTask(f);
    f.run("walletSessionRef = 'same-ref'");
    setTaskWallet(f, {allowance:'0', canStart:false, canStartReason:'SESSION_IN_PROGRESS'});
    assert.equal(f.elements.get('walletStartBtn').disabled, false);
    assert.match(f.elements.get('walletStartStatus').textContent, /same session/);
    f.run("walletSessions = [{sessionId:'active', stage:'RUNNING', settled:false}]; renderWallet()");
    assert.equal(f.elements.get('walletStartBtn').disabled, true);
    assert.match(f.elements.get('walletStartStatus').textContent, /already in progress/);
});


test('task completion is visible before End without preventing a sensor stop', () => {
    const f = fixture(); f.select('user_1'); setTaskWallet(f);
    f.run(`walletSessions = [{ sessionId: 'task-session', taskId: 'task-1', stage: 'RUNNING', settled: false,
        stakedAmount: '0.2', taskSnapshot: { task: { completionRule: { requiredCycles: 20, minimumDurationS: 30 } } },
        taskCompletion: { status: 'MISSING', requiredCycles: 20, observedCycles: null } }]; renderWallet()`);
    const status = f.elements.get('walletCompletionStatus');
    assert.equal(status.hidden, false);
    assert.match(status.textContent, /20 cycles.*before End.*no reward/);
    assert.equal(f.elements.get('walletEndBtn').disabled, false);
    f.run("walletSessions[0].taskCompletion = {status:'APPROVED',requiredCycles:20,observedCycles:20}; renderWallet()");
    assert.match(status.textContent, /verified by the operator: 20\/20 cycles/);
    f.run("walletSessions[0].taskCompletion = {status:'INSUFFICIENT',requiredCycles:20,observedCycles:12}; renderWallet()");
    assert.match(status.textContent, /12\/20 cycles.*without reward/);
});

test('HRC Reports displays refunded sessions and receipt-derived stake transactions even with no safety events', () => {
    const f = fixture(); f.select('user_1'); setTaskWallet(f);
    f.run(`walletSessions = [{ sessionId: 'missing-cycles', userId:'user_1', taskId:'task-1', stage:'SETTLED', settled:true,
        outcomeCode:3, stakeReturned:'0.2', rewardPaid:'0', penaltyCollected:'0', blockNumber:29,
        outcomeReason:'task_completion_evidence_missing' }];
        walletTransactions = [{action:'STAKE',walletDelta:'-0.2',lockedDelta:'0.2',blockNumber:28,events:[{name:'SessionStarted',amount:'0.2'}]}];
        renderWallet()`);
    const rows = f.elements.get('reportSessionsTableBody').children;
    assert.equal(rows.length, 1);
    assert.equal(rows[0].children.length, 9);
    assert.equal(rows[0].children[1].textContent, 'missing-cycles');
    assert.match(rows[0].children[4].textContent, /REFUNDED.*Operator did not approve.*before End/);
    assert.equal(rows[0].children[5].textContent, '0.2 HRC');
    assert.equal(f.elements.get('reportWalletTransactions').hidden, false);
    const detail = f.elements.get('reportWalletTransactionsTableBody').children[0].children[0].textContent;
    assert.match(detail, /Available −0.2 HRC · Locked \+0.2 HRC/);
    assert.equal(f.run("walletLedgerDescription({action:'STAKE',lockedDelta:'2',events:[]})"), 'Available −2 HRC · Locked +2 HRC · Live session started');
    assert.match(f.elements.get('walletCompletionStatus').textContent, /Operator did not approve/);
    f.run("selectedTaskId = 'task-1'; renderWallet()");
    assert.match(f.elements.get('walletCompletionStatus').textContent, /Operator did not approve/);
    f.run('clearWalletSession(); renderWallet()');
    assert.equal(f.elements.get('reportSessionsTableBody').children[0].children[0].textContent, 'No sessions recorded yet.');
    assert.equal(f.elements.get('reportWalletTransactions').hidden, true);
});

test('admin HRC reports refresh independently of customer wallet routes and report failures visibly', async () => {
    let fail = false;
    const f = fixture(async url => {
        if (url.endsWith('/api/admin/wallet/sessions')) return fail ? reply({success:false,error:'RPC unavailable'},503)
            : reply({success:true,sessions:[{sessionId:'refund-all-accounts',userId:'user_2',settled:true,stage:'SETTLED',outcomeCode:3,stakeReturned:'0.2',rewardPaid:'0',penaltyCollected:'0'}]});
        assert.ok(!url.endsWith('/api/me/wallet') && !url.endsWith('/api/me/sessions'));
        return reply({success:true,tasks:[],sessions:[]});
    });
    f.select('admin'); await f.run('loadWalletAccountData()');
    assert.equal(f.elements.get('reportSessionsTableBody').children[0].children[1].textContent, 'refund-all-accounts');
    assert.match(f.elements.get('reportRewardMessage').textContent, /All accounts.*1 HRC sessions/);
    fail = true; await f.run('loadWalletAccountData({fresh:true})');
    assert.match(f.elements.get('reportRewardMessage').textContent, /Unable to load HRC reports: RPC unavailable/);
    assert.equal(f.run('reportSessions.length'), 0);
});

test('switching accounts during an admin report request cannot expose earlier account reports', async () => {
    const hold = deferred();
    const f = fixture(async url => {
        if (url.endsWith('/api/admin/wallet/sessions')) {
            await hold.promise;
            return reply({success:true,sessions:[{sessionId:'admin-only-session',userId:'user_2'}]});
        }
        return reply(url.endsWith('/wallet') ? wallet('user_1') : {success:true,sessions:[],transactions:[]});
    });
    f.select('admin'); const oldRead = f.run('loadWalletAccountData()');
    f.run('clearWalletSession()'); f.select('user_1');
    await f.run('loadWalletAccountData()'); hold.resolve(); await oldRead;
    assert.equal(f.run('reportSessions.length'), 0);
    assert.doesNotMatch(f.elements.get('reportSessionsTableBody').children[0].children[0].textContent, /admin-only-session/);
});

test('settled assignments stop presenting a stale selection and direct users to another task', async () => {
    const f = fixture(async url => {
        if (url.endsWith('/wallet')) return reply({...wallet('user_1'), rewardProtocolVersion:'task-rewards-v2'});
        if (url.endsWith('/api/tasks')) return reply({success:true,tasks:[{taskId:'task-local',title:'Task',status:'OPEN'}]});
        if (url.endsWith('/assignments')) return reply({success:true,assignments:[{taskId:'task-local',status:'SETTLED'}]});
        return reply({success:true,sessions:[],transactions:[]});
    });
    f.select('user_1'); setTaskWallet(f); selectAssignedTask(f);
    await f.run('loadWalletAccountData({fresh:true})');
    assert.equal(f.run('selectedTaskId'), null);
    assert.equal(f.elements.get('walletTaskList').children[0].children[1].textContent, 'SETTLED · choose another task');
    assert.equal(f.elements.get('walletTaskList').children[0].children[1].disabled, true);
    assert.equal(f.elements.get('walletStartBtn').disabled, true);
});
