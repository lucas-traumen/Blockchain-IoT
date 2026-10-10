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

No token is generated, printed, or embedded in the frontend. Missing/short tokens
disable their protected routes with HTTP 503. Send `Authorization: Bearer <token>`.
Tokens are bearer credentials; use a trusted local connection or HTTPS when exposing
the API. Ingestion token does not grant admin access, and admin token does not grant
ingestion access. Keep tokens distinct and outside source control.

`POST /api/safety-event` requires ingestion token.
`POST /api/robots/:deviceId/unlock` requires admin token.
`GET /api/events/:eventId/payload` requires admin token.
Existing public event and robot status GET routes do not need a token.

The unchanged dashboard does **not** send an admin token. Its Unlock action now
receives 401 until a separate authenticated admin client is used. Never embed an
admin token in public frontend JavaScript. MetaMask/authenticated UI is outside
this change.

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
Deleting it loses retry/archive history; on-chain data cannot reconstruct payloads.
The store is for a **single blockchain_api writer** with exclusive use of the signing
wallet. Do not use the same wallet in concurrent scripts or other services. A
pending operation deliberately blocks new writes until resolved. Recovery is
request-driven; no background worker or automatic startup broadcast is installed.
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
They never load .env, contact a live RPC/Sepolia network, redeploy a contract, or leave servers
running. They do not substitute for executing contract tests or hardware validation.

Dashboard robot control uses MetaMask on the local Hardhat network (`chainId=31337`)
for this demo and enables Lock/Unlock
only when the connected account matches the contract owner. It no longer asks
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
