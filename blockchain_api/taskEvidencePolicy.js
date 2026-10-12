// Evidence eligibility for task-rewards-v2. Frozen task policy and raw/processed
// statistics are authoritative; result flags or duration supplied by a client
// are not proof. Legacy assignments without this field use the V2 defaults.
const TASK_EVIDENCE_POLICY = Object.freeze({
    version: 'task-evidence-v1',
    sampleRateHz: 10,
    minimumSampleCoverage: 0.90,
    minimumProcessedCoverage: 0.95,
    minimumCadenceHz: 8,
    maximumCadenceHz: 12,
    minimumTimingSpanRatio: 0.90,
    maximumTimingGapS: 0.50,
});
const ISO_TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;
const finite = value => typeof value === 'number' && Number.isFinite(value);
const count = value => Number.isSafeInteger(value) && value >= 0;
const timestamp = value => typeof value === 'string' && ISO_TIMESTAMP.test(value) ? Date.parse(value) : NaN;

function validateTaskEvidence(document, task) {
    const policy = { ...TASK_EVIDENCE_POLICY, ...(task.evidencePolicy || {}) };
    const stats = document.stats || {};
    const result = document.result || {};
    const session = document.session || {};
    const durationS = (timestamp(session.ended_at) - timestamp(session.started_at)) / 1000;
    const reasons = [];
    // This protocol supports only the released policy. An unexpected/malformed
    // snapshot must fail closed rather than quietly weaken eligibility.
    if (Object.entries(TASK_EVIDENCE_POLICY).some(([key, value]) => policy[key] !== value)) reasons.push('task_evidence_policy_unsupported');
    if (!finite(durationS) || durationS < 0 || !finite(result.durationS) || Math.abs(result.durationS - durationS) > 1e-6) reasons.push('task_duration_evidence_mismatch');
    if (!Number.isSafeInteger(task.completionRule?.minimumDurationS) || task.completionRule.minimumDurationS < 120
        || !Number.isSafeInteger(task.completionRule?.maximumDurationS)
        || task.completionRule.maximumDurationS < task.completionRule.minimumDurationS) reasons.push('task_duration_rule_invalid');
    if (session.status !== 'closed') reasons.push('task_sensor_session_not_closed');
    if (finite(durationS) && durationS < task.completionRule?.minimumDurationS) reasons.push('task_duration_below_minimum');
    if (finite(durationS) && durationS > task.completionRule?.maximumDurationS) reasons.push('task_duration_above_maximum');
    const expectedSamples = finite(durationS) && durationS >= 0
        ? Math.ceil(Math.max(120, durationS) * TASK_EVIDENCE_POLICY.sampleRateHz * TASK_EVIDENCE_POLICY.minimumSampleCoverage) : null;
    if (!count(stats.sample_count) || expectedSamples === null || stats.sample_count < expectedSamples) reasons.push('sample_count_below_task_minimum');
    if (!count(stats.raw_count) || expectedSamples === null || stats.raw_count < expectedSamples
        || stats.raw_count !== stats.sample_count) reasons.push('raw_sample_coverage_below_task_minimum');
    if (!count(stats.processed_count) || !count(stats.raw_count) || !stats.raw_count
        || stats.processed_count > stats.raw_count || !finite(stats.processed_coverage)
        || Math.abs(stats.processed_coverage - stats.processed_count / stats.raw_count) > 1e-6
        || stats.processed_coverage < TASK_EVIDENCE_POLICY.minimumProcessedCoverage) reasons.push('processed_coverage_below_task_minimum');
    if (!finite(stats.timing_span_s) || !finite(durationS) || stats.timing_span_s < durationS * TASK_EVIDENCE_POLICY.minimumTimingSpanRatio
        || stats.timing_span_s > durationS + 0.5) reasons.push('timing_span_out_of_task_range');
    if (!finite(stats.mean_cadence_hz) || stats.mean_cadence_hz < TASK_EVIDENCE_POLICY.minimumCadenceHz
        || stats.mean_cadence_hz > TASK_EVIDENCE_POLICY.maximumCadenceHz) reasons.push('cadence_out_of_task_range');
    if (!finite(stats.max_timing_gap_s) || stats.max_timing_gap_s < 0
        || stats.max_timing_gap_s > TASK_EVIDENCE_POLICY.maximumTimingGapS) reasons.push('timing_gap_above_task_maximum');
    if (stats.gap_count !== 0) reasons.push('sequence_gap_in_task_evidence');
    if (!count(stats.confirmed_violation_count) || stats.confirmed_violation_count > 0) reasons.push('task_confirmed_violation_present_or_missing');
    if (!count(stats.unknown_count) || stats.unknown_count > 0 || session.boot_change !== false) reasons.push('task_sensor_fault_present_or_missing');
    return { reasons, durationS, expectedSamples, policy };
}


function validateTaskOutcome(document) {
    const stats = document?.stats || {};
    const session = document?.session || {};
    const requested = document?.result?.outcome;
    const reasons = [];
    const closed = session.status === 'closed';
    const confirmed = count(stats.confirmed_violation_count) && stats.confirmed_violation_count > 0;
    const fault = (count(stats.unknown_count) && stats.unknown_count > 0) || session.boot_change === true;
    if (requested === 'VIOLATION_CONFIRMED') {
        if (!closed) reasons.push('task_sensor_session_not_closed');
        if (!confirmed) reasons.push('confirmed_violation_evidence_missing');
    } else if (requested === 'SENSOR_FAULT') {
        if (!closed) reasons.push('task_sensor_session_not_closed');
        if (!fault) reasons.push('sensor_fault_evidence_missing');
    } else if (!['VALID_NO_VIOLATION', 'INCOMPLETE', 'OWNER_CANCELLED', 'SYSTEM_CANCELLED'].includes(requested)) {
        reasons.push('task_outcome_unsupported');
    }
    return { reasons, confirmedViolation: confirmed, sensorFault: fault };
}

module.exports = { TASK_EVIDENCE_POLICY, validateTaskEvidence, validateTaskOutcome };
