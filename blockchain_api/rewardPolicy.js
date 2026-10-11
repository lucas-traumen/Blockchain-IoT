const { ethers } = require("ethers");

const POLICY_VERSION = "session-reward-v3-duration-bonus";
const OUTCOMES = Object.freeze({
    VALID_NO_VIOLATION: 0,
    VIOLATION_CONFIRMED: 1,
    SENSOR_FAULT: 2,
    INCOMPLETE: 3
});
const SETTLEMENT_STATUS = Object.freeze({
    REWARDED: 0,
    REWARD_DAILY_LIMIT: 1,
    REWARD_POOL_EMPTY: 2,
    PENALTY: 3,
    NO_REWARD: 4
});
const AMOUNTS = Object.freeze({
    stake: "2",
    reward: "3",
    maxReward: "6",
    penalty: "2",
    minDurationSeconds: 30,
    fullBonusDurationSeconds: 60,
    maxBonusPercent: 100
});

function normalizeOutcome(value) {
    if (typeof value !== "string" || !Object.hasOwn(OUTCOMES, value)) {
        throw new Error("outcome must be one of VALID_NO_VIOLATION, VIOLATION_CONFIRMED, SENSOR_FAULT, INCOMPLETE");
    }
    return { name: value, code: OUTCOMES[value] };
}

function sessionKey(sessionId) {
    if (typeof sessionId !== "string" || !/^[A-Za-z0-9_.:-]{1,128}$/.test(sessionId)) {
        throw new Error("sessionId must be 1–128 ASCII letters, digits or _ . : -");
    }
    return ethers.id(sessionId);
}

function evidenceHash(value) {
    if (typeof value !== "string" || !/^0x[0-9a-fA-F]{64}$/.test(value) || value === ethers.ZeroHash) {
        throw new Error("evidenceHash must be a non-zero bytes32 hex value");
    }
    return value;
}

function statusName(value) {
    return Object.entries(SETTLEMENT_STATUS).find(([, code]) => Number(code) === Number(value))?.[0] || "UNKNOWN";
}

module.exports = {
    POLICY_VERSION,
    OUTCOMES,
    SETTLEMENT_STATUS,
    AMOUNTS,
    normalizeOutcome,
    sessionKey,
    evidenceHash,
    statusName
};
