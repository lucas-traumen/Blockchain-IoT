// Disposable verification chain, actual ESP32 HTTP API and native Chromium UI.
// Does not redeploy or change the saved localhost manifests/demo session store.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawn } = require('node:child_process');
const { ethers, network } = require('hardhat');
const { HARDHAT_MNEMONIC } = require('../../demo_wallets');
const { DemoSessionStore } = require('../../blockchain_api/demoSessionStore');
const { DemoWalletService } = require('../../blockchain_api/demoWalletService');
const { DurableContractWriter } = require('../../blockchain_api/durableContractWriter');
const { IotSessionClient } = require('../../blockchain_api/iotSessionClient');
const { LiveSessionService } = require('../../blockchain_api/liveSessionService');
const { RewardService } = require('../../blockchain_api/rewardService');
const { RewardReadService } = require('../../blockchain_api/rewardReadService');
const { createEventReader } = require('../../blockchain_api/eventReader');
const { getTransactionHistory } = require('../../blockchain_api/transactionHistory');
const { createApp } = require('../../blockchain_api/server');

async function main() {
    if (network.name !== 'hardhat') throw new Error('Use the isolated in-process hardhat network for this verifier');
    const provider = ethers.provider;
    const wallets = [0, 1, 2, 3].map(index => ethers.HDNodeWallet.fromPhrase(HARDHAT_MNEMONIC, undefined, `m/44'/60'/0'/0/${index}`).connect(provider));
    const [operator, treasury, user1, user2] = wallets;
    const iot = new IotSessionClient({ baseUrl: process.env.HRC_IOT_API_URL || 'http://127.0.0.1:8001' });
    const [health, latest, sensors] = await Promise.all([iot.health(), iot.latest(), iot.list()]);
    if (!health.mqtt || latest.source_mode !== 'esp32' || health.reward_auto_start || health.reward_auto_settle) throw new Error('The actual ESP32 must be running with the fixed-beneficiary reward bridge disabled');
    if (sensors.some(row => row.device_id === 'esp32_01' && row.status === 'running')) throw new Error('Sensor is measuring another session; retain that session instead of opening a new one');

    const safety = await (await ethers.getContractFactory('HRCSafetyLog', operator)).deploy(); await safety.waitForDeployment();
    const token = await (await ethers.getContractFactory('HRCRewardToken', operator)).deploy('HRC Test', 'HRC', ethers.parseEther('10000'), operator.address); await token.waitForDeployment();
    const rewards = await (await ethers.getContractFactory('HRCSessionRewards', operator)).deploy(await token.getAddress(), treasury.address); await rewards.waitForDeployment();
    await (await token.approve(await rewards.getAddress(), ethers.parseEther('1000'))).wait();
    await (await rewards.fundRewardPool(ethers.parseEther('1000'))).wait();
    for (const wallet of [user1, user2]) await (await token.transfer(wallet.address, ethers.parseEther('20'))).wait();

    const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'hrc-live-esp32-chain-'));
    const evidence = path.resolve(process.env.HRC_UI_EVIDENCE_DIR || '../data/results/live-wallet-sessions-20261011/isolated-chain');
    fs.mkdirSync(evidence, { recursive: true });
    const file = path.join(directory, 'sessions.json');
    const store = new DemoSessionStore(file);
    const transactionSender = new DurableContractWriter({ provider, store, waitMs: 0 });
    const config = { chainId: 31337, network: 'hardhat', rpcUrl: null, tokenAddress: await token.getAddress(), rewardAddress: await rewards.getAddress(),
        rewardsAddress: await rewards.getAddress(), deploymentBlock: 0, dashboardBeneficiary: user2.address,
        tokenAbi: token.interface.fragments, rewardAbi: rewards.interface.fragments,
        deploymentTransactions: { token: token.deploymentTransaction().hash, rewards: rewards.deploymentTransaction().hash } };
    const users = new Map([['user_1', user1], ['user_2', user2]].map(([userId, wallet]) => [userId, { userId, label: userId, role: 'customer', walletAddress: wallet.address, wallet }]));
    const demo = new DemoWalletService({ provider, token, rewards, users, config, store, transactionSender });
    const settler = new RewardService({ provider, wallet: operator, contract: rewards, transactionSender });
    const live = new LiveSessionService({ walletService: demo, iot, settler });
    const reads = new RewardReadService({ provider, token, rewards, config });
    const service = {
        getPublicConfig: () => ({ success: true, chainId: 31337, network: 'localhost', rpcUrl: null, safety: { address: safety.target, deploymentBlock: 0 }, rewards: { tokenAddress: token.target, address: rewards.target, deploymentBlock: 0 } }),
        getAllSafetyEvents: createEventReader({ provider, contract: safety, deploymentBlock: 0 }),
        getAllTransactions: () => getTransactionHistory({ provider, contract: safety, deploymentBlock: 0 }),
        getRobotStatus: async deviceId => ({ success: true, deviceId, robotLocked: await safety.isRobotLocked(deviceId) }),
        getRewardConfig: () => reads.getConfig(), getRewardSummary: beneficiary => reads.getSummary(beneficiary),
        getRewardSessions: options => reads.getSessions(options), getRewardSession: id => reads.getSession(id), getRewardTransactions: options => reads.getTransactions(options)
    };
    const app = createApp({ service, demoService: demo, liveService: live, tokens: { HRC_INGEST_API_TOKEN: crypto.randomBytes(32).toString('hex'), HRC_ADMIN_API_TOKEN: crypto.randomBytes(32).toString('hex') } });
    const server = await new Promise(resolve => { const listener = app.listen(0, '127.0.0.1', () => resolve(listener)); });
    const api = 'http://127.0.0.1:' + server.address().port;
    live.startRecovery();
    let verificationError;
    try {
        console.log('Verifying actual ESP32 and full browser transaction flow on an isolated Hardhat chain');
        const verifyBrowser = async (output, sessionId = '') => {
            const child = spawn(process.execPath, [path.resolve('../scripts/verify_demo_wallet_ui.mjs'), '--transact'], { cwd: path.resolve('..'), stdio: 'inherit', env: { ...process.env,
                HRC_CHAIN_API_URL: api, HRC_UI_EVIDENCE_DIR: output, HRC_UI_CHAIN_LABEL: 'isolated-hardhat-real-esp32', HRC_UI_SESSION_ID: sessionId } });
            const code = await new Promise((resolve, reject) => { child.once('error', reject); child.once('exit', resolve); });
            if (code !== 0) throw new Error('Browser verification failed with exit code ' + code);
        };
        await verifyBrowser(evidence);
        const settled = store.list('user_2').find(row => row.stage === 'SETTLED');
        if (!settled) throw new Error('The real sensor session has not settled');
        console.log('Verifying browser retry of the same settled session without another stake');
        await verifyBrowser(path.join(evidence, 'resume'), settled.sessionId);
    } catch (error) { verificationError = error; }
    finally {
        // A verifier failure after Start must still End the very same ID.
        for (const row of store.list()) {
            if (row.source === 'esp32' && !['SETTLED', 'START_FAILED'].includes(row.stage)) {
                try { await live.end(row.userId, row.sessionId); } catch (error) { verificationError ||= error; }
            }
        }
        live.stopRecovery();
        const sessions = [];
        for (const account of ['user_1', 'user_2']) sessions.push(...(await demo.listSessions(account)).sessions);
        const state = { source: 'isolated-hardhat-real-esp32', chainId: 31337, completed: !verificationError, error: verificationError?.message || null,
            accounts: [await demo.getWallet('user_1'), await demo.getWallet('user_2')], sessions };
        fs.writeFileSync(path.join(evidence, 'verification-state.json'), JSON.stringify(state, null, 2) + '\n');
        await new Promise(resolve => server.close(resolve));
        if (sessions.some(row => row.started && !row.settled)) {
            console.error('Unsettled verification state retained at ' + file);
        } else fs.rmSync(directory, { recursive: true, force: true });
    }
    if (verificationError) throw verificationError;
    console.log('PASS: isolated contracts, real ESP32, browser receipts and wallet settlement');
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });
