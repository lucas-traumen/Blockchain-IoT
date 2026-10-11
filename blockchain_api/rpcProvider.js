const { ethers } = require("ethers");

function createRpcProvider(url, options = {}) {
    // Some RPC endpoints omit responses in JSON-RPC batches. Send each request
    // separately, including concurrent dashboard and transaction-history reads.
    return new ethers.JsonRpcProvider(url, undefined, { ...options, batchMaxCount: 1 });
}

module.exports = { createRpcProvider };
