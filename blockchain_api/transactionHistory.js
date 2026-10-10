async function getTransactionHistory({ provider, contract, deploymentBlock = 11852711, chunkSize = 2000 }) {
    const latest = await provider.getBlockNumber();
    const transactions = new Map();
    const { readLogs } = require("./logReader");
    const results = await Promise.all(["SafetyEventRecorded", "RobotUnlocked", "EmergencyStopTriggered"].map(name =>
        readLogs({ contract, name, latestBlock: latest, deploymentBlock, chunkSize })));
    {
        for (const [index, logs] of results.entries()) {
            for (const log of logs) {
                let entry = transactions.get(log.transactionHash);
                if (!entry) {
                    entry = { transactionHash: log.transactionHash, blockNumber: log.blockNumber,
                        actions: [], eventId: null, deviceId: log.args.deviceId, riskLevel: null };
                    transactions.set(log.transactionHash, entry);
                }
                const action = ["SAFETY_EVENT", "ROBOT_UNLOCK", "ROBOT_LOCK"][index];
                if (!entry.actions.includes(action)) entry.actions.push(action);
                if (index === 0) {
                    entry.eventId = log.args.eventId;
                    entry.riskLevel = Number(log.args.riskLevel);
                }
                if (index === 2 && !entry.eventId) entry.eventId = log.args.eventId;
            }
        }
    }
    const rows = [...transactions.values()].sort((a, b) => b.blockNumber - a.blockNumber);
    return { success: true, count: rows.length, transactions: rows };
}
module.exports = { getTransactionHistory };
