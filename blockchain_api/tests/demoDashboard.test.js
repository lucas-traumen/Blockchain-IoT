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
        constructor() { this.children = []; this.textContent = ''; this.disabled = false; this.hidden = false; this.value = ''; this.style = {}; this.dataset = {}; this.attrs = new Map(); this.classes = new Set();
            this.classList = { toggle: (name, on) => on ? this.classes.add(name) : this.classes.delete(name), contains: name => this.classes.has(name), add: name => this.classes.add(name), remove: name => this.classes.delete(name) }; }
        appendChild(child) { this.children.push(child); return child; }
        append(...children) { this.children.push(...children); }
        replaceChildren(...children) { this.children = children; }
        setAttribute(name, value) { this.attrs.set(name, value); }
        removeAttribute(name) { this.attrs.delete(name); }
        focus() {}
        querySelector() { return null; }
        cloneNode() { return new Element(); }
    }
    const elements = new Map([...html.matchAll(/id="([^"]+)"/g)].map(match => [match[1], new Element()]));
    const document = { getElementById: id => elements.get(id) || null, createElement: () => new Element(), createTextNode: value => ({ textContent: value }), querySelector: () => new Element(), querySelectorAll: () => [], body: new Element() };
    const context = vm.createContext({
        console, URL, AbortSignal, fetch: fetchImpl, document,
        window: { location: { protocol: 'http:', href: 'http://localhost:8080/', hash: '' }, setTimeout: () => 1 },
        sessionStorage: { getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) },
        text: (id, value) => { const el = elements.get(id); if (el) el.textContent = String(value ?? '--'); },
        liveStatus: () => 'LIVE', setState() {}, renderTransactions() {}, renderAppIdentity() {}, renderUserPortal() {}, renderCustomers() {}
    });
    vm.runInContext(source.slice(0, source.indexOf('async function appRequest(')), context);
    // Exercise the actual refresh-to-render call chain; a legacy render used to overwrite demo balances.
    vm.runInContext(source.slice(source.indexOf('function rewardStatusClass('), source.indexOf('async function loadRewards(')), context);
    vm.runInContext(source.slice(source.indexOf('function syncSidebarViews('), source.indexOf('async function loadTransactions(')), context);
    const run = code => vm.runInContext(code, context);
    const select = id => { context.loginUser = users[id]; run('demoSession = {token: loginUser.userId + "-token", user: loginUser};'); };
    const setWallet = id => { context.walletData = wallet(id); run('demoWallet = walletData; renderDemoWallet();'); };
    return { context, elements, run, select, setWallet, storage };
}

test('dashboard refresh preserves the selected demo wallet identity and balance', () => {
    const f = fixture(); f.select('user_1'); f.setWallet('user_1');
    const before = f.elements.get('rewardBalance').textContent;
    f.run('syncSidebarViews()');
    assert.equal(f.elements.get('rewardBalance').textContent, before);
    assert.match(f.elements.get('walletUserStatus').textContent, /user_1/);
});

test('admin has reports but no transaction controls; users have their own actions', () => {
    const f = fixture(); f.select('admin'); f.run('renderDemoWallet()');
    assert.equal(f.elements.get('demoApproveBtn').hidden, true);
    assert.equal(f.elements.get('demoStartBtn').hidden, true);
    f.select('user_2'); f.setWallet('user_2');
    assert.equal(f.elements.get('demoApproveBtn').hidden, false);
    assert.equal(f.elements.get('demoStartBtn').hidden, false);
});

test('a delayed read for the previous account cannot replace the current user wallet', async () => {
    const old = deferred();
    const f = fixture(async (url, options) => {
        const id = options.headers.Authorization.includes('user_1') ? 'user_1' : 'user_2';
        if (id === 'user_1') await old.promise;
        return reply(url.endsWith('/wallet') ? wallet(id) : { success: true, sessions: [], transactions: [] });
    });
    f.select('user_1'); const pending = f.run('loadDemoAccountData()');
    f.select('user_2'); await f.run('loadDemoAccountData()');
    old.resolve(); await pending;
    assert.equal(f.elements.get('demoWalletAddress').textContent, users.user_2.walletAddress);
    assert.equal(f.elements.get('demoAccountButtonLabel').textContent, 'user_2');
});

test('double-clicking Start submits once and leaves the consumed allowance disabled', async () => {
    const hold = deferred(); let starts = 0;
    const f = fixture(async url => {
        if (url.endsWith('/start')) { starts++; await hold.promise; return reply({ success: true, started: true, sessionId: 'demo-user_1-run', transactionHash: '0xabc', blockNumber: 12, event: { name: 'SessionStarted' } }); }
        if (url.endsWith('/wallet')) return reply({ ...wallet('user_1'), allowance: '0', canStart: false });
        return reply({ success: true, sessions: [], transactions: [] });
    });
    f.select('user_1'); f.setWallet('user_1');
    const first = f.run('startDemoSession()'); const second = f.run('startDemoSession()');
    await new Promise(resolve => setImmediate(resolve));
    const sent = starts;
    hold.resolve(); await Promise.all([first, second]);
    assert.equal(sent, 1);
    assert.equal(f.elements.get('demoStartBtn').disabled, true);
    assert.equal(f.elements.get('demoApproveBtn').disabled, false);
});

test('an expired demo login clears previous wallet data and session storage', async () => {
    const f = fixture(async () => reply({ success: false, error: 'Expired' }, 401));
    f.select('user_1'); f.setWallet('user_1');
    f.storage.set('hrc-demo-session', JSON.stringify({ token: 'user_1-token', user: users.user_1 }));
    await f.run('loadDemoAccountData()');
    assert.equal(f.elements.get('demoWalletAddress').textContent, '--');
    assert.equal(f.elements.get('demoAccountButtonLabel').textContent, 'Choose account');
    assert.equal(f.storage.has('hrc-demo-session'), false);
});

test('a lost start response keeps the same session reference after a page reload, even with no remaining allowance', async () => {
    const storage = new Map(); let firstBody;
    const first = fixture(async (url, options) => {
        if (url.endsWith('/start')) { firstBody = JSON.parse(options.body); throw new Error('Response lost after broadcast'); }
        return reply({ success: true });
    }, storage);
    storage.set('hrc-demo-session', JSON.stringify({ token: 'user_1-token', user: users.user_1 }));
    first.select('user_1'); first.setWallet('user_1');
    await first.run('startDemoSession()');
    assert.equal(storage.get('hrc-demo-pending-user_1'), firstBody.sessionRef);
    let secondBody;
    const reloaded = fixture(async (url, options) => {
        if (url.endsWith('/start')) { secondBody = JSON.parse(options.body); return reply({ success: true, started: true, duplicate: true, sessionId: 'demo-user_1-retry', events: [{ name: 'SessionStarted' }] }); }
        return reply(url.endsWith('/wallet') ? { ...wallet('user_1'), allowance: '0', canStart: false } : { success: true, sessions: [], transactions: [] });
    }, storage);
    reloaded.run('restoreDemoSession()');
    await reloaded.run('loadDemoAccountData()');
    assert.equal(reloaded.elements.get('demoStartBtn').disabled, false);
    assert.match(reloaded.elements.get('demoStartBtn').textContent, /Retry/);
    await reloaded.run('startDemoSession()');
    assert.equal(secondBody.sessionRef, firstBody.sessionRef);
    assert.equal(storage.has('hrc-demo-pending-user_1'), false);
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
    f.run('demoWallet = walletData; renderDemoWallet()');
    const reading = f.run('loadDemoAccountData()');
    const approving = f.run('approveDemoWallet()');
    await approved.promise;
    oldRead.resolve(); await Promise.all([reading, approving]);
    assert.equal(walletReads, 2);
    assert.equal(f.elements.get('demoWalletAllowance').textContent, '2 HRC');
    assert.equal(f.elements.get('demoApproveBtn').disabled, true);
    assert.equal(f.elements.get('demoStartBtn').disabled, false);
});

test('the account picker contains only admin and two users, and defaults to the current account', async () => {
    const f=fixture(async()=>reply({success:true,accounts:Object.values(users)}));
    f.select('user_2');await f.run('loadDemoAccounts()');
    const select=f.elements.get('demoAccountSelect');
    assert.deepEqual(select.children.map(option=>option.value),['admin','user_1','user_2']);
    assert.deepEqual(select.children.map(option=>option.textContent),['admin','user_1','user_2']);
    assert.equal(select.value,'user_2');assert.equal(f.elements.get('demoLoginBtn').disabled,false);
    f.run('clearDemoSession(); setDemoAccountModal(true)');
    assert.equal(select.value,'admin');
});

test('End sends only the owning session reference and displays refunded stake, reward and wallet activity', async () => {
    let endRequests=0,body;
    const id='demo-user_1-sensor';
    const settled={sessionId:id,started:true,settled:true,stage:'SETTLED',stakedAmount:'2',stakeReturned:'2',rewardPaid:'3',penaltyCollected:'0',settlementStatusCode:0,outcomeCode:0};
    const f=fixture(async(url,options)=>{
        if(url.endsWith('/end')){endRequests++;body=JSON.parse(options.body);return reply({success:true,...settled});}
        if(url.endsWith('/wallet'))return reply({...wallet('user_1'),balance:'23',lockedBalance:'0',canStart:false});
        if(url.endsWith('/transactions'))return reply({success:true,transactions:[{action:'SETTLEMENT',walletDelta:'5',lockedDelta:'-2',stakeReturned:'2',rewardPaid:'3',penaltyCollected:'0',blockNumber:14,transactionHash:'0xsettle',events:[{name:'SessionSettled'}]}]});
        return reply({success:true,sessions:[settled]});
    });
    f.select('user_1');f.setWallet('user_1');
    f.context.active={sessionId:id,started:true,settled:false,stage:'RUNNING',stakedAmount:'2',sampleCount:40,iotSession:{durationS:30}};
    f.run('demoSessions=[active]; renderDemoWallet()');
    assert.equal(f.elements.get('demoEndBtn').hidden,false);
    await f.run('endDemoSession()');
    assert.equal(endRequests,1);assert.deepEqual(body,{});
    assert.equal(f.elements.get('demoWalletBalance').textContent,'23 HRC');
    assert.match(f.elements.get('demoSessionState').textContent,/Stake returned \+2 HRC/);
    const entry=f.elements.get('demoRecentActivity').children[0];
    assert.equal(entry.children[1].textContent,'+5 HRC');
    assert.match(entry.children[2].textContent,/Stake returned \+2 HRC · Reward \+3 HRC/);
});

test('penalty ledger reports loss of locked stake without an additional wallet debit', () => {
    const f=fixture();f.context.penalty={action:'SETTLEMENT',walletDelta:'0',lockedDelta:'-2',penaltyCollected:'2',events:[{name:'SessionSettled'}]};
    assert.match(f.run('demoLedgerDescription(penalty)'),/Penalty 2 HRC from locked stake · no additional wallet charge/);
    f.select('admin');f.run('renderDemoWallet()');
    assert.equal(f.elements.get('demoEndBtn').hidden,true);
});


test('Open closes the authenticated picker and displays a wallet read failure in the workspace', async () => {
    const f=fixture(async url => {
        if(url.endsWith('/login')) return reply({success:true,token:'user_1-token',user:users.user_1});
        return reply({success:false,error:'Local RPC is unavailable'},503);
    });
    f.elements.get('demoAccountSelect').value='user_1';
    f.run('setDemoAccountModal(true)');
    f.elements.get('demoAccountSelect').value='user_1';
    await f.run('loginDemoAccount()');
    assert.equal(f.elements.get('demoAccountModal').classList.contains('hidden'),true);
    assert.equal(f.elements.get('demoAccountButtonLabel').textContent,'user_1');
    assert.equal(f.elements.get('demoWalletAddress').textContent,users.user_1.walletAddress);
    assert.match(f.elements.get('demoWalletMessage').textContent,/Local RPC is unavailable/);
    assert.equal(f.elements.get('demoStartBtn').disabled,true);
});

test('an HTML proxy response is reported as a stale or unavailable local demo instead of a JSON parser error', async () => {
    const f = fixture(async url => {
        if (url.endsWith('/login')) return reply({success:true,token:'user_2-token',user:users.user_2});
        return htmlReply(502);
    });
    f.elements.get('demoAccountSelect').value='user_2';
    await f.run('loginDemoAccount()');
    assert.match(f.elements.get('demoWalletMessage').textContent, /Blockchain API returned HTML \(HTTP 502\)/);
    assert.match(f.elements.get('demoWalletMessage').textContent, /run_local_demo\.sh/);
    assert.doesNotMatch(f.elements.get('demoWalletMessage').textContent, /Unexpected token/);
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
    const result = await f.run("demoRequest('/api/me/wallet')");
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
    await f.run('loadDemoAccountData()');
    assert.equal(f.elements.get('demoWalletBalance').textContent, '20 HRC');
    assert.doesNotMatch(f.elements.get('demoWalletMessage').textContent, /Unable to/);
});

test('a recovered Start clears its pending reference after refresh without another stake request', async () => {
    const f=fixture(async url => {
        if(url.endsWith('/wallet')) return reply({...wallet('user_1'),allowance:'0',canStart:false});
        if(url.endsWith('/sessions')) return reply({success:true,sessions:[{sessionId:'demo-user_1-recovered',sessionRef:'recovered',stage:'RUNNING',started:true,settled:false,source:'esp32'}]});
        return reply({success:true,transactions:[]});
    });
    f.select('user_1');
    f.storage.set('hrc-demo-pending-user_1','recovered');
    f.run('demoSessionRef="recovered"');
    await f.run('loadDemoAccountData()');
    assert.equal(f.storage.has('hrc-demo-pending-user_1'),false);
    assert.equal(f.elements.get('demoStartBtn').disabled,true);
    assert.equal(f.elements.get('demoStartBtn').textContent,'Start Session');
});


test('a restored wallet and a successful retry replace the stale choose-account or read-error message', async () => {
    let available = true;
    const storage = new Map([['hrc-demo-session', JSON.stringify({ token: 'user_2-token', user: users.user_2 })]]);
    const f = fixture(async url => {
        if (!available) return reply({ success: false, error: 'RPC unavailable' }, 503);
        return reply(url.endsWith('/wallet') ? wallet('user_2') : { success: true, sessions: [], transactions: [] });
    }, storage);
    f.run('restoreDemoSession()');
    await f.run('loadDemoAccountData()');
    assert.doesNotMatch(f.elements.get('demoWalletMessage').textContent, /Choose user_/);
    available = false;
    await f.run('loadDemoAccountData({ fresh: true })');
    assert.match(f.elements.get('demoWalletMessage').textContent, /Unable to refresh/);
    available = true;
    await f.run('loadDemoAccountData({ fresh: true })');
    assert.doesNotMatch(f.elements.get('demoWalletMessage').textContent, /Unable to refresh/);
});

test('anonymous wallet mode preserves the connect label across dashboard refreshes', () => {
    const f = fixture();
    f.run('appAuthMode = "siwe"');
    f.elements.get('demoAccountButtonLabel').textContent = 'Connect wallet';
    f.run('syncSidebarViews()');
    assert.equal(f.elements.get('demoAccountButtonLabel').textContent, 'Connect wallet');
    assert.equal(f.elements.get('connectedWalletAddress').textContent, 'Not connected');
});


test('session timer ticks from sensor start, freezes when closed and clears on account change', () => {
    const f = fixture();
    f.select('user_1');
    f.context.now = Date.parse('2026-10-12T10:01:49Z');
    f.run('Date.now = () => now');
    f.context.active = { sessionId: 'timer-session', stage: 'RUNNING', settled: false, sampleCount: 1095,
        iotSession: { started_at: '2026-10-12T10:00:00Z', ended_at: null, durationS: 109 } };
    f.run('demoSessions = [active]; renderDemoWallet()');
    assert.equal(f.elements.get('demoSessionTimer').textContent, '00:01:49');
    f.context.now += 1000;
    f.run('renderDemoSessionTimer()');
    assert.equal(f.elements.get('demoSessionTimer').textContent, '00:01:50');
    assert.match(f.elements.get('demoSessionTimerDetail').textContent, /1,095 samples/);
    f.run("active.stage = 'SETTLEMENT_PENDING'; active.iotSession.ended_at = '2026-10-12T10:01:50Z'; renderDemoSessionTimer()");
    f.context.now += 60000;
    f.run('renderDemoSessionTimer()');
    assert.equal(f.elements.get('demoSessionTimer').textContent, '00:01:50');
    assert.match(f.elements.get('demoSessionTimerDetail').textContent, /Stopped/);
    f.run("active.stage = 'SETTLED'; active.settled = true; renderDemoSessionTimer()");
    assert.equal(f.elements.get('demoSessionTimer').textContent, '00:01:50');
    f.run('clearDemoSession(); renderDemoWallet()');
    assert.equal(f.elements.get('demoSessionTimer').textContent, '--:--:--');
});

test('session timer never extrapolates missing or malformed sensor timestamps', () => {
    const f = fixture();
    f.select('user_1');
    f.run("demoSessions = [{stage: 'RUNNING', settled: false, durationS: 45}]; renderDemoWallet()");
    assert.equal(f.elements.get('demoSessionTimer').textContent, '00:00:45');
    f.run("demoSessions[0].iotSession = {started_at: 'invalid', durationS: NaN}; demoSessions[0].durationS = null; renderDemoSessionTimer()");
    assert.equal(f.elements.get('demoSessionTimer').textContent, '--:--:--');
});
