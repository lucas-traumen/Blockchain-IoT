"""Prepare runtime-shaped train/fault views from a synthetic V2 release.

The generator output is an audit dataset and intentionally contains ground
truth.  This command derives the telemetry-shaped JSONL consumed by the LSTM
pipeline.  It copies only measured values and session metadata; ideal distance,
future targets, crossing labels, split and generator seed never enter a row
used as a runtime feature.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

from scripts.synthetic_dataset import ECHO_US_PER_MM


def _canonical(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: dict[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    path.write_text(payload, encoding="utf-8")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _convert_row(row: dict[str, Any], *, target_type: str) -> dict[str, Any]:
    measured = row["distance_measured_m"]
    distance_mm = None if measured is None else int(round(float(measured) * 1000))
    return {
        "schema_version": "1.0",
        "synthetic": True,
        "session_label": row["session_id"],
        "scenario": row["scenario"],
        "speed_level": row["speed_level"],
        "target_type": target_type,
        "session_id": row["session_id"],
        "device_id": "sim-v2",
        "boot_id": row["session_id"],
        "sequence": row["sequence"],
        "monotonic_us": row["monotonic_us"],
        "measured_at": row["measured_at"],
        "time_synced": True,
        "distance_mm": distance_mm,
        "echo_us": None if distance_mm is None else round(distance_mm * ECHO_US_PER_MM),
        "valid": bool(row["sensor_valid"]),
        "error_code": row["fault_code"] if not row["sensor_valid"] else None,
    }


def prepare_release(release_dir: str | Path, out_dir: str | Path) -> dict[str, Any]:
    release = Path(release_dir)
    if not release.is_dir():
        raise FileNotFoundError(f"synthetic V2 release not found: {release}")
    required = ("dataset.jsonl", "dataset_manifest.json", "split_manifest.json")
    missing = [name for name in required if not (release / name).is_file()]
    if missing:
        raise FileNotFoundError(f"synthetic V2 release is incomplete: {missing}")

    manifest = json.loads((release / "dataset_manifest.json").read_text(encoding="utf-8"))
    split = json.loads((release / "split_manifest.json").read_text(encoding="utf-8"))
    session_splits = {
        name: sorted(str(value) for value in split["session_splits"][name])
        for name in ("train", "val", "test")
    }
    forecast_ids = set().union(*session_splits.values())
    fault_ids = set(str(value) for value in split["fault_sessions"])
    if forecast_ids & fault_ids:
        raise ValueError("forecast and fault session sets overlap")

    out = Path(out_dir)
    if out.exists():
        raise FileExistsError(f"output directory already exists: {out}")
    out.parent.mkdir(parents=True, exist_ok=True)

    forecast_counts = {name: 0 for name in session_splits}
    forecast_rows = 0
    fault_rows = 0
    seen_forecast: set[str] = set()
    seen_fault: set[str] = set()
    forecast_hash = hashlib.sha256()
    fault_hash = hashlib.sha256()

    with tempfile.TemporaryDirectory(prefix=".synthetic-v2-prepared-", dir=out.parent) as temp:
        stage = Path(temp) / "prepared"
        stage.mkdir()
        forecast_path = stage / "training_dataset.jsonl"
        fault_path = stage / "fault_dataset.jsonl"
        with (
            (release / "dataset.jsonl").open(encoding="utf-8") as source,
            forecast_path.open("w", encoding="utf-8") as forecast,
            fault_path.open("w", encoding="utf-8") as faults,
        ):
            for line_no, raw in enumerate(source, start=1):
                if not raw.strip():
                    continue
                row = json.loads(raw)
                if not isinstance(row, dict):
                    raise ValueError(f"dataset line {line_no} is not an object")
                session_id = str(row.get("session_id", ""))
                if session_id in forecast_ids:
                    target = "forecast"
                    converted = _convert_row(row, target_type=target)
                    encoded = _canonical(converted) + "\n"
                    forecast.write(encoded)
                    forecast_hash.update(encoded.encode("utf-8"))
                    seen_forecast.add(session_id)
                    forecast_rows += 1
                    split_name = next(name for name, ids in session_splits.items() if session_id in ids)
                    forecast_counts[split_name] += 1
                elif session_id in fault_ids:
                    target = "fault"
                    converted = _convert_row(row, target_type=target)
                    encoded = _canonical(converted) + "\n"
                    faults.write(encoded)
                    fault_hash.update(encoded.encode("utf-8"))
                    seen_fault.add(session_id)
                    fault_rows += 1
                else:
                    raise ValueError(f"dataset line {line_no} has unknown session {session_id!r}")

        if seen_forecast != forecast_ids:
            raise ValueError(
                f"forecast sessions missing from dataset: {sorted(forecast_ids - seen_forecast)[:5]}"
            )
        if seen_fault != fault_ids:
            raise ValueError(
                f"fault sessions missing from dataset: {sorted(fault_ids - seen_fault)[:5]}"
            )

        prepared_split = {
            "schema_version": "synthetic-v2-training-split",
            "seed": int(split["seed"]),
            "ratios": [0.7, 0.15, 0.15],
            "sessions": session_splits,
            "source_split_manifest_sha256": _sha256(release / "split_manifest.json"),
            "fault_sessions_excluded_from_training": sorted(fault_ids),
        }
        _write_json(stage / "training_split_manifest.json", prepared_split)
        prepared_manifest = {
            "schema_version": "synthetic-v2-prepared",
            "source_release": str(release),
            "source_dataset_sha256": _sha256(release / "dataset.jsonl"),
            "source_manifest_sha256": _sha256(release / "dataset_manifest.json"),
            "source_split_manifest_sha256": _sha256(release / "split_manifest.json"),
            "training_dataset_sha256": forecast_hash.hexdigest(),
            "fault_dataset_sha256": fault_hash.hexdigest(),
            "training_session_counts": {name: len(ids) for name, ids in session_splits.items()},
            "training_row_counts": forecast_counts,
            "training_rows": forecast_rows,
            "fault_sessions": len(fault_ids),
            "fault_rows": fault_rows,
            "feature_exclusions": [
                "distance_true_m", "target_d*_m", "crossing_h*", "seed", "split",
            ],
            "runtime_fields": [
                "device_id", "boot_id", "sequence", "monotonic_us", "measured_at",
                "time_synced", "distance_mm", "echo_us", "valid", "error_code",
            ],
            "limitations": ["synthetic-only", "fault sessions are excluded from model training"],
        }
        _write_json(stage / "prepared_manifest.json", prepared_manifest)
        stage.rename(out)
    return prepared_manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True)
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args(argv)
    result = prepare_release(args.release, args.out_dir)
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
