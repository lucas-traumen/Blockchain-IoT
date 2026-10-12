const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { ethers } = require('ethers');
const { WalletService } = require('../walletService');
const { JsonSessionStore } = require('../sessionStore');
const { sessionKey } = require('../rewardPolicy');

const USER1 = '0x0000000000000000000000000000000000000011';
const USER2 = '0x0000000000000000000000000000000000000022';
const TOKEN = '0x0000000000000000000000000000000000000033';
const REWARDS = '0x0000000000000000000000000000000000000044';
const tokenEvents = new ethers.Interface(['event Approval(address indexed owner,address indexed spender,uint256 value)']);
const rewardEvents = new ethers.Interface([
    'event SessionStarted(bytes32 indexed sessionId,address indexed beneficiary,uint256 amount,uint256 startedAt)',
    'event SessionSettled(bytes32 indexed sessionId,address indexed beneficiary,uint8 outcome,uint8 settlementStatus,uint256 stakeReturned,uint256 rewardPaid,uint256 penaltyCollected,bytes32 evidenceHash,uint256 timestamp,uint256 endedAt)',
    'event RewardPaid(bytes32 indexed sessionId,address indexed beneficiary,uint256 amount,uint256 rewardDay)'
]);
const hash = value => '0x' + String(value).repeat(64);
const log = (iface, name, args, address, transactionHash, blockNumber, index = 0) => ({ ...iface.encodeEventLog(iface.getEvent(name), args), address, transactionHash, blockNumber, index });

function fixture(t) {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'hrc-demo-service-'));
    t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
    const store = new JsonSessionStore(path.join(directory, 'sessions.json'));
    const state = { chainId: 31337, starts: 0, approvals: 0, tuples: new Map(), logs: [], allowance: ethers.parseEther('2') };
    const users = new Map([['user_1', { userId: 'user_1', role: 'customer', walletAddress: USER1, wallet: {} }], ['user_2', { userId: 'user_2', role: 'customer', walletAddress: USER2, wallet: {} }]]);
    const provider = {
        getNetwork: async () => ({ chainId: state.chainId }), getBlockNumber: async () => 30, getCode: async () => '0x1234',
        getLogs: async filter => state.logs.filter(item => item.address === filter.address && item.blockNumber >= filter.fromBlock && item.blockNumber <= filter.toBlock && filter.topics.every((topic, index) => topic == null || (Array.isArray(topic) ? topic.includes(item.topics[index]) : topic === item.topics[index])))
    };
    const token = {
        balanceOf: async () => ethers.parseEther('20'), allowance: async () => state.allowance,
        symbol: async () => 'HRC', decimals: async () => 18,
        connect() { return { approve: async () => { state.approvals++; return { hash: hash('a'), wait: async () => ({ blockNumber: 10, logs: [] }) }; } }; }
    };
    const empty = { beneficiary: ethers.ZeroAddress, startedAt: 0n, endedAt: 0n, settledAt: 0n, stakedAmount: 0n, rewardPaid: 0n, penaltyCollected: 0n, outcome: 0, settlementStatus: 0, evidenceHash: ethers.ZeroHash, outcomeTimestamp: 0n, started: false, settled: false };
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
    const service = new WalletService({ provider, token, rewards, config: { tokenAddress: TOKEN, rewardAddress: REWARDS, chainId: 31337, deploymentBlock: 1 }, users, store });
    return { service, state, directory };
}

test('API reads an externally staked session after reopening and isolates its owner', async t => {
    const f = fixture(t);
    const id = f.service._sessionId('user_1', { sessionRef: 'retry-test' });
    const key = sessionKey(id);
    f.state.tuples.set(key, { beneficiary: USER1, startedAt: 100n, settledAt: 0n, stakedAmount: ethers.parseEther('2'),
        rewardPaid: 0n, penaltyCollected: 0n, outcome: 0, settlementStatus: 0, evidenceHash: ethers.ZeroHash, outcomeTimestamp: 0n, started: true, settled: false });
    f.state.logs.push(log(rewardEvents, 'SessionStarted', [key, USER1, ethers.parseEther('2'), 100], REWARDS, hash('b'), 11));
    f.service.store.save({sessionId:id, userId:'user_1'});
    f.service.store = new JsonSessionStore(path.join(f.directory, 'sessions.json'));
    const after = await f.service.getSession('user_1', id);
    assert.equal(after.session.stakedAmount, '2.0'); assert.equal(after.session.events[0].name, 'SessionStarted');
    assert.equal((await f.service.listSessions('user_2')).count, 0);
    await assert.rejects(f.service.getSession('user_2', id), { code: 'SESSION_OWNER_MISMATCH' });
    assert.equal(f.service.approve, undefined); assert.equal(f.service.startSession, undefined);
});

test('wallet session response carries the persisted task policy snapshot for its owner', t => {
    const f = fixture(t);
    const taskSnapshot = { task: { taskId: 'task-1', completionRule: { minimumDurationS: 120 } }, snapshotHash: hash('e') };
    const session = f.service._formatSession({ sessionId: 'task-session', userId: 'user_1', taskId: 'task-1',
        assignmentId: 'assignment-1', taskSnapshot, policySnapshot: { minimumSafeDuration: '120' } }, {
        beneficiary: USER1, startedAt: 100n, endedAt: 0n, settledAt: 0n, stakedAmount: ethers.parseEther('0.2'),
        rewardPaid: 0n, penaltyCollected: 0n, outcome: 0, settlementStatus: 0, evidenceHash: ethers.ZeroHash,
        outcomeTimestamp: 0n, started: true, settled: false
    }, []);
    assert.equal(session.taskId, 'task-1');
    assert.equal(session.assignmentId, 'assignment-1');
    assert.deepEqual(session.taskSnapshot, taskSnapshot);
    assert.equal(session.policySnapshot.minimumSafeDuration, '120');
});

test('wallet history includes approvals, start and settlement events belonging only to the signed-in user', async t => {
    const f = fixture(t); const key = sessionKey('demo-user_1-history');
    f.state.logs = [
        log(tokenEvents, 'Approval', [USER1, REWARDS, ethers.parseEther('2')], TOKEN, hash('a'), 10),
        log(tokenEvents, 'Approval', [USER1, REWARDS, 0], TOKEN, hash('b'), 11),
        log(rewardEvents, 'SessionStarted', [key, USER1, ethers.parseEther('2'), 100], REWARDS, hash('b'), 11, 1),
        log(rewardEvents, 'RewardPaid', [key, USER1, ethers.parseEther('3'), 1], REWARDS, hash('c'), 12),
        log(rewardEvents, 'SessionSettled', [key, USER1, 0, 0, ethers.parseEther('2'), ethers.parseEther('3'), 0, hash('d'), 110, 105], REWARDS, hash('c'), 12, 1),
        log(tokenEvents, 'Approval', [USER2, REWARDS, ethers.parseEther('2')], TOKEN, hash('e'), 13),
        log(tokenEvents, 'Approval', [USER1, USER2, ethers.parseEther('9')], TOKEN, hash('f'), 14)
    ];
    const history = await f.service.getTransactions('user_1');
    assert.equal(history.count, 3);
    assert.deepEqual(history.transactions.map(row => row.transactionHash), [hash('c'), hash('b'), hash('a')]);
    assert.deepEqual(history.transactions[1].events.map(event => event.name), ['Approval', 'SessionStarted']);
    assert.equal(history.transactions[0].events.find(event => event.name === 'SessionSettled').rewardPaid, '3.0');
    assert.equal(history.transactions[0].events.find(event => event.name === 'SessionSettled').endedAt, 105);
    assert.equal((await f.service.getTransactions('user_2')).count, 1);
    await assert.rejects(f.service.getTransactions('unknown'), { code: 'WALLET_ACCOUNT_REQUIRED' });
});

test('wallet reads require the configured network and deployed contract code', async t => {
    const f = fixture(t);
    f.state.chainId = 11155111;
    await assert.rejects(f.service.getWallet('user_1'), { code: 'WRONG_CHAIN' });
    for (const chainId of [11155111, 80002]) {
        f.service.config.chainId = chainId; f.state.chainId = chainId;
        assert.equal((await f.service.getWallet('user_1')).chainId, chainId);
    }
    f.service.provider.getCode = async () => '0x';
    await assert.rejects(f.service.getWallet('user_1'), { code: 'REWARD_DEPLOYMENT_MISSING' });
});

test('service recreation creates no fixed accounts, keys or demo login tokens', t => {
    const f = fixture(t);
    const file = path.join(f.directory, 'reward-localhost.json');
    fs.writeFileSync(file, JSON.stringify({network:'localhost',chainId:31337,tokenAddress:TOKEN,rewardsAddress:REWARDS}));
    const options = {HRC_APP_STORE:'json', HRC_SESSION_STORE_PATH:path.join(f.directory,'persistent.json'),HRC_REWARD_DEPLOYMENT:file};
    const first=WalletService.fromEnv(options), second=WalletService.fromEnv(options);
    assert.equal(first.users.size,0); assert.equal(second.users.size,0);
    const user=first.walletUser(USER1);
    assert.equal(user.wallet,null); assert.equal(user.walletFirst,true);
    assert.equal(first.login,undefined); assert.equal(first.authenticateToken,undefined);
    assert.equal(second.walletUser(USER1).userId,user.userId);
});

test('legacy fixed sessions migrate only when the verified address matches the deployment manifest', t => {
    const f=fixture(t);
    f.service.store.save({sessionId:'legacy-fixed',userId:'user_1'});
    f.service.store.save({sessionId:'unproven',userId:'user_2'});
    f.service.legacyWallets=[{userId:'user_1',address:USER1}];
    f.service.walletUser(USER2);
    assert.equal(f.service.store.get('legacy-fixed').userId,'user_1');
    const user=f.service.walletUser(USER1);
    assert.equal(f.service.store.get('legacy-fixed').userId,user.userId);
    assert.equal(f.service.store.get('unproven').userId,'user_2');
});

test('a replacement empty chain is rejected when saved deployment receipts disappear',async t=>{
    const f=fixture(t); f.service.config.deploymentTransactions={token:hash('1'),rewards:hash('2')};
    f.service.provider.getTransactionReceipt=async()=>null;
    await assert.rejects(f.service.getWallet('user_1'),{code:'CHAIN_HISTORY_MISMATCH'});
});
