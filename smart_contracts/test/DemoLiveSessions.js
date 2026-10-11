// Isolated Hardhat integration: actual API, signed ERC-20/reward transactions and
// event ledger. Sensor HTTP responses are explicitly test fixtures, never live data.
const { expect } = require('chai');
const { ethers } = require('hardhat');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const { DemoWalletService } = require('../../blockchain_api/demoWalletService');
const { DemoSessionStore } = require('../../blockchain_api/demoSessionStore');
const { DurableContractWriter } = require('../../blockchain_api/durableContractWriter');
const { IotSessionClient } = require('../../blockchain_api/iotSessionClient');
const { LiveSessionService } = require('../../blockchain_api/liveSessionService');
const { RewardService } = require('../../blockchain_api/rewardService');
const { createApp } = require('../../blockchain_api/server');
const { HARDHAT_MNEMONIC } = require('../../demo_wallets');

async function fixture() {
    const wallets=[0,1,2,3].map(index=>ethers.HDNodeWallet.fromPhrase(HARDHAT_MNEMONIC,undefined,`m/44'/60'/0'/0/${index}`).connect(ethers.provider));
    const [operator,treasury,user1,user2]=wallets;
    const token=await(await ethers.getContractFactory('HRCRewardToken',operator)).deploy('HRC Test','HRC',ethers.parseEther('10000'),operator.address);await token.waitForDeployment();
    const rewards=await(await ethers.getContractFactory('HRCSessionRewards',operator)).deploy(await token.getAddress(),treasury.address);await rewards.waitForDeployment();
    await(await token.approve(await rewards.getAddress(),ethers.parseEther('1000'))).wait();await(await rewards.fundRewardPool(ethers.parseEther('1000'))).wait();
    for(const user of [user1,user2])await(await token.transfer(user.address,ethers.parseEther('20'))).wait();
    const directory=fs.mkdtempSync(path.join(os.tmpdir(),'hrc-live-contract-'));
    const file=path.join(directory,'sessions.json'),store=new DemoSessionStore(file);
    const state={rows:new Map(),outcome:'VALID_NO_VIOLATION',sensorStarts:0,stops:0,ready:true,dropBroadcast:false};
    const sensor=http.createServer(async(req,res)=>{
        let raw='';for await(const chunk of req)raw+=chunk;
        const body=raw?JSON.parse(raw):null;const parts=req.url.split('/').filter(Boolean);let status=200,result;
        if(req.url==='/health')result={mqtt:state.ready,reward_auto_start:false,reward_auto_settle:false};
        else if(req.url==='/live/latest')result={source_mode:'esp32',latest:{telemetry:{device_id:'esp32_01'},received_at:new Date().toISOString()},provenance:'isolated-test-fixture'};
        else if(req.url==='/sessions')result=[...state.rows.values()];
        else if(req.url==='/sessions/start'){
            if([...state.rows.values()].some(row=>row.status==='running')){status=409;result={detail:'busy'};}
            else{state.sensorStarts++;result={session_id:body.session_id,device_id:'esp32_01',status:'running',started_at:new Date(Date.now()-31000).toISOString(),sample_count:40};state.rows.set(body.session_id,result);}
        }else{
            const row=state.rows.get(decodeURIComponent(parts[1]||''));
            if(!row){status=404;result={detail:'not found'};}
            else if(parts[2]==='stop'){
                if(row.status!=='closed'){state.stops++;row.status='closed';row.ended_at=new Date().toISOString();}result=row;
            }else if(parts[2]==='outcome')result={sessionId:row.session_id,deviceId:'esp32_01',session:row,stats:{sample_count:40,confirmed_violation_count:state.outcome==='VIOLATION_CONFIRMED'?1:0},
                result:{outcome:state.outcome,sampleCount:40,durationS:31,reason:'isolated_test_'+state.outcome}};
            else result=row;
        }
        res.writeHead(status,{'Content-Type':'application/json'});res.end(JSON.stringify(result));
    });
    await new Promise(resolve=>sensor.listen(0,'127.0.0.1',resolve));
    const provider=new Proxy(ethers.provider,{get(target,key){if(key==='broadcastTransaction')return async raw=>{if(state.dropBroadcast)throw new Error('Unsent transaction; simulated outage');return target.broadcastTransaction(raw);};const value=Reflect.get(target,key);return typeof value==='function'?value.bind(target):value;}});
    const sender=new DurableContractWriter({provider,store,waitMs:0});
    const users=new Map([['user_1',user1],['user_2',user2]].map(([userId,wallet])=>[userId,{userId,role:'customer',wallet,walletAddress:wallet.address}]));
    const walletService=new DemoWalletService({provider,token,rewards,users,store,transactionSender:sender,config:{tokenAddress:await token.getAddress(),rewardAddress:await rewards.getAddress(),deploymentBlock:0}});
    const settler=new RewardService({provider,wallet:operator,contract:rewards.connect(operator),transactionSender:sender});
    const iot=new IotSessionClient({baseUrl:`http://127.0.0.1:${sensor.address().port}`});
    let live=new LiveSessionService({walletService,iot,settler,logger:()=>{}});
    const app=createApp({demoService:walletService,liveService:live,service:{},tokens:{HRC_INGEST_API_TOKEN:'i'.repeat(40),HRC_ADMIN_API_TOKEN:'a'.repeat(40)}});
    const server=await new Promise(resolve=>{const s=app.listen(0,'127.0.0.1',()=>resolve(s));});
    const call=async(route,userId='user_1',body)=>{
        const login=walletService.login(userId);
        const response=await fetch(`http://127.0.0.1:${server.address().port}`+route,{method:body===undefined?'GET':'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer '+login.token},body:body===undefined?undefined:JSON.stringify(body)});
        return{status:response.status,data:await response.json()};
    };
    const restart=()=>{walletService.store=new DemoSessionStore(file);walletService.transactionSender=new DurableContractWriter({provider,store:walletService.store,waitMs:0});settler.transactionSender=walletService.transactionSender;
        live=new LiveSessionService({walletService,iot,settler,logger:()=>{}});return live;};
    return{walletService,live,call,token,rewards,user1,user2,treasury,state,restart,close:async()=>{await Promise.all([new Promise(resolve=>server.close(resolve)),new Promise(resolve=>sensor.close(resolve))]);fs.rmSync(directory,{recursive:true,force:true});}};
}

describe('Account sensor session API and contract ledger (isolated sensor fixtures)',function(){
    let f;beforeEach(async()=>{f=await fixture();});afterEach(async()=>{await f.close();});
    it('Approve changes allowance only; Start and End produce exact stake/refund/reward transfers and visible ledger amounts',async()=>{
        const initial=await f.token.balanceOf(f.user1.address);
        expect((await f.call('/api/me/wallet/approve','user_1',{})).status).to.equal(200);
        expect(await f.token.balanceOf(f.user1.address)).to.equal(initial);
        const start=await f.call('/api/me/sessions/start','user_1',{sessionRef:'complete'});expect(start.status).to.equal(200);expect(start.data.stage).to.equal('RUNNING');
        expect(f.state.rows.has(start.data.sessionId)).to.equal(true);expect(await f.token.balanceOf(f.user1.address)).to.equal(initial-ethers.parseEther('2'));
        const end=await f.call('/api/me/sessions/'+start.data.sessionId+'/end','user_1',{});expect(end.data.settled).to.equal(true);expect(end.data.rewardPaid).to.equal('3.0');
        expect(await f.token.balanceOf(f.user1.address)).to.equal(initial+ethers.parseEther('3'));
        const history=(await f.call('/api/me/wallet/transactions')).data.transactions;
        expect(history.find(tx=>tx.action==='APPROVE').walletDelta).to.equal('0.0');
        expect(history.find(tx=>tx.action==='STAKE').walletDelta).to.equal('-2.0');
        const settlement=history.find(tx=>tx.action==='SETTLEMENT');expect(settlement.walletDelta).to.equal('5.0');expect(settlement.lockedDelta).to.equal('-2.0');expect(settlement.rewardPaid).to.equal('3.0');
        expect((await f.call('/api/me/wallet')).data.lockedBalance).to.equal('0.0');
    });
    it('same-reference Start and repeated End are idempotent, and users/admin cannot End another user session',async()=>{
        await f.call('/api/me/wallet/approve','user_1',{});
        const started=(await f.call('/api/me/sessions/start','user_1',{sessionRef:'repeat'})).data;
        expect((await f.call('/api/me/sessions/start','user_1',{sessionRef:'repeat'})).data.duplicate).to.equal(true);expect(f.state.sensorStarts).to.equal(1);
        expect((await f.call('/api/me/sessions/'+started.sessionId+'/end','user_2',{})).status).to.equal(403);
        expect((await f.call('/api/me/sessions/'+started.sessionId+'/end','admin',{})).status).to.equal(403);
        expect((await f.call('/api/me/sessions/'+started.sessionId+'/end','user_1',{outcome:'VALID_NO_VIOLATION'})).status).to.equal(400);
        await f.call('/api/me/sessions/'+started.sessionId+'/end','user_1',{});
        expect((await f.call('/api/me/sessions/'+started.sessionId+'/end','user_1',{})).data.duplicate).to.equal(true);expect(f.state.stops).to.equal(1);
        expect((await f.rewards.queryFilter(f.rewards.filters.SessionStarted())).length).to.equal(1);expect((await f.rewards.queryFilter(f.rewards.filters.SessionSettled())).length).to.equal(1);
    });
    it('confirmed violation moves the existing stake to treasury without charging the wallet twice',async()=>{
        f.state.outcome='VIOLATION_CONFIRMED';const treasuryBefore=await f.token.balanceOf(f.treasury.address);
        await f.call('/api/me/wallet/approve','user_1',{});const start=(await f.call('/api/me/sessions/start','user_1',{sessionRef:'violation'})).data;
        const result=(await f.call('/api/me/sessions/'+start.sessionId+'/end','user_1',{})).data;
        expect(result.penaltyCollected).to.equal('2.0');expect(await f.token.balanceOf(f.user1.address)).to.equal(ethers.parseEther('18'));
        expect(await f.token.balanceOf(f.treasury.address)).to.equal(treasuryBefore+ethers.parseEther('2'));
        const tx=(await f.call('/api/me/wallet/transactions')).data.transactions.find(row=>row.action==='SETTLEMENT');expect(tx.walletDelta).to.equal('0.0');expect(tx.lockedDelta).to.equal('-2.0');
    });
    for(const outcome of ['SENSOR_FAULT','INCOMPLETE'])it(outcome+' returns the stake without reward',async()=>{
        f.state.outcome=outcome;await f.call('/api/me/wallet/approve','user_1',{});const start=(await f.call('/api/me/sessions/start','user_1',{sessionRef:outcome})).data;
        const result=(await f.call('/api/me/sessions/'+start.sessionId+'/end','user_1',{})).data;
        expect(result.stakeReturned).to.equal('2.0');expect(result.rewardPaid).to.equal('0.0');expect(await f.token.balanceOf(f.user1.address)).to.equal(ethers.parseEther('20'));
        expect((await f.call('/api/me/wallet/transactions')).data.transactions.find(row=>row.action==='SETTLEMENT').walletDelta).to.equal('2.0');
    });
    it('the daily limit returns stake on a second valid session and the ledger shows zero additional reward',async()=>{
        for(const ref of ['first','second']){await f.call('/api/me/wallet/approve','user_1',{});const start=(await f.call('/api/me/sessions/start','user_1',{sessionRef:ref})).data;await f.call('/api/me/sessions/'+start.sessionId+'/end','user_1',{});}
        const latest=(await f.call('/api/me/wallet/transactions')).data.transactions.find(row=>row.action==='SETTLEMENT');expect(latest.walletDelta).to.equal('2.0');expect(latest.settlementStatusCode).to.equal(1);
        expect(await f.token.balanceOf(f.user1.address)).to.equal(ethers.parseEther('23'));
    });
    it('a pending signed stake survives recreation and resumes the same hash/nonce, followed by one real settlement',async()=>{
        await f.call('/api/me/wallet/approve','user_1',{});f.state.dropBroadcast=true;
        const pending=(await f.call('/api/me/sessions/start','user_1',{sessionRef:'restart'})).data;expect(pending.pending).to.equal(true);expect(await f.token.balanceOf(f.user1.address)).to.equal(ethers.parseEther('20'));
        const signed=f.walletService.store.getTransaction('stake:'+pending.sessionId);expect(signed.transactionHash).to.equal(pending.transactionHash);
        f.state.dropBroadcast=false;const resumed=f.restart();await resumed.drain();
        const started=(await resumed.session('user_1',pending.sessionId)).session;expect(started.started).to.equal(true);expect(started.transactionHash).to.equal(signed.transactionHash);expect(f.state.sensorStarts).to.equal(1);
        await resumed.end('user_1',pending.sessionId);expect((await f.rewards.queryFilter(f.rewards.filters.SessionStarted())).length).to.equal(1);expect((await f.rewards.queryFilter(f.rewards.filters.SessionSettled())).length).to.equal(1);
    });
    it('legacy orphan recovery refunds once without inventing sensor evidence or resetting chain history',async()=>{
        await f.walletService.approve('user_1');await f.walletService.startSession('user_1',{sessionRef:'orphan'});
        await f.live.drain();await f.live.drain();const row=(await f.live.sessions('user_1')).sessions[0];
        expect(row.source).to.equal('recovery');expect(row.recoveryReason).to.equal('missing_iot_session');expect(row.rewardPaid).to.equal('0.0');expect(row.stakeReturned).to.equal('2.0');
        expect(await f.token.balanceOf(f.user1.address)).to.equal(ethers.parseEther('20'));expect(f.state.sensorStarts).to.equal(0);expect((await f.rewards.queryFilter(f.rewards.filters.SessionSettled())).length).to.equal(1);
    });
    it('an unavailable sensor rejects Start before any stake transaction is sent',async()=>{
        await f.call('/api/me/wallet/approve','user_1',{});f.state.ready=false;
        expect((await f.call('/api/me/sessions/start','user_1',{sessionRef:'unavailable'})).status).to.equal(503);expect(await f.token.balanceOf(f.user1.address)).to.equal(ethers.parseEther('20'));
        expect((await f.rewards.queryFilter(f.rewards.filters.SessionStarted())).length).to.equal(0);
    });
});
