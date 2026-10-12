const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { ethers } = require('ethers');
const { WalletService } = require('../walletService');
const { JsonSessionStore } = require('../sessionStore');
const { LiveSessionService } = require('../liveSessionService');
const { ApiError } = require('../eventPayload');

const USER1 = '0x0000000000000000000000000000000000000011';
const USER2 = '0x0000000000000000000000000000000000000022';
const TOKEN = '0x0000000000000000000000000000000000000033';
const REWARDS = '0x0000000000000000000000000000000000000044';
const OPERATOR = '0x0000000000000000000000000000000000000055';
const unit = ethers.parseEther;
const eventAbi = new ethers.Interface([
    'event SessionStarted(bytes32 indexed sessionId,address indexed beneficiary,uint256 amount,uint256 startedAt)',
    'event SessionSettled(bytes32 indexed sessionId,address indexed beneficiary,uint8 outcome,uint8 settlementStatus,uint256 stakeReturned,uint256 rewardPaid,uint256 penaltyCollected,bytes32 evidenceHash,uint256 timestamp,uint256 endedAt)'
]);
const rewardAbi = new ethers.Interface([
    'function startSession(bytes32 sessionId)',
    'event SessionStarted(bytes32 indexed sessionId,address indexed beneficiary,uint256 amount,uint256 startedAt)',
    'event SessionSettled(bytes32 indexed sessionId,address indexed beneficiary,uint8 outcome,uint8 settlementStatus,uint256 stakeReturned,uint256 rewardPaid,uint256 penaltyCollected,bytes32 evidenceHash,uint256 timestamp,uint256 endedAt)'
]);
const empty = { beneficiary: ethers.ZeroAddress, startedAt: 0n, endedAt: 0n, settledAt: 0n, stakedAmount: 0n, rewardPaid: 0n, penaltyCollected: 0n,
    outcome: 0, settlementStatus: 0, evidenceHash: ethers.ZeroHash, outcomeTimestamp: 0n, started: false, settled: false };

function fixture(t) {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'hrc-live-session-'));
    t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
    const storePath = path.join(directory, 'sessions.json');
    const state = { now: Date.parse('2026-10-11T10:00:00Z'), balances: new Map([[USER1,unit('20')],[USER2,unit('20')]]), allowances: new Map([[USER1,unit('2')],[USER2,unit('2')]]),
        tuples: new Map(), logs: [], stakes: 0, stops: 0, sensorStarts: 0, block: 1, settlementCalls: [], ready: true, busy: null,
        failStake: null, failSettlement: false, loseSensorStart: false, outcome: 'VALID_NO_VIOLATION', sensors: new Map() };
    const users = new Map([['user_1', USER1], ['user_2', USER2]].map(([userId, address]) => [userId, { userId, walletAddress: address, wallet: { address }, role: 'customer', walletFirst: true }]));
    const provider = { getNetwork: async () => ({ chainId: 31337 }), getBalance: async () => unit('1'), getCode: async () => '0x1234',
        getTransaction: async hash => state.walletTransactions?.[hash] || null,
        getTransactionReceipt: async hash => state.walletReceipts?.[hash] || null,
        getBlock: async () => ({ timestamp: Math.floor(state.now / 1000) }),
        getBlockNumber: async () => state.block,
        getLogs: async filter => state.logs.filter(log => log.address === filter.address && filter.topics.every((topic,i) => topic == null || (Array.isArray(topic) ? topic.includes(log.topics[i]) : topic === log.topics[i]))) };
    const token = { balanceOf: async address => state.balances.get(address), allowance: async address => state.allowances.get(address), symbol: async () => 'HRC', decimals: async () => 18 };
    const emit = (name,args) => { state.block++; state.logs.push({ ...eventAbi.encodeEventLog(eventAbi.getEvent(name),args), address: REWARDS, transactionHash: ethers.id('tx-'+state.block), blockNumber: state.block, index: 0 }); };
    const rewards = { stakeAmount: async () => unit('2'), getSession: async key => state.tuples.get(key) || empty,
        connect: wallet => ({ startSession: async key => {
            if (state.failStake) throw new ApiError(503, state.failStake, 'Stake could not be prepared');
            state.stakes++;
            return { hash: ethers.id('tx-'+(state.block+1)), wait: async () => {
                state.balances.set(wallet.address, state.balances.get(wallet.address)-unit('2')); state.allowances.set(wallet.address,0n);
                state.tuples.set(key,{...empty,beneficiary:wallet.address,started:true,startedAt:BigInt(Math.floor(state.now/1000)),stakedAmount:unit('2')});
                emit('SessionStarted',[key,wallet.address,unit('2'),Math.floor(state.now/1000)]); return {blockNumber:state.block};
            }};
        } }) };
    const walletService = new WalletService({ provider, token, rewards, users, store: new JsonSessionStore(storePath), config: { tokenAddress: TOKEN, rewardAddress: REWARDS, chainId: 31337, deploymentBlock: 1 } });
    walletService.rewardInterface = rewardAbi;
    users.get('user_1').walletFirst = true;
    const iot = {
        health: async () => ({ mqtt: state.ready }),
        latest: async () => ({ source_mode:'esp32',latest:{telemetry:{device_id:'esp32_01'},received_at:new Date(state.now).toISOString()} }),
        list: async () => [...state.sensors.values(), ...(state.busy ? [{ session_id:state.busy,device_id:'esp32_01',status:'running' }] : [])],
        get: async id => state.sensors.get(id) ? structuredClone(state.sensors.get(id)) : null,
        start: async id => { state.sensorStarts++; const info={session_id:id,device_id:'esp32_01',status:'running',started_at:new Date(state.now).toISOString(),sample_count:0};state.sensors.set(id,info);
            if(state.loseSensorStart){state.loseSensorStart=false;throw new ApiError(503,'IOT_UNAVAILABLE','Start response lost');} return structuredClone(info); },
        stop: async id => { const info=state.sensors.get(id); if(info.status==='running'){state.stops++;info.status='closed';info.ended_at=new Date(state.now).toISOString();info.sample_count=40;}return structuredClone(info); },
        outcome: async id => ({sessionId:id,deviceId:'esp32_01',session:structuredClone(state.sensors.get(id)),stats:{sample_count:40,confirmed_violation_count:0},
            result:{outcome:state.outcome,reason:'complete_and_clear',sampleCount:40,durationS:30}})
    };
    const settler = {wallet:{getAddress:async()=>OPERATOR},contract:{getAddress:async()=>REWARDS,operators:async()=>true},
        processSessionOutcome: async payload => {
            state.settlementCalls.push(structuredClone(payload));
            if(state.failSettlement){state.failSettlement=false;throw new ApiError(503,'REWARD_SETTLEMENT_FAILED','Operator RPC unavailable');}
            const key=ethers.id(payload.sessionId), current=state.tuples.get(key);
            if(current.settled) return {success:true,duplicate:true};
            const returned=payload.outcome==='VIOLATION_CONFIRMED'?0n:unit('2');
            const reward=payload.outcome==='VALID_NO_VIOLATION'?unit('3'):0n;
            const penalty=payload.outcome==='VIOLATION_CONFIRMED'?unit('2'):0n;
            const code=['VALID_NO_VIOLATION','VIOLATION_CONFIRMED','SENSOR_FAULT','INCOMPLETE'].indexOf(payload.outcome);
            const status=penalty>0n?3:reward>0n?0:4;
            state.balances.set(current.beneficiary,state.balances.get(current.beneficiary)+returned+reward);
            state.tuples.set(key,{...current,settled:true,endedAt:BigInt(payload.endedAt),settledAt:BigInt(Math.floor(state.now/1000)),rewardPaid:reward,penaltyCollected:penalty,outcome:code,settlementStatus:status,evidenceHash:payload.evidenceHash});
            emit('SessionSettled',[key,current.beneficiary,code,status,returned,reward,penalty,payload.evidenceHash,payload.timestamp,payload.endedAt]);
            return {success:true,outcome:payload.outcome,stakeReturned:ethers.formatEther(returned),rewardPaid:ethers.formatEther(reward),penaltyCollected:ethers.formatEther(penalty),transactionHash:ethers.id('tx-'+state.block)};
        }};
    const options={walletService,iot,settler,clock:()=>state.now,logger:()=>{}};
    const live = new LiveSessionService(options);
    async function stakeWallet(userId, sessionId, reserve = true) {
        const user = walletService._user(userId);
        if (reserve) await live.reserveWalletStart(userId, sessionId);
        const key = ethers.id(sessionId);
        if (!state.tuples.get(key)?.started) {
            // Simulated external wallet submits the transaction; the API only reads the receipt.
            const transaction = await rewards.connect(user.wallet).startSession(key);
            await transaction.wait();
            state.walletTransactions ||= {}; state.walletReceipts ||= {};
            state.walletTransactions[transaction.hash] = { chainId: 31337, from: user.walletAddress, to: REWARDS,
                value: 0n, data: rewardAbi.encodeFunctionData('startSession', [key]) };
            state.walletReceipts[transaction.hash] = { hash: transaction.hash, status: 1, from: user.walletAddress, to: REWARDS,
                blockNumber: state.block, logs: [state.logs.at(-1)] };
        }
        const transactionHash = state.logs.find(log => log.topics[1] === key)?.transactionHash;
        return transactionHash;
    }
    async function start(userId, body) {
        live.validateBody(body, ['sessionRef']);
        const id = walletService._sessionId(userId, body);
        const previous = walletService.store.get(id);
        if (previous && ['RUNNING', 'SETTLED'].includes(previous.stage)) return live.response(previous, { duplicate: true });
        const hash = await stakeWallet(userId, id);
        return live.confirmWalletStart(userId, id, { transactionHash: hash });
    }
    return {live,state,walletService,iot,options,storePath,start,stakeWallet};
}

test('Start/End bind one sensor session to the user, and concurrent retries do not stake or settle twice', async t => {
    const f=fixture(t);
    const starts=await Promise.all([f.start('user_1',{sessionRef:'same'}),f.start('user_1',{sessionRef:'same'})]);
    assert.equal(f.state.sensorStarts,1);assert.equal(f.state.stakes,1);assert.equal(starts[0].stage,'RUNNING');assert.equal(starts[1].duplicate,true);
    assert.equal((await f.live.wallet('user_1')).lockedBalance,'2.0');
    await assert.rejects(f.start('user_2',{sessionRef:'other'}),{code:'DEVICE_BUSY'});assert.equal(f.state.stakes,1);
    f.state.now+=30000;
    const ended=await Promise.all([f.live.end('user_1',starts[0].sessionId),f.live.end('user_1',starts[0].sessionId)]);
    assert.equal(ended[0].stage,'SETTLED');assert.equal(ended[1].duplicate,true);assert.equal(f.state.stops,1);assert.equal(f.state.settlementCalls.length,1);
    assert.equal(f.state.settlementCalls[0].beneficiary,USER1);
    assert.equal(f.state.settlementCalls[0].endedAt, Math.floor(f.state.now / 1000));
    assert.equal(f.state.settlementCalls[0].evidenceHash, '0x' + require('node:crypto').createHash('sha256').update(require('../eventPayload').canonicalJson(f.walletService.store.get(starts[0].sessionId).evidence)).digest('hex'));
    assert.equal((await f.live.wallet('user_1')).balance,'23.0');assert.equal((await f.live.wallet('user_1')).lockedBalance,'0.0');
});

test('V2 sensor readiness reports the task minimum and 90-percent 10 Hz sample count', async t => {
    const f = fixture(t);
    f.live.walletService.config.rewardProtocolVersion = 'task-rewards-v2';
    const device = await f.live.deviceState({ fresh: true });
    assert.equal(device.minDurationS, 120);
    assert.equal(device.minSamplesAtMinimumDuration, 1080);
});

test('wallet-first signed start is reconciled into the sensor session store', async t => {
    const f = fixture(t);
    const sessionId = f.walletService._sessionId('user_1', { sessionRef: 'wallet-confirm' });
    const key = ethers.id(sessionId);
    await f.live.reserveWalletStart('user_1', sessionId);
    f.state.tuples.set(key, { ...empty, beneficiary: USER1, started: true, startedAt: 100n, stakedAmount: unit('2') });
    const transactionHash = ethers.id('wallet-first-start');
    const event = rewardAbi.encodeEventLog(rewardAbi.getEvent('SessionStarted'), [key, USER1, unit('2'), 100]);
    f.state.walletTransactions = { [transactionHash]: {
        chainId: 31337, from: USER1, to: REWARDS,
        data: rewardAbi.encodeFunctionData('startSession', [key]), value: 0n
    } };
    f.state.walletReceipts = { [transactionHash]: {
        hash: transactionHash, status: 1, from: USER1, to: REWARDS, blockNumber: 2,
        logs: [{ ...event, address: REWARDS }]
    } };
    const result = await f.live.confirmWalletStart('user_1', sessionId, { transactionHash });
    assert.equal(result.stage, 'RUNNING');
    assert.equal(result.transactionHash, transactionHash);
    assert.equal(f.state.sensorStarts, 1);
    assert.equal(f.walletService.store.get(sessionId).source, 'esp32');
    assert.equal((await f.live.wallet('user_1')).activeSessionId, sessionId);
});

test('wallet start keeps a pending receipt reserved and opens the sensor only after the receipt and event confirm', async t => {
    const f = fixture(t);
    const sessionId = f.walletService._sessionId('user_1', { sessionRef: 'pending-wallet-confirm' });
    const key = ethers.id(sessionId);
    await f.live.reserveWalletStart('user_1', sessionId);
    f.state.tuples.set(key, { ...empty, beneficiary: USER1, started: true, startedAt: 100n, stakedAmount: unit('2') });
    const transactionHash = ethers.id('pending-wallet-start');
    const event = rewardAbi.encodeEventLog(rewardAbi.getEvent('SessionStarted'), [key, USER1, unit('2'), 100]);
    f.state.walletTransactions = { [transactionHash]: {
        chainId: 31337, from: USER1, to: REWARDS,
        data: rewardAbi.encodeFunctionData('startSession', [key]), value: 0n
    } };
    f.state.walletReceipts = {};

    const pending = await f.live.confirmWalletStart('user_1', sessionId, { transactionHash });
    assert.equal(pending.pending, true);
    assert.equal(f.state.sensorStarts, 0);
    assert.equal(f.walletService.store.get(sessionId).stage, 'WALLET_INTENT');

    f.state.walletReceipts[transactionHash] = {
        hash: transactionHash, status: 1, from: USER1, to: REWARDS, blockNumber: 3,
        logs: [{ ...event, address: REWARDS }]
    };
    await f.live.drain();
    assert.equal(f.walletService.store.get(sessionId).stage, 'RUNNING');
    assert.equal(f.state.sensorStarts, 1);
});

test('wallet start rejects mismatched receipt fields before opening the sensor', async t => {
    for (const mismatch of ['chain', 'from', 'to', 'status', 'calldata', 'event']) {
        await t.test(mismatch, async st => {
            const f = fixture(st);
            const sessionId = f.walletService._sessionId('user_1', { sessionRef: 'bad-' + mismatch });
            const key = ethers.id(sessionId);
            await f.live.reserveWalletStart('user_1', sessionId);
            const transactionHash = ethers.id('bad-wallet-' + mismatch);
            const event = rewardAbi.encodeEventLog(rewardAbi.getEvent('SessionStarted'), [key, USER1, unit('2'), 100]);
            const transaction = { chainId: 31337, from: USER1, to: REWARDS,
                data: rewardAbi.encodeFunctionData('startSession', [key]), value: 0n };
            const receipt = { hash: transactionHash, status: 1, from: USER1, to: REWARDS,
                logs: [{ ...event, address: REWARDS }] };
            if (mismatch === 'chain') transaction.chainId = 1;
            if (mismatch === 'from') transaction.from = USER2;
            if (mismatch === 'to') receipt.to = TOKEN;
            if (mismatch === 'status') receipt.status = 0;
            if (mismatch === 'calldata') transaction.data = '0x';
            if (mismatch === 'event') receipt.logs = [];
            f.state.walletTransactions = { [transactionHash]: transaction };
            f.state.walletReceipts = { [transactionHash]: receipt };
            await assert.rejects(f.live.confirmWalletStart('user_1', sessionId, { transactionHash }));
            assert.equal(f.state.sensorStarts, 0);
        });
    }
});

test('wallet intent lease prevents a second wallet session from reserving the shared sensor', async t => {
    const f = fixture(t);
    const firstId = f.walletService._sessionId('user_1', { sessionRef: 'lease-first' });
    const secondId = f.walletService._sessionId('user_2', { sessionRef: 'lease-second' });
    f.walletService._user('user_2').walletFirst = true;
    await f.live.reserveWalletStart('user_1', firstId);
    await assert.rejects(f.live.reserveWalletStart('user_2', secondId), { code: 'DEVICE_BUSY' });
    assert.equal(f.walletService.store.get(secondId), null);
});

test('unsubmitted wallet intents expire and release the shared sensor lease', async t => {
    const f = fixture(t);
    const firstId = f.walletService._sessionId('user_1', { sessionRef: 'lease-expiring' });
    const secondId = f.walletService._sessionId('user_2', { sessionRef: 'lease-after-expiry' });
    f.walletService._user('user_2').walletFirst = true;
    await f.live.reserveWalletStart('user_1', firstId);
    f.state.now += 121000;
    await f.live.drain();
    assert.equal(f.walletService.store.get(firstId).stage, 'INTENT_EXPIRED');
    const next = await f.live.reserveWalletStart('user_2', secondId);
    assert.equal(next.stage, 'WALLET_INTENT');
});

test('offline or busy sensor is rejected before moving any HRC', async t => {
    const f=fixture(t);f.state.ready=false;
    await assert.rejects(f.start('user_1',{sessionRef:'offline'}),{code:'SENSOR_UNAVAILABLE'});
    f.state.ready=true;f.state.busy='collection-session';
    await assert.rejects(f.start('user_1',{sessionRef:'busy'}),{code:'DEVICE_BUSY'});
    assert.equal(f.state.stakes,0);assert.equal(f.state.sensorStarts,0);assert.equal(f.walletService.store.list().length,0);
});

test('sensor startup failure after a confirmed wallet stake refunds INCOMPLETE without opening another session', async t => {
    const f = fixture(t); f.state.loseSensorStart = true;
    const result = await f.start('user_1', { sessionRef: 'lost-sensor' });
    assert.equal(result.stage, 'SETTLED');
    assert.equal(result.outcomeCode, 3);
    assert.equal(f.state.stakes, 1); assert.equal(f.state.sensorStarts, 1);
    assert.equal((await f.live.wallet('user_1')).balance, '20.0');
});

test('operator failure and service recreation retain frozen outcome/evidence and retry one settlement', async t => {
    const f=fixture(t);const started=await f.start('user_1',{sessionRef:'restart'});f.state.now+=30000;f.state.failSettlement=true;
    const pending=await f.live.end('user_1',started.sessionId);assert.equal(pending.stage,'SETTLEMENT_PENDING');
    const payload=f.state.settlementCalls[0];
    assert.equal(payload.endedAt, Math.floor(f.state.now / 1000));
    assert.equal(payload.endedAt, Math.floor(Date.parse(f.walletService.store.get(started.sessionId).evidence.session.ended_at) / 1000));
    f.walletService.store=new JsonSessionStore(f.storePath);f.state.outcome='VIOLATION_CONFIRMED';f.state.now+=31000;
    const resumed=new LiveSessionService({...f.options,walletService:f.walletService});await resumed.drain();
    assert.deepEqual(f.state.settlementCalls[1],payload);assert.equal((await resumed.session('user_1',started.sessionId)).session.stage,'SETTLED');
    assert.equal(f.state.logs.filter(log => log.topics[0] === ethers.id('SessionSettled(bytes32,address,uint8,uint8,uint256,uint256,uint256,bytes32,uint256,uint256)')).length,1);
    assert.equal(f.state.stops,1);assert.equal(f.state.stakes,1);
});

test('old unlinked stakes are returned as INCOMPLETE, with recovery provenance and idempotent restart', async t => {
    const f=fixture(t); const id=f.walletService._sessionId('user_1',{sessionRef:'old'});
    await f.stakeWallet('user_1',id,false);
    f.walletService.store.save({sessionId:id,userId:'user_1',createdAt:new Date(f.state.now).toISOString()});
    assert.equal((await f.walletService.getWallet('user_1')).balance,'18.0');
    await f.live.drain();await f.live.drain();
    const row=(await f.live.sessions('user_1')).sessions[0];
    assert.equal(row.stage,'SETTLED');assert.equal(row.source,'recovery');assert.equal(row.recoveryReason,'missing_iot_session');
    assert.equal(f.state.settlementCalls.length,1);assert.equal(f.state.settlementCalls[0].outcome,'INCOMPLETE');assert.equal(f.state.sensorStarts,0);
    assert.equal((await f.live.wallet('user_1')).balance,'20.0');
});

test('users cannot End another user session or supply outcome, beneficiary or reward amounts', async t => {
    const f=fixture(t);const started=await f.start('user_1',{sessionRef:'owned'});
    await assert.rejects(f.live.end('user_2',started.sessionId),{code:'SESSION_OWNER_MISMATCH'});
    await assert.rejects(f.start('user_1',{sessionRef:'tamper',outcome:'VALID_NO_VIOLATION'}),{code:'INVALID_PAYLOAD'});
    assert.throws(()=>f.live.end('user_1',started.sessionId,{rewardPaid:'100'}),{code:'INVALID_PAYLOAD'});
    assert.equal(f.state.stops,0);assert.equal(f.state.settlementCalls.length,0);
});


test('recovery timer coalesces scans while an earlier scan is still waiting on RPC', async t => {
    const f=fixture(t);let tick,calls=0,release;
    const hold=new Promise(resolve=>{release=resolve;});
    t.mock.method(global,'setInterval',callback=>{tick=callback;return {unref(){}};});
    t.mock.method(global,'clearInterval',()=>{});
    f.live.drain=()=>{calls++;return hold;};
    f.live.startRecovery();tick();tick();
    assert.equal(calls,1);
    release();await new Promise(resolve=>setImmediate(resolve));
    tick();assert.equal(calls,2);
    f.live.stopRecovery();
});


test('End closes and freezes sensor evidence while the operator is unavailable, then retries that evidence', async t => {
    const f=fixture(t);const started=await f.start('user_1',{sessionRef:'operator-down'});f.state.now+=30000;
    f.options.settler.contract.operators=async()=>{throw new Error('RPC unavailable');};
    const pending=await f.live.end('user_1',started.sessionId);
    assert.equal(pending.stage,'SETTLEMENT_PENDING');assert.equal(f.state.stops,1);assert.equal(f.state.settlementCalls.length,0);
    const payload=f.walletService.store.get(started.sessionId).settlementPayload;
    assert.equal(payload.outcome,'VALID_NO_VIOLATION');
    f.options.settler.contract.operators=async()=>true;f.state.now+=30000;
    await f.live.drain();
    assert.deepEqual(f.state.settlementCalls[0],payload);assert.equal(f.state.stops,1);
    assert.equal(f.walletService.store.get(started.sessionId).stage,'SETTLED');
});


test('End immediately clears the cached busy sensor state used by the next wallet refresh', async t => {
    const f = fixture(t);
    const started = await f.start('user_1', { sessionRef: 'sensor-cache' });
    f.state.now += 30000;
    assert.equal((await f.live.deviceState({ fresh: true })).busySessionId, started.sessionId);
    await f.live.end('user_1', started.sessionId);
    assert.equal((await f.live.wallet('user_1')).device.busySessionId, null);
});

test('a late duplicate confirm does not reopen the sensor or settle a running session after its lease expires', async t => {
    const f = fixture(t);
    const started = await f.start('user_1', { sessionRef: 'duplicate-confirm' });
    const hash = f.walletService.store.get(started.sessionId).transactionHash;
    f.state.now += 180000;
    const again = await f.live.confirmWalletStart('user_1', started.sessionId, { transactionHash: hash });
    assert.equal(again.stage, 'RUNNING'); assert.equal(again.duplicate, true);
    assert.equal(f.state.sensorStarts, 1); assert.equal(f.state.settlementCalls.length, 0);
});
