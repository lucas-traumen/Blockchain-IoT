"""Audit a generated synthetic V2 release without training or runtime access."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audit_release(release_dir: str | Path) -> dict[str, Any]:
    release = Path(release_dir)
    manifest = json.loads((release / "dataset_manifest.json").read_text(encoding="utf-8"))
    split = json.loads((release / "split_manifest.json").read_text(encoding="utf-8"))
    audit = json.loads((release / "audit_report.json").read_text(encoding="utf-8"))
    scenario_counts: Counter[str] = Counter()
    fault_counts: Counter[str] = Counter()
    session_rows: defaultdict[str, int] = defaultdict(int)
    crossing_counts: Counter[str] = Counter()
    leakage_fields = {"distance_true_m", "target_d100_m", "crossing_h500", "scenario", "seed", "split"}
    runtime_leakage: set[str] = set()
    sample_count = 0
    with (release / "dataset.jsonl").open(encoding="utf-8") as dataset:
        for raw in dataset:
            if not raw.strip():
                continue
            row = json.loads(raw)
            sample_count += 1
            session_rows[str(row["session_id"])] += 1
            scenario_counts[str(row["scenario"])] += 1
            if row["fault_code"] is not None:
                fault_counts[str(row["fault_code"])] += 1
    with (release / "telemetry.jsonl").open(encoding="utf-8") as telemetry:
        for raw in telemetry:
            if not raw.strip():
                continue
            row = json.loads(raw)
            runtime_leakage.update(leakage_fields.intersection(row))
    with (release / "ground_truth.jsonl").open(encoding="utf-8") as truth:
        for raw in truth:
            if not raw.strip():
                continue
            row = json.loads(raw)
            if row["distance_true_m"] <= manifest["threshold_m"]:
                crossing_counts["below_threshold"] += 1
            else:
                crossing_counts["above_threshold"] += 1

    forecast_sessions = sum(len(values) for values in split["session_splits"].values())
    fault_sessions = len(split["fault_sessions"])
    checks = {
        "forecast_sessions": forecast_sessions == 1200,
        "fault_sessions": fault_sessions == 150,
        "sample_count": sample_count == 270000,
        "session_disjoint": bool(audit["leakage_checks"]["session_disjoint"]),
        "seed_disjoint": bool(audit["leakage_checks"]["seed_disjoint"]),
        "runtime_ground_truth_separation": not runtime_leakage,
        "simulation_mode": bool(audit["leakage_checks"]["simulation_mode"]),
        "dataset_hash": _sha256(release / "dataset.jsonl") == manifest["dataset_sha256"],
        "telemetry_hash": _sha256(release / "telemetry.jsonl") == manifest["telemetry_sha256"],
        "ground_truth_hash": _sha256(release / "ground_truth.jsonl") == manifest["ground_truth_sha256"],
    }
    return {
        "schema_version": "synthetic-v2-audit",
        "status": "PASS" if all(checks.values()) else "FAIL",
        "release": str(release),
        "dataset_sha256": manifest["dataset_sha256"],
        "telemetry_sha256": manifest["telemetry_sha256"],
        "ground_truth_sha256": manifest["ground_truth_sha256"],
        "sample_count": sample_count,
        "session_count": len(session_rows),
        "forecast_session_count": forecast_sessions,
        "fault_session_count": fault_sessions,
        "split_session_counts": {name: len(values) for name, values in split["session_splits"].items()},
        "scenario_sample_counts": dict(sorted(scenario_counts.items())),
        "fault_sample_counts": dict(sorted(fault_counts.items())),
        "crossing_support": dict(sorted(crossing_counts.items())),
        "checks": checks,
        "runtime_leakage_fields": sorted(runtime_leakage),
        "limitations": ["synthetic-only", "no hardware calibration", "no public-chain evidence"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    result = audit_release(args.release)
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "output": str(output)}, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
