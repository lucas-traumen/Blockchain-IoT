const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { ethers } = require('ethers');
const { DemoWalletService } = require('../demoWalletService');
const { DemoSessionStore } = require('../demoSessionStore');
const { sessionKey } = require('../rewardPolicy');

const USER1 = '0x0000000000000000000000000000000000000011';
const USER2 = '0x0000000000000000000000000000000000000022';
const TOKEN = '0x0000000000000000000000000000000000000033';
const REWARDS = '0x0000000000000000000000000000000000000044';
const tokenEvents = new ethers.Interface(['event Approval(address indexed owner,address indexed spender,uint256 value)']);
const rewardEvents = new ethers.Interface([
    'event SessionStarted(bytes32 indexed sessionId,address indexed beneficiary,uint256 amount,uint256 startedAt)',
    'event SessionSettled(bytes32 indexed sessionId,address indexed beneficiary,uint8 outcome,uint8 settlementStatus,uint256 stakeReturned,uint256 rewardPaid,uint256 penaltyCollected,bytes32 evidenceHash,uint256 timestamp)',
    'event RewardPaid(bytes32 indexed sessionId,address indexed beneficiary,uint256 amount,uint256 rewardDay)'
]);
const hash = value => '0x' + String(value).repeat(64);
const log = (iface, name, args, address, transactionHash, blockNumber, index = 0) => ({ ...iface.encodeEventLog(iface.getEvent(name), args), address, transactionHash, blockNumber, index });

function fixture(t) {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'hrc-demo-service-'));
    t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
    const store = new DemoSessionStore(path.join(directory, 'sessions.json'));
    const state = { chainId: 31337, starts: 0, approvals: 0, tuples: new Map(), logs: [], allowance: ethers.parseEther('2') };
    const users = new Map([['user_1', { userId: 'user_1', role: 'customer', walletAddress: USER1, wallet: {} }], ['user_2', { userId: 'user_2', role: 'customer', walletAddress: USER2, wallet: {} }]]);
    const provider = {
        getNetwork: async () => ({ chainId: state.chainId }), getBlockNumber: async () => 30,
        getLogs: async filter => state.logs.filter(item => item.address === filter.address && item.blockNumber >= filter.fromBlock && item.blockNumber <= filter.toBlock && filter.topics.every((topic, index) => topic == null || (Array.isArray(topic) ? topic.includes(item.topics[index]) : topic === item.topics[index])))
    };
    const token = {
        balanceOf: async () => ethers.parseEther('20'), allowance: async () => state.allowance,
        symbol: async () => 'HRC', decimals: async () => 18,
        connect() { return { approve: async () => { state.approvals++; return { hash: hash('a'), wait: async () => ({ blockNumber: 10, logs: [] }) }; } }; }
    };
    const empty = { beneficiary: ethers.ZeroAddress, startedAt: 0n, settledAt: 0n, stakedAmount: 0n, rewardPaid: 0n, penaltyCollected: 0n, outcome: 0, settlementStatus: 0, evidenceHash: ethers.ZeroHash, outcomeTimestamp: 0n, started: false, settled: false };
    const rewards = {
        stakeAmount: async () => ethers.parseEther('2'), getSession: async key => state.tuples.get(key) || empty,
        connect() { return { startSession: async key => {
            state.starts++;
            return { hash: hash('b'), wait: async () => {
                state.tuples.set(key, { ...empty, beneficiary: USER1, startedAt: 100n, stakedAmount: ethers.parseEther('2'), started: true });
                state.logs.push(log(rewardEvents, 'SessionStarted', [key, USER1, ethers.parseEther('2'), 100], REWARDS, hash('b'), 11));
                state.allowance = 0n;
                return { blockNumber: 11 };
            } };
        } }; }
    };
    const service = new DemoWalletService({ provider, token, rewards, config: { tokenAddress: TOKEN, rewardAddress: REWARDS, chainId: 31337, deploymentBlock: 1 }, users, store });
    return { service, state, directory };
}

test('concurrent retries stake once and reopened API reads the same session from contract events', async t => {
    const f = fixture(t);
    const input = { sessionRef: 'retry-test' };
    const result = await Promise.all([f.service.startSession('user_1', input), f.service.startSession('user_1', input)]);
    assert.equal(f.state.starts, 1);
    assert.deepEqual(result.map(row => row.duplicate), [false, true]);
    const id = 'demo-user_1-retry-test';
    f.service.store = new DemoSessionStore(path.join(f.directory, 'sessions.json'));
    const after = await f.service.getSession('user_1', id);
    assert.equal(after.session.stakedAmount, '2.0');
    assert.equal(after.session.events[0].name, 'SessionStarted');
    assert.equal(after.session.transactionHash, hash('b'));
    assert.equal((await f.service.listSessions('user_2')).count, 0);
    await assert.rejects(f.service.getSession('user_2', id), { code: 'SESSION_OWNER_MISMATCH' });
});

test('wallet history includes approvals, start and settlement events belonging only to the signed-in user', async t => {
    const f = fixture(t); const key = sessionKey('demo-user_1-history');
    f.state.logs = [
        log(tokenEvents, 'Approval', [USER1, REWARDS, ethers.parseEther('2')], TOKEN, hash('a'), 10),
        log(tokenEvents, 'Approval', [USER1, REWARDS, 0], TOKEN, hash('b'), 11),
        log(rewardEvents, 'SessionStarted', [key, USER1, ethers.parseEther('2'), 100], REWARDS, hash('b'), 11, 1),
        log(rewardEvents, 'RewardPaid', [key, USER1, ethers.parseEther('3'), 1], REWARDS, hash('c'), 12),
        log(rewardEvents, 'SessionSettled', [key, USER1, 0, 0, ethers.parseEther('2'), ethers.parseEther('3'), 0, hash('d'), 110], REWARDS, hash('c'), 12, 1),
        log(tokenEvents, 'Approval', [USER2, REWARDS, ethers.parseEther('2')], TOKEN, hash('e'), 13),
        log(tokenEvents, 'Approval', [USER1, USER2, ethers.parseEther('9')], TOKEN, hash('f'), 14)
    ];
    const history = await f.service.getTransactions('user_1');
    assert.equal(history.count, 3);
    assert.deepEqual(history.transactions.map(row => row.transactionHash), [hash('c'), hash('b'), hash('a')]);
    assert.deepEqual(history.transactions[1].events.map(event => event.name), ['Approval', 'SessionStarted']);
    assert.equal(history.transactions[0].events.find(event => event.name === 'SessionSettled').rewardPaid, '3.0');
    assert.equal((await f.service.getTransactions('user_2')).count, 1);
    await assert.rejects(f.service.getTransactions('unknown'), { code: 'DEMO_ACCOUNT_REQUIRED' });
});

test('demo signing fails closed on a network other than local chain 31337', async t => {
    const f = fixture(t); f.state.chainId = 11155111;
    await assert.rejects(f.service.approve('user_1'), { code: 'LOCAL_CHAIN_REQUIRED' });
    await assert.rejects(f.service.startSession('user_1', { sessionRef: 'wrong-chain' }), { code: 'LOCAL_CHAIN_REQUIRED' });
    assert.equal(f.state.starts, 0); assert.equal(f.state.approvals, 0);
});

test('fixed user wallet mapping survives service recreation and exposes no private keys', t => {
    const f = fixture(t);
    const deploymentPath = path.join(f.directory, 'reward-localhost.json');
    fs.writeFileSync(deploymentPath, JSON.stringify({
        network: 'localhost', chainId: 31337,
        tokenAddress: TOKEN, rewardsAddress: REWARDS
    }));
    const options = {
        HRC_DEMO_SESSION_STORE_PATH: path.join(f.directory, 'fixed-sessions.json'),
        HRC_REWARD_DEPLOYMENT: deploymentPath
    };
    const first = DemoWalletService.fromEnv(options), second = DemoWalletService.fromEnv(options);
    assert.deepEqual(first.listAccounts(), second.listAccounts());
    assert.deepEqual(first.listAccounts().map(row => row.userId), ['admin', 'user_1', 'user_2']);
    const user = first.login('user_1').user;
    assert.equal(user.walletAddress, '0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC');
    assert.equal(Object.hasOwn(user, 'privateKey'), false);
    assert.throws(() => first.login('0x0000000000000000000000000000000000000099'), { code: 'UNKNOWN_DEMO_ACCOUNT' });
});


test('a replacement empty chain cannot accept approval or a new stake using the saved deployment',async t=>{
    const f=fixture(t);f.service.config.deploymentTransactions={token:hash('1'),rewards:hash('2')};
    f.service.provider.getTransactionReceipt=async()=>null;
    await assert.rejects(f.service.approve('user_1'),{code:'CHAIN_HISTORY_MISMATCH'});
    await assert.rejects(f.service.startSession('user_1',{sessionRef:'new-chain'}),{code:'CHAIN_HISTORY_MISMATCH'});
    assert.equal(f.state.approvals,0);assert.equal(f.state.starts,0);
});
