const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { ethers } = require('ethers');
const { DemoSessionStore } = require('../demoSessionStore');
const { DurableContractWriter } = require('../durableContractWriter');

function fixture(t) {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(),'hrc-durable-writes-'));
    t.after(()=>fs.rmSync(directory,{recursive:true,force:true}));
    const file=path.join(directory,'sessions.json');
    const store=new DemoSessionStore(file);
    const state={nonceReads:0,broadcasts:[],receipt:null,drop:true};
    const provider={getNetwork:async()=>({chainId:31337n}),getTransactionCount:async()=>{state.nonceReads++;return 0;},
        estimateGas:async()=>21000n,getFeeData:async()=>({gasPrice:1n,maxFeePerGas:null,maxPriorityFeePerGas:null}),getTransactionReceipt:async()=>state.receipt,
        broadcastTransaction:async raw=>{assert.ok(new DemoSessionStore(file).listTransactions().some(record=>record.rawTransaction===raw),'signed bytes were persisted before send');state.broadcasts.push(raw);if(state.drop)throw new Error('Lost response');return{hash:ethers.keccak256(raw)};}};
    const wallet=ethers.Wallet.createRandom().connect(provider);
    const contract={call:{populateTransaction:async value=>({to:'0x0000000000000000000000000000000000000044',data:'0x'+value.toString(16).padStart(2,'0')})}};
    return{file,store,state,provider,wallet,contract,writer:new DurableContractWriter({provider,store,waitMs:0})};
}

test('an uncertain broadcast and store reopen reuse exactly the same signature/hash/nonce',async t=>{
    const f=fixture(t);const pending=await f.writer.send('stake:one',f.wallet,f.contract,'call',[1]);
    assert.equal(pending.pending,true);assert.equal(f.state.nonceReads,1);
    const reopened=new DurableContractWriter({provider:f.provider,store:new DemoSessionStore(f.file),waitMs:0});
    await reopened.send('stake:one',f.wallet,f.contract,'call',[1]);
    assert.equal(f.state.broadcasts.length,2);assert.equal(f.state.broadcasts[0],f.state.broadcasts[1]);assert.equal(f.state.nonceReads,1);
    f.state.receipt={status:1,blockNumber:9};const confirmed=await reopened.send('stake:one',f.wallet,f.contract,'call',[1]);
    assert.equal(confirmed.pending,false);assert.equal(confirmed.hash,pending.hash);assert.equal(confirmed.receipt.blockNumber,9);assert.equal(f.state.broadcasts.length,2);
});

test('an unresolved signer transaction blocks a different operation and changed parameters cannot reuse its signature',async t=>{
    const f=fixture(t);await f.writer.send('stake:one',f.wallet,f.contract,'call',[1]);
    await assert.rejects(f.writer.send('stake:two',f.wallet,f.contract,'call',[2]),{code:'SIGNER_BUSY'});
    await assert.rejects(f.writer.send('stake:one',f.wallet,f.contract,'call',[2]),{code:'STORE_INTEGRITY_ERROR'});
    assert.equal(f.state.nonceReads,1);assert.equal(f.state.broadcasts.length,1);
});

test('corrupted signed bytes and reverted receipts never cause a replacement broadcast',async t=>{
    const f=fixture(t);await f.writer.send('stake:one',f.wallet,f.contract,'call',[1]);
    const record=f.store.getTransaction('stake:one');f.store.saveTransaction({...record,rawTransaction:'0x1234'});
    await assert.rejects(f.writer.send('stake:one',f.wallet,f.contract,'call',[1]),{code:'STORE_INTEGRITY_ERROR'});
    f.store.saveTransaction(record);f.state.receipt={status:0,blockNumber:8};
    await assert.rejects(f.writer.send('stake:one',f.wallet,f.contract,'call',[1]),{code:'TRANSACTION_REVERTED'});
    await assert.rejects(f.writer.send('stake:one',f.wallet,f.contract,'call',[1]),{code:'TRANSACTION_REVERTED'});
    assert.equal(f.state.broadcasts.length,1);assert.equal(f.state.nonceReads,1);
});


test('a confirmed operation whose receipt disappeared is not reported as confirmed or rebroadcast on an empty chain',async t=>{
    const f=fixture(t);f.state.receipt={status:1,blockNumber:9};
    await f.writer.send('stake:one',f.wallet,f.contract,'call',[1]);
    f.state.receipt=null;
    await assert.rejects(f.writer.send('stake:one',f.wallet,f.contract,'call',[1]),{code:'CHAIN_HISTORY_MISMATCH'});
    assert.equal(f.state.broadcasts.length,1);assert.equal(f.state.nonceReads,1);
    assert.equal(f.store.getTransaction('stake:one').status,'CONFIRMED');
});
