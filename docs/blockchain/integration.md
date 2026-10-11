# HRC blockchain API: ingestion, archived hashes and transaction recovery

Contract and deployment remain unchanged. Public event/status routes retain their
response shapes. This document describes the new ingestion contract; historical
on-chain events and hashes are not rewritten or reconstructed.

## Configuration without editing .env

Set these environment variables in the **blockchain_api process**, before starting it:

- `HRC_INGEST_API_TOKEN`: random secret of at least 32 characters, for the gateway.
- `HRC_ADMIN_API_TOKEN`: a different random secret of at least 32 characters, for
  administrative unlock and archived payload access.
- `HRC_EVENT_STORE_PATH`: optional directory; default `blockchain_api/data/events`.
- `HRC_CUSTOMER_STORE_PATH`: optional JSON file for application users, wallet
  bindings, app sessions and behavior; default `blockchain_api/data/customers.json`.
- `HRC_BLOCKCHAIN_NETWORK`: `localhost`, `sepolia` or `amoy`; it deliberately
  does not use the generic host variable `NETWORK`.
- `HRC_APP_CHAIN_ID`: chain used by wallet challenge login. If omitted, it follows
  `HRC_BLOCKCHAIN_NETWORK` (`31337` localhost, `11155111` Sepolia, `80002` Amoy).
- `HRC_APP_ADMIN_WALLETS`: comma-separated wallet addresses allowed to read the
  customer directory.
- `HRC_APP_ORIGIN`: HTTP(S) origin included in the signed challenge text; default
  `http://localhost:8080`. Keep it aligned with the dashboard origin.

No ingest/admin token is generated, printed, or embedded in the frontend. Missing/short tokens
disable their protected routes with HTTP 503. Send `Authorization: Bearer <token>`.
Tokens are bearer credentials; use a trusted local connection or HTTPS when exposing
the API. Ingestion token does not grant admin access, and admin token does not grant
ingestion access. Keep tokens distinct and outside source control.

`POST /api/safety-event` requires ingestion token.
`POST /api/robots/:deviceId/unlock` requires admin token.
`GET /api/events/:eventId/payload` requires admin token.
Existing public event and robot status GET routes do not need a token.

The dashboard does **not** send an admin or ingest token. Robot API clients still
use those protected routes. The default local dashboard signs into a fixed demo
account with a separate app session. Default `HRC_APP_AUTH_MODE=demo` also blocks
legacy EIP-191 wallet registration before creating customer data. The separate
legacy application requires explicit `HRC_APP_AUTH_MODE=wallet`. Never embed an operator token in frontend JavaScript.

## Local demo accounts and session transactions

Open `http://127.0.0.1:8080/` and click **DEMO ACCOUNT**. The compact picker
contains exactly `admin`, `user_1`, and `user_2`; it selects the current account,
or admin on first opening. **Open account** logs in and closes the picker; RPC
read errors appear in the wallet workspace. **My Wallet / My Sessions** lives
inside Dashboard and Reports. Navigation remains Dashboard, Live Monitoring,
Safety Events and Reports.

The two wallets derive from fixed Hardhat indexes in `demo_wallets.js`. The
backend keeps their private keys; the whitelist does not register arbitrary
wallets as demo customers. New deployments seed 20 HRC per user. Restarting the
API preserves identity, session references and transactions; app tokens are
in memory and require another login. Admin sees both users with no transaction
controls and cannot call customer Approve/Start/End routes.

Configuration:

- `HRC_APP_AUTH_MODE=demo`: fixed accounts only; arbitrary wallet registration is disabled.
- `RPC_URL`: the existing local Hardhat RPC, normally `http://127.0.0.1:8545`.
- `HRC_REWARD_DEPLOYMENT`: manifest for that node's token/reward deployment.
- `HRC_DEMO_MNEMONIC`: local Hardhat mnemonic, retained on the backend.
- `HRC_DEMO_INITIAL_BALANCE`: initial funding for a new deployment, default 20.
- `HRC_DEMO_SESSION_STORE_PATH`: durable reference/evidence/transaction store,
  default `blockchain_api/data/demo-sessions.json`; retain it on API restart.
- `HRC_IOT_API_URL`: the actual IoT API, normally `http://127.0.0.1:8001`.
- `HRC_DEMO_DEVICE_ID`: the shared sensor, default `esp32_01`.
- IoT: `HRC_SOURCE_MODE=esp32`, `HRC_REWARD_AUTO_START=false`, and
  `HRC_REWARD_AUTO_SETTLE=false`. Disable the fixed-beneficiary bridge while
  the Node coordinator binds sensor sessions to individual demo accounts.

| Route | Access and behavior |
| --- | --- |
| `GET /api/demo/accounts` | Fixed whitelist and public addresses |
| `POST /api/demo/login` | `{userId}`; returns a separate demo app token |
| `POST /api/demo/logout` | Revokes the authenticated demo token |
| `GET /api/me/wallet` | Available balance, locked stake, allowance, device readiness, active/pending ID |
| `POST /api/me/wallet/approve` | Signs ERC-20 allowance for 2 HRC; balance does not change |
| `POST /api/me/sessions/start` | Stable `{sessionRef}`; opens matching IoT session, then stakes 2 HRC |
| `POST /api/me/sessions/:sessionId/end` | Owner only; closes sensor session, freezes outcome and operator settlement |
| `GET /api/me/sessions` | Only this user's sessions, including sensor progress and outcome reason |
| `GET /api/me/sessions/:sessionId` | Owned contract state, sensor metadata and events |
| `GET /api/me/wallet/transactions` | Wallet-filtered Approval, Transfer and reward events with amounts |
| `GET /api/demo/admin/overview` | Read-only balances and sessions for both users |

The Start preflight requires local chain 31337, an authorized operator, the
configured ESP32 source, recent telemetry (at most five seconds), an idle
sensor, allowance and balance. Only one user can measure on the shared device
at a time. A durable reservation precedes opening the sensor; the user's signed
stake is persisted before broadcast. A definite preparation failure closes the
new sensor session without charging the user. A lost response resumes that
same device/contract session. IDs must fit the firmware's 63-character limit.

End accepts an empty body. The backend closes the matching sensor session and
reads `/sessions/:id/outcome`; IoT processes only pending raw samples inside
that session's device/boot/sequence boundary. It does not replay the historical
raw table or refeed old rows into the live model on outcome reads. The outcome,
evidence hash and timestamp are frozen before operator submission. Sensor closure
and evidence can finish while the operator is offline; settlement waits for RPC.
Users cannot submit outcome, beneficiary, reward or penalty. The protected
`POST /api/session-outcome` operator route remains available to backend clients;
a demo app token cannot call it.

`HRCSessionRewards` decides all amounts: valid returns 2 HRC and pays up to 3 HRC
once per beneficiary per UTC day if the pool allows; confirmed violation moves
the locked 2 HRC to treasury; fault/incomplete returns the stake without reward.
A valid session requires at least 30 seconds and 20 samples, with valid coverage
and no confirmed violation. The UI shows the reason and daily limit separately.
**Recent wallet activity** is visible without expanding transaction history:
approve has no wallet delta, stake has -2 available/+2 locked, and settlement
shows stake return plus reward or penalty from the existing locked amount.
Hashes/blocks/events and token transfers substantiate the displayed amounts.

Device lifecycle and signer writes are serialized. Background recovery runs
once every five seconds with retry backoff and coalesces overlapping scans.
It reuses the persisted reference, immutable settlement payload and signed
transaction bytes/nonce after a restart or uncertain broadcast. A reverted or
corrupted transaction requires recovery rather than allocating a replacement.
The browser also retains a pending reference until refreshed state shows a
confirmed or failed Start; repeated Start/End never stakes or settles twice.

Old stakes with no matching IoT session are refunded by the operator as
`INCOMPLETE`, source `recovery`, reason `missing_iot_session`. They have real
settlement receipts and receive no reward or penalty. Keep the existing chain,
deployment and store. Hardhat's default node is in memory: restarting it can
remove contracts and historical receipts. The API checks saved deployment and
confirmed receipt anchors; a chain missing that history cannot accept a new
stake or reuse a saved confirmed response. API persistence is not a chain backup.

From the repo root, run `node scripts/verify_demo_wallet_ui.mjs` for read-only
Chrome input/click, isolation, refresh, admin/report and desktop/mobile checks.
`--transact` uses one real ESP32 session, including at least 31 seconds of samples,
then verifies End, the measured outcome, balance, token ledger and repeated End.
It resumes a user's existing active session, or the ID in `HRC_UI_SESSION_ID`,
instead of opening another stake on retry. It needs no operator token and injects
no simulated outcome. `HRC_CHAIN_API_URL` can select a separate test API in both
the browser and verifier. Evidence is written to
`data/results/live-wallet-sessions-20261011/` or `HRC_UI_EVIDENCE_DIR`.

To exercise real ESP32 data on a disposable chain without touching the live
manifests, store or RPC, keep the IoT API and dashboard running with an idle
sensor, then run from the repo root:

```bash
(cd smart_contracts && npx hardhat run scripts/verify_live_wallet.js --network hardhat)
```

This deploys actual test contracts in process, verifies Approve/Start/End through
Chromium, and then resumes the same settled session to check browser retry.
It uses the real IoT HTTP endpoints, not sensor fixtures. Evidence is written
under `isolated-chain/` and `isolated-chain/resume/`; it does not restore a stopped
original Hardhat node or complete a pending session on that original chain.

`smart_contracts/test/DemoLiveSessions.js` independently tests the HTTP API and
real contracts on an isolated in-process Hardhat chain. Its sensor HTTP fixtures
are explicitly simulated and cover valid, daily limit, violation, fault,
incomplete, offline sensor, signed-transaction restart, orphan recovery and roles.
These checks do not establish hardware behavior; live verification records its
own measured session and receipts.

## Application wallet login and behavior

With explicit `HRC_APP_AUTH_MODE=wallet`, the legacy MetaMask client supports
owner-only robot controls and application login that binds a wallet to a customer
record. The default demo blocks the challenge and login endpoints with 403. This is separate from the default
demo account chooser. A client requests a challenge, signs the exact message with
`personal_sign`, and sends the signature to the API. The API verifies the recovered
address before issuing an app session token. No private key leaves the wallet and
no wallet address is accepted from an unsigned request.

Routes:

- `POST /api/auth/challenge` — public challenge for `{walletAddress, chainId}`.
- `POST /api/auth/wallet` — verifies `{walletAddress, chainId, message, signature}`.
- `GET /api/me` — current signed-in user.
- `POST /api/auth/logout` — revoke the current app session.
- `PATCH /api/me/profile` — update the authenticated display name.
- `POST /api/behavior` — authenticated behavior event with an idempotency key.
- `GET /api/me/behavior` — authenticated user's activity history.
- `GET /api/customers` — admin-wallet-only customer directory.
- `GET /api/customers/:userId/behavior` — admin-wallet-only customer activity.

Behavior records are off-chain product telemetry, not safety evidence. Safety
telemetry, blockchain event payloads and reward evidence keep their existing
stores and authorization boundaries. The legacy wallet path records wallet login,
page views, dashboard refreshes, reward loading, event detail views and robot
control outcomes. Duplicate events with the same user and idempotency key return the
original record without creating a second row. Reusing a key with another payload
returns `409 BEHAVIOR_CONFLICT`. The dashboard retries transient failures and tags
queued events with the wallet that produced them, then drops a queued event if a
different wallet is active.

## Payload v1

```json
{
  "schemaVersion": "1.0",
  "deviceId": "ROBOT-ARM-01",
  "bootId": "boot-001",
  "sessionId": "SESSION-001",
  "sequence": 1,
  "timestamp": 1790996000,
  "sensorData": { "distance": 0.18, "speed": 2.1, "force": 15.2 },
  "aiResult": { "model": "LSTM", "state": "VIOLATION", "riskLevel": 3 }
}
```

- `deviceId`, `timestamp`, nonempty `sensorData`, and `aiResult.riskLevel` required.
- Timestamp must be a positive safe integer in Unix **seconds**. Producer owns
  measurement time; blockchain_api no longer substitutes its current time. Future skew
  beyond 300 seconds is rejected. Preserve measurement/receipt provenance as
  metadata if the device clock is not synchronized.
- Risk must be a JSON **number** integer 0–3; strings, null and booleans rejected.
- Provide a stable explicit `eventId`, **or** `sessionId`/`bootId` and positive
  integer `sequence`. Generated ID hashes device/boot/session/sequence, excluding
  timestamp. Keep source identifiers stable across retries and change boot/session
  when sequence resets. Explicit event IDs must be globally unique on the contract.
- Identifiers accept 1–128 ASCII letters/digits and `_ . : -`.
- Known sensor numbers must be finite/nonnegative; `distance: null` requires
  `valid: false`. Other bounded JSON metadata is preserved, without interpreting
  it as a sensor measurement or enforcing a particular model's output shape.
- Use meters, seconds, meters/second and newtons for distance/speed/force at the
  ingestion boundary. Converting upstream units is the gateway's responsibility.
- Optional state: CLEAR/LOW/WARNING/VIOLATION/UNKNOWN. VIOLATION requires risk 3;
  WARNING requires 2; LOW requires 1. CLEAR with risk 2 requires `earlyWarning:true`.
  Invalid sensors and UNKNOWN require explicit risk 2 or 3; no implicit SAFE.
  Choice of UNKNOWN policy remains with the producer; only risk 3 auto-locks.
- Body limit 64 KiB; max depth 8; max 100 fields/object, 1000 items/array,
  4096 characters/string. Unknown top-level fields and reserved keys are rejected.

## Canonical payload and device signature

`eventPayload.preparePayload()` normalizes these fields before hashing:
`schemaVersion,eventId,deviceId,timestamp,bootId,sessionId,sequence,sensorData,aiResult`.
Absent optional source fields normalize to null. Signature itself is excluded.
`hrc-canonical-json-v1` sorts object keys recursively, keeps array order, and uses
JavaScript JSON number/string encoding. Hash is Keccak-256 of UTF-8 bytes.
This is a **versioned API convention, not an RFC 8785/JCS implementation**. Producers
in another language must match these encodings (especially floats); using the JS
gateway helper avoids cross-language differences. No existing historical hash is
recomputed using this convention.

A registered device must send `signature` (65-byte hex) from its registered key.
The existing unsigned path remains available to unregistered devices via a trusted
ingestion gateway token; it does not prove device authorship. Registration and key
provisioning themselves remain outside this change.

Gateway signing example, using its device key through a secure signer:

```js
const { preparePayload, TYPES } = require("./eventPayload");
const prepared = preparePayload(input);
const domain = {
  name: "HRCSafetyLog", version: "1", chainId: 11155111,
  verifyingContract: "0xF6CD683a1E46357Ae9A6e8e68D7d0094bfacd49C"
};
const signature = await deviceSigner.signTypedData(domain, TYPES, {
  eventId: prepared.payload.eventId,
  deviceId: prepared.payload.deviceId,
  timestamp: prepared.payload.timestamp,
  dataHash: prepared.dataHash,
  riskLevel: prepared.riskLevel
});
// POST the original input plus signature; don't insert normalized null source fields.
```

Backend verifies live chain ID/contract, registration/revocation and EIP-712 signer
before selecting `recordSafetyEventSigned`. Missing/wrong signature returns 403.

## Storage, idempotency and recovery

Atomic JSON snapshots retain canonical payload, hash, operation, signed transaction
bytes/hash, block and status. Files are flushed before broadcasting. Payload/hash
cannot change for an existing ID (409). Concurrent requests are serialized. A
second process using the same store is refused; stale writer locks are recovered
only if the prior PID no longer exists. Incomplete locks/recovery locks require
operator inspection; do not delete locks while a writer is running.

Transaction lifecycle:

```
prepared -> signed (persisted before send) -> pending -> confirmed / failed
```

- 200: confirmed; existing response fields retained, plus `transactionStatus`,
  `duplicate` and `hashFormat`.
- 202: still pending/uncertain; transaction hash returned, robot state and E-Stop
  occurrence are null. Repeat the **same authenticated POST and original payload**
  to reconcile or rebroadcast the exact same signed bytes, including after restart.
- 409: conflicting payload, reverted receipt, existing historical on-chain event,
  or another unresolved transaction preventing safe nonce allocation.
- 400/401/403/413: validation/auth/signature/body errors. RPC/storage problems 503.

No replacement transaction or new event is silently created after a timeout.
Failed receipts are persisted, not automatically retried. Unlock operations use
the current lock event/timestamp as their identity and the same durable send path.
The archived payload route exposes no raw signed transaction or private key.

Back up the store, restrict filesystem access and preserve it across restart.
The customer store uses an atomic JSON snapshot and a PID lock; a lock is recovered
only when its recorded process no longer exists. A running second writer is refused.
Deleting it loses retry/archive history; on-chain data cannot reconstruct payloads.
The store is for a **single blockchain_api writer** with exclusive use of the signing
wallet. Do not use the same wallet in concurrent scripts or other services. A
pending operation deliberately blocks new writes until resolved. Safety-event recovery is
request-driven. The account-bound live-session coordinator separately runs its
background recovery described above.
One mined receipt is used; full chain-finality/reorg handling and fee replacement
remain outside this implementation. Filesystem flush/rename is not a distributed
database or a guarantee against hardware/filesystem failure.

## Tests

```powershell
cd blockchain_api
npm test
```

Tests exercise real Express HTTP on temporary loopback ports, real ethers signing
and verification, temporary store persistence, and a simulated blockchain transport.
Service dependencies are injected with simulated transports; the npm suite does
not contact a live RPC, redeploy the running contracts or leave HTTP servers
running. The browser verifier above separately uses the running local demo.
These checks do not substitute for contract tests or hardware validation.

The optional legacy robot wallet controls use MetaMask on the local Hardhat
network (`chainId=31337`) and enable Lock/Unlock only when the connected account
matches the contract owner. It no longer asks
for an admin token. Lock references an existing WARNING/VIOLATION event for the
same robot; the wallet verifies the event and the owner's operator permission
required by the unchanged contract. Unlock warns when the latest recorded state
is still VIOLATION. Pending transactions require refreshing status before retrying.
The transaction history includes EmergencyStopTriggered as ROBOT_LOCK and keeps
automatic lock plus safety recording in one row when they share a transaction hash.
This owner restriction for Lock is a dashboard policy: the deployed contract
still permits operators to call triggerEmergencyStop directly. The existing admin
access and blockchain_api unlock APIs retain their token and owner checks for API clients.
No private keys or tokens are entered into the dashboard.

## Session rewards bridge

The following fixed-beneficiary bridge is for standalone IoT sessions. Keep its
auto-start/auto-settle flags disabled when using account-bound Dashboard sessions.

The IoT service can settle a closed session through `blockchain_api` without
holding a wallet key. Configure the bridge in `compose.env` with the API URL,
ingest token and beneficiary, then enable `HRC_REWARD_AUTO_START` and
`HRC_REWARD_AUTO_SETTLE`. The start call stakes 2 HRC; the outcome call lets the
contract apply the fixed policy for `VALID_NO_VIOLATION`, `VIOLATION_CONFIRMED`,
`SENSOR_FAULT` or `INCOMPLETE`.

The IoT SQLite database contains the `reward_settlements` outbox. It records the
deterministic evidence hash and status before the HTTP call. A failed attempt is
`ERROR` and can be retried with `POST /sessions/{session_id}/settle`; a successful
row is `SETTLED` and subsequent retries return the saved response without sending
another transaction. No historical session is automatically staked or settled.

Use `npm test` to run the offline service regression suite and integration tests.
`node e2eTest.js` is still a **live transaction-producing script**; it requires the
ingestion token in the shell, uses a fresh session and retries 202 with the same
payload. It is not part of `npm test` and was not run for this change.
