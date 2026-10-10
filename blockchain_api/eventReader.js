function createEventReader({ provider, contract, deploymentBlock }) {
    const details = new Map();
    let pending = null;
    async function load() {
        const latestBlock = await provider.getBlockNumber();
        const logs = await require("./safetyEventLogs").getSafetyEventLogs({ contract, latestBlock, deploymentBlock });
        const events = new Array(logs.length);
        let next = 0;
        await Promise.all([0, 1].map(async () => {
            while (next < logs.length) {
                const index = next++, log = logs[index], id = log.args.eventId;
                let result = details.get(id);
                if (!result) { result = await contract.getSafetyEvent(id); details.set(id, result); }
                events[index] = { eventId: result[0], deviceId: result[1], timestamp: Number(result[2]),
                    dataHash: result[3], riskLevel: Number(result[4]), recordedAt: Number(result[5]),
                    transactionHash: log.transactionHash, blockNumber: log.blockNumber };
            }
        }));
        return { success: true, count: events.length, events };
    }
    return async () => {
        if (!pending) pending = load().finally(() => { pending = null; });
        return pending;
    };
}
module.exports = { createEventReader };
