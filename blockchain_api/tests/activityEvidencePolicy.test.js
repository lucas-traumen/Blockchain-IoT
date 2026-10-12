const test = require('node:test');
const assert = require('node:assert/strict');
const { ACTIVITY_POLICY, validateActivityEvidence } = require('../activityEvidencePolicy');
function fixture() {
    const session = { session_id: 'movement-session', device_id: 'esp32_01' };
    const stats = { raw_count: 300, timing_span_s: 29.9 };
    const task = { deviceId: 'esp32_01', completionRule: { type: 'sensor_activity', requiredActiveSeconds: 20,
        maximumDurationS: 50, activityPolicy: { ...ACTIVITY_POLICY } } };
    const activity = { policy: { ...ACTIVITY_POLICY }, session_id: session.session_id, device_id: session.device_id,
        sample_count: 300, active_seconds: 20, stationary_seconds: 9.5, unknown_seconds: 0.4, observed_seconds: 29.9,
        state: 'MOVING', evidence_hash: '0x' + 'a'.repeat(64) };
    return { session, stats, task, activity };
}
test('activity completion requires measured active time even when total duration and samples are sufficient', () => {
    const f = fixture();
    assert.equal(validateActivityEvidence(f.activity, f).achieved, true);
    f.activity.active_seconds = 19.999999; f.activity.stationary_seconds = 9.500001;
    assert.equal(validateActivityEvidence(f.activity, f).achieved, false);
    f.activity.active_seconds = 0; f.activity.stationary_seconds = 29.5;
    assert.equal(validateActivityEvidence(f.activity, f).achieved, false);
});
test('mismatched session, device, sample coverage, policy and impossible totals cannot authorize activity rewards', () => {
    for (const mutate of [
        f => f.activity.session_id = 'another-session',
        f => { f.sessionId = 'original-session'; f.activity.session_id = f.session.session_id = 'another-session'; }, f => f.activity.device_id = 'another-device',
        f => f.activity.sample_count = 301, f => f.activity.active_seconds = 90,
        f => f.activity.policy.displacementMm = 1, f => delete f.task.completionRule.activityPolicy,
        f => f.activity.observed_seconds = 29.91, f => f.activity.unknown_seconds = -1,
        f => f.activity.evidence_hash = 'unbound', f => f.activity.state = 'GUESS',
        f => f.task.completionRule.requiredActiveSeconds = 0, f => f.activity.active_seconds = '20'
    ]) {
        const f = fixture(); mutate(f);
        const result = validateActivityEvidence(f.activity, f);
        assert.equal(result.achieved, false); assert.equal(result.activeSeconds, 0); assert.ok(result.reasons.length);
    }
    assert.equal(validateActivityEvidence(null, fixture()).achieved, false);
});
