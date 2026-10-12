// Released distance-activity policy. Activity measures changes in a sensor's
// distance stream; it does not attest identity, steps or a walking route.
const ACTIVITY_POLICY = Object.freeze({ version: 'distance-activity-v1', displacementMm: 10,
    windowS: 1, medianSamples: 5, maximumGapS: 0.5 });
const finite = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;

function validateActivityEvidence(activity, { session, stats, task, sessionId = session?.session_id } = {}) {
    const reasons = [];
    const expected = task?.completionRule?.activityPolicy;
    const matches = value => value && Object.keys(value).length === Object.keys(ACTIVITY_POLICY).length
        && Object.entries(ACTIVITY_POLICY).every(([key, released]) => value[key] === released);
    if (!matches(expected) || !matches(activity?.policy)) reasons.push('task_activity_policy_unsupported');
    if (!activity || activity.session_id !== sessionId || session?.session_id !== sessionId || activity.device_id !== task?.deviceId
        || activity.device_id !== session?.device_id) reasons.push('task_activity_session_mismatch');
    if (!Number.isSafeInteger(activity?.sample_count) || activity.sample_count < 0
        || activity.sample_count !== stats?.raw_count) reasons.push('task_activity_sample_mismatch');
    const durations = ['active_seconds', 'stationary_seconds', 'unknown_seconds', 'observed_seconds'];
    if (durations.some(key => !finite(activity?.[key]))
        || Math.abs(activity.active_seconds + activity.stationary_seconds + activity.unknown_seconds - activity.observed_seconds) > 2e-6
        || !finite(stats?.timing_span_s) || activity.observed_seconds > stats.timing_span_s + 2e-6) {
        reasons.push('task_activity_duration_evidence_invalid');
    }
    if (!/^0x[0-9a-f]{64}$/.test(activity?.evidence_hash || '')
        || !['MOVING', 'STATIONARY', 'UNKNOWN'].includes(activity?.state)) reasons.push('task_activity_evidence_invalid');
    const requiredActiveSeconds = task?.completionRule?.requiredActiveSeconds;
    if (!Number.isSafeInteger(requiredActiveSeconds) || requiredActiveSeconds < 1
        || requiredActiveSeconds > task?.completionRule?.maximumDurationS) reasons.push('task_activity_rule_invalid');
    const activeSeconds = reasons.length ? 0 : activity.active_seconds;
    return { reasons: [...new Set(reasons)], activeSeconds, requiredActiveSeconds,
        achieved: reasons.length === 0 && activeSeconds >= requiredActiveSeconds };
}
module.exports = { ACTIVITY_POLICY, validateActivityEvidence };
