const test = require('node:test');
const assert = require('node:assert/strict');
const { TASK_EVIDENCE_POLICY, validateTaskEvidence } = require('../taskEvidencePolicy');

function fixture() {
    return {
        task: { evidencePolicy: { ...TASK_EVIDENCE_POLICY }, completionRule: { minimumDurationS: 120, maximumDurationS: 900 } },
        document: { session: { started_at: '2026-10-12T10:00:00Z', ended_at: '2026-10-12T10:02:00Z', status: 'closed', boot_change: false },
            stats: { sample_count: 1200, raw_count: 1200, processed_count: 1200, processed_coverage: 1,
                timing_span_s: 119.9, mean_cadence_hz: 10, max_timing_gap_s: 0.1, gap_count: 0,
                confirmed_violation_count: 0, unknown_count: 0 },
            result: { outcome: 'VALID_NO_VIOLATION', durationS: 120, sampleCount: 1200 } }
    };
}

test('task evidence is checked against the frozen wall-clock boundary and raw coverage', () => {
    const f = fixture();
    const result = validateTaskEvidence(f.document, f.task);
    assert.deepEqual(result.reasons, []);
    assert.equal(result.expectedSamples, 1080);
    delete f.task.evidencePolicy;
    assert.deepEqual(validateTaskEvidence(f.document, f.task).reasons, []);
    f.task.completionRule.minimumDurationS = 180;
    assert.ok(validateTaskEvidence(f.document, f.task).reasons.includes('task_duration_below_minimum'));
});

for (const [name, mutate, expected] of [
    ['20 samples', f => { f.document.stats.sample_count = f.document.stats.raw_count = f.document.stats.processed_count = 20; }, 'sample_count_below_task_minimum'],
    ['forged result duration', f => { f.document.session.ended_at = '2026-10-12T10:00:01Z'; }, 'task_duration_evidence_mismatch'],
    ['missing timestamps', f => { delete f.document.session.started_at; }, 'task_duration_evidence_mismatch'],
    ['processed gap', f => { f.document.stats.processed_count = 1000; f.document.stats.processed_coverage = 1000 / 1200; }, 'processed_coverage_below_task_minimum'],
    ['lying coverage', f => { f.document.stats.processed_count = 20; }, 'processed_coverage_below_task_minimum'],
    ['short timing span', f => { f.document.stats.timing_span_s = 10; }, 'timing_span_out_of_task_range'],
    ['oversized timing span', f => { f.document.stats.timing_span_s = 240; }, 'timing_span_out_of_task_range'],
    ['wrong cadence', f => { f.document.stats.mean_cadence_hz = 1; }, 'cadence_out_of_task_range'],
    ['large gap', f => { f.document.stats.max_timing_gap_s = 1; }, 'timing_gap_above_task_maximum'],
    ['missing gap metadata', f => { delete f.document.stats.gap_count; }, 'sequence_gap_in_task_evidence'],
    ['missing raw metadata', f => { delete f.document.stats.raw_count; }, 'raw_sample_coverage_below_task_minimum'],
    ['null cadence', f => { f.document.stats.mean_cadence_hz = null; }, 'cadence_out_of_task_range'],
    ['NaN timing', f => { f.document.stats.timing_span_s = NaN; }, 'timing_span_out_of_task_range'],
    ['string count', f => { f.document.stats.sample_count = '1200'; }, 'sample_count_below_task_minimum'],
    ['unclosed sensor', f => { f.document.session.status = 'running'; }, 'task_sensor_session_not_closed'],
    ['fault hidden by result', f => { f.document.stats.unknown_count = 1; }, 'task_sensor_fault_present_or_missing'],
    ['violation hidden by result', f => { f.document.stats.confirmed_violation_count = 1; }, 'task_confirmed_violation_present_or_missing'],
    ['weakened policy', f => { f.task.evidencePolicy.minimumSampleCoverage = 0.01; }, 'task_evidence_policy_unsupported'],
]) test(`safe flag cannot bypass ${name}`, () => {
    const f = fixture(); mutate(f);
    assert.ok(validateTaskEvidence(f.document, f.task).reasons.includes(expected));
});
