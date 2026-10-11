#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_ROOT="$ROOT_DIR/.runtime/local-demo"
RUN_ID="$(date -u +%Y%m%d%H%M%S)-$$"
RUN_DIR="$RUNTIME_ROOT/$RUN_ID"
DEPLOYMENT_DIR="$RUN_DIR/deployments"
LOG_DIR="$RUN_DIR/logs"
IOT_DATA_DIR="$RUN_DIR/iot-data"
RPC_URL="http://127.0.0.1:8545"
API_URL="http://127.0.0.1:3000"
BLOCKCHAIN_ONLY=false
SMOKE=false
SKIP_INSTALL=false
COMPOSE_STARTED=false
HARDHAT_PID=""
API_PID=""

usage() {
    cat <<'EOF'
Usage: ./scripts/run_local_demo.sh [options]

Starts a fresh Hardhat chain, deploys both contracts and runs blockchain_api.
By default it also starts MQTT, IoT/AI and the dashboard with Docker Compose.

  --blockchain-only  Start only Hardhat and blockchain_api
  --smoke            Verify the clean blockchain/API startup, then exit
  --skip-install     Do not run npm ci when node_modules is missing
  -h, --help         Show this help
EOF
}

for argument in "$@"; do
    case "$argument" in
        --blockchain-only) BLOCKCHAIN_ONLY=true ;;
        --smoke) SMOKE=true; BLOCKCHAIN_ONLY=true ;;
        --skip-install) SKIP_INSTALL=true ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $argument" >&2; usage >&2; exit 2 ;;
    esac
done

for command_name in node npm curl; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "Missing required command: $command_name" >&2
        exit 1
    fi
done
if [[ "$BLOCKCHAIN_ONLY" == false ]]; then
    if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
        echo "Docker Compose is required for the full demo. Use --blockchain-only to run just the chain and API." >&2
        exit 1
    fi
fi

mkdir -p "$DEPLOYMENT_DIR" "$LOG_DIR" "$IOT_DATA_DIR"
chmod 0777 "$IOT_DATA_DIR"
ln -sfn "$RUN_ID" "$RUNTIME_ROOT/latest"

cleanup() {
    local exit_code=$?
    trap - EXIT
    if [[ "$COMPOSE_STARTED" == true ]]; then
        # The project name is unique to this run, so its MQTT volume can be
        # removed safely. IoT data is a preserved bind mount under RUN_DIR.
        docker compose --env-file "$RUN_DIR/compose.env" down --volumes >/dev/null 2>&1 || true
    fi
    for child in "$API_PID" "$HARDHAT_PID"; do
        if [[ -n "$child" ]] && kill -0 "$child" >/dev/null 2>&1; then
            kill "$child" >/dev/null 2>&1 || true
        fi
    done
    for child in "$API_PID" "$HARDHAT_PID"; do
        if [[ -n "$child" ]]; then wait "$child" >/dev/null 2>&1 || true; fi
    done
    echo "Run logs and generated deployments: $RUN_DIR"
    exit "$exit_code"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ "$SKIP_INSTALL" == false ]]; then
    if [[ ! -x "$ROOT_DIR/smart_contracts/node_modules/.bin/hardhat" ]]; then
        echo "Installing smart contract dependencies..."
        npm --prefix "$ROOT_DIR/smart_contracts" ci
    fi
    if [[ ! -d "$ROOT_DIR/blockchain_api/node_modules/ethers" ]]; then
        echo "Installing blockchain API dependencies..."
        npm --prefix "$ROOT_DIR/blockchain_api" ci
    fi
fi

if [[ ! -x "$ROOT_DIR/smart_contracts/node_modules/.bin/hardhat" || ! -d "$ROOT_DIR/blockchain_api/node_modules/ethers" ]]; then
    echo "Node dependencies are missing. Run without --skip-install once." >&2
    exit 1
fi

DEMO_MNEMONIC="${HRC_DEMO_MNEMONIC:-test test test test test test test test test test test junk}"

echo "Starting a fresh Hardhat chain..."
(
    cd "$ROOT_DIR/smart_contracts"
    export HRC_DEMO_MNEMONIC="$DEMO_MNEMONIC"
    exec npx hardhat node --hostname 127.0.0.1 >"$LOG_DIR/hardhat.log" 2>&1
) &
HARDHAT_PID=$!

wait_for_rpc() {
    local response
    for _ in $(seq 1 120); do
        if ! kill -0 "$HARDHAT_PID" >/dev/null 2>&1; then
            echo "Hardhat stopped before RPC became ready:" >&2
            tail -n 30 "$LOG_DIR/hardhat.log" >&2 || true
            return 1
        fi
        response="$(curl -sS --max-time 1 -H 'Content-Type: application/json' \
            --data '{"jsonrpc":"2.0","method":"eth_chainId","params":[],"id":1}' \
            "$RPC_URL" 2>/dev/null || true)"
        if [[ "$response" == *'"result":"0x7a69"'* ]]; then return 0; fi
        sleep 0.25
    done
    echo "Timed out waiting for Hardhat RPC at $RPC_URL" >&2
    return 1
}
wait_for_rpc

echo "Deploying safety and reward contracts..."
(
    cd "$ROOT_DIR/smart_contracts"
    export HRC_DEMO_MNEMONIC="$DEMO_MNEMONIC"
    export HRC_HARDHAT_RPC_URL="$RPC_URL"
    export HRC_DEPLOYMENT_DIR="$DEPLOYMENT_DIR"
    npx hardhat run scripts/deploy.js --network localhost
    npx hardhat run scripts/deploy_reward.js --network localhost
) >"$LOG_DIR/deploy.log" 2>&1

if [[ ! -s "$DEPLOYMENT_DIR/localhost.json" || ! -s "$DEPLOYMENT_DIR/reward-localhost.json" ]]; then
    echo "Deployment files were not created:" >&2
    cat "$LOG_DIR/deploy.log" >&2 || true
    exit 1
fi

mapfile -t WALLET_VALUES < <(
    cd "$ROOT_DIR"
    HRC_DEMO_MNEMONIC="$DEMO_MNEMONIC" node <<'NODE'
const { ethers } = require("./blockchain_api/node_modules/ethers");
const mnemonic = process.env.HRC_DEMO_MNEMONIC;
const derive = index => ethers.HDNodeWallet.fromPhrase(
    mnemonic, undefined, `m/44'/60'/0'/0/${index}`
);
console.log(derive(0).privateKey);
console.log(derive(2).address);
NODE
)
OPERATOR_PRIVATE_KEY="${WALLET_VALUES[0]}"
DEMO_BENEFICIARY="${WALLET_VALUES[1]}"
INGEST_TOKEN="$(node -e 'console.log(require("node:crypto").randomBytes(32).toString("hex"))')"
ADMIN_TOKEN="$(node -e 'console.log(require("node:crypto").randomBytes(32).toString("hex"))')"

cat >"$RUN_DIR/run.json" <<EOF
{
  "runId": "$RUN_ID",
  "network": "localhost",
  "chainId": 31337,
  "rpcUrl": "$RPC_URL",
  "blockchainApiUrl": "$API_URL",
  "demoBeneficiary": "$DEMO_BENEFICIARY"
}
EOF

echo "Starting blockchain API..."
(
    cd "$ROOT_DIR/blockchain_api"
    export RPC_URL="$RPC_URL"
    export HRC_BLOCKCHAIN_NETWORK=localhost
    export HRC_SAFETY_DEPLOYMENT="$DEPLOYMENT_DIR/localhost.json"
    export HRC_REWARD_DEPLOYMENT="$DEPLOYMENT_DIR/reward-localhost.json"
    export CONTRACT_ADDRESS=""
    export HRC_REWARD_ADDRESS=""
    export HRC_REWARD_TOKEN_ADDRESS=""
    export PRIVATE_KEY="$OPERATOR_PRIVATE_KEY"
    export PORT=3000
    export HRC_INGEST_API_TOKEN="$INGEST_TOKEN"
    export HRC_ADMIN_API_TOKEN="$ADMIN_TOKEN"
    export HRC_DASHBOARD_BENEFICIARY="$DEMO_BENEFICIARY"
    export HRC_DEMO_MNEMONIC="$DEMO_MNEMONIC"
    export HRC_EVENT_STORE_PATH="$RUN_DIR/events"
    export HRC_DEMO_SESSION_STORE_PATH="$RUN_DIR/demo-sessions.json"
    export HRC_CUSTOMER_STORE_PATH="$RUN_DIR/customers.json"
    export HRC_IOT_API_URL=http://127.0.0.1:8001
    export HRC_APP_AUTH_MODE=demo
    export HRC_APP_CHAIN_ID=31337
    export HRC_APP_ORIGIN=http://localhost:8080
    export HRC_CORS_ORIGINS=http://localhost:8080,http://127.0.0.1:8080
    exec node server.js >"$LOG_DIR/blockchain-api.log" 2>&1
) &
API_PID=$!

wait_for_api() {
    for _ in $(seq 1 120); do
        if ! kill -0 "$API_PID" >/dev/null 2>&1; then
            echo "blockchain_api stopped before becoming ready:" >&2
            tail -n 40 "$LOG_DIR/blockchain-api.log" >&2 || true
            return 1
        fi
        if curl -fsS --max-time 1 "$API_URL/api/config" >"$RUN_DIR/api-config.json" 2>/dev/null \
            && curl -fsS --max-time 1 "$API_URL/api/rewards/config" >"$RUN_DIR/reward-config.json" 2>/dev/null; then
            return 0
        fi
        sleep 0.25
    done
    echo "Timed out waiting for blockchain_api at $API_URL" >&2
    return 1
}
wait_for_api

if [[ "$SMOKE" == true ]]; then
    echo "Running a real API-to-contract smoke transaction..."
    NOW="$(date +%s)"
    HTTP_STATUS="$(curl -sS -o "$RUN_DIR/safety-event.json" -w '%{http_code}' \
        -X POST "$API_URL/api/safety-event" \
        -H 'Content-Type: application/json' \
        -H "Authorization: Bearer $INGEST_TOKEN" \
        --data "{\"schemaVersion\":\"1.0\",\"eventId\":\"smoke-$RUN_ID\",\"deviceId\":\"esp32_smoke\",\"bootId\":\"boot-1\",\"sequence\":1,\"timestamp\":$NOW,\"sensorData\":{\"distance\":0.08,\"valid\":true,\"safetyThreshold\":0.04},\"aiResult\":{\"state\":\"WARNING\",\"sourceEventType\":\"PREDICTED_WARNING\",\"riskLevel\":2,\"earlyWarning\":true}}")"
    if [[ "$HTTP_STATUS" != 200 && "$HTTP_STATUS" != 202 ]]; then
        echo "Safety-event smoke failed with HTTP $HTTP_STATUS:" >&2
        cat "$RUN_DIR/safety-event.json" >&2
        exit 1
    fi
    curl -fsS -X POST "$API_URL/api/demo/login" \
        -H 'Content-Type: application/json' --data '{"userId":"user_1"}' \
        >"$RUN_DIR/demo-login.json"
    DEMO_TOKEN="$(node -e 'const x=require(process.argv[1]); if(!x.success||!x.token) process.exit(1); process.stdout.write(x.token)' "$RUN_DIR/demo-login.json")"
    curl -fsS "$API_URL/api/me/wallet" -H "Authorization: Bearer $DEMO_TOKEN" \
        >"$RUN_DIR/demo-wallet.json"
    node - "$RUN_DIR/safety-event.json" "$RUN_DIR/demo-wallet.json" <<'NODE'
const fs = require("node:fs");
for (const file of process.argv.slice(2)) {
    const value = JSON.parse(fs.readFileSync(file, "utf8"));
    if (value.success !== true) throw new Error(`Smoke response failed: ${file}`);
}
NODE
    echo "Smoke passed: Hardhat 31337, both deployments, API readback and one safety transaction."
    exit 0
fi

if [[ "$BLOCKCHAIN_ONLY" == true ]]; then
    echo "Blockchain demo is ready: $API_URL"
    echo "Press Ctrl+C to stop."
else
    cat >"$RUN_DIR/compose.env" <<EOF
HRC_COMPOSE_PROJECT_NAME=hrcdemo-$RUN_ID
HRC_IOT_DATA_MOUNT=$IOT_DATA_DIR
HRC_IMAGE=${HRC_IMAGE:-blockchain-hrc:ai}
HRC_RUNTIME_TARGET=${HRC_RUNTIME_TARGET:-ai}
HRC_MODEL_TYPE=lstm
HRC_MODEL_SUBDIR=${HRC_MODEL_SUBDIR:-lstm-v1}
HRC_SOURCE_MODE=${HRC_SOURCE_MODE:-esp32}
HRC_DEVICE_ID=${HRC_DEVICE_ID:-esp32_01}
HRC_MQTT_HOST_PORT=${HRC_MQTT_HOST_PORT:-1885}
HRC_API_HOST_PORT=${HRC_API_HOST_PORT:-8001}
HRC_DASHBOARD_HOST_PORT=${HRC_DASHBOARD_HOST_PORT:-8080}
HRC_REWARD_API_URL=http://host.docker.internal:3000
HRC_REWARD_INGEST_TOKEN=$INGEST_TOKEN
HRC_REWARD_AUTO_START=false
HRC_REWARD_AUTO_SETTLE=false
HRC_CHAIN_API_URL=http://host.docker.internal:3000
HRC_CHAIN_INGEST_TOKEN=$INGEST_TOKEN
HRC_CHAIN_AUTO_SUBMIT=true
HRC_VIOLATION_CONFIRMATION=true
EOF
    chmod 0600 "$RUN_DIR/compose.env"
    echo "Starting MQTT, IoT/AI and dashboard..."
    COMPOSE_STARTED=true
    docker compose --env-file "$RUN_DIR/compose.env" up -d --build --force-recreate --wait mqtt iot_backend dashboard
    DASHBOARD_PORT="${HRC_DASHBOARD_HOST_PORT:-8080}"
    curl -fsS --max-time 5 "http://127.0.0.1:$DASHBOARD_PORT/api/chain/api/config" \
        >"$RUN_DIR/dashboard-chain-proxy.json"
    echo "Demo is ready: http://127.0.0.1:$DASHBOARD_PORT/#rewards"
    echo "Press Ctrl+C to stop."
fi

set +e
wait -n "$HARDHAT_PID" "$API_PID"
CHILD_STATUS=$?
set -e
echo "A required local process stopped. Check $LOG_DIR" >&2
exit "$CHILD_STATUS"
