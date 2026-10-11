const { ethers } = require('ethers');
const { ApiError } = require('./eventPayload');

// Callers serialize writes for the signing address using signerQueue.
// Signed bytes are flushed before broadcast; retries never allocate another nonce.
class DurableContractWriter {
    constructor({ provider, store, waitMs = 1500 }) { Object.assign(this, { provider, store, waitMs }); }

    async refresh(record) {
        if (record.status === 'CONFIRMED') {
            const receipt = await this.provider.getTransactionReceipt(record.transactionHash);
            if (!receipt || Number(receipt.status) !== 1 || receipt.blockNumber !== record.blockNumber
                || (record.blockHash && receipt.blockHash !== record.blockHash)) {
                throw new ApiError(503, 'CHAIN_HISTORY_MISMATCH', 'The RPC no longer contains this confirmed receipt; the saved transaction is preserved.');
            }
            return record;
        }
        if (!['SIGNED', 'PENDING'].includes(record.status)) return record;
        const receipt = await this.provider.getTransactionReceipt(record.transactionHash);
        if (receipt) return this.store.saveTransaction({ ...record, status: Number(receipt.status) === 1 ? 'CONFIRMED' : 'REVERTED', blockNumber: receipt.blockNumber, blockHash: receipt.blockHash || null });
        return record;
    }

    async assertSignerReady(address, exceptKey) {
        for (let record of this.store.listTransactions()) {
            if (record.key === exceptKey || record.signer.toLowerCase() !== address.toLowerCase() || !['SIGNED', 'PENDING'].includes(record.status)) continue;
            record = await this.refresh(record);
            if (['SIGNED', 'PENDING'].includes(record.status)) throw new ApiError(409, 'SIGNER_BUSY', 'A transaction is unresolved. Wait for recovery of the same session before another write.');
        }
    }

    async send(key, wallet, contract, method, args) {
        const address = await wallet.getAddress();
        const network = await this.provider.getNetwork();
        if (Number(network.chainId) !== 31337) throw new ApiError(503, 'LOCAL_CHAIN_REQUIRED', 'Demo transactions require Hardhat local chain 31337');
        const call = await contract[method].populateTransaction(...args);
        const requestHash = ethers.keccak256(ethers.toUtf8Bytes(JSON.stringify({ to: call.to.toLowerCase(), data: call.data, chainId: 31337 })));
        let record = this.store.getTransaction(key);
        if (record) {
            if (record.requestHash !== requestHash || record.signer.toLowerCase() !== address.toLowerCase()
                || ethers.keccak256(record.rawTransaction) !== record.transactionHash) throw new ApiError(503, 'STORE_INTEGRITY_ERROR', 'Stored transaction does not match this operation');
            const signed = ethers.Transaction.from(record.rawTransaction);
            if (Number(signed.chainId) !== 31337 || signed.from.toLowerCase() !== address.toLowerCase()
                || signed.to.toLowerCase() !== call.to.toLowerCase() || signed.data !== call.data) throw new ApiError(503, 'STORE_INTEGRITY_ERROR', 'Stored signed transaction has different parameters');
            record = await this.refresh(record);
        } else {
            await this.assertSignerReady(address, key);
            let rawTransaction;
            try {
                const nonce = await this.provider.getTransactionCount(address, 'pending');
                const transaction = await wallet.populateTransaction({ ...call, nonce });
                rawTransaction = await wallet.signTransaction(transaction);
            } catch (error) {
                throw new ApiError(503, 'TRANSACTION_PREPARE_FAILED', error.shortMessage || 'Transaction could not be prepared; no transaction was broadcast');
            }
            record = this.store.saveTransaction({ key, requestHash, signer: address, rawTransaction,
                transactionHash: ethers.keccak256(rawTransaction), status: 'SIGNED', createdAt: new Date().toISOString() });
        }
        if (record.status === 'REVERTED') throw new ApiError(409, 'TRANSACTION_REVERTED', 'Transaction reverted; its receipt requires review before retry');
        if (record.status !== 'CONFIRMED') {
            try {
                await this.provider.broadcastTransaction(record.rawTransaction);
                record = this.store.saveTransaction({ ...record, status: 'PENDING' });
            } catch { /* Broadcast can succeed despite a lost response. Reuse these exact bytes. */ }
            record = await this.refresh(record);
            if (record.status !== 'CONFIRMED' && record.status !== 'REVERTED' && this.waitMs > 0) {
                try { await this.provider.waitForTransaction(record.transactionHash, 1, this.waitMs); } catch { /* recover in the next request or worker */ }
                record = await this.refresh(record);
            }
        }
        if (record.status === 'REVERTED') throw new ApiError(409, 'TRANSACTION_REVERTED', 'Transaction reverted; inspect its receipt');
        return { hash: record.transactionHash, pending: record.status !== 'CONFIRMED', receipt: record.status === 'CONFIRMED' ? { blockNumber: record.blockNumber, status: 1 } : null };
    }
}
module.exports = { DurableContractWriter };
