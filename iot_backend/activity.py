"""Deterministic distance activity evidence from persisted session samples.

The first five consecutive safe samples establish a trailing median. Intervals
with an UNKNOWN endpoint during warmup receive no active/stationary credit.
After warmup, the current sample classifies its preceding continuous interval.
Discontinuities have UNKNOWN duration; missing clocks and boot transitions have
no measurable interval. A per-boot clock high watermark prevents a regressed
clock from counting the same elapsed time twice. No wall clock is consulted.
"""

import hashlib
import json
import math
from collections import deque
from collections.abc import Iterable, Mapping
from statistics import median
from types import MappingProxyType
from typing import Any

from iot_backend.safety import STATUS_CLEAR

__all__ = ["ACTIVITY_POLICY", "compute_session_activity"]

ACTIVITY_POLICY = MappingProxyType({
    "version": "distance-activity-v1",
    "displacementMm": 10,
    "windowS": 1,
    "medianSamples": 5,
    "maximumGapS": 0.5,
})

_MOVING = "MOVING"
_STATIONARY = "STATIONARY"
_UNKNOWN = "UNKNOWN"
_RAW_FIELDS = (
    "device_id", "boot_id", "sequence", "received_at", "measured_at",
    "monotonic_us", "time_synced", "distance_mm", "valid",
)


def _clock(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _safety_status(result_json: str | None) -> str | None:
    try:
        result = json.loads(result_json) if result_json is not None else None
    except (TypeError, ValueError):
        return None
    safety = result.get("safety") if isinstance(result, dict) else None
    status = safety.get("status") if isinstance(safety, dict) else None
    return status if isinstance(status, str) else None


def _valid_distance(value: Any) -> bool:
    return (
        type(value) in (int, float)
        and math.isfinite(value)
        and value >= 0
    )


def _evidence_value(value: Any) -> Any:
    # SQLite permits non-finite REAL values even in an INTEGER column. They
    # cannot earn credit, but still need a deterministic, JSON-safe hash.
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def compute_session_activity(
    rows: Iterable[Mapping[str, Any]],
    *,
    session_id: str,
    device_id: str | None,
    processor_version: str,
) -> dict:
    """Compute evidence from raw rows with an exact-version assessment join.

    ``activity_assessment_json`` is the selected processed result or NULL. The
    caller supplies one row per raw sample. Rows are evaluated in sequence
    order within each device/boot, so late ingestion can fill a gap on reread.
    The latest boot is selected by received_at, then its highest sequence is
    the last sample (consistent with Store.latest_raw). Database IDs and
    processor execution metadata do not participate in the evidence hash.
    """
    ordered = sorted(
        (dict(row) for row in rows),
        key=lambda row: (row["device_id"], row["boot_id"], row["sequence"]),
    )
    totals_us = {_MOVING: 0, _STATIONARY: 0, _UNKNOWN: 0}
    window_us = int(ACTIVITY_POLICY["windowS"] * 1_000_000)
    maximum_gap_us = int(ACTIVITY_POLICY["maximumGapS"] * 1_000_000)
    distances: deque = deque(maxlen=ACTIVITY_POLICY["medianSamples"])
    medians: deque = deque()
    previous: dict | None = None
    previous_safe = False
    previous_state = _UNKNOWN
    high_water_us: int | None = None
    evidence_samples = []
    states = {}

    for row in ordered:
        boot_key = (row["device_id"], row["boot_id"])
        if previous is None or boot_key != (previous["device_id"], previous["boot_id"]):
            previous = None
            previous_safe = False
            previous_state = _UNKNOWN
            high_water_us = None
            distances.clear()
            medians.clear()

        current_us = _clock(row["monotonic_us"])
        previous_us = _clock(previous["monotonic_us"]) if previous is not None else None
        status = _safety_status(row["activity_assessment_json"])
        clock_advances = current_us is not None and (
            high_water_us is None or current_us > high_water_us
        )
        safe = (
            row["device_id"] == device_id
            and status == STATUS_CLEAR
            and row["valid"] == 1
            and row["time_synced"] == 1
            and isinstance(row["measured_at"], str)
            and bool(row["measured_at"])
            and _valid_distance(row["distance_mm"])
            and clock_advances
        )
        delta_us = (
            current_us - previous_us
            if current_us is not None and previous_us is not None else 0
        )
        continuous = (
            previous is not None
            and row["sequence"] == previous["sequence"] + 1
            and safe
            and previous_safe
            and previous_us == high_water_us
            and 0 < delta_us <= maximum_gap_us
        )
        if not continuous:
            distances.clear()
            medians.clear()

        state = _UNKNOWN
        if safe:
            distances.append(row["distance_mm"])
            if len(distances) == ACTIVITY_POLICY["medianSamples"]:
                medians.append((current_us, median(distances)))
                while medians and medians[0][0] < current_us - window_us:
                    medians.popleft()
                median_values = [value for _, value in medians]
                state = (
                    _MOVING
                    if max(median_values) - min(median_values) >= ACTIVITY_POLICY["displacementMm"]
                    else _STATIONARY
                )

        # Count only positive, non-overlapping monotonic intervals. After a
        # regression, only time beyond the old high watermark is new evidence;
        # that recovery interval remains UNKNOWN and starts fresh warmup.
        interval_us = 0
        if delta_us > 0:
            interval_us = max(0, current_us - max(previous_us, high_water_us))
        interval_state = (
            state if continuous and previous_state != _UNKNOWN else _UNKNOWN
        )
        totals_us[interval_state] += interval_us
        reference = (row["device_id"], row["boot_id"], row["sequence"])
        states[reference] = state
        evidence_samples.append({
            **{field: _evidence_value(row[field]) for field in _RAW_FIELDS},
            "assessment_status": status,
            "classification": state,
            "interval_classification": interval_state,
            "interval_us": interval_us,
        })
        if current_us is not None:
            high_water_us = max(current_us, high_water_us) if high_water_us is not None else current_us
        previous = row
        previous_safe = safe
        previous_state = state

    last = None
    if ordered:
        latest_received = max(
            ordered,
            key=lambda row: (row["received_at"], row["device_id"], row["boot_id"], row["sequence"]),
        )
        latest_boot = (latest_received["device_id"], latest_received["boot_id"])
        last = max(
            (row for row in ordered if (row["device_id"], row["boot_id"]) == latest_boot),
            key=lambda row: row["sequence"],
        )

    policy = dict(ACTIVITY_POLICY)
    canonical = json.dumps(
        {
            "policy": policy,
            "session": {
                "session_id": session_id,
                "device_id": device_id,
                "processor_version": processor_version,
            },
            "samples": evidence_samples,
        },
        sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False,
    ).encode("utf-8")
    return {
        "policy": policy,
        "session_id": session_id,
        "device_id": device_id,
        "sample_count": len(ordered),
        "active_seconds": round(totals_us[_MOVING] / 1_000_000, 6),
        "stationary_seconds": round(totals_us[_STATIONARY] / 1_000_000, 6),
        "unknown_seconds": round(totals_us[_UNKNOWN] / 1_000_000, 6),
        "observed_seconds": round(sum(totals_us.values()) / 1_000_000, 6),
        "state": states[(last["device_id"], last["boot_id"], last["sequence"])] if last else _UNKNOWN,
        "last_sample_at": last["received_at"] if last else None,
        "last_sequence": last["sequence"] if last else None,
        "last_boot_id": last["boot_id"] if last else None,
        "evidence_hash": "0x" + hashlib.sha256(canonical).hexdigest(),
    }
