const { ethers } = require("ethers");

class ApiError extends Error {
    constructor(statusCode, code, message) {
        super(message);
        this.statusCode = statusCode;
        this.code = code;
    }
}
function invalid(message) { throw new ApiError(400, "INVALID_PAYLOAD", message); }
function identifier(value, name) {
    if (typeof value !== "string" || !/^[A-Za-z0-9_.:-]{1,128}$/.test(value)) {
        invalid(`${name} must be 1–128 ASCII letters, digits or _ . : -`);
    }
    return value;
}
function object(value, name) {
    if (!value || typeof value !== "object" || Array.isArray(value)) invalid(`${name} must be an object`);
}
// Versioned canonical JSON for this API: recursively sorted keys, array order
// preserved, finite JSON numbers. This is not advertised as RFC 8785/JCS.
function canonicalJson(value, depth = 0) {
    if (depth > 8) invalid("Payload nesting exceeds 8 levels");
    if (value === null || typeof value === "boolean" || typeof value === "string") {
        if (typeof value === "string" && value.length > 4096) invalid("String exceeds 4096 characters");
        return JSON.stringify(value);
    }
    if (typeof value === "number") {
        if (!Number.isFinite(value)) invalid("Numbers must be finite");
        return JSON.stringify(value);
    }
    if (Array.isArray(value)) {
        if (value.length > 1000) invalid("Array exceeds 1000 items");
        return "[" + value.map(item => canonicalJson(item, depth + 1)).join(",") + "]";
    }
    object(value, "JSON value");
    const keys = Object.keys(value).sort();
    if (keys.length > 100) invalid("Object exceeds 100 fields");
    return "{" + keys.map(key => {
        if (["__proto__", "prototype", "constructor"].includes(key)) invalid("Reserved JSON key");
        return JSON.stringify(key) + ":" + canonicalJson(value[key], depth + 1);
    }).join(",") + "}";
}
const TYPES = { SafetyEvent: [
    { name: "eventId", type: "string" }, { name: "deviceId", type: "string" },
    { name: "timestamp", type: "uint256" }, { name: "dataHash", type: "bytes32" },
    { name: "riskLevel", type: "uint8" }
] };
function preparePayload(input, { enforceTime = true, now = Date.now() } = {}) {
    object(input, "payload");
    const allowed = new Set(["schemaVersion", "eventId", "deviceId", "bootId", "timestamp", "sessionId", "sequence", "sensorData", "aiResult", "signature"]);
    for (const key of Object.keys(input)) if (!allowed.has(key)) invalid(`Unsupported field: ${key}`);
    if (input.schemaVersion !== undefined && input.schemaVersion !== "1.0") invalid("schemaVersion must be 1.0");
    const deviceId = identifier(input.deviceId, "deviceId");
    const sessionId = input.sessionId === undefined ? null : identifier(input.sessionId, "sessionId");
    const bootId = input.bootId === undefined ? null : identifier(input.bootId, "bootId");
    const sequence = input.sequence === undefined ? null : input.sequence;
    if (sequence !== null && (!Number.isSafeInteger(sequence) || sequence < 1)) invalid("sequence must be a positive safe integer");
    if (Object.hasOwn(input, "sequence") && input.sequence === null) invalid("sequence cannot be null");
    if (!Number.isSafeInteger(input.timestamp) || input.timestamp <= 0) invalid("timestamp must be a positive integer Unix timestamp in seconds");
    if (enforceTime && input.timestamp > Math.floor(now / 1000) + 300) invalid("timestamp is more than 5 minutes in the future");
    object(input.sensorData, "sensorData");
    if (!Object.keys(input.sensorData).length) invalid("sensorData cannot be empty");
    object(input.aiResult, "aiResult");
    const riskLevel = input.aiResult.riskLevel;
    if (!Number.isInteger(riskLevel) || riskLevel < 0 || riskLevel > 3) invalid("aiResult.riskLevel must be a numeric integer from 0 to 3");
    for (const field of ["distance", "speed", "force", "safetyThreshold"]) {
        const value = input.sensorData[field];
        if (value !== undefined && value !== null && (typeof value !== "number" || !Number.isFinite(value) || value < 0)) invalid(`sensorData.${field} must be a non-negative finite number or null`);
    }
    if (input.sensorData.distance === null && input.sensorData.valid !== false) invalid("null distance requires sensorData.valid=false");
    if (input.sensorData.valid !== undefined && typeof input.sensorData.valid !== "boolean") invalid("sensorData.valid must be boolean");
    for (const field of ["anomaly", "earlyWarning"]) {
        if (input.aiResult[field] !== undefined && typeof input.aiResult[field] !== "boolean") invalid(`aiResult.${field} must be boolean`);
    }
    const state = input.aiResult.state;
    if (state !== undefined && !["CLEAR", "LOW", "WARNING", "VIOLATION", "UNKNOWN"].includes(state)) invalid("Unsupported aiResult.state");
    if ((state === "VIOLATION" && riskLevel !== 3) || (state === "WARNING" && riskLevel !== 2) ||
        (state === "LOW" && riskLevel !== 1) || (state === "CLEAR" && riskLevel >= 2 && !(riskLevel === 2 && input.aiResult.earlyWarning === true))) invalid("aiResult.state conflicts with riskLevel");
    if ((state === "UNKNOWN" || input.sensorData.valid === false) && riskLevel < 2) invalid("Invalid/UNKNOWN sensor data cannot be classified SAFE or LOW");
    let eventId;
    if (input.eventId !== undefined) eventId = identifier(input.eventId, "eventId");
    else {
        if ((!sessionId && !bootId) || sequence === null) invalid("Provide eventId, or sessionId/bootId plus sequence for stable identity");
        eventId = "EVT-v1-" + ethers.keccak256(ethers.toUtf8Bytes(canonicalJson({ deviceId, sessionId, bootId, sequence }))).slice(2);
    }
    const payload = { schemaVersion: "1.0", eventId, deviceId, timestamp: input.timestamp, sessionId, bootId, sequence,
        sensorData: input.sensorData, aiResult: input.aiResult };
    const canonicalPayload = canonicalJson(payload);
    if (Buffer.byteLength(canonicalPayload) > 65536) invalid("Canonical payload exceeds 64 KiB");
    const dataHash = ethers.keccak256(ethers.toUtf8Bytes(canonicalPayload));
    if (input.signature !== undefined && !/^0x[0-9a-fA-F]{130}$/.test(input.signature)) invalid("signature must be a 65-byte hex signature");
    return { payload: JSON.parse(canonicalPayload), canonicalPayload, dataHash, riskLevel, signature: input.signature };
}
module.exports = { ApiError, identifier, canonicalJson, preparePayload, TYPES };
