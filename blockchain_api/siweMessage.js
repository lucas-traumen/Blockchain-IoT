const { ethers } = require("ethers");
const { ApiError } = require("./eventPayload");

const SIWE_VERSION = "1";
const DEFAULT_STATEMENT =
    "Sign in to HRC Safety Log. This signature proves you own this wallet and costs no gas.";
const DOMAIN_PATTERN = /^[a-zA-Z0-9.\-_]{1,253}(:\d{1,5})?$/;
const NONCE_PATTERN = /^[a-zA-Z0-9]{8,}$/;
const ADDRESS_PATTERN = /^0x[0-9a-fA-F]{40}$/;

function buildSiweMessage(input) {
    const domain = String(input?.domain || "");
    if (!DOMAIN_PATTERN.test(domain)) throw new Error("SIWE domain is invalid");
    const address = ethers.getAddress(input?.address);
    const uri = String(input?.uri || "");
    let parsedUri;
    try { parsedUri = new URL(uri); } catch { throw new Error("SIWE uri is invalid"); }
    if (!["http:", "https:"].includes(parsedUri.protocol) || parsedUri.username || parsedUri.password) {
        throw new Error("SIWE uri must be an HTTP(S) origin");
    }
    const chainId = Number(input?.chainId);
    if (!Number.isSafeInteger(chainId) || chainId < 1) throw new Error("SIWE chainId is invalid");
    const nonce = String(input?.nonce || "");
    if (!NONCE_PATTERN.test(nonce)) throw new Error("SIWE nonce is invalid");
    const issuedAt = String(input?.issuedAt || "");
    if (Number.isNaN(Date.parse(issuedAt))) throw new Error("SIWE issuedAt is invalid");
    const statement = input?.statement === undefined ? DEFAULT_STATEMENT : String(input.statement);
    if (/[\u0000-\u001f]/.test(statement)) throw new Error("SIWE statement contains control characters");

    const lines = [
        `${domain} wants you to sign in with your Ethereum account:`,
        address,
        "",
        statement,
        "",
        `URI: ${uri}`,
        `Version: ${SIWE_VERSION}`,
        `Chain ID: ${chainId}`,
        `Nonce: ${nonce}`,
        `Issued At: ${issuedAt}`
    ];
    if (input?.expirationTime) {
        if (Number.isNaN(Date.parse(input.expirationTime))) throw new Error("SIWE expirationTime is invalid");
        lines.push(`Expiration Time: ${input.expirationTime}`);
    }
    return lines.join("\n");
}

function parseSiweMessage(message) {
    if (typeof message !== "string" || message.length < 40 || message.length > 4000) {
        throw new ApiError(400, "INVALID_MESSAGE", "message is invalid");
    }
    const lines = message.split("\n");
    const header = /^([^\s]+) wants you to sign in with your Ethereum account:$/.exec(lines[0] || "");
    if (!header) throw new ApiError(400, "INVALID_MESSAGE", "message is not an EIP-4361 SIWE message");
    const domain = header[1];
    if (!DOMAIN_PATTERN.test(domain)) throw new ApiError(400, "INVALID_MESSAGE", "SIWE domain is invalid");

    const address = lines[1];
    if (!ADDRESS_PATTERN.test(address || "")) throw new ApiError(400, "INVALID_MESSAGE", "SIWE address is invalid");
    if (lines[2] !== "") throw new ApiError(400, "INVALID_MESSAGE", "SIWE message is missing the statement separator");

    let cursor = 3;
    let statement = "";
    if (lines[cursor] !== "") {
        statement = lines[cursor] || "";
        cursor += 1;
    }
    if (lines[cursor] !== "") throw new ApiError(400, "INVALID_MESSAGE", "SIWE message is missing a blank line before the fields");
    cursor += 1;

    const fields = {};
    for (; cursor < lines.length; cursor += 1) {
        const line = lines[cursor];
        if (!line) continue;
        const separator = line.indexOf(": ");
        if (separator < 1) throw new ApiError(400, "INVALID_MESSAGE", `SIWE line is malformed: ${line.slice(0, 24)}`);
        const key = line.slice(0, separator);
        if (fields[key] !== undefined) throw new ApiError(400, "INVALID_MESSAGE", `SIWE field is duplicated: ${key}`);
        fields[key] = line.slice(separator + 2);
    }

    for (const required of ["URI", "Version", "Chain ID", "Nonce", "Issued At"]) {
        if (fields[required] === undefined) throw new ApiError(400, "INVALID_MESSAGE", `SIWE message is missing ${required}`);
    }
    if (fields.Version !== SIWE_VERSION) throw new ApiError(400, "INVALID_MESSAGE", "SIWE version must be 1");
    const chainId = Number(fields["Chain ID"]);
    if (!Number.isSafeInteger(chainId) || chainId < 1) throw new ApiError(400, "INVALID_MESSAGE", "SIWE chain ID is invalid");
    if (!NONCE_PATTERN.test(fields.Nonce)) throw new ApiError(400, "INVALID_MESSAGE", "SIWE nonce is invalid");
    const issuedAt = Date.parse(fields["Issued At"]);
    if (Number.isNaN(issuedAt)) throw new ApiError(400, "INVALID_MESSAGE", "SIWE issued-at timestamp is invalid");
    let expirationTime = null;
    if (fields["Expiration Time"] !== undefined) {
        expirationTime = Date.parse(fields["Expiration Time"]);
        if (Number.isNaN(expirationTime)) throw new ApiError(400, "INVALID_MESSAGE", "SIWE expiration timestamp is invalid");
    }
    let uri;
    try { uri = new URL(fields.URI); } catch { throw new ApiError(400, "INVALID_MESSAGE", "SIWE URI is invalid"); }
    if (!["http:", "https:"].includes(uri.protocol)) throw new ApiError(400, "INVALID_MESSAGE", "SIWE URI must be HTTP(S)");

    return {
        domain,
        address: ethers.getAddress(address),
        statement,
        uri: fields.URI,
        version: fields.Version,
        chainId,
        nonce: fields.Nonce,
        issuedAt: fields["Issued At"],
        expirationTime: fields["Expiration Time"] ?? null
    };
}

module.exports = { buildSiweMessage, parseSiweMessage, SIWE_VERSION, DEFAULT_STATEMENT };
