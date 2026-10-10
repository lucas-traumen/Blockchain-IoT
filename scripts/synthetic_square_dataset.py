"""Reproducible SYNTHETIC movement benchmark at the simulated 40 mm boundary.

This generates telemetry, not sensor measurements. It is stored outside the
real collector root. Session parameters and train/validation/test membership
are fixed before training; ideal references are never model inputs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import tempfile
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from ai_model.prepare import build_split_manifest, split_by_session, validate_split
from scripts.synthetic_dataset import ECHO_US_PER_MM, US_PER_SAMPLE, write_jsonl

SP_MM = 40
STRATA = (
    ("approach", "slow"), ("approach", "medium"), ("approach", "fast"),
    ("stop", "slow"), ("stop", "medium"), ("stop", "fast"),
    ("stationary", "near_threshold"), ("stationary", "still"),
    ("oscillation", "variable"), ("recede", "variable"),
)
SPEED_MM_S = {"slow": (10.0, 16.0), "medium": (22.0, 32.0), "fast": (40.0, 65.0)}
ERROR_CODES = ("ECHO_TIMEOUT", "ECHO_STUCK_LOW", "ECHO_STUCK_HIGH", "TRIG_FAIL")
TIME_BASE = datetime(2026, 10, 10, tzinfo=UTC)


def _iso_time(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _profile(n: int, scenario: str, speed: str, rng: np.random.Generator) -> tuple[np.ndarray, dict]:
    t = np.arange(n) * 0.1
    far = float(rng.uniform(160, 180))
    low = float(rng.uniform(28, 34))
    start_s = float(rng.uniform(1.5, 2.5))
    params: dict = {"scenario": scenario, "speed_level": speed}
    if scenario in ("approach", "stop", "recede"):
        velocity = float(rng.uniform(*SPEED_MM_S.get(speed, (12.0, 45.0))))
        if scenario == "stop":
            end = float(rng.uniform(50, 70))
            # Slow down over the last 20 mm instead of stopping abruptly.
            length = far - end
            ramp_s = 40.0 / velocity
            cruise_s = (length - 20.0) / velocity
            elapsed = np.maximum(t - start_s, 0)
            travelled = velocity * np.minimum(elapsed, cruise_s)
            decel = np.clip(elapsed - cruise_s, 0, ramp_s)
            travelled += velocity * decel - velocity * decel**2 / (2 * ramp_s)
            curve = np.maximum(end, far - travelled)
        else:
            start, end = (low, far) if scenario == "recede" else (far, low)
            # 0.4 s acceleration followed by a steady approach or retreat.
            elapsed = np.maximum(t - start_s, 0)
            ramp = np.minimum(elapsed, 0.4)
            travelled = velocity * (ramp**2 / 0.8 + np.maximum(elapsed - 0.4, 0))
            travelled = np.minimum(travelled, abs(start - end))
            curve = start + math.copysign(1, end - start) * travelled
        params.update(start_s=start_s, velocity_mm_s=velocity, far_mm=far, low_mm=low, end_mm=float(curve[-1]))
    elif scenario == "stationary":
        distance = float(rng.uniform(48, 62) if speed == "near_threshold" else rng.uniform(160, 180))
        curve = np.full(n, distance)
        params["distance_mm"] = distance
    elif scenario == "oscillation":
        period = float(rng.uniform(3.0, 5.5))
        amplitude = float(rng.uniform(12.0, 18.0))
        centre = float(rng.uniform(41, 44))
        curve = centre + amplitude * np.cos(2 * np.pi * np.maximum(t - start_s, 0) / period)
        params.update(start_s=start_s, period_s=period, amplitude_mm=amplitude, centre_mm=centre)
    else:
        raise ValueError(f"unknown scenario: {scenario}")
    return curve, params


def _telemetry(sid: str, idx: int, distances: np.ndarray, scenario: str, speed: str) -> list[dict]:
    start = TIME_BASE + timedelta(seconds=idx * 120)
    return [
        {
            "schema_version": "1.0", "device_id": "esp32_sim_square40",
            "boot_id": f"synthetic_{sid}", "sequence": i + 1,
            "monotonic_us": 1_000_000 + i * US_PER_SAMPLE,
            "measured_at": _iso_time(start + timedelta(seconds=i * 0.1)),
            "time_synced": True, "distance_mm": int(mm),
            "echo_us": max(1, round(float(mm) * ECHO_US_PER_MM)),
            "valid": True, "error_code": None,
            "synthetic": True, "session_label": sid, "scenario": scenario,
            "target_type": "simulated_target", "speed_level": speed,
        }
        for i, mm in enumerate(distances)
    ]


def _crossings(values: list[int] | np.ndarray) -> int:
    return sum(a > SP_MM >= b for a, b in zip(values, values[1:]))


def _json(path: Path, document: dict) -> None:
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def generate_bundle(
    out_dir: str | Path,
    *,
    sessions_per_stratum: int = 40,
    samples_per_session: int = 240,
    seed: int = 20261010,
) -> dict:
    """Generate disjoint, stratified sessions, plus a separate fault fixture.

    Each stratum has the same 70/15/15 allocation (largest remainder). Only
    membership and scenario are used for splitting, never model predictions.
    """
    if sessions_per_stratum < 10:
        raise ValueError("sessions_per_stratum phải >= 10")
    if samples_per_session < 200:
        raise ValueError("samples_per_session phải >= 200 (20 giây @ 10 Hz)")
    if seed < 0:
        raise ValueError("seed phải >= 0")
    out = Path(out_dir)
    if out.exists():
        raise FileExistsError(f"{out} đã tồn tại; chọn thư mục mới")

    rows: list[dict] = []
    references: list[dict] = []
    parameters = {}
    sessions = {name: [] for name in ("train", "val", "test")}
    support = {name: {"sessions": 0, "positive_sessions": 0, "no_crossing_sessions": 0,
                      "raw_crossings": 0, "ideal_crossings": 0, "scenarios": Counter()} for name in sessions}
    for stratum_index, (scenario, speed) in enumerate(STRATA):
        labels = [f"synthetic40_{seed}_{scenario}_{speed}_{i:03d}" for i in range(sessions_per_stratum)]
        assigned = split_by_session([{"session_label": sid} for sid in labels], seed + stratum_index)
        for split, members in assigned.items():
            sessions[split].extend(sorted(members))
        for i, sid in enumerate(labels):
            rng = np.random.default_rng(np.random.SeedSequence([seed, stratum_index, i]))
            ideal, params = _profile(samples_per_session, scenario, speed, rng)
            sigma = float(rng.uniform(0.6, 1.4))
            # Noise is declared up front; no noise/trajectory tuning from F1.
            distances = np.clip(np.rint(ideal + rng.normal(0, sigma, samples_per_session)), 0, 4500).astype(int)
            session_rows = _telemetry(sid, len(parameters), distances, scenario, speed)
            rows.extend(session_rows)
            references.extend({"synthetic": True, "session_label": sid, "sequence": j + 1,
                               "ideal_distance_mm": float(value)} for j, value in enumerate(ideal))
            split = next(name for name, members in assigned.items() if sid in members)
            raw_events = int(_crossings(distances))
            ideal_events = int(_crossings(ideal))
            parameters[sid] = {**params, "split": split, "noise_sigma_mm": sigma,
                               "raw_crossings": raw_events, "ideal_crossings": ideal_events}
            s = support[split]
            s["sessions"] += 1
            s["positive_sessions"] += int(raw_events > 0)
            s["no_crossing_sessions"] += int(raw_events == 0)
            s["raw_crossings"] += raw_events
            s["ideal_crossings"] += ideal_events
            s["scenarios"][scenario] += 1

    validate_split(sessions, parameters)
    fault_rows = []
    for fault_index, code in enumerate(ERROR_CODES):
        for repetition in range(5):
            sid = f"synthetic40_{seed}_{code}_{repetition:03d}"
            faults = _telemetry(sid, len(parameters) + fault_index * 5 + repetition,
                                np.full(samples_per_session, 170), "sensor_check", "simulated_fault")
            for j, row in enumerate(faults):
                if 30 <= j < samples_per_session - 30 and (repetition % 2 == 0 or j % 4 != 0):
                    row.update(distance_mm=None, echo_us=None, valid=False, error_code=code)
            fault_rows.extend(faults)

    # Stage the entire bundle so a failed run cannot expose a partial dataset.
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".synthetic40-", dir=out.parent) as temp_dir:
        stage = Path(temp_dir) / "bundle"
        stage.mkdir()
        write_jsonl(rows, stage / "dataset.jsonl")
        write_jsonl(references, stage / "ideal-reference.jsonl")
        write_jsonl(fault_rows, stage / "sensor-fault-dataset.jsonl")
        split = build_split_manifest(sessions, seed, created_at=_iso_time(TIME_BASE))
        split.update(synthetic=True, threshold_m=SP_MM / 1000,
                     strategy="stratified_by_scenario_and_speed_before_training")
        _json(stage / "split_manifest.json", split)
        summary = {
            "synthetic": True, "source": "generated_simulation",
            "generator": "scripts.synthetic_square_dataset", "generator_version": "square40-v1",
            "seed": seed, "threshold_m": SP_MM / 1000, "delta_s": 0.1,
            "samples_per_session": samples_per_session,
            "rows": len(rows), "sessions": len(parameters), "coverage": support,
            "dataset_sha256": hashlib.sha256((stage / "dataset.jsonl").read_bytes()).hexdigest(),
            "fault_rows": len(fault_rows), "fault_sessions": 20,
            "fault_codes": list(ERROR_CODES), "session_parameters": parameters,
            "limitations": ["Generated motion and noise; not calibrated HC-SR04 measurements.",
                            "Targets for standard evaluator are generated noisy telemetry.",
                            "Ideal references are separate and never supplied to LSTM.",
                            "Synthetic F1 must not be reported as real ESP32 F1."],
        }
        _json(stage / "dataset.summary.json", summary)
        _json(stage / "generation.json", summary)
        stage.rename(out)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--sessions-per-stratum", type=int, default=40)
    parser.add_argument("--samples-per-session", type=int, default=240)
    parser.add_argument("--seed", type=int, default=20261010)
    args = parser.parse_args(argv)
    summary = generate_bundle(args.out_dir, sessions_per_stratum=args.sessions_per_stratum,
                              samples_per_session=args.samples_per_session, seed=args.seed)
    print(json.dumps({key: summary[key] for key in ("synthetic", "rows", "sessions", "coverage", "dataset_sha256", "fault_sessions")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
