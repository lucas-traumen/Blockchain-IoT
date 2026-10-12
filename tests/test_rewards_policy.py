from iot_backend.rewards.policy import (
    INCOMPLETE,
    SENSOR_FAULT,
    VALID_NO_VIOLATION,
    VIOLATION_CONFIRMED,
    evaluate_session,
)


def session(**overrides):
    value = {
        "status": "closed",
        "started_at": "2026-10-09T00:00:00Z",
        "ended_at": "2026-10-09T00:00:30Z",
        "boot_change": False,
    }
    value.update(overrides)
    return value


def stats(**overrides):
    value = {
        "sample_count": 300,
        "raw_count": 300,
        "processed_count": 300,
        "processed_coverage": 1.0,
        "timing_span_s": 29.9,
        "mean_cadence_hz": 10.0,
        "max_timing_gap_s": 0.1,
        "late_count": 0,
        "gap_count": 0,
        "violation_count": 0,
        "clear_count": 300,
        "unknown_count": 0,
    }
    value.update(overrides)
    return value


def test_complete_clear_session_is_rewardable():
    result = evaluate_session(session(), stats())
    assert result["outcome"] == VALID_NO_VIOLATION
    assert result["dataValid"] is True
    assert result["coverageValid"] is True


def test_confirmed_violation_wins_over_later_sensor_fault():
    result = evaluate_session(session(boot_change=True), stats(violation_count=1, unknown_count=8))
    assert result["outcome"] == VIOLATION_CONFIRMED
    assert result["reason"] == "confirmed_violation"


def test_single_observed_violation_does_not_trigger_penalty():
    result = evaluate_session(session(), stats(violation_count=1, confirmed_violation_count=0))
    assert result["outcome"] == VALID_NO_VIOLATION
    assert result["confirmedViolationCount"] == 0


def test_sensor_fault_is_zero_change_case():
    result = evaluate_session(session(), stats(unknown_count=1))
    assert result["outcome"] == SENSOR_FAULT
    assert result["dataValid"] is False


def test_short_or_gapped_session_is_incomplete():
    result = evaluate_session(session(ended_at="2026-10-09T00:00:10Z"), stats(sample_count=100, gap_count=2))
    assert result["outcome"] == INCOMPLETE
    assert "duration_below_minimum" in result["reason"]
    assert "sequence_gap" in result["reason"]


def test_unclosed_session_is_incomplete_even_without_fault():
    result = evaluate_session(session(status="running", ended_at=None), stats())
    assert result["outcome"] == INCOMPLETE


def test_30_second_session_requires_about_300_raw_samples():
    result = evaluate_session(session(), stats(sample_count=269, raw_count=269))
    assert result["outcome"] == INCOMPLETE
    assert result["expectedSampleCount"] == 270
    assert "sample_count_below_minimum" in result["reason"]
    assert "raw_sample_coverage_below_minimum" in result["reason"]


def test_session_requires_processed_coverage_and_sensor_timing():
    result = evaluate_session(
        session(),
        stats(
            processed_count=280,
            processed_coverage=280 / 300,
            timing_span_s=10,
            max_timing_gap_s=0.6,
            mean_cadence_hz=30,
        ),
    )
    assert result["outcome"] == INCOMPLETE
    assert "processed_coverage_below_minimum" in result["reason"]
    assert "timing_span_below_minimum" in result["reason"]
    assert "timing_gap_above_maximum" in result["reason"]
    assert "cadence_out_of_range" in result["reason"]


def test_missing_coverage_metadata_fails_closed():
    result = evaluate_session(session(), {"sample_count": 300})
    assert result["outcome"] == INCOMPLETE
    assert "processed_coverage_below_minimum" in result["reason"]
