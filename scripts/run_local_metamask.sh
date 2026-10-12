#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_ROOT="$ROOT_DIR/.runtime/local-metamask"
RUN_ID="$(date -u +%Y%m%d%H%M%S)-$$"
RUN_DIR="$RUNTIME_ROOT/$RUN_ID"
DEPLOYMENT_DIR="$RUN_DIR/deployments"
LOG_DIR="$RUN_DIR/logs"
IOT_DATA_DIR="$RUN_DIR/iot-data"
RPC_PORT="${HRC_RPC_HOST_PORT:-8545}"
API_PORT="${HRC_BLOCKCHAIN_API_HOST_PORT:-3000}"
RPC_URL="http://127.0.0.1:$RPC_PORT"
API_URL="http://127.0.0.1:$API_PORT"
UI_PORT="${HRC_DASHBOARD_HOST_PORT:-8081}"
APP_ORIGIN="http://localhost:$UI_PORT"
APP_AUTH_MODE="${HRC_APP_AUTH_MODE:-siwe}"
BLOCKCHAIN_ONLY=false
SMOKE=false
SKIP_INSTALL=false
TASK_REWARDS_V2=false
COMPOSE_STARTED=false
HARDHAT_PID=""
API_PID=""

usage() {
    cat <<'EOF'
Usage: ./scripts/run_local_metamask.sh [options]

Starts a fresh Hardhat chain, deploys both contracts and runs blockchain_api.
By default it also starts MQTT, IoT/AI and the dashboard with Docker Compose.

  --blockchain-only  Start only Hardhat and blockchain_api
  --smoke            Verify the clean blockchain/API startup, then exit
  --task-rewards-v2  Deploy and select the isolated task-bound V2 reward contract
  --skip-install     Do not run npm ci when node_modules is missing
  -h, --help         Show this help
EOF
}

for argument in "$@"; do
    case "$argument" in
        --blockchain-only) BLOCKCHAIN_ONLY=true ;;
        --smoke) SMOKE=true; BLOCKCHAIN_ONLY=true ;;
        --skip-install) SKIP_INSTALL=true ;;
        --task-rewards-v2) TASK_REWARDS_V2=true ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $argument" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ "$APP_AUTH_MODE" != "siwe" ]]; then
    echo "This launcher only supports MetaMask SIWE authentication (HRC_APP_AUTH_MODE=siwe)" >&2
    exit 2
fi
if [[ "$TASK_REWARDS_V2" == true ]]; then export HRC_EXPECT_TASK_REWARDS=true; fi

for command_name in node npm curl; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "Missing required command: $command_name" >&2
        exit 1
    fi
done
if [[ "$BLOCKCHAIN_ONLY" == false ]]; then
    if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
        echo "Docker Compose is required for the full local stack. Use --blockchain-only to run just the chain and API." >&2
        exit 1
    fi
fi

# Refuse occupied ports before deploying: an existing chain must never receive
# deployments intended for a new run.
node - "$RPC_PORT" "$API_PORT" "$BLOCKCHAIN_ONLY" "$UI_PORT" "${HRC_API_HOST_PORT:-8002}" "${HRC_MQTT_HOST_PORT:-1886}" <<'NODE'
const net = require("node:net");
const [rpc, api, blockchainOnly, ui, iot, mqtt] = process.argv.slice(2);
const ports = blockchainOnly === "true" ? [rpc, api] : [rpc, api, ui, iot, mqtt];
Promise.all(ports.map(value => new Promise((resolve, reject) => {
    const port = Number(value);
    if (!Number.isInteger(port) || port < 1 || port > 65535) return reject(new Error(`Invalid port: ${value}`));
    const server = net.createServer();
    server.once("error", () => reject(new Error(`Port ${port} is occupied; preserve the existing stack or choose an unused port.`)));
    server.listen(port, "0.0.0.0", () => server.close(resolve));
}))).catch(error => { console.error(error.message); process.exitCode = 1; });
NODE
mkdir -p "$DEPLOYMENT_DIR" "$LOG_DIR" "$IOT_DATA_DIR"
chmod 0700 "$RUN_DIR"
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

LOCAL_MNEMONIC="${HRC_LOCAL_CHAIN_MNEMONIC:-test test test test test test test test test test test junk}"

HRC_LOCAL_TEST_WALLET_ADDRESS="$(cd "$ROOT_DIR"; HRC_LOCAL_CHAIN_MNEMONIC="$LOCAL_MNEMONIC" node <<'NODE'
const fs = require("node:fs");
const { ethers } = require("./blockchain_api/node_modules/ethers");
const wallet = ethers.HDNodeWallet.fromPhrase(process.env.HRC_LOCAL_CHAIN_MNEMONIC, undefined, "m/44'/60'/0'/0/2");
console.log(wallet.address);
NODE
)"
export HRC_LOCAL_TEST_WALLET_ADDRESS
HRC_LOCAL_CHAIN_MNEMONIC="$LOCAL_MNEMONIC" node - "$RUN_DIR/metamask-local-import.txt" <<'NODE'
const fs = require("node:fs");
const { ethers } = require("./blockchain_api/node_modules/ethers");
const wallet = ethers.HDNodeWallet.fromPhrase(process.env.HRC_LOCAL_CHAIN_MNEMONIC, undefined, "m/44'/60'/0'/0/2");
fs.writeFileSync(process.argv[2], `Local Hardhat only (31337) - never use for real funds.\nAddress: ${wallet.address}\nPrivate key for MetaMask import: ${wallet.privateKey}\n`, { mode: 0o600 });
NODE
chmod 0600 "$RUN_DIR/metamask-local-import.txt"
echo "Local MetaMask test wallet: $HRC_LOCAL_TEST_WALLET_ADDRESS (20 HRC, funded Hardhat ETH)"
echo "Import instructions/key file: $RUN_DIR/metamask-local-import.txt"
ADMIN_WALLET="$(HRC_LOCAL_CHAIN_MNEMONIC="$LOCAL_MNEMONIC" node -e 'const { ethers } = require("./blockchain_api/node_modules/ethers"); console.log(ethers.HDNodeWallet.fromPhrase(process.env.HRC_LOCAL_CHAIN_MNEMONIC, undefined, "m/44\x27/60\x27/0\x27/0/0").address)')"
ADMIN_PRIVATE_KEY="$(HRC_LOCAL_CHAIN_MNEMONIC="$LOCAL_MNEMONIC" node -e 'const { ethers } = require("./blockchain_api/node_modules/ethers"); console.log(ethers.HDNodeWallet.fromPhrase(process.env.HRC_LOCAL_CHAIN_MNEMONIC, undefined, "m/44\x27/60\x27/0\x27/0/0").privateKey)')"
printf 'Local Hardhat only (31337) - admin/operator wallet; never use for real funds.\nAddress: %s\nPrivate key for MetaMask import: %s\n' \
    "$ADMIN_WALLET" "$ADMIN_PRIVATE_KEY" >"$RUN_DIR/metamask-admin-import.txt"
chmod 0600 "$RUN_DIR/metamask-admin-import.txt"
echo "Local task admin/operator wallet: $ADMIN_WALLET"
echo "Admin import instructions/key file: $RUN_DIR/metamask-admin-import.txt"

echo "Starting a fresh Hardhat chain..."
(
    cd "$ROOT_DIR/smart_contracts"
    export HRC_LOCAL_CHAIN_MNEMONIC="$LOCAL_MNEMONIC"
    exec npx hardhat node --hostname 127.0.0.1 --port "$RPC_PORT" >"$LOG_DIR/hardhat.log" 2>&1
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
    export HRC_LOCAL_CHAIN_MNEMONIC="$LOCAL_MNEMONIC"
    export HRC_HARDHAT_RPC_URL="$RPC_URL"
    export HRC_DEPLOYMENT_DIR="$DEPLOYMENT_DIR"
    npx hardhat run scripts/deploy.js --network localhost
    npx hardhat run scripts/deploy_reward.js --network localhost
    if [[ "$TASK_REWARDS_V2" == true ]]; then
        HRC_REWARD_DEPLOYMENT="$DEPLOYMENT_DIR/reward-localhost.json" npx hardhat run scripts/deploy_task_rewards.js --network localhost
    fi
) >"$LOG_DIR/deploy.log" 2>&1

if [[ ! -s "$DEPLOYMENT_DIR/localhost.json" || ! -s "$DEPLOYMENT_DIR/reward-localhost.json" ]]; then
    echo "Deployment files were not created:" >&2
    cat "$LOG_DIR/deploy.log" >&2 || true
    exit 1
fi

mapfile -t WALLET_VALUES < <(
    cd "$ROOT_DIR"
    HRC_LOCAL_CHAIN_MNEMONIC="$LOCAL_MNEMONIC" node <<'NODE'
const { ethers } = require("./blockchain_api/node_modules/ethers");
const mnemonic = process.env.HRC_LOCAL_CHAIN_MNEMONIC;
const derive = index => ethers.HDNodeWallet.fromPhrase(
    mnemonic, undefined, `m/44'/60'/0'/0/${index}`
);
console.log(derive(0).privateKey);
console.log(derive(2).address);
console.log(derive(0).address);
NODE
)
OPERATOR_PRIVATE_KEY="${WALLET_VALUES[0]}"
LOCAL_BENEFICIARY="${WALLET_VALUES[1]}"
LOCAL_ADMIN_WALLET="${WALLET_VALUES[2]}"
INGEST_TOKEN="$(node -e 'console.log(require("node:crypto").randomBytes(32).toString("hex"))')"

cat >"$RUN_DIR/run.json" <<EOF
{
  "runId": "$RUN_ID",
  "network": "localhost",
  "chainId": 31337,
  "appAuthMode": "$APP_AUTH_MODE",
  "rpcUrl": "$RPC_URL",
  "blockchainApiUrl": "$API_URL",
  "localTestWallet": "$LOCAL_BENEFICIARY"
}
EOF

echo "Starting blockchain API..."
(
    cd "$ROOT_DIR/blockchain_api"
    export RPC_URL="$RPC_URL"
    export HRC_BLOCKCHAIN_NETWORK=localhost
    export HRC_SAFETY_DEPLOYMENT="$DEPLOYMENT_DIR/localhost.json"
    if [[ "$TASK_REWARDS_V2" == true ]]; then
        export HRC_REWARD_DEPLOYMENT="$DEPLOYMENT_DIR/task-rewards-localhost.json"
    else
        export HRC_REWARD_DEPLOYMENT="$DEPLOYMENT_DIR/reward-localhost.json"
    fi
    export CONTRACT_ADDRESS=""
    export HRC_REWARD_ADDRESS=""
    export HRC_REWARD_TOKEN_ADDRESS=""
    export PRIVATE_KEY="$OPERATOR_PRIVATE_KEY"
    export PORT="$API_PORT"
    export HRC_INGEST_API_TOKEN="$INGEST_TOKEN"
    export HRC_LOCAL_FAUCET_PRIVATE_KEY="$OPERATOR_PRIVATE_KEY"
    export HRC_LOCAL_ONBOARDING_FUNDING=true
    export HRC_DASHBOARD_BENEFICIARY="$LOCAL_BENEFICIARY"
    unset HRC_LOCAL_CHAIN_MNEMONIC
    export HRC_EVENT_STORE_PATH="$RUN_DIR/events"
    export HRC_SESSION_STORE_PATH="$RUN_DIR/wallet-sessions.json"
    export HRC_CUSTOMER_STORE_PATH="$RUN_DIR/customers.json"
    export HRC_APP_STORE=sqlite
    export HRC_APP_DB_PATH="$RUN_DIR/customers.db"
    export HRC_APP_SESSION_DB_PATH="$RUN_DIR/wallet-sessions.db"
    export HRC_IOT_API_URL="http://127.0.0.1:${HRC_API_HOST_PORT:-8002}"
    export HRC_APP_AUTH_MODE="$APP_AUTH_MODE"
    export HRC_APP_ADMIN_WALLETS="$LOCAL_ADMIN_WALLET"
    export HRC_APP_OPERATOR_WALLETS="$LOCAL_ADMIN_WALLET"
    export HRC_APP_CHAIN_ID=31337
    export HRC_APP_ORIGIN="$APP_ORIGIN"
    export HRC_CORS_ORIGINS="$APP_ORIGIN,http://127.0.0.1:$UI_PORT"
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

wait_for_dashboard_chain_proxy() {
    local dashboard_port="$1"
    local output_file="$RUN_DIR/dashboard-chain-proxy.json"
    local proxy_url="http://127.0.0.1:$dashboard_port/api/chain/api/config"
    for _ in $(seq 1 40); do
        if curl -sS --max-time 2 "$proxy_url" >"$output_file" 2>/dev/null \
            && node -e '
                const fs = require("node:fs");
                try {
                    const value = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
                    if (value.success !== true) process.exit(1);
                } catch { process.exit(1); }
            ' "$output_file"; then
            return 0
        fi
        sleep 0.25
    done
    echo "Dashboard blockchain proxy did not return a successful JSON response from $proxy_url" >&2
    if [[ -s "$output_file" ]]; then
        echo "Last proxy response:" >&2
        sed -n '1,12p' "$output_file" >&2
    fi
    echo "Check this run's dashboard proxy configuration and API connectivity." >&2
    docker compose --env-file "$RUN_DIR/compose.env" logs --no-color --tail 40 dashboard >&2 || true
    return 1
}

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
    cd "$ROOT_DIR"
    HRC_LOCAL_CHAIN_MNEMONIC="$LOCAL_MNEMONIC" node - "$API_URL" "$RUN_DIR/wallet.json" <<'NODE'
const fs = require("node:fs");
const { ethers } = require("./blockchain_api/node_modules/ethers");
async function main() {
    const [api, output] = process.argv.slice(2);
    const derive = index => ethers.HDNodeWallet.fromPhrase(process.env.HRC_LOCAL_CHAIN_MNEMONIC, undefined, `m/44'/60'/0'/0/${index}`);
    async function login(wallet) {
        const challengeResponse = await fetch(`${api}/api/auth/challenge`, {
            method: "POST", headers: { "content-type": "application/json" },
            body: JSON.stringify({ walletAddress: wallet.address, chainId: 31337 })
        });
        const challenge = await challengeResponse.json();
        const signature = await wallet.signMessage(challenge.message);
        const response = await fetch(`${api}/api/auth/wallet`, {
            method: "POST", headers: { "content-type": "application/json" },
            body: JSON.stringify({ walletAddress: wallet.address, chainId: 31337, message: challenge.message, signature })
        });
        const result = await response.json();
        if (!response.ok || !result.success || !result.token) throw new Error(result.error || "SIWE smoke login failed");
        return result;
    }
    async function call(path, token, method = "GET", body) {
        const response = await fetch(`${api}${path}`, { method,
            headers: { authorization: `Bearer ${token}`, ...(body === undefined ? {} : { "content-type": "application/json" }) },
            body: body === undefined ? undefined : JSON.stringify(body) });
        const value = await response.json();
        if (!response.ok || value.success !== true) throw new Error(value.error || `${path} returned ${response.status}`);
        return value;
    }
    const customer = await login(derive(2));
    const account = await call("/api/me/wallet", customer.token);
    if (process.env.HRC_EXPECT_TASK_REWARDS === "true" && account.rewardProtocolVersion !== "task-rewards-v2") {
        throw new Error("Task rewards V2 was not selected by the wallet API");
    }
    if (account.rewardProtocolVersion === "task-rewards-v2" && Number(account.rewardPoolBalance) <= 0) {
        throw new Error("Task reward pool is unavailable");
    }
    if (account.rewardProtocolVersion === "task-rewards-v2") {
        // Guard the duration-curve regression: the global eligibility floor is
        // 30 seconds and the base payout is 0.001 HRC. A stale 120-second floor
        // must fail this smoke instead of silently passing.
        if (Number(account.baseReward) !== 0.001 || Number(account.safeReward) !== 0.002) {
            throw new Error(`Unexpected reward curve: base=${account.baseReward} max=${account.safeReward}`);
        }
        const admin = await login(derive(0));
        if (admin.user.role !== "admin") throw new Error("Local owner wallet did not receive the admin SIWE role");
        const draft = await call("/api/tasks", admin.token, "POST", { title: "Launcher smoke task", description: "Local disposable task",
            deviceId: "esp32_01", zoneId: "smoke", completionRule: { type: "robot_cycle_count", requiredCycles: 1,
                minimumDurationS: 30, maximumDurationS: 180 } });
        const published = await call(`/api/tasks/${encodeURIComponent(draft.task.taskId)}/publish`, admin.token, "POST", {});
        if (draft.task.completionRule.minimumDurationS !== 30 || draft.task.completionRule.maximumDurationS !== 180) {
            throw new Error(`Task duration rule was not preserved: ${JSON.stringify(draft.task.completionRule)}`);
        }
        const accepted = await call(`/api/tasks/${encodeURIComponent(draft.task.taskId)}/accept`, customer.token, "POST", {});
        if (accepted.assignment.taskDefinitionHash !== published.task.definitionHash) throw new Error("Task assignment snapshot did not match the published task");
    }
    fs.writeFileSync(output, JSON.stringify({ success: true, walletAddress: customer.user.walletAddress,
        balance: account.balance, rewardProtocolVersion: account.rewardProtocolVersion }));
}
main().catch(error => { console.error(error.message); process.exit(1); });
NODE
    node - "$RUN_DIR/safety-event.json" "$RUN_DIR/wallet.json" <<'NODE'
const fs = require("node:fs");
for (const file of process.argv.slice(2)) {
    const value = JSON.parse(fs.readFileSync(file, "utf8"));
    if (value.success !== true) throw new Error(`Smoke response failed: ${file}`);
}
NODE
    echo "Smoke passed: Hardhat 31337, SIWE wallet readback, safety transaction and configured reward workflow."
    exit 0
fi

if [[ "$BLOCKCHAIN_ONLY" == true ]]; then
    echo "Blockchain stack is ready: $API_URL"
    echo "Press Ctrl+C to stop."
else
    cat >"$RUN_DIR/compose.env" <<EOF
HRC_COMPOSE_PROJECT_NAME=hrcwallet-$RUN_ID
HRC_IOT_DATA_MOUNT=$IOT_DATA_DIR
HRC_IMAGE=${HRC_IMAGE:-blockchain-hrc:ai}
HRC_RUNTIME_TARGET=${HRC_RUNTIME_TARGET:-ai}
HRC_MODEL_TYPE=lstm
HRC_MODEL_SUBDIR=${HRC_MODEL_SUBDIR:-lstm-v1}
HRC_SOURCE_MODE=${HRC_SOURCE_MODE:-esp32}
HRC_DEVICE_ID=${HRC_DEVICE_ID:-esp32_01}
HRC_MQTT_HOST_PORT=${HRC_MQTT_HOST_PORT:-1886}
HRC_API_HOST_PORT=${HRC_API_HOST_PORT:-8002}
HRC_DASHBOARD_HOST_PORT=$UI_PORT
HRC_REWARD_API_URL=http://host.docker.internal:$API_PORT
HRC_REWARD_INGEST_TOKEN=$INGEST_TOKEN
HRC_REWARD_AUTO_START=false
HRC_REWARD_AUTO_SETTLE=false
HRC_CHAIN_API_URL=http://host.docker.internal:$API_PORT
HRC_CHAIN_INGEST_TOKEN=$INGEST_TOKEN
HRC_CHAIN_AUTO_SUBMIT=true
HRC_VIOLATION_CONFIRMATION=true
EOF
    chmod 0600 "$RUN_DIR/compose.env"
    echo "Starting MQTT, IoT/AI and dashboard..."
    COMPOSE_STARTED=true
    docker compose --env-file "$RUN_DIR/compose.env" up -d --build --force-recreate --wait mqtt iot_backend dashboard
    wait_for_dashboard_chain_proxy "$UI_PORT"
    echo "MetaMask stack is ready: $APP_ORIGIN/"
    echo "Press Ctrl+C to stop."
fi

set +e
wait -n "$HARDHAT_PID" "$API_PID"
CHILD_STATUS=$?
set -e
echo "A required local process stopped. Check $LOG_DIR" >&2
exit "$CHILD_STATUS"
