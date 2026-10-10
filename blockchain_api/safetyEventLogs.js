async function getSafetyEventLogs({ contract, latestBlock, deploymentBlock = 11852711, chunkSize = 2000 }) {
    if (!Number.isSafeInteger(chunkSize) || chunkSize <= 0 || chunkSize > 2000) {
        throw new Error("Safety event chunk size must be between 1 and 2000");
    }
    return require("./logReader").readLogs({ contract, name: "SafetyEventRecorded",
        latestBlock, deploymentBlock, chunkSize });
}

module.exports = { getSafetyEventLogs };
