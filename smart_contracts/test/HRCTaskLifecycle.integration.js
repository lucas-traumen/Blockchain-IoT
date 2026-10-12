const { expect } = require("chai");
const { ethers, network } = require("hardhat");
const { time } = require("@nomicfoundation/hardhat-toolbox/network-helpers");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");

const { WalletService } = require("../../blockchain_api/walletService");
const { JsonSessionStore } = require("../../blockchain_api/sessionStore");
const { LiveSessionService } = require("../../blockchain_api/liveSessionService");
const { TaskRewardService } = require("../../blockchain_api/taskRewardService");
const { WalletFirstService } = require("../../blockchain_api/walletFirstService");
const { TaskService } = require("../../blockchain_api/taskService");
const { DurableContractWriter } = require("../../blockchain_api/durableContractWriter");
const { sessionKey } = require("../../blockchain_api/rewardPolicy");
const { IotSessionClient } = require("../../blockchain_api/iotSessionClient");
const { TASK_EVIDENCE_POLICY } = require("../../blockchain_api/taskEvidencePolicy");
const { SqliteCustomerStore, SqliteSessionStore } = require("../../blockchain_api/sqliteStores");
const { CustomerService } = require("../../blockchain_api/customerService");
const { createApp } = require("../../blockchain_api/server");

const USER_ID = "usr_integration_customer";
const TASK_ID = "task-integration-safe-cycles";
const REQUIRED_CYCLES = 2;
const temporaryDirectories = [];
const fixtureProcesses = [];
const httpServers = [];
const sqliteStores = [];

afterEach(async () => {
    await network.provider.send('evm_setAutomine', [true]);
    for (const server of httpServers.splice(0)) await new Promise(resolve => server.close(resolve));
    for (const store of sqliteStores.splice(0)) store.close();
    for (const process of fixtureProcesses.splice(0)) {
        if (process.exitCode !== null || process.signalCode !== null) continue;
        const exited = new Promise(resolve => process.once('exit', resolve));
        process.kill('SIGTERM');
        const timeout = setTimeout(() => process.kill('SIGKILL'), 5000);
        await exited;
        clearTimeout(timeout);
    }
    for (const directory of temporaryDirectories.splice(0)) fs.rmSync(directory, { recursive: true, force: true });
});

async function fixture({ outcome = "VALID_NO_VIOLATION", completion = true, failSettlementOnce = false } = {}) {
    const [owner, customer, treasury] = await ethers.getSigners();
    const Token = await ethers.getContractFactory("HRCRewardToken");
    const token = await Token.deploy("HRC", "HRC", ethers.parseEther("1000"), owner.address);
    const Rewards = await ethers.getContractFactory("HRCTaskRewards");
    const rewards = await Rewards.deploy(await token.getAddress(), treasury.address);
    await token.transfer(customer.address, ethers.parseEther("1"));
    await token.approve(await rewards.getAddress(), ethers.parseEther("10"));
    await rewards.fundRewardPool(ethers.parseEther("1"));
    await token.connect(customer).approve(await rewards.getAddress(), ethers.parseEther("0.2"));

    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "hrc-v2-lifecycle-"));
    temporaryDirectories.push(directory);
    const store = new JsonSessionStore(path.join(directory, "sessions.json"));
    const mnemonic = process.env.HRC_LOCAL_CHAIN_MNEMONIC || "test test test test test test test test test test test junk";
    const operatorWallet = ethers.HDNodeWallet.fromPhrase(mnemonic, undefined, "m/44'/60'/0'/0/0").connect(ethers.provider);
    const transactionSender = new DurableContractWriter({ provider: ethers.provider, store, waitMs: 0 });
    const assignment = {
        assignmentId: "asg-integration-1", taskId: TASK_ID, userId: USER_ID,
        walletAddress: customer.address, status: "ASSIGNED", taskDefinitionHash: ethers.id("task-definition"),
        policySnapshot: {
            task: { taskId: TASK_ID, title: "Integration cycles", status: "OPEN", deviceId: "esp32_01",
                evidencePolicy: { ...TASK_EVIDENCE_POLICY },
                completionRule: { type: "robot_cycle_count", requiredCycles: REQUIRED_CYCLES,
                minimumDurationS: 120, maximumDurationS: 600 } },
            snapshotHash: ethers.id("task-definition")
        }
    };
    let completionEvidence = null;
    const identityStore = {
        getAssignment: () => assignment,
        getAssignmentForUser: () => assignment,
        updateAssignment(_id, fields) { Object.assign(assignment, fields); return assignment; },
        getCompletionEvidence: () => completionEvidence,
        saveCompletionEvidence(evidence) { completionEvidence ||= structuredClone(evidence); return completionEvidence; }
    };
    const recordUsers = new Map([[USER_ID, { userId: USER_ID, walletAddress: customer.address,
        wallet: customer, role: "customer", walletFirst: true }]]);
    const walletService = new WalletService({ provider: ethers.provider, token,
        rewards: rewards.connect(owner), users: recordUsers, store, identityStore, transactionSender,
        config: { tokenAddress: await token.getAddress(), rewardAddress: await rewards.getAddress(), chainId: 31337,
            deploymentBlock: 0, deploymentTransactions: {}, rewardAbi: rewards.interface.fragments,
            rewardProtocolVersion: "task-rewards-v2" }
    });
    const rewardService = new TaskRewardService({ provider: ethers.provider, wallet: operatorWallet,
        contract: rewards.connect(operatorWallet), transactionSender });

    const sensor = { session: null, starts: 0, stops: 0 };
    const clock = { now: Date.now() };
    const iot = {
        health: async () => ({ mqtt: true, reward_auto_start: false, reward_auto_settle: false }),
        latest: async () => ({ source_mode: "esp32", latest: { telemetry: { device_id: "esp32_01" }, received_at: new Date().toISOString() } }),
        list: async () => sensor.session?.status === "running" ? [{ session_id: sensor.session.session_id,
            device_id: "esp32_01", status: "running" }] : [],
        get: async id => sensor.session?.session_id === id ? structuredClone(sensor.session) : null,
        start: async id => {
            sensor.starts += 1;
            const startedAt = Number((await rewards.sessions(sessionKey(id))).startedAt);
            sensor.session = { session_id: id, device_id: "esp32_01", status: "running",
                started_at: new Date(startedAt * 1000).toISOString(), ended_at: null, sample_count: 0, boot_change: false };
            return structuredClone(sensor.session);
        },
        stop: async id => {
            sensor.stops += 1;
            const chainSession = await rewards.sessions(sessionKey(id));
            const end = Number(chainSession.startedAt) + 120;
            await time.increaseTo(end);
            sensor.session = { ...sensor.session, status: "closed", ended_at: new Date(end * 1000).toISOString(), sample_count: 1200 };
            return structuredClone(sensor.session);
        },
        outcome: async id => ({ sessionId: id, deviceId: "esp32_01", session: structuredClone(sensor.session),
            stats: { sample_count: 1200, raw_count: 1200, processed_count: 1200,
                processed_coverage: 1, timing_span_s: 119.9, mean_cadence_hz: 10, max_timing_gap_s: 0.1,
                gap_count: 0, unknown_count: outcome === "SENSOR_FAULT" ? 1 : 0,
                confirmed_violation_count: outcome === "VIOLATION_CONFIRMED" ? 2 : 0 },
            result: { outcome, reason: "integration_fixture", sampleCount: 1200, durationS: 120, reasonCodes: [] } })
    };
    let firstSettlementFailure = failSettlementOnce;
    const settler = { contract: rewards.connect(operatorWallet), wallet: operatorWallet,
        processSessionOutcome: async payload => {
            if (firstSettlementFailure) {
                firstSettlementFailure = false;
                throw Object.assign(new Error("isolated RPC outage"), { code: "RPC_UNAVAILABLE" });
            }
            return rewardService.processSessionOutcome(payload);
        } };
    const live = new LiveSessionService({ walletService, iot, settler, completionStore: identityStore,
        clock: () => clock.now, logger: () => {} });
    return { owner, customer, treasury, token, rewards, store, assignment, identityStore, walletService, rewardService,
        sensor, live, iot, settler, clock, completion, directory, operatorWallet };
}

async function startSession(f) {
    const walletFirst = new WalletFirstService({ customerService: {}, walletService: f.walletService, sessionKey });
    const prepared = await walletFirst.prepareStartSession(USER_ID, { sessionRef: "v2-integration",
        taskSnapshot: f.assignment.policySnapshot });
    await f.live.reserveWalletStart(USER_ID, prepared.sessionId, f.assignment);
    const tx = await f.customer.sendTransaction({ to: prepared.to, data: prepared.data, value: prepared.value });
    const receipt = await tx.wait();
    const confirmed = await f.live.confirmWalletStart(USER_ID, prepared.sessionId, { transactionHash: tx.hash });
    expect(receipt.status).to.equal(1);
    expect(confirmed.stage).to.equal("RUNNING");
    if (f.httpFixture) {
        const sensor = await f.iot.get(prepared.sessionId);
        expect(sensor.boundary_source).to.equal('device_ack');
        expect(sensor.status).to.equal('running');
    } else expect(f.sensor.starts).to.equal(1);
    if (f.completion) {
        const tasks = new TaskService({ store: f.identityStore });
        tasks.sessionStore = f.store;
        const evidence = tasks.approveCompletion({ walletAddress: f.owner.address }, {
            taskId: TASK_ID, sessionId: prepared.sessionId, observedCycles: REQUIRED_CYCLES, reason: "verified test cycles"
        });
        expect(evidence.evidence.achieved).to.equal(true);
    }
    return prepared.sessionId;
}

describe("V2 task sensor-to-settlement lifecycle", function () {
    it('rejects an IoT safe flag with insufficient raw coverage before freezing a payout', async function () {
        const f = await fixture();
        const id = await startSession(f);
        const originalOutcome = f.iot.outcome;
        f.iot.outcome = async sessionId => {
            const document = await originalOutcome(sessionId);
            document.stats.sample_count = document.stats.raw_count = document.stats.processed_count = 20;
            document.result.dataValid = document.result.coverageValid = true;
            return document;
        };
        await f.live.end(USER_ID, id);
        const frozen = f.store.get(id);
        expect(frozen.settlementPayload.outcome).to.equal('INCOMPLETE');
        expect(frozen.evidence.result.reasonCodes).to.include('sample_count_below_task_minimum');
        expect(frozen.evidence.result.dataValid).to.equal(false);
        expect(frozen.evidence.result.coverageValid).to.equal(false);
        expect(frozen.evidence.taskEvidenceValidation.expectedSamples).to.equal(1080);
        const chain = await f.rewards.sessions(sessionKey(id));
        expect(chain.rewardPaid).to.equal(0n);
        expect(chain.penaltyCollected).to.equal(0n);
        expect(await f.rewards.accountingInvariant()).to.equal(true);
    });

    it("settles real closed sensor evidence once and preserves endedAt through delayed retry", async function () {
        const f = await fixture({ failSettlementOnce: true });
        const sessionId = await startSession(f);
        const startedAt = Number((await f.rewards.sessions(sessionKey(sessionId))).startedAt);
        const pending = await f.live.end(USER_ID, sessionId);
        expect(pending.stage).to.equal("SETTLEMENT_PENDING");
        expect(f.sensor.stops).to.equal(1);
        const frozen = f.store.get(sessionId);
        const payload = frozen.settlementPayload;
        expect(payload.endedAt).to.equal(startedAt + 120);
        expect(payload.endedAt).to.equal(Math.floor(Date.parse(frozen.evidence.session.ended_at) / 1000));
        expect(payload.evidenceHash).to.equal("0x" + require("node:crypto").createHash("sha256")
            .update(require("../../blockchain_api/eventPayload").canonicalJson(frozen.evidence)).digest("hex"));

        await time.increase(900);
        f.clock.now += 10000;
        await f.live.drain();
        const chain = await f.rewards.sessions(sessionKey(sessionId));
        expect(Number(chain.endedAt)).to.equal(startedAt + 120);
        expect(Number(chain.settledAt)).to.be.greaterThan(Number(chain.endedAt));
        expect(chain.evidenceHash).to.equal(payload.evidenceHash);
        expect(chain.rewardPaid).to.equal(ethers.parseEther("0.002"));
        expect(await f.rewards.accountingInvariant()).to.equal(true);
        expect(f.sensor.stops).to.equal(1);
        expect((await f.live.end(USER_ID, sessionId)).duplicate).to.equal(true);
    });

    it("refunds an otherwise safe task without completion evidence", async function () {
        const f = await fixture({ completion: false });
        const sessionId = await startSession(f);
        await f.live.end(USER_ID, sessionId);
        const chain = await f.rewards.sessions(sessionKey(sessionId));
        expect(chain.outcome).to.equal(3);
        expect(chain.rewardPaid).to.equal(0n);
        expect(chain.penaltyCollected).to.equal(0n);
        expect(chain.evidenceHash).not.to.equal(ethers.ZeroHash);
        expect(await f.rewards.accountingInvariant()).to.equal(true);
    });

    it("applies the fixed confirmed-violation penalty and keeps pool accounting exact", async function () {
        const f = await fixture({ outcome: "VIOLATION_CONFIRMED" });
        const sessionId = await startSession(f);
        await f.live.end(USER_ID, sessionId);
        const chain = await f.rewards.sessions(sessionKey(sessionId));
        expect(chain.outcome).to.equal(1);
        expect(chain.rewardPaid).to.equal(0n);
        expect(chain.penaltyCollected).to.equal(ethers.parseEther("0.010"));
        expect(await f.token.balanceOf(await f.rewards.getAddress())).to.equal(
            (await f.rewards.rewardPoolBalance()) + (await f.rewards.escrowedStakeTotal()));
        expect(await f.rewards.accountingInvariant()).to.equal(true);
    });

    it("refunds SENSOR_FAULT without reward or penalty", async function () {
        const f = await fixture({ outcome: "SENSOR_FAULT" });
        const sessionId = await startSession(f);
        await f.live.end(USER_ID, sessionId);
        const chain = await f.rewards.sessions(sessionKey(sessionId));
        expect(chain.outcome).to.equal(2);
        expect(chain.rewardPaid).to.equal(0n);
        expect(chain.penaltyCollected).to.equal(0n);
        expect(await f.token.balanceOf(f.customer.address)).to.equal(ethers.parseEther("1"));
        expect(await f.rewards.accountingInvariant()).to.equal(true);
    });
});

async function usePythonIot(f) {
    const root = path.resolve(__dirname, '../..');
    const python = process.env.HRC_TEST_PYTHON || path.join(root, '.venv/bin/python');
    const child = spawn(python, [path.join(__dirname, 'fixtures/python_iot_http.py'),
        path.join(f.directory, 'iot.db')], { cwd: root, stdio: ['ignore', 'pipe', 'pipe'],
        env: { ...process.env, HRC_APP_AUTOCREATE: '0' } });
    fixtureProcesses.push(child);
    let diagnostics = '';
    child.stderr.on('data', value => { diagnostics = (diagnostics + value).slice(-8000); });
    const port = await new Promise((resolve, reject) => {
        let buffer = '';
        const timeout = setTimeout(() => reject(new Error('Python IoT startup timed out: ' + diagnostics)), 15000);
        const finish = fn => value => { clearTimeout(timeout); fn(value); };
        child.once('error', finish(reject));
        child.once('exit', finish(() => reject(new Error('Python IoT exited before readiness: ' + diagnostics))));
        child.stdout.on('data', value => {
            buffer += value.toString();
            const end = buffer.indexOf('\n');
            if (end >= 0) {
                try { finish(resolve)(JSON.parse(buffer.slice(0, end)).port); }
                catch (error) { finish(reject)(error); }
            }
        });
    });
    const baseUrl = `http://127.0.0.1:${port}`;
    let ready = false;
    for (let attempt = 0; attempt < 100; attempt++) {
        if (child.exitCode !== null || child.signalCode !== null) throw new Error('Python IoT stopped: ' + diagnostics);
        try { ready = (await fetch(baseUrl + '/health', { signal: AbortSignal.timeout(1000) })).ok; } catch { /* startup */ }
        if (ready) break;
        await new Promise(resolve => setTimeout(resolve, 50));
    }
    expect(ready, diagnostics).to.equal(true);
    f.httpFixture = async (route, body) => {
        const response = await fetch(baseUrl + '/__fixture/' + route, {
            method: body ? 'POST' : 'GET', headers: { 'Content-Type': 'application/json' },
            body: body ? JSON.stringify(body) : undefined, signal: AbortSignal.timeout(15000) });
        const data = await response.json();
        expect(response.ok, JSON.stringify(data)).to.equal(true);
        return data;
    };
    const nextStart = Number(await time.latest()) + 1;
    await time.setNextBlockTimestamp(nextStart);
    await f.httpFixture('prepare', { timestamp: nextStart });
    // This Node client uses the real Python HTTP endpoints without changing
    // their response/outcome data. Only the fixture's MQTT and clock are fake.
    f.iot = new IotSessionClient({ baseUrl, timeoutMs: 15000 });
    f.clock.now = nextStart * 1000;
    f.live = new LiveSessionService({ walletService: f.walletService, iot: f.iot,
        settler: f.settler, completionStore: f.identityStore, clock: () => f.clock.now, logger: () => {} });
}

describe('V2 real Python IoT HTTP to real contract integration (synthetic transport)', function () {
    this.timeout(40000);
    for (const [scenario, expectedOutcome, reward, penalty, collectOptions = {}] of [
        ['safe', 0, '0.002', '0'], ['spike', 0, '0.002', '0'],
        ['repeated_spikes', 0, '0.002', '0'], ['sustained_violation', 1, '0', '0.010'],
        ['violation', 1, '0', '0.010'], ['fault', 2, '0', '0'], ['insufficient', 3, '0', '0'],
        ['safe', 0, '0.002', '0', { sampleCount: 1080 }],
        ['safe', 3, '0', '0', { sampleCount: 1079 }],
        ['safe', 3, '0', '0', { durationS: 119.999 }],
        ['timing_gap', 0, '0.002', '0', { gapS: 0.5 }],
        ['timing_gap', 3, '0', '0', { gapS: 0.500001 }],
        ['sequence_gap', 3, '0', '0'], ['boot_reset', 2, '0', '0']
    ]) it(`evaluates ${scenario} ${JSON.stringify(collectOptions)} telemetry in Python, freezes evidence and settles once`, async function () {
        const f = await fixture();
        await usePythonIot(f);
        const id = await startSession(f);
        const collected = await f.httpFixture('collect', { sessionId: id, scenario, ...collectOptions });
        const endedAt = Math.floor(Date.parse(collected.endedAt) / 1000);
        await time.increaseTo(endedAt);
        f.clock.now = endedAt * 1000;
        const settled = await f.live.end(USER_ID, id);
        expect(settled.stage, settled.lastError).to.equal('SETTLED');
        const frozen = f.store.get(id);
        expect(frozen.evidence.stats.sample_count).to.equal(collected.sampleCount - (scenario === 'boot_reset' ? 1 : 0));
        expect(frozen.evidence.stats.processed_coverage).to.equal(1);
        expect(frozen.evidence.session.boundary_source).to.equal('device_ack');
        expect(frozen.settlementPayload.endedAt).to.equal(endedAt);
        const chain = await f.rewards.sessions(sessionKey(id));
        expect(Number(chain.outcome)).to.equal(expectedOutcome);
        expect(chain.rewardPaid).to.equal(ethers.parseEther(reward));
        expect(chain.penaltyCollected).to.equal(ethers.parseEther(penalty));
        expect(chain.evidenceHash).to.equal(frozen.settlementPayload.evidenceHash);
        if (scenario === 'spike' || scenario === 'repeated_spikes') {
            expect(frozen.evidence.stats.violation_count).to.equal(scenario === 'spike' ? 1 : 300);
            expect(frozen.evidence.stats.confirmed_violation_count).to.equal(0);
        }
        expect((await f.live.end(USER_ID, id)).duplicate).to.equal(true);
        expect((await f.httpFixture('diagnostics')).commands).to.deep.equal(['session_start', 'session_stop']);
        expect(await f.rewards.accountingInvariant()).to.equal(true);
    });

    it('recovers after coordinator recreation and delayed RPC retry without re-closing the sensor', async function () {
        const f = await fixture({ failSettlementOnce: true });
        await usePythonIot(f);
        const id = await startSession(f);
        const collected = await f.httpFixture('collect', { sessionId: id, scenario: 'safe' });
        const endedAt = Math.floor(Date.parse(collected.endedAt) / 1000);
        await time.increaseTo(endedAt);
        f.clock.now = endedAt * 1000;
        expect((await f.live.end(USER_ID, id)).stage).to.equal('SETTLEMENT_PENDING');
        const before = f.store.get(id);
        f.walletService.store = new JsonSessionStore(path.join(f.directory, 'sessions.json'));
        const durable = new DurableContractWriter({ provider: ethers.provider, store: f.walletService.store, waitMs: 0 });
        f.walletService.transactionSender = durable;
        f.rewardService.transactionSender = durable;
        f.live = new LiveSessionService({ walletService: f.walletService, iot: f.iot, settler: f.settler,
            completionStore: f.identityStore, clock: () => f.clock.now, logger: () => {} });
        await time.increase(900);
        f.clock.now += 900000;
        await f.live.drain();
        const after = f.walletService.store.get(id);
        expect(after.stage, after.lastError).to.equal('SETTLED');
        expect(after.settlementPayload).to.deep.equal(before.settlementPayload);
        expect(after.evidence).to.deep.equal(before.evidence);
        const chain = await f.rewards.sessions(sessionKey(id));
        expect(Number(chain.endedAt)).to.equal(endedAt);
        expect(chain.rewardPaid).to.equal(ethers.parseEther('0.002'));
        expect((await f.httpFixture('diagnostics')).commands).to.deep.equal(['session_start', 'session_stop']);
        expect(await f.rewards.accountingInvariant()).to.equal(true);
    });
});

async function useSqliteSessions(f) {
    const store = new SqliteSessionStore(path.join(f.directory, 'wallet.db'));
    sqliteStores.push(store);
    f.store = f.walletService.store = store;
    const writer = new DurableContractWriter({ provider: ethers.provider, store, waitMs: 0, chainId: 31337 });
    f.walletService.transactionSender = f.rewardService.transactionSender = writer;
    f.live = new LiveSessionService({ walletService: f.walletService, iot: f.iot, settler: f.settler,
        completionStore: f.identityStore, clock: () => f.clock.now, logger: () => {} });
    return writer;
}

async function useSiweHttp(f) {
    const identities = new SqliteCustomerStore(path.join(f.directory, 'identity.db'));
    sqliteStores.push(identities);
    const customers = new CustomerService({ store: identities, chainId: 31337,
        operatorWallets: [f.owner.address], appOrigin: 'http://localhost:8081' });
    const app = createApp({ service: {}, customerService: customers, walletService: f.walletService,
        liveService: f.live, tokens: { HRC_APP_AUTH_MODE: 'siwe', HRC_REWARD_PROTOCOL_VERSION: 'task-rewards-v2',
            HRC_LOCAL_FUNDING_ENABLED: 'false', HRC_INGEST_API_TOKEN: 'i'.repeat(40) } });
    const server = app.listen(0, '127.0.0.1');
    httpServers.push(server);
    await new Promise((resolve, reject) => { server.once('listening', resolve); server.once('error', reject); });
    const baseUrl = `http://127.0.0.1:${server.address().port}`;
    const call = async (route, token, body, headers = {}) => {
        const response = await fetch(baseUrl + route, { method: 'POST',
            headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: 'Bearer ' + token } : {}), ...headers },
            body: JSON.stringify(body), signal: AbortSignal.timeout(15000) });
        return { status: response.status, body: await response.json() };
    };
    const login = async signer => {
        const challenge = await call('/api/auth/challenge', null, { walletAddress: signer.address, chainId: 31337 });
        expect(challenge.status).to.equal(200);
        const result = await call('/api/auth/wallet', null, { walletAddress: signer.address, chainId: 31337,
            message: challenge.body.message, signature: await signer.signMessage(challenge.body.message) });
        expect(result.status).to.equal(200);
        return result.body;
    };
    return { call, login };
}

describe('V2 durable receipt recovery and HTTP authorization (isolated real chain)', function () {
    this.timeout(40000);

    for (const failure of ['pending', 'receipt-outage-after-broadcast']) {
        it(`recovers ${failure} through SQLite reopen using one signed transaction and one settlement event`, async function () {
            const f = await fixture();
            await usePythonIot(f);
            await useSqliteSessions(f);
            const id = await startSession(f);
            const collected = await f.httpFixture('collect', { sessionId: id, scenario: 'safe' });
            const endedAt = Math.floor(Date.parse(collected.endedAt) / 1000);
            await time.increaseTo(endedAt);
            f.clock.now = endedAt * 1000;
            const broadcasts = [];
            let nonceReads = 0;
            let receiptUnavailable = false;
            const proxy = new Proxy(ethers.provider, {
                get(target, property) {
                    if (property === 'getTransactionCount') return async (...args) => {
                        nonceReads++;
                        return target.getTransactionCount(...args);
                    };
                    if (property === 'getTransactionReceipt') return async (...args) => {
                        if (receiptUnavailable) throw new Error('synthetic receipt RPC outage');
                        return target.getTransactionReceipt(...args);
                    };
                    if (property === 'broadcastTransaction') return async raw => {
                        // Read from a separate SQLite handle before broadcast to
                        // prove signed bytes have been committed durably.
                        const persisted = new SqliteSessionStore(path.join(f.directory, 'wallet.db'));
                        try { expect(persisted.getTransaction(`settlement:${id}`).rawTransaction).to.equal(raw); }
                        finally { persisted.close(); }
                        broadcasts.push(raw);
                        const transaction = await target.broadcastTransaction(raw);
                        if (failure === 'receipt-outage-after-broadcast') {
                            receiptUnavailable = true;
                            throw new Error('synthetic lost broadcast response');
                        }
                        return transaction;
                    };
                    const value = Reflect.get(target, property, target);
                    return typeof value === 'function' ? value.bind(target) : value;
                }
            });
            const wallet = f.operatorWallet.connect(proxy);
            f.rewardService.wallet = wallet;
            f.rewardService.contract = f.rewards.connect(wallet);
            f.rewardService.transactionSender = new DurableContractWriter({ provider: proxy, store: f.store, waitMs: 0, chainId: 31337 });
            if (failure === 'pending') await network.provider.send('evm_setAutomine', [false]);
            expect((await f.live.end(USER_ID, id)).stage).to.equal('SETTLEMENT_PENDING');
            const before = f.store.get(id);
            const signedBefore = f.store.getTransaction(`settlement:${id}`);
            expect(['SIGNED', 'PENDING']).to.include(signedBefore.status);
            expect(broadcasts).to.have.length(1);
            expect(nonceReads).to.equal(1);
            const transaction = ethers.Transaction.from(signedBefore.rawTransaction);
            const decoded = f.rewards.interface.parseTransaction({ data: transaction.data });
            expect(Number(decoded.args.endedAt)).to.equal(endedAt);
            expect(decoded.args.evidenceHash).to.equal(before.settlementPayload.evidenceHash);
            if (failure === 'pending') {
                expect((await f.rewards.sessions(sessionKey(id))).settled).to.equal(false);
                // Another retry before mining must reuse the exact same signed bytes.
                await f.live.end(USER_ID, id);
                expect(broadcasts).to.have.length(2);
                expect(broadcasts[1]).to.equal(broadcasts[0]);
                expect(nonceReads).to.equal(1);
                await network.provider.send('evm_mine');
                await network.provider.send('evm_setAutomine', [true]);
            }
            receiptUnavailable = false;
            f.store.close();
            const reopened = new SqliteSessionStore(path.join(f.directory, 'wallet.db'));
            sqliteStores.push(reopened);
            f.store = f.walletService.store = reopened;
            f.rewardService.transactionSender = new DurableContractWriter({ provider: proxy, store: reopened, waitMs: 0, chainId: 31337 });
            f.live = new LiveSessionService({ walletService: f.walletService, iot: f.iot, settler: f.settler,
                completionStore: f.identityStore, clock: () => f.clock.now, logger: () => {} });
            f.clock.now += 30000;
            await f.live.drain();
            const after = reopened.get(id);
            expect(after.stage, after.lastError).to.equal('SETTLED');
            expect(after.settlementPayload).to.deep.equal(before.settlementPayload);
            expect(after.evidence).to.deep.equal(before.evidence);
            const saved = reopened.getTransaction(`settlement:${id}`);
            expect(saved.status).to.equal('CONFIRMED');
            expect(saved.rawTransaction).to.equal(signedBefore.rawTransaction);
            expect(saved.transactionHash).to.equal(signedBefore.transactionHash);
            expect(after.settlementResult.transactionHash).to.equal(saved.transactionHash);
            expect(nonceReads).to.equal(1);
            expect(broadcasts).to.have.length(failure === 'pending' ? 2 : 1);
            const events = await f.rewards.queryFilter(f.rewards.filters.SessionSettled(sessionKey(id)));
            expect(events).to.have.length(1);
            expect(events[0].args.endedAt).to.equal(BigInt(endedAt));
            expect((await f.live.end(USER_ID, id)).duplicate).to.equal(true);
            expect((await f.httpFixture('diagnostics')).commands).to.deep.equal(['session_start', 'session_stop']);
            expect(await f.token.balanceOf(f.customer.address)).to.equal(ethers.parseEther('1.002'));
            expect(await f.rewards.accountingInvariant()).to.equal(true);
        });
    }

    it('rejects non-operators, wrong owners, beneficiaries and user-chosen outcomes through the real SIWE HTTP API without moving tokens', async function () {
        const f = await fixture();
        await usePythonIot(f);
        const http = await useSiweHttp(f);
        const login = await http.login(f.customer);
        const user = f.walletService.walletUser(f.customer.address);
        const other = await http.login((await ethers.getSigners())[3]);
        const operator = await http.login(f.owner);
        // Keep this assignment tied to the canonical SIWE wallet identity.
        f.assignment.userId = user.userId;
        const prepared = await new WalletFirstService({ customerService: {}, walletService: f.walletService, sessionKey })
            .prepareStartSession(user.userId, { sessionRef: 'siwe-v2', taskSnapshot: f.assignment.policySnapshot });
        await f.live.reserveWalletStart(user.userId, prepared.sessionId, f.assignment);
        const tx = await f.customer.sendTransaction({ to: prepared.to, data: prepared.data, value: prepared.value });
        await tx.wait();
        const id = prepared.sessionId;
        expect((await http.call(`/api/me/sessions/${id}/confirm-start`, login.token, { transactionHash: tx.hash })).status).to.equal(200);
        const initial = await Promise.all([f.token.balanceOf(f.customer.address), f.token.balanceOf(f.treasury.address),
            f.token.balanceOf(await f.rewards.getAddress()), f.rewards.rewardPoolBalance(), f.rewards.escrowedStakeTotal()]);
        const cases = [
            ['/api/session-outcome', login.token, { sessionId: id, outcome: 'COMPLETED_SAFE' }, 403, 'FORBIDDEN'],
            ['/api/session-outcome', 'i'.repeat(40), { sessionId: id, outcome: 'COMPLETED_SAFE' }, 401, 'UNAUTHORIZED'],
            ['/api/session-outcome', operator.token, { sessionId: id, outcome: 'COMPLETED_SAFE' }, 410, 'TASK_SETTLEMENT_ROUTE_RETIRED'],
            [`/api/admin/task-sessions/${id}/settle`, login.token, {}, 403, 'FORBIDDEN'],
            [`/api/me/sessions/${id}/end`, other.token, {}, 403, 'SESSION_OWNER_MISMATCH'],
            [`/api/me/sessions/${id}/end`, login.token, { beneficiary: f.owner.address }, 400, 'INVALID_PAYLOAD'],
            [`/api/me/sessions/${id}/end`, login.token, { outcome: 'COMPLETED_SAFE', rewardPaid: '999' }, 400, 'INVALID_PAYLOAD'],
            [`/api/admin/task-sessions/${id}/settle`, operator.token, { beneficiary: f.owner.address }, 400, 'INVALID_PAYLOAD'],
            [`/api/admin/task-sessions/${id}/settle`, operator.token, {}, 409, 'EVIDENCE_NOT_FROZEN'],
            [`/api/sessions/${id}/completion-evidence`, login.token, { taskId: TASK_ID, observedCycles: 999, reason: 'self approved' }, 403, 'FORBIDDEN'],
        ];
        for (const [route, token, body, status, code] of cases) {
            const response = await http.call(route, token, body, { 'x-hrc-actor': f.owner.address });
            expect(response.status, route + ': ' + JSON.stringify(response.body)).to.equal(status);
            expect(response.body.code).to.equal(code);
        }
        const after = await Promise.all([f.token.balanceOf(f.customer.address), f.token.balanceOf(f.treasury.address),
            f.token.balanceOf(await f.rewards.getAddress()), f.rewards.rewardPoolBalance(), f.rewards.escrowedStakeTotal()]);
        expect(after).to.deep.equal(initial);
        expect(f.store.listTransactions()).to.have.length(0);
        expect((await f.rewards.sessions(sessionKey(id))).settled).to.equal(false);
        expect((await f.httpFixture('diagnostics')).commands).to.deep.equal(['session_start']);
        expect(await f.rewards.accountingInvariant()).to.equal(true);
    });

    it('does not penalize a forged confirmed-violation result without confirmed event evidence', async function () {
        const f = await fixture({ outcome: 'VIOLATION_CONFIRMED' });
        const id = await startSession(f);
        const original = f.iot.outcome;
        f.iot.outcome = async sessionId => {
            const document = await original(sessionId);
            document.stats.confirmed_violation_count = 0;
            return document;
        };
        await f.live.end(USER_ID, id);
        const record = f.store.get(id);
        expect(record.settlementPayload.outcome).to.equal('INCOMPLETE');
        expect(record.evidence.result.reasonCodes).to.include('confirmed_violation_evidence_missing');
        expect((await f.rewards.sessions(sessionKey(id))).penaltyCollected).to.equal(0n);
        expect(await f.token.balanceOf(f.customer.address)).to.equal(ethers.parseEther('1'));
    });
});
