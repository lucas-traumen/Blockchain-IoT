"""Deterministic session result policy used before blockchain settlement.

The policy deliberately returns an outcome only. Token amounts are fixed by the
smart contract, so a caller cannot change the reward or penalty by editing an
HTTP payload.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any, Mapping

POLICY_VERSION = "session-reward-v2"
VALID_NO_VIOLATION = "VALID_NO_VIOLATION"
VIOLATION_CONFIRMED = "VIOLATION_CONFIRMED"
SENSOR_FAULT = "SENSOR_FAULT"
INCOMPLETE = "INCOMPLETE"

DEFAULT_MIN_DURATION_S = 30.0
SAMPLE_RATE_HZ = 10.0
DEFAULT_MIN_SAMPLES = 270
MIN_PROCESSED_COVERAGE = 0.95
MIN_TIMING_SPAN_RATIO = 0.90
MAX_TIMING_GAP_S = 0.50
MIN_CADENCE_HZ = 8.0
MAX_CADENCE_HZ = 12.0


def _parse_iso(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty ISO-8601 string")
    text = value.strip().replace("Z", "+00:00")
    try:
        result = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} is not valid ISO-8601: {value!r}") from exc
    return (result if result.tzinfo else result.replace(tzinfo=UTC)).astimezone(UTC)


def _reason(outcome: str, details: list[str]) -> str:
    return ";".join(details) if details else outcome


def evaluate_session(
    session: Mapping[str, Any],
    stats: Mapping[str, Any],
    *,
    min_duration_s: float = DEFAULT_MIN_DURATION_S,
    min_samples: int = DEFAULT_MIN_SAMPLES,
) -> dict[str, Any]:
    """Evaluate a closed session without assigning token amounts.

    A confirmed violation has priority over later sensor faults or an
    interrupted close, preserving the rule that a violation cannot be erased
    by disconnecting the sensor afterwards. Technical faults never become a
    penalty when no violation was confirmed.
    """
    if min_duration_s < 0 or min_samples < 1:
        raise ValueError("minimum duration/samples are invalid")

    status = session.get("status")
    sample_count = int(stats.get("sample_count", 0) or 0)
    violation_count = int(stats.get("violation_count", 0) or 0)
    confirmed_violation_count = int(
        stats.get("confirmed_violation_count", violation_count) or 0
    )
    unknown_count = int(stats.get("unknown_count", 0) or 0)
    gap_count = int(stats.get("gap_count", 0) or 0)
    late_count = int(stats.get("late_count", 0) or 0)
    boot_change = bool(session.get("boot_change", False))

    duration_s: float | None = None
    if session.get("started_at") and session.get("ended_at"):
        duration_s = (
            _parse_iso(session["ended_at"], "ended_at")
            - _parse_iso(session["started_at"], "started_at")
        ).total_seconds()

    raw_count = int(stats.get("raw_count", 0) or 0)
    processed_count = int(stats.get("processed_count", 0) or 0)
    processed_coverage = float(stats.get("processed_coverage", 0.0) or 0.0)
    timing_span_s = float(stats.get("timing_span_s", 0.0) or 0.0)
    max_timing_gap_s = float(stats.get("max_timing_gap_s", 0.0) or 0.0)
    mean_cadence_hz_value = stats.get("mean_cadence_hz")
    mean_cadence_hz = (
        float(mean_cadence_hz_value)
        if mean_cadence_hz_value is not None else None
    )
    expected_samples = (
        max(min_samples, math.ceil(duration_s * SAMPLE_RATE_HZ * 0.90))
        if duration_s is not None else min_samples
    )

    details: list[str] = []
    if confirmed_violation_count > 0:
        outcome = VIOLATION_CONFIRMED
        details.append("confirmed_violation")
    elif status != "closed":
        outcome = INCOMPLETE
        details.append("session_not_closed")
    elif unknown_count > 0 or boot_change:
        outcome = SENSOR_FAULT
        if unknown_count > 0:
            details.append("unknown_sensor_or_assessment")
        if boot_change:
            details.append("boot_change")
    else:
        outcome = INCOMPLETE
        if duration_s is None:
            details.append("missing_duration")
        elif duration_s < min_duration_s:
            details.append("duration_below_minimum")
        if sample_count < expected_samples:
            details.append("sample_count_below_minimum")
        if raw_count < expected_samples:
            details.append("raw_sample_coverage_below_minimum")
        if processed_count < raw_count or processed_coverage < MIN_PROCESSED_COVERAGE:
            details.append("processed_coverage_below_minimum")
        if duration_s is None or timing_span_s < duration_s * MIN_TIMING_SPAN_RATIO:
            details.append("timing_span_below_minimum")
        if max_timing_gap_s > MAX_TIMING_GAP_S:
            details.append("timing_gap_above_maximum")
        if mean_cadence_hz is None or not MIN_CADENCE_HZ <= mean_cadence_hz <= MAX_CADENCE_HZ:
            details.append("cadence_out_of_range")
        if gap_count > 0:
            details.append("sequence_gap")
        # A late row is evidence to retain and report. It does not by itself
        # invalidate a session when the sequence and timing coverage are whole.
        if not details:
            outcome = VALID_NO_VIOLATION
            details.append("complete_and_clear")

    return {
        "policyVersion": POLICY_VERSION,
        "outcome": outcome,
        "durationS": duration_s,
        "sampleCount": sample_count,
        "rawSampleCount": raw_count,
        "processedSampleCount": processed_count,
        "processedCoverage": processed_coverage,
        "expectedSampleCount": expected_samples,
        "timingSpanS": timing_span_s,
        "meanCadenceHz": mean_cadence_hz,
        "maxTimingGapS": max_timing_gap_s,
        "violationCount": violation_count,
        "confirmedViolationCount": confirmed_violation_count,
        "unknownCount": unknown_count,
        "gapCount": gap_count,
        "lateCount": late_count,
        "bootChange": boot_change,
        "dataValid": outcome == VALID_NO_VIOLATION,
        "coverageValid": outcome == VALID_NO_VIOLATION,
        "reason": _reason(outcome, details),
    }
