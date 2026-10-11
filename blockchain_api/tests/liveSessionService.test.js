const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { ethers } = require('ethers');
const { DemoWalletService } = require('../demoWalletService');
const { DemoSessionStore } = require('../demoSessionStore');
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
    'event SessionSettled(bytes32 indexed sessionId,address indexed beneficiary,uint8 outcome,uint8 settlementStatus,uint256 stakeReturned,uint256 rewardPaid,uint256 penaltyCollected,bytes32 evidenceHash,uint256 timestamp)'
]);
const empty = { beneficiary: ethers.ZeroAddress, startedAt: 0n, settledAt: 0n, stakedAmount: 0n, rewardPaid: 0n, penaltyCollected: 0n,
    outcome: 0, settlementStatus: 0, evidenceHash: ethers.ZeroHash, outcomeTimestamp: 0n, started: false, settled: false };

function fixture(t) {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'hrc-live-session-'));
    t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
    const storePath = path.join(directory, 'sessions.json');
    const state = { now: Date.parse('2026-10-11T10:00:00Z'), balances: new Map([[USER1,unit('20')],[USER2,unit('20')]]), allowances: new Map([[USER1,unit('2')],[USER2,unit('2')]]),
        tuples: new Map(), logs: [], stakes: 0, stops: 0, sensorStarts: 0, block: 1, settlementCalls: [], ready: true, busy: null,
        failStake: null, failSettlement: false, loseSensorStart: false, outcome: 'VALID_NO_VIOLATION', sensors: new Map() };
    const users = new Map([['user_1', USER1], ['user_2', USER2]].map(([userId, address]) => [userId, { userId, walletAddress: address, wallet: { address }, role: 'customer' }]));
    const provider = { getNetwork: async () => ({ chainId: 31337 }), getBlockNumber: async () => state.block,
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
    const walletService = new DemoWalletService({ provider, token, rewards, users, store: new DemoSessionStore(storePath), config: { tokenAddress: TOKEN, rewardAddress: REWARDS, deploymentBlock: 1 } });
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
            state.tuples.set(key,{...current,settled:true,settledAt:BigInt(Math.floor(state.now/1000)),rewardPaid:reward,penaltyCollected:penalty,outcome:code,settlementStatus:status,evidenceHash:payload.evidenceHash});
            emit('SessionSettled',[key,current.beneficiary,code,status,returned,reward,penalty,payload.evidenceHash,payload.timestamp]);
            return {success:true,outcome:payload.outcome,stakeReturned:ethers.formatEther(returned),rewardPaid:ethers.formatEther(reward),penaltyCollected:ethers.formatEther(penalty),transactionHash:ethers.id('tx-'+state.block)};
        }};
    const options={walletService,iot,settler,clock:()=>state.now,logger:()=>{}};
    const live = new LiveSessionService(options);
    return {live,state,walletService,iot,options,storePath};
}

test('Start/End bind one sensor session to the user, and concurrent retries do not stake or settle twice', async t => {
    const f=fixture(t);
    const starts=await Promise.all([f.live.start('user_1',{sessionRef:'same'}),f.live.start('user_1',{sessionRef:'same'})]);
    assert.equal(f.state.sensorStarts,1);assert.equal(f.state.stakes,1);assert.equal(starts[0].stage,'RUNNING');assert.equal(starts[1].duplicate,true);
    assert.equal((await f.live.wallet('user_1')).lockedBalance,'2.0');
    await assert.rejects(f.live.start('user_2',{sessionRef:'other'}),{code:'DEVICE_BUSY'});assert.equal(f.state.stakes,1);
    f.state.now+=30000;
    const ended=await Promise.all([f.live.end('user_1',starts[0].sessionId),f.live.end('user_1',starts[0].sessionId)]);
    assert.equal(ended[0].stage,'SETTLED');assert.equal(ended[1].duplicate,true);assert.equal(f.state.stops,1);assert.equal(f.state.settlementCalls.length,1);
    assert.equal(f.state.settlementCalls[0].beneficiary,USER1);
    assert.equal((await f.live.wallet('user_1')).balance,'23.0');assert.equal((await f.live.wallet('user_1')).lockedBalance,'0.0');
});

test('wallet-first signed start is reconciled into the sensor session store', async t => {
    const f = fixture(t);
    const sessionId = f.walletService._sessionId('user_1', { sessionRef: 'wallet-confirm' });
    const key = ethers.id(sessionId);
    f.state.tuples.set(key, { ...empty, beneficiary: USER1, started: true, startedAt: 100n, stakedAmount: unit('2') });
    const result = await f.live.confirmWalletStart('user_1', sessionId, { transactionHash: '0x' + 'a'.repeat(64) });
    assert.equal(result.stage, 'RUNNING');
    assert.equal(result.transactionHash, '0x' + 'a'.repeat(64));
    assert.equal(f.state.sensorStarts, 1);
    assert.equal(f.walletService.store.get(sessionId).source, 'esp32');
    assert.equal((await f.live.wallet('user_1')).activeSessionId, sessionId);
});

test('offline or busy sensor is rejected before moving any HRC', async t => {
    const f=fixture(t);f.state.ready=false;
    await assert.rejects(f.live.start('user_1',{sessionRef:'offline'}),{code:'SENSOR_UNAVAILABLE'});
    f.state.ready=true;f.state.busy='collection-session';
    await assert.rejects(f.live.start('user_1',{sessionRef:'busy'}),{code:'DEVICE_BUSY'});
    assert.equal(f.state.stakes,0);assert.equal(f.state.sensorStarts,0);assert.equal(f.walletService.store.list().length,0);
});

test('a definite stake preparation failure closes the opened sensor session without charging the user', async t => {
    const f=fixture(t);f.state.failStake='TRANSACTION_PREPARE_FAILED';
    const result=await f.live.start('user_1',{sessionRef:'no-charge'});
    assert.equal(result.stage,'START_FAILED');assert.equal(result.started,false);assert.equal(f.state.stops,1);assert.equal(f.state.stakes,0);
    assert.equal((await f.live.wallet('user_1')).balance,'20.0');
});

test('a lost sensor start response resumes the same sensor reference instead of opening or staking another session', async t => {
    const f=fixture(t);f.state.loseSensorStart=true;
    const first=await f.live.start('user_1',{sessionRef:'lost'});assert.equal(first.pending,true);assert.equal(f.state.stakes,0);
    const resumed=await f.live.start('user_1',{sessionRef:'lost'});
    assert.equal(resumed.stage,'RUNNING');assert.equal(f.state.sensorStarts,1);assert.equal(f.state.stakes,1);
});

test('operator failure and service recreation retain frozen outcome/evidence and retry one settlement', async t => {
    const f=fixture(t);const started=await f.live.start('user_1',{sessionRef:'restart'});f.state.now+=30000;f.state.failSettlement=true;
    const pending=await f.live.end('user_1',started.sessionId);assert.equal(pending.stage,'SETTLEMENT_PENDING');
    const payload=f.state.settlementCalls[0];
    f.walletService.store=new DemoSessionStore(f.storePath);f.state.outcome='VIOLATION_CONFIRMED';f.state.now+=31000;
    const resumed=new LiveSessionService({...f.options,walletService:f.walletService});await resumed.drain();
    assert.deepEqual(f.state.settlementCalls[1],payload);assert.equal((await resumed.session('user_1',started.sessionId)).session.stage,'SETTLED');
    assert.equal(f.state.stops,1);assert.equal(f.state.stakes,1);
});

test('old unlinked stakes are returned as INCOMPLETE, with recovery provenance and idempotent restart', async t => {
    const f=fixture(t);await f.walletService.startSession('user_1',{sessionRef:'old'});
    assert.equal((await f.walletService.getWallet('user_1')).balance,'18.0');
    await f.live.drain();await f.live.drain();
    const row=(await f.live.sessions('user_1')).sessions[0];
    assert.equal(row.stage,'SETTLED');assert.equal(row.source,'recovery');assert.equal(row.recoveryReason,'missing_iot_session');
    assert.equal(f.state.settlementCalls.length,1);assert.equal(f.state.settlementCalls[0].outcome,'INCOMPLETE');assert.equal(f.state.sensorStarts,0);
    assert.equal((await f.live.wallet('user_1')).balance,'20.0');
});

test('users cannot End another user session or supply outcome, beneficiary or reward amounts', async t => {
    const f=fixture(t);const started=await f.live.start('user_1',{sessionRef:'owned'});
    await assert.rejects(f.live.end('user_2',started.sessionId),{code:'SESSION_OWNER_MISMATCH'});
    assert.throws(()=>f.live.start('user_1',{sessionRef:'tamper',outcome:'VALID_NO_VIOLATION'}),{code:'INVALID_PAYLOAD'});
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
    const f=fixture(t);const started=await f.live.start('user_1',{sessionRef:'operator-down'});f.state.now+=30000;
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
    const started = await f.live.start('user_1', { sessionRef: 'sensor-cache' });
    f.state.now += 30000;
    assert.equal((await f.live.deviceState({ fresh: true })).busySessionId, started.sessionId);
    await f.live.end('user_1', started.sessionId);
    assert.equal((await f.live.wallet('user_1')).device.busySessionId, null);
});
