require("dotenv").config();

const { ethers } = require("ethers");
const { createRpcProvider } = require("./rpcProvider");
const { operatorPrivateKey } = require("./operatorKey");

const {
    CONTRACT_ADDRESS,
    RPC_URL,
    NETWORK,
    DEPLOYMENT_BLOCK,
    ABI
} = require("./contractConfig");


// ======================================================
// CONFIGURATION
// ======================================================

const PRIVATE_KEY = operatorPrivateKey(process.env, NETWORK);


// ======================================================
// BLOCKCHAIN CONNECTION
// ======================================================

const provider = createRpcProvider(RPC_URL);

const baseWallet =
    new ethers.Wallet(
        PRIVATE_KEY,
        provider
    );

const contract =
    new ethers.Contract(
        CONTRACT_ADDRESS,
        ABI,
        baseWallet
    );


// ======================================================
// HELPER
// ======================================================

let durableService;
function getDurableService() {
    if (!durableService) {
        const path = require("node:path");
        const { EventStore } = require("./eventStore");
        const { EventService } = require("./eventService");
        const { chainIdForNetwork } = require("./networkConfig");
        if (NETWORK === "sepolia" && CONTRACT_ADDRESS.toLowerCase() !== "0xf6cd683a1e46357ae9a6e8e68d7d0094bfacd49c") {
            throw new Error("Sepolia deployment address does not match the existing contract");
        }
        const store = new EventStore(process.env.HRC_EVENT_STORE_PATH || path.join(__dirname, "data", "events"));
        process.once("exit", () => store.close());
        durableService = new EventService({ provider, wallet: baseWallet, contract, store,
            chainId: chainIdForNetwork(NETWORK), contractAddress: CONTRACT_ADDRESS });
    }
    return durableService;
}
async function processSafetyEvent(payload) {
    return getDurableService().processSafetyEvent(payload);
}
async function unlockRobot(deviceId) {
    return getDurableService().unlockRobot(deviceId);
}
function getStoredPayload(eventId) {
    return getDurableService().getStoredPayload(eventId);
}

async function getSafetyEventById(
    eventId
) {

    try {

        const result =
            await contract
                .getSafetyEvent(
                    eventId
                );


        return {

            success: true,

            event: {

                eventId:
                    result[0],

                deviceId:
                    result[1],

                timestamp:
                    Number(
                        result[2]
                    ),

                dataHash:
                    result[3],

                riskLevel:
                    Number(
                        result[4]
                    ),

                recordedAt:
                    Number(
                        result[5]
                    )
            }
        };


    } catch (error) {

        return {

            success: false,

            error:
                error.shortMessage
                ||
                error.message
        };
    }
}


// ======================================================
// GET ROBOT STATUS
// ======================================================

async function getRobotStatus(
    deviceId
) {

    try {

        const locked =
            await contract
                .isRobotLocked(
                    deviceId
                );


        return {

            success: true,

            deviceId,

            robotLocked:
                locked,

            status:
                locked
                    ? "LOCKED"
                    : "UNLOCKED"
        };


    } catch (error) {

        return {

            success: false,

            error:
                error.shortMessage
                ||
                error.message
        };
    }
}


// ======================================================
// GET ALL EVENTS
// ======================================================

// ======================================================
// GET ALL EVENTS
// ======================================================
// ======================================================
// GET ALL EVENTS
// ======================================================

const readSafetyEvents = require("./eventReader").createEventReader({ provider, contract, deploymentBlock: DEPLOYMENT_BLOCK });
async function getAllSafetyEvents() {
    try { return await readSafetyEvents(); }
    catch (error) {
        console.error("getAllSafetyEvents error:", error.shortMessage || error.message);
        return { success: false, error: error.shortMessage || error.message };
    }
}

// ======================================================
// UNLOCK ROBOT
// ======================================================

// ======================================================
// EXPORTS
// ======================================================

async function getAllTransactions() {
    const { getTransactionHistory } = require("./transactionHistory");
    return getTransactionHistory({ provider, contract, deploymentBlock: DEPLOYMENT_BLOCK });
}

async function getAdminAccess() {
    return require("./adminAccess").getAdminAccess({ contract, wallet: baseWallet });
}

module.exports = {

    processSafetyEvent,

    getSafetyEventById,

    getRobotStatus,

    getAllSafetyEvents,

    unlockRobot,
    getStoredPayload,
    getAllTransactions,
    getAdminAccess
};
