const { ApiError } = require("./eventPayload");

async function getAdminAccess({ contract, wallet }) {
    const [ownerAddress, backendAddress] = await Promise.all([
        contract.owner(), wallet.getAddress()
    ]);
    if (ownerAddress.toLowerCase() !== backendAddress.toLowerCase()) {
        throw new ApiError(403, "NOT_OWNER", "Backend wallet is not the contract owner");
    }
    return { success: true, role: "admin", canUnlock: true, ownerAddress, backendAddress };
}

module.exports = { getAdminAccess };
