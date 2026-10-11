// Coordinate nonce allocation for an address across API services in this process.
const tails = new Map();
function withSignerWrite(address, work) {
    const key = String(address).toLowerCase();
    const previous = tails.get(key) || Promise.resolve();
    const current = previous.catch(() => {}).then(work);
    tails.set(key, current);
    return current.finally(() => { if (tails.get(key) === current) tails.delete(key); });
}
module.exports = { withSignerWrite };
