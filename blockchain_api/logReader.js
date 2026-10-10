const readers = new WeakMap();

async function readLogs({ contract, name, latestBlock, deploymentBlock = 11852711, chunkSize = 2000 }) {
    let entries = readers.get(contract);
    if (!entries) { entries = new Map(); readers.set(contract, entries); }
    const key = `${name}:${deploymentBlock}:${chunkSize}`;
    let entry = entries.get(key);
    if (!entry) { entry = { last: deploymentBlock - 1, logs: [], pending: null }; entries.set(key, entry); }
    if (entry.pending) {
        await entry.pending;
        if (entry.last >= latestBlock) return entry.logs.slice();
        return readLogs({ contract, name, latestBlock, deploymentBlock, chunkSize });
    }
    entry.pending = (async () => {
        const from = Math.max(deploymentBlock, Math.min(entry.last, latestBlock) - 12);
        const ranges = [];
        for (let start = from; start <= latestBlock; start += chunkSize) {
            ranges.push([start, Math.min(start + chunkSize - 1, latestBlock)]);
        }
        const results = new Array(ranges.length);
        let next = 0;
        const filter = contract.filters[name]();
        // Bound concurrency; do not launch a request per range all at once.
        await Promise.all([0, 1].map(async () => {
            while (next < ranges.length) {
                const index = next++;
                results[index] = await contract.queryFilter(filter, ...ranges[index]);
            }
        }));
        // Commit only a complete successful scan. Replace the overlapping tail.
        entry.logs = entry.logs.filter(log => log.blockNumber < from).concat(results.flat());
        entry.last = latestBlock;
    })();
    try { await entry.pending; return entry.logs.slice(); }
    finally { entry.pending = null; }
}

module.exports = { readLogs };
