"""Sensor activity evidence on isolated SQLite databases; no live runtime IO."""

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from iot_backend.activity import ACTIVITY_POLICY
from iot_backend.ingestion.service import PROCESSOR_VERSION
from iot_backend.ingestion.store import Store

_POLICY = {
    "version": "distance-activity-v1",
    "displacementMm": 10,
    "windowS": 1,
    "medianSamples": 5,
    "maximumGapS": 0.5,
}
_SESSION = "activity-session"
_DEVICE = "esp32_01"
_START = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _received(offset_us):
    return (_START + timedelta(microseconds=offset_us)).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _session(store, session_id=_SESSION, device_id=_DEVICE):
    store.insert_session(
        session_id, device_id, None, None, None, _received(0), None,
        None, None, "db_fallback",
    )


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "activity.sqlite") as instance:
        _session(instance)
        yield instance


def _assessment(store, payload, *, status="CLEAR", version=PROCESSOR_VERSION, result=None):
    record = {"safety": {"status": status}} if result is None else result
    return store.insert_processed(
        device_id=payload["device_id"], boot_id=payload["boot_id"],
        sequence=payload["sequence"], processor_version=version,
        computed_at=_received(1_000_000), mode="live",
        result_json=json.dumps(record, sort_keys=True),
    )


def _sample(
    store, sequence, distance=600, *, time_us=None, session_id=_SESSION,
    device_id=_DEVICE, boot_id="boot-a", status="CLEAR", version=PROCESSOR_VERSION,
    received_at=None, **overrides,
):
    payload = {
        "schema_version": "1.0", "device_id": device_id, "boot_id": boot_id,
        "sequence": sequence,
        "monotonic_us": (sequence - 1) * 100_000 if time_us is None else time_us,
        "measured_at": _received((sequence - 1) * 100_000),
        "time_synced": True, "distance_mm": distance, "echo_us": 3500,
        "valid": True, "error_code": None,
    }
    payload.update(overrides)
    result = store.insert_raw(
        payload, received_at or _received(payload["monotonic_us"] or 0),
    )
    store.assign_session_sample(session_id, device_id, boot_id, sequence)
    if status is not None:
        _assessment(store, payload, status=status, version=version)
    return payload, result


def _series(store, distances, *, start=1, step_us=100_000, **kwargs):
    for index, distance in enumerate(distances, start=start):
        _sample(store, index, distance, time_us=(index - 1) * step_us, **kwargs)


def _activity(store, session_id=_SESSION, version=PROCESSOR_VERSION):
    stats = store.session_stats(session_id, version)
    result = stats["activity"]
    assert result["policy"] == _POLICY
    assert result["session_id"] == session_id
    assert result["sample_count"] == stats["raw_count"]
    assert result["state"] in {"MOVING", "STATIONARY", "UNKNOWN"}
    assert re.fullmatch(r"0x[0-9a-f]{64}", result["evidence_hash"])
    durations = [result[name] for name in (
        "active_seconds", "stationary_seconds", "unknown_seconds",
    )]
    assert all(value >= 0 and value == round(value, 6) for value in durations)
    assert result["observed_seconds"] == pytest.approx(sum(durations), abs=1e-12)
    return result


def test_policy_is_immutable_and_result_policy_is_detached(store):
    assert dict(ACTIVITY_POLICY) == _POLICY
    with pytest.raises(TypeError):
        ACTIVITY_POLICY["windowS"] = 2
    evidence = _activity(store)
    evidence["policy"]["windowS"] = 2
    assert _activity(store)["policy"] == _POLICY


def test_empty_session_contract_and_zero_credit(store):
    evidence = _activity(store)
    assert evidence["device_id"] == _DEVICE
    assert evidence["sample_count"] == 0
    assert evidence["state"] == "UNKNOWN"
    assert evidence["last_sample_at"] is None
    assert evidence["last_sequence"] is None
    assert evidence["last_boot_id"] is None
    assert evidence["active_seconds"] == evidence["stationary_seconds"] == 0
    assert evidence["unknown_seconds"] == evidence["observed_seconds"] == 0
    assert _activity(store) == evidence


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_warmup_remains_unknown_even_during_large_movement(store, count):
    _series(store, [600 + 100 * index for index in range(count)])
    evidence = _activity(store)
    assert evidence["state"] == "UNKNOWN"
    assert evidence["active_seconds"] == evidence["stationary_seconds"] == 0
    assert evidence["unknown_seconds"] == (count - 1) / 10


def test_fifth_sample_establishes_median_without_retroactive_credit(store):
    _series(store, [600, 650, 700, 750, 800])
    evidence = _activity(store)
    assert evidence["state"] == "STATIONARY"
    assert evidence["unknown_seconds"] == 0.4
    assert evidence["stationary_seconds"] == evidence["active_seconds"] == 0
    _sample(store, 6, 850)
    evidence = _activity(store)
    assert evidence["state"] == "MOVING"
    assert evidence["active_seconds"] == 0.1
    assert evidence["unknown_seconds"] == 0.4


def test_stationary_series_has_no_active_credit(store):
    _series(store, [600] * 21)
    evidence = _activity(store)
    assert evidence["state"] == "STATIONARY"
    assert evidence["active_seconds"] == 0
    assert evidence["stationary_seconds"] == 1.6
    assert evidence["unknown_seconds"] == 0.4
    assert evidence["observed_seconds"] == 2


@pytest.mark.parametrize("noise_mm", [1, 2, 3])
def test_small_noise_has_zero_active_credit(store, noise_mm):
    _series(store, [600 + noise_mm * (index % 3 - 1) for index in range(50)])
    evidence = _activity(store)
    assert evidence["state"] == "STATIONARY"
    assert evidence["active_seconds"] == 0
    assert evidence["observed_seconds"] == 4.9


@pytest.mark.parametrize("spike_mm", [20, 300, -300])
def test_isolated_spikes_are_removed_by_trailing_median(store, spike_mm):
    distances = [600] * 50
    for index in (1, 15, 30):
        distances[index] += spike_mm
    _series(store, distances)
    evidence = _activity(store)
    assert evidence["state"] == "STATIONARY"
    assert evidence["active_seconds"] == 0
    assert evidence["observed_seconds"] == 4.9


def test_sustained_safe_movement_accumulates_only_after_warmup(store):
    _series(store, [600 + 10 * index for index in range(21)])
    evidence = _activity(store)
    assert evidence["state"] == "MOVING"
    assert evidence["active_seconds"] == 1.6
    assert evidence["stationary_seconds"] == 0
    assert evidence["unknown_seconds"] == 0.4
    assert evidence["observed_seconds"] == store.session_stats(_SESSION)["timing_span_s"] == 2


@pytest.mark.parametrize("displacement,expected", [(9, "STATIONARY"), (10, "MOVING")])
def test_displacement_threshold_is_inclusive(store, displacement, expected):
    _series(store, [600] * 6 + [600 + displacement] * 5)
    evidence = _activity(store)
    assert evidence["state"] == expected
    assert (evidence["active_seconds"] > 0) == (expected == "MOVING")


def test_moving_to_stationary_tail_expires_after_one_second(store):
    _series(store, [600 + 10 * index for index in range(11)])
    _series(store, [700] * 12, start=12)
    at_boundary = _activity(store)
    # Median at t=1.2 is 700; median at t=1.1 is 690. The inclusive
    # one-second tail still contains the older value at t=2.1.
    assert at_boundary["state"] == "STATIONARY"
    assert at_boundary["stationary_seconds"] == 0.1
    before = at_boundary["active_seconds"]
    _series(store, [700] * 20, start=24)
    after = _activity(store)
    assert after["state"] == "STATIONARY"
    assert after["active_seconds"] == before
    assert after["stationary_seconds"] == 2.1


def test_moving_tail_boundary_includes_median_exactly_one_second_old(store):
    _series(store, [600 + 10 * index for index in range(11)])
    _series(store, [700] * 11, start=12)
    evidence = _activity(store)
    assert evidence["state"] == "MOVING"
    _sample(store, 23, 700)
    assert _activity(store)["state"] == "STATIONARY"


@pytest.mark.parametrize("gap_us", [500_001, 2_000_000])
def test_large_time_gap_receives_unknown_time_and_resets_warmup(store, gap_us):
    _series(store, [600 + 10 * index for index in range(7)])
    before = _activity(store)
    _sample(store, 8, 800, time_us=600_000 + gap_us)
    after = _activity(store)
    assert after["state"] == "UNKNOWN"
    assert after["active_seconds"] == before["active_seconds"]
    assert after["unknown_seconds"] == pytest.approx(0.4 + gap_us / 1_000_000)
    assert after["observed_seconds"] == pytest.approx(0.6 + gap_us / 1_000_000)


def test_maximum_gap_boundary_is_allowed(store):
    _series(store, [600 + 10 * index for index in range(7)])
    _sample(store, 8, 670, time_us=1_100_000)
    evidence = _activity(store)
    assert evidence["state"] == "MOVING"
    assert evidence["active_seconds"] == 0.7
    assert evidence["unknown_seconds"] == 0.4


def test_sequence_gap_is_unknown_and_requires_fresh_consecutive_samples(store):
    _series(store, [600 + 10 * index for index in range(7)])
    baseline = _activity(store)
    _sample(store, 9, 680)
    _series(store, [690, 700, 710], start=10)
    warmup = _activity(store)
    assert warmup["state"] == "UNKNOWN"
    assert warmup["active_seconds"] == baseline["active_seconds"] == 0.2
    assert warmup["unknown_seconds"] == 0.9
    _sample(store, 13, 720)
    assert _activity(store)["state"] == "STATIONARY"
    assert _activity(store)["active_seconds"] == 0.2
    _sample(store, 14, 730)
    assert _activity(store)["active_seconds"] == 0.3


@pytest.mark.parametrize("overrides,status", [
    ({"valid": False}, "CLEAR"),
    ({"valid": None}, "CLEAR"),
    ({"distance_mm": None}, "CLEAR"),
    ({"distance_mm": float("inf")}, "CLEAR"),
    ({"time_synced": False}, "CLEAR"),
    ({"time_synced": None}, "CLEAR"),
    ({"measured_at": None}, "CLEAR"),
    ({}, "VIOLATION"),
    ({}, "UNKNOWN"),
    ({}, None),
])
def test_invalid_unsafe_unsynced_unprocessed_sample_resets_windows(store, overrides, status):
    _series(store, [600 + 10 * index for index in range(7)])
    _sample(store, 8, 670, status=status, **overrides)
    broken = _activity(store)
    assert broken["state"] == "UNKNOWN"
    assert broken["active_seconds"] == 0.2
    assert broken["unknown_seconds"] == 0.5
    _series(store, [680, 690, 700, 710, 720], start=9)
    warmed = _activity(store)
    assert warmed["state"] == "STATIONARY"
    assert warmed["active_seconds"] == 0.2
    assert warmed["unknown_seconds"] == 1
    _sample(store, 14, 730)
    assert _activity(store)["active_seconds"] == 0.3


def test_missing_clock_never_bridges_time(store):
    _series(store, [600 + 10 * index for index in range(7)])
    _sample(store, 8, 670, monotonic_us=None)
    _series(store, [680, 690, 700, 710], start=9)
    evidence = _activity(store)
    assert evidence["active_seconds"] == 0.2
    assert evidence["state"] == "UNKNOWN"
    assert evidence["observed_seconds"] == 0.9  # No interval into or out of NULL.
    assert evidence["unknown_seconds"] == 0.7


@pytest.mark.parametrize("time_us", [600_000, 300_000])
def test_nonmonotonic_clock_resets_without_double_counting_elapsed_time(store, time_us):
    _series(store, [600 + 10 * index for index in range(7)])
    _sample(store, 8, 900, time_us=time_us)
    assert _activity(store)["state"] == "UNKNOWN"
    assert _activity(store)["observed_seconds"] == 0.6
    _sample(store, 9, 910, time_us=700_000)
    evidence = _activity(store)
    assert evidence["active_seconds"] == 0.2
    assert evidence["unknown_seconds"] == 0.5
    assert evidence["observed_seconds"] == 0.7
    assert evidence["observed_seconds"] <= store.session_stats(_SESSION)["timing_span_s"]
    _series(store, [920, 930, 940, 950], start=10)
    assert _activity(store)["active_seconds"] == 0.2
    _sample(store, 14, 960)
    assert _activity(store)["active_seconds"] == 0.3


def test_repeated_regressed_timestamps_cannot_replay_active_time(store):
    _series(store, [600 + 10 * index for index in range(21)])
    baseline = _activity(store)
    for sequence in range(22, 32):
        _sample(store, sequence, 1000 + sequence * 10, time_us=(sequence - 22) * 100_000)
    repeated = _activity(store)
    assert repeated["state"] == "UNKNOWN"
    assert repeated["active_seconds"] == baseline["active_seconds"]
    assert repeated["observed_seconds"] == baseline["observed_seconds"]


def test_boot_transition_has_no_credit_and_latest_boot_is_selected_by_receipt(store):
    # Use boot names in reverse chronological order to catch lexical sorting
    # incorrectly reporting the old boot as the latest activity state.
    _series(store, [600 + 10 * index for index in range(7)], boot_id="z-old")
    _sample(store, 1, 900, boot_id="a-new", time_us=0, received_at=_received(60_000_000))
    evidence = _activity(store)
    assert evidence["state"] == "UNKNOWN"
    assert evidence["active_seconds"] == 0.2
    assert evidence["observed_seconds"] == 0.6
    assert evidence["last_boot_id"] == "a-new"
    assert evidence["last_sequence"] == 1
    assert evidence["last_sample_at"] == _received(60_000_000)


def test_multiple_boots_accumulate_only_their_own_monotonic_intervals(store):
    _series(store, [600 + 10 * index for index in range(7)], boot_id="z-old")
    for sequence in range(1, 8):
        _sample(
            store, sequence, 900 + 10 * (sequence - 1), boot_id="a-new",
            received_at=_received(60_000_000 + (sequence - 1) * 100_000),
        )
    evidence = _activity(store)
    assert evidence["active_seconds"] == 0.4
    assert evidence["unknown_seconds"] == 0.8
    assert evidence["observed_seconds"] == 1.2
    # Legacy stats intentionally retain max(span per boot); Node rejects a
    # boot-changed whole task rather than changing these legacy values.
    assert store.session_stats(_SESSION)["timing_span_s"] == 0.6


def test_assessments_are_joined_by_exact_version_without_double_counts(store):
    _series(store, [600 + 10 * index for index in range(21)], status="VIOLATION")
    for row in store.session_raw_rows(_SESSION):
        _assessment(store, row, status="CLEAR", version="other-safety")
    evidence = _activity(store)
    assert evidence["active_seconds"] == 0
    assert evidence["unknown_seconds"] == 2
    replay = _activity(store, version="other-safety")
    assert replay["sample_count"] == 21
    assert replay["active_seconds"] == 1.6
    assert replay["evidence_hash"] != evidence["evidence_hash"]
    unfiltered = store.session_stats(_SESSION)
    assert unfiltered["processed_count"] == 42  # Legacy version aggregation.
    assert unfiltered["violation_count"] == unfiltered["clear_count"] == 21
    assert unfiltered["activity"] == evidence
    missing_version = _activity(store, version="absent-version")
    assert missing_version["sample_count"] == 21
    assert missing_version["state"] == "UNKNOWN"
    assert missing_version["unknown_seconds"] == 2


def test_other_version_cannot_fill_missing_live_assessment(store):
    _series(store, [600 + 10 * index for index in range(7)])
    _sample(store, 8, 670, version="other-safety")
    evidence = _activity(store)
    assert evidence["state"] == "UNKNOWN"
    assert evidence["active_seconds"] == 0.2
    assert evidence["sample_count"] == 8


def test_duplicate_ingestion_and_assignment_do_not_change_evidence(store):
    _series(store, [600 + 10 * index for index in range(21)])
    before = _activity(store)
    for sequence in range(1, 22):
        _, result = _sample(
            store, sequence, 1200, received_at=_received(300_000_000),
            status="VIOLATION",
        )
        assert not result.inserted
    assert _activity(store) == before
    assert store.session_stats(_SESSION)["processed_count"] == 21


def test_late_gap_fill_recomputes_deterministically_and_does_not_freshen_last_sample(store, tmp_path):
    for sequence in list(range(1, 8)) + list(range(9, 22)):
        _sample(store, sequence, 600 + 10 * (sequence - 1))
    incomplete = _activity(store)
    _, result = _sample(store, 8, 670, received_at=_received(600_000_000))
    assert result.inserted and result.is_late
    complete = _activity(store)
    assert complete["active_seconds"] == 1.6
    assert complete["evidence_hash"] != incomplete["evidence_hash"]
    assert complete["last_sequence"] == 21
    assert complete["last_sample_at"] == _received(2_000_000)
    assert _activity(store) == complete
    with Store(tmp_path / "ordered.sqlite") as ordered:
        _session(ordered)
        for sequence in range(1, 22):
            _sample(
                ordered, sequence, 600 + 10 * (sequence - 1),
                received_at=_received(600_000_000) if sequence == 8 else None,
            )
        # is_late and DB insertion order are not activity measurements.
        assert _activity(ordered) == complete


def test_sqlite_reopen_preserves_all_evidence(tmp_path):
    path = tmp_path / "reopened.sqlite"
    with Store(path) as instance:
        _session(instance)
        _series(instance, [600 + 10 * index for index in range(21)])
        before = _activity(instance)
        instance.close_session(_SESSION, _received(10_000_000), 21)
    with Store(path) as reopened:
        assert _activity(reopened) == before


def test_unrelated_sessions_and_unassigned_rows_cannot_affect_activity(store):
    _series(store, [600 + 10 * index for index in range(21)])
    before = _activity(store)
    _session(store, "second-session")
    _series(store, [1000] * 30, start=22, session_id="second-session")
    _session(store, "other-device", "esp32_02")
    _series(store, [2000 + 10 * index for index in range(21)], session_id="other-device", device_id="esp32_02")
    payload = json.loads(store.session_raw_rows(_SESSION)[0]["payload_json"])
    payload["sequence"] = 500
    store.insert_raw(payload, _received(500_000_000))
    _assessment(store, payload)
    assert _activity(store) == before
    other = _activity(store, session_id="second-session")
    assert other["device_id"] == _DEVICE
    assert other["active_seconds"] == 0
    assert other["sample_count"] == 30
    assert _activity(store, session_id="other-device")["device_id"] == "esp32_02"


def test_hash_binds_session_identity_even_when_samples_are_shared(store):
    _series(store, [600 + 10 * index for index in range(7)])
    before = _activity(store)
    _session(store, "shared-samples")
    for row in store.session_raw_rows(_SESSION):
        store.assign_session_sample("shared-samples", row["device_id"], row["boot_id"], row["sequence"])
    shared = _activity(store, session_id="shared-samples")
    assert shared["active_seconds"] == before["active_seconds"]
    assert shared["evidence_hash"] != before["evidence_hash"]


def test_selected_assessment_arriving_later_changes_hash_and_credit(store):
    _series(store, [600 + 10 * index for index in range(7)], status=None)
    unprocessed = _activity(store)
    assert unprocessed["unknown_seconds"] == 0.6
    for row in store.session_raw_rows(_SESSION):
        _assessment(store, row)
    processed = _activity(store)
    assert processed["active_seconds"] == 0.2
    assert processed["evidence_hash"] != unprocessed["evidence_hash"]
    assert _activity(store) == processed


@pytest.mark.parametrize("result_json", ["{", "null", "[]", '{"safety": null}', '{"safety": {"status": 1}}'])
def test_malformed_selected_assessment_is_unknown(store, result_json):
    _series(store, [600 + 10 * index for index in range(7)])
    _sample(store, 8, 670, status=None)
    store.insert_processed(
        device_id=_DEVICE, boot_id="boot-a", sequence=8,
        processor_version=PROCESSOR_VERSION, computed_at=_received(1_000_000),
        mode="live", result_json=result_json,
    )
    assert _activity(store)["state"] == "UNKNOWN"
    assert _activity(store)["active_seconds"] == 0.2


def test_microsecond_durations_have_consistent_rounded_totals(store):
    distances = [600 + 10 * index for index in range(9)] + [680] * 20
    _series(store, distances, step_us=123_457)
    evidence = _activity(store)
    assert evidence["active_seconds"] > 0
    assert evidence["stationary_seconds"] > 0
    assert evidence["unknown_seconds"] == 0.493828
    assert evidence["observed_seconds"] == 3.456796
    assert evidence["observed_seconds"] <= store.session_stats(_SESSION)["timing_span_s"]


def test_closed_stale_telemetry_and_wall_clock_never_add_elapsed_credit(store, monkeypatch):
    _series(store, [600 + 10 * index for index in range(21)])
    baseline = _activity(store)
    store.close_session(_SESSION, _received(86_400_000_000), 21)
    monkeypatch.setattr("iot_backend.util.utc_now_iso", lambda: _received(86_400_000_000))
    for _ in range(5):
        assert _activity(store) == baseline
    assert baseline["last_sample_at"] == _received(2_000_000)
    assert baseline["observed_seconds"] == 2


def test_sample_receipt_timestamp_is_not_measured_timestamp(store):
    for sequence in range(1, 8):
        _sample(
            store, sequence, 600 + 10 * (sequence - 1),
            received_at=_received(50_000_000 + (sequence - 1) * 100_000),
        )
    evidence = _activity(store)
    assert evidence["last_sample_at"] == _received(50_600_000)
    assert evidence["last_sample_at"] != store.session_raw_rows(_SESSION)[-1]["measured_at"]
    assert evidence["observed_seconds"] == 0.6
