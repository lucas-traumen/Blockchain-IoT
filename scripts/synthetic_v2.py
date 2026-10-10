"""Generate the deterministic synthetic-only V2 benchmark release.

The generator deliberately keeps the ideal trajectory and the measured signal
in separate files.  ``dataset.jsonl`` is an audit/training source with labels;
``telemetry.jsonl`` is the runtime-shaped stream and never contains the ideal
distance or future labels.  Session membership is assigned before any windows
are built, and every output hash is derived from canonical JSONL bytes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from scripts.synthetic_dataset import ECHO_US_PER_MM

TIME_BASE = datetime(2026, 1, 1, tzinfo=UTC)
HORIZONS_MS = (100, 200, 300, 400, 500)
SCENARIOS = (
    "stationary_clear", "stationary_near_threshold", "approach_slow",
    "approach_medium", "approach_fast", "stop_before", "recede",
    "oscillation", "sudden_step", "mixed_motion",
)
FAULTS = ("dropout", "spike", "frozen", "sequence_gap", "burst_noise")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _write_json(path: Path, value: Any) -> str:
    encoded = (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode()
    path.write_bytes(encoded)
    return hashlib.sha256(encoded).hexdigest()


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> str:
    encoded = "".join(_canonical(row) + "\n" for row in rows).encode()
    path.write_bytes(encoded)
    return hashlib.sha256(encoded).hexdigest()


def _write_yaml(path: Path, value: Any) -> str:
    encoded = yaml.safe_dump(value, sort_keys=True).encode()
    path.write_bytes(encoded)
    return hashlib.sha256(encoded).hexdigest()


def _session_seed(release_seed: int, session_id: str) -> int:
    """Derive a stable, release-specific seed with no scenario index reuse."""
    digest = hashlib.sha256(f"synthetic-v2:{release_seed}:{session_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFFFFFF


def _load_config(path: str | Path) -> dict[str, Any]:
    config = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("simulation config must be an object")
    if config.get("version") != "square-bench-v2":
        raise ValueError("simulation config version must be square-bench-v2")
    if config.get("simulation_mode") is not True or float(config.get("threshold_m", 0)) != 0.04:
        raise ValueError("square-bench-v2 must use simulation_mode=true and threshold_m=0.04")
    if tuple(config.get("horizons_ms", ())) != HORIZONS_MS:
        raise ValueError("horizons_ms must be [100, 200, 300, 400, 500]")
    if int(config.get("sample_period_ms", 0)) != 100:
        raise ValueError("sample_period_ms must be 100")
    if int(config.get("window_size", 0)) != 10:
        raise ValueError("window_size must be 10")
    for key in SCENARIOS:
        if int(config.get("forecast_sessions", {}).get(key, 0)) < 0:
            raise ValueError(f"invalid session count for {key}")
    for key in FAULTS:
        if int(config.get("fault_sessions", {}).get(key, 0)) < 0:
            raise ValueError(f"invalid fault count for {key}")
    return config


def _split_ids(ids: list[str], seed: int) -> dict[str, list[str]]:
    shuffled = list(ids)
    random.Random(seed).shuffle(shuffled)
    n = len(shuffled)
    n_val = max(1, round(n * 0.15)) if n >= 3 else 0
    n_test = max(1, round(n * 0.15)) if n >= 3 else 0
    return {
        "train": sorted(shuffled[: n - n_val - n_test]),
        "val": sorted(shuffled[n - n_val - n_test : n - n_test]),
        "test": sorted(shuffled[n - n_test :]) if n_test else [],
    }


def _trajectory(scenario: str, t: np.ndarray, rng: np.random.Generator, threshold: float) -> tuple[np.ndarray, dict[str, Any]]:
    start = float(rng.uniform(0.30, 0.95))
    speed = float(rng.uniform(0.02, 0.80))
    acceleration = float(rng.uniform(-0.60, 0.60))
    phase = float(rng.uniform(0, 2 * math.pi))
    if scenario == "stationary_clear":
        values = np.full_like(t, float(rng.uniform(0.25, 0.95)))
    elif scenario == "stationary_near_threshold":
        values = np.full_like(t, float(rng.uniform(0.028, 0.055)))
    elif scenario.startswith("approach"):
        scale = {"approach_slow": 0.35, "approach_medium": 0.65, "approach_fast": 1.0}[scenario]
        values = start - speed * scale * t - 0.5 * acceleration * t * t
    elif scenario == "stop_before":
        stop_margin = float(rng.uniform(0.001, 0.030))
        stop_at = threshold + stop_margin
        values = np.maximum(stop_at, start - speed * t + 0.5 * abs(acceleration) * t * t)
    elif scenario == "recede":
        values = max(threshold * 0.7, start) + speed * t + 0.5 * acceleration * t * t
    elif scenario == "oscillation":
        amplitude = float(rng.uniform(0.003, 0.035))
        frequency = float(rng.uniform(0.05, 0.35))
        values = threshold + float(rng.uniform(-0.004, 0.010)) + amplitude * np.sin(2 * np.pi * frequency * t + phase)
    elif scenario == "sudden_step":
        step_at = float(rng.uniform(4.0, 12.0))
        step = float(rng.uniform(-0.08, 0.08))
        values = np.full_like(t, start)
        values = values + np.where(t >= step_at, step, 0.0)
    elif scenario == "mixed_motion":
        values = start - speed * np.minimum(t, 5.0)
        values += np.where(t >= 5.0, speed * (t - 5.0), 0.0)
        values += np.where(t >= 10.0, -0.75 * speed * (t - 10.0), 0.0)
    else:
        raise ValueError(f"unknown scenario: {scenario}")
    bounds = (0.001, 4.5)
    return np.clip(values, *bounds), {
        "start_m": start, "speed_m_s": speed, "acceleration_m_s2": acceleration,
        "phase_rad": phase, "scenario": scenario,
    }


def _crossing_episode_ids(values: np.ndarray, threshold: float) -> list[int | None]:
    result: list[int | None] = []
    episode = 0
    inside = False
    for value in values:
        now = bool(value <= threshold)
        if now and not inside:
            episode += 1
        result.append(episode if now else None)
        inside = now
    return result


def _session_rows(
    session_id: str, scenario: str, seed: int, split: str, config: dict[str, Any], *, fault: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    sample_period_s = int(config["sample_period_ms"]) / 1000
    n = int(round(float(config["duration_s"]) / sample_period_s))
    rng = np.random.default_rng(np.random.SeedSequence([seed, sum(map(ord, session_id))]))
    t = np.arange(n, dtype=float) * sample_period_s
    true_values, params = _trajectory("stationary_clear" if fault else scenario, t, rng, float(config["threshold_m"]))
    measurement = config["measurement"]
    bias = float(rng.uniform(*measurement["bias_m"]))
    sigma = float(rng.uniform(*measurement["noise_sigma_m"]))
    drift_sigma = float(rng.uniform(*measurement["drift_sigma_m"]))
    drift = np.cumsum(rng.normal(0, drift_sigma, n))
    measured = true_values + bias + drift + rng.normal(0, sigma, n)
    measured = np.round(np.clip(measured, 0, 4.5) * 1000) / 1000
    valid = np.ones(n, dtype=bool)
    fault_codes: list[str | None] = [None] * n
    if fault:
        fault_start = max(10, n // 3)
        fault_end = min(n - 10, fault_start + max(3, n // 12))
        if fault == "dropout":
            valid[fault_start:fault_end] = False
            fault_codes[fault_start:fault_end] = ["ECHO_TIMEOUT"] * (fault_end - fault_start)
        elif fault == "spike":
            measured[fault_start:fault_end] = np.clip(measured[fault_start:fault_end] + 0.15, 0, 4.5)
            fault_codes[fault_start:fault_end] = ["SPIKE"] * (fault_end - fault_start)
        elif fault == "frozen":
            measured[fault_start:fault_end] = measured[fault_start]
            fault_codes[fault_start:fault_end] = ["FROZEN"] * (fault_end - fault_start)
        elif fault == "sequence_gap":
            fault_codes[fault_start:fault_end] = ["SEQUENCE_GAP"] * (fault_end - fault_start)
        elif fault == "burst_noise":
            measured[fault_start:fault_end] = np.clip(measured[fault_start:fault_end] + rng.normal(0, 0.04, fault_end - fault_start), 0, 4.5)
            fault_codes[fault_start:fault_end] = ["BURST_NOISE"] * (fault_end - fault_start)
    episode_ids = _crossing_episode_ids(true_values, float(config["threshold_m"]))
    start_time = TIME_BASE + timedelta(seconds=seed % 1000 + len(session_id))
    dataset: list[dict[str, Any]] = []
    telemetry: list[dict[str, Any]] = []
    for index in range(n):
        targets: dict[str, float | None] = {}
        crossings: dict[str, bool] = {}
        for horizon_ms in HORIZONS_MS:
            offset = int(horizon_ms / int(config["sample_period_ms"]))
            target_index = index + offset
            targets[f"target_d{horizon_ms}_m"] = float(true_values[target_index]) if target_index < n else None
            future = true_values[index + 1 : min(n, target_index + 1)]
            crossings[f"crossing_h{horizon_ms}"] = bool(true_values[index] > config["threshold_m"] and np.any(future <= config["threshold_m"]))
        delta = None if index == 0 or not valid[index] or not valid[index - 1] else float(measured[index] - measured[index - 1])
        measured_m = float(measured[index]) if valid[index] else None
        measured_at = (start_time + timedelta(seconds=index * sample_period_s)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        sequence = index + 1
        if fault == "sequence_gap" and fault_codes[index] == "SEQUENCE_GAP":
            sequence += 1
        common = {
            "dataset_version": "synthetic-v2", "generator_version": config["generator_version"],
            "session_id": session_id, "sequence": sequence, "measured_at": measured_at,
            "monotonic_us": int(index * int(config["sample_period_ms"]) * 1000),
            "distance_true_m": float(true_values[index]), "distance_measured_m": measured_m,
            "delta_distance_m": delta, "sensor_valid": bool(valid[index]),
            "fault_code": fault_codes[index], "scenario": scenario, "speed_level": scenario,
            "seed": seed, "split": split, "simulation_mode": True,
            "threshold_m": float(config["threshold_m"]), "crossing_id": episode_ids[index],
            **targets, **crossings,
        }
        dataset.append(common)
        telemetry.append({
            "schema_version": "1.0", "device_id": "sim-v2", "boot_id": session_id,
            "sequence": sequence, "monotonic_us": common["monotonic_us"], "measured_at": measured_at,
            "time_synced": True, "distance_mm": round(measured_m * 1000) if measured_m is not None else None,
            "echo_us": round(measured_m * 1000 * ECHO_US_PER_MM) if measured_m is not None else None,
            "valid": bool(valid[index]), "error_code": fault_codes[index] if not valid[index] else None,
        })
    metadata = {**params, "session_id": session_id, "scenario": scenario, "split": split, "fault": fault}
    return dataset, telemetry, metadata


def generate_release(config_path: str | Path, out_dir: str | Path, *, seed: int = 20261010,
                     sessions_per_scenario: int | None = None, fault_sessions: int | None = None,
                     dry_run: bool = False) -> dict[str, Any]:
    config = _load_config(config_path)
    if seed < 0:
        raise ValueError("seed must be non-negative")
    forecast_counts = {name: int(count) for name, count in config["forecast_sessions"].items()}
    fault_counts = {name: int(count) for name, count in config["fault_sessions"].items()}
    if sessions_per_scenario is not None:
        if sessions_per_scenario < 3:
            raise ValueError("sessions_per_scenario must be >= 3")
        forecast_counts = {name: sessions_per_scenario for name in SCENARIOS}
    if fault_sessions is not None:
        if fault_sessions < 1:
            raise ValueError("fault_sessions must be >= 1")
        fault_counts = {name: fault_sessions for name in FAULTS}
    if dry_run:
        forecast_counts = {name: min(1, count) for name, count in forecast_counts.items()}
        fault_counts = {name: min(1, count) for name, count in fault_counts.items()}
    out = Path(out_dir)
    if out.exists():
        raise FileExistsError(f"output directory already exists: {out}")
    session_splits: dict[str, list[str]] = {"train": [], "val": [], "test": []}
    sessions: list[tuple[str, str, int, str, str | None]] = []
    session_seeds: dict[str, int] = {}
    for scenario, count in forecast_counts.items():
        ids = [f"simv2_{seed}_{scenario}_{index:04d}" for index in range(count)]
        splits = _split_ids(ids, seed + sum(map(ord, scenario)))
        for split, members in splits.items():
            session_splits[split].extend(members)
        for index, sid in enumerate(ids):
            split = next(name for name, members in splits.items() if sid in members)
            session_seed = _session_seed(seed, sid)
            if session_seed in session_seeds.values():
                raise ValueError(f"derived session seed collision for {sid}")
            session_seeds[sid] = session_seed
            sessions.append((sid, scenario, session_seed, split, None))
    fault_session_ids: list[str] = []
    for fault, count in fault_counts.items():
        for index in range(count):
            sid = f"simv2_{seed}_fault_{fault}_{index:04d}"
            fault_session_ids.append(sid)
            session_seed = _session_seed(seed, sid)
            if session_seed in session_seeds.values():
                raise ValueError(f"derived session seed collision for {sid}")
            session_seeds[sid] = session_seed
            sessions.append((sid, "fault_only", session_seed, "train", fault))
    if dry_run:
        return {"dry_run": True, "forecast_sessions": len(sessions) - len(fault_session_ids), "fault_sessions": len(fault_session_ids), "seed": seed}
    dataset: list[dict[str, Any]] = []
    telemetry: list[dict[str, Any]] = []
    references: list[dict[str, Any]] = []
    session_metadata: list[dict[str, Any]] = []
    for sid, scenario, session_seed, split, fault in sessions:
        data_rows, telemetry_rows, metadata = _session_rows(sid, scenario, session_seed, split, config, fault=fault)
        dataset.extend(data_rows)
        telemetry.extend(telemetry_rows)
        session_metadata.append(metadata)
        references.extend({"session_id": sid, "sequence": row["sequence"], "distance_true_m": row["distance_true_m"]} for row in data_rows)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".synthetic-v2-", dir=out.parent) as temp:
        stage = Path(temp) / "release"
        stage.mkdir()
        dataset_hash = _write_jsonl(stage / "dataset.jsonl", dataset)
        telemetry_hash = _write_jsonl(stage / "telemetry.jsonl", telemetry)
        reference_hash = _write_jsonl(stage / "ground_truth.jsonl", references)
        config_hash = hashlib.sha256(Path(config_path).read_bytes()).hexdigest()
        generator_config_hash = _write_yaml(stage / "generator_config.yaml", config)
        scenario_catalog = {"version": "square-bench-v2", "threshold_m": config["threshold_m"], "scenarios": list(SCENARIOS), "faults": list(FAULTS)}
        scenario_catalog_hash = _write_json(stage / "scenario_catalog.json", scenario_catalog)
        split_manifest = {"schema_version": "synthetic-v2-split", "seed": seed, "session_splits": {k: sorted(v) for k, v in session_splits.items()}, "fault_sessions": sorted(fault_session_ids), "session_seeds": {key: session_seeds[key] for key in sorted(session_seeds)}, "strategy": "stratified_session_before_windowing"}
        split_hash = _write_json(stage / "split_manifest.json", split_manifest)
        split_seed_sets = [{session_seeds[sid] for sid in session_splits[name]} for name in ("train", "val", "test")]
        audit = {"session_count": len(sessions) - len(fault_session_ids), "fault_session_count": len(fault_session_ids), "sample_count": len(dataset), "split_session_counts": {k: len(v) for k, v in session_splits.items()}, "dataset_sha256": dataset_hash, "telemetry_sha256": telemetry_hash, "ground_truth_sha256": reference_hash, "config_sha256": config_hash, "generator_config_sha256": generator_config_hash, "scenario_catalog_sha256": scenario_catalog_hash, "split_manifest_sha256": split_hash, "feature_exclusions": ["distance_true_m", "target_d*_m", "crossing_h*", "scenario", "seed", "split"], "leakage_checks": {"session_disjoint": len(set(session_splits["train"]) & set(session_splits["val"]) | set(session_splits["train"]) & set(session_splits["test"]) | set(session_splits["val"]) & set(session_splits["test"])) == 0, "seed_disjoint": not (split_seed_sets[0] & split_seed_sets[1] or split_seed_sets[0] & split_seed_sets[2] or split_seed_sets[1] & split_seed_sets[2]), "simulation_mode": True}}
        _write_json(stage / "audit_report.json", audit)
        manifest = {"schema_version": "synthetic-v2", "dataset_version": "synthetic-v2", "generator_version": config["generator_version"], "seed": seed, "simulation_mode": True, "threshold_m": config["threshold_m"], "sample_period_ms": config["sample_period_ms"], "dataset_sha256": dataset_hash, "telemetry_sha256": telemetry_hash, "ground_truth_sha256": reference_hash, "config_sha256": config_hash, "generator_config_sha256": generator_config_hash, "scenario_catalog_sha256": scenario_catalog_hash, "split_manifest_sha256": split_hash, "session_metadata": session_metadata, "limitations": ["synthetic-only", "threshold_m is a mathematical benchmark, not hardware calibration", "ground_truth is excluded from runtime telemetry and model features"]}
        _write_json(stage / "dataset_manifest.json", manifest)
        stage.rename(out)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/simulation/square-bench-v2.yaml")
    parser.add_argument("--out-dir", required=False)
    parser.add_argument("--seed", type=int, default=20261010)
    parser.add_argument("--sessions-per-scenario", type=int)
    parser.add_argument("--fault-sessions", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not args.dry_run and not args.out_dir:
        parser.error("--out-dir is required unless --dry-run is used")
    result = generate_release(args.config, args.out_dir or ".", seed=args.seed, sessions_per_scenario=args.sessions_per_scenario, fault_sessions=args.fault_sessions, dry_run=args.dry_run)
    print(json.dumps({k: result[k] for k in ("dry_run", "dataset_sha256", "split_manifest_sha256", "sample_count", "session_count", "fault_session_count") if k in result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
