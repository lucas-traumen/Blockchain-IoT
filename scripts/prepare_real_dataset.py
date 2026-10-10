"""Merge real collection exports without altering their telemetry measurements."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

METADATA = {"synthetic", "scenario", "session_label", "target_type", "speed_level", "session_id"}
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas/telemetry_v1.schema.json"


def _read_rows(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_no}: JSON không phải object")
        rows.append(value)
    return rows


def prepare(input_root: str | Path, output: str | Path) -> dict[str, Any]:
    root, out = Path(input_root), Path(output)
    paths = sorted(p for p in root.rglob("dataset.jsonl") if p.is_file() and p.resolve() != out.resolve())
    if not paths:
        raise ValueError(f"Không tìm thấy dataset.jsonl dưới {root}")
    validator = Draft202012Validator(json.loads(SCHEMA_PATH.read_text()), format_checker=FormatChecker())
    all_rows, source_files, warnings = [], [], []
    sessions, keys, measurements = {}, set(), set()
    normalized_speed = 0
    for path in paths:
        rows = _read_rows(path)
        source_files.append({
            "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "rows": len(rows), "sessions": sorted({str(r.get("session_label")) for r in rows}),
        })
        source_manifest = path.parent / "manifest.json"
        manifest_sessions = {}
        if source_manifest.is_file():
            manifest_sessions = {s["session_id"]: s for s in json.loads(source_manifest.read_text()).get("sessions", [])}
        checked_manifest_sessions = set()
        for row in rows:
            validator.validate({k: v for k, v in row.items() if k not in METADATA})
            session = row.get("session_label")
            if not isinstance(session, str) or not session:
                raise ValueError(f"{path}: thiếu session_label")
            if row.get("session_id") not in (None, session):
                raise ValueError(f"{path}: session_id lệch session_label")
            if row.get("synthetic") is not False or row["boot_id"].startswith(("synthetic_", "sim_")):
                raise ValueError(f"{path}: dataset không phải real tại session {session}")
            key = (row["device_id"], row["boot_id"], row["sequence"])
            if key in keys:
                raise ValueError(f"Trùng sample key {key}; không merge âm thầm")
            keys.add(key)
            measured_at = row.get("measured_at")
            if measured_at is not None:
                measurement = (
                    row["device_id"], measured_at, row.get("distance_mm"),
                    row.get("echo_us"), row["valid"], row.get("error_code"),
                )
                if measurement in measurements:
                    raise ValueError(
                        "Trùng phép đo theo device_id/measured_at/nội dung "
                        f"{measurement}; không merge âm thầm"
                    )
                measurements.add(measurement)
            row = dict(row)
            if row.get("speed_level") == "mdeium":
                row["speed_level"] = "medium"
                normalized_speed += 1
            meta = {k: row.get(k) for k in ("scenario", "target_type", "speed_level")}
            previous = sessions.setdefault(session, {**meta, "rows": 0})
            if any(previous[k] != meta[k] for k in meta):
                raise ValueError(f"Session {session} có metadata không nhất quán")
            previous["rows"] += 1
            if session not in checked_manifest_sessions and session in manifest_sessions:
                manifest_speed = manifest_sessions[session].get("speed_level")
                if manifest_speed != meta["speed_level"]:
                    warnings.append({
                        "session_id": session, "source": str(source_manifest),
                        "field": "speed_level", "manifest_value": manifest_speed,
                        "dataset_value": meta["speed_level"],
                        "resolution": "use consistent per-row dataset label; retain original manifest",
                    })
                checked_manifest_sessions.add(session)
            all_rows.append(row)
    all_rows.sort(key=lambda r: (r["session_label"], r["device_id"], r["boot_id"], r["sequence"]))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(r, ensure_ascii=False, allow_nan=False) + "\n" for r in all_rows), encoding="utf-8")
    summary = {
        "input_root": str(root), "output": str(out),
        "dataset_sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
        "source_files": source_files, "rows": len(all_rows), "sessions": len(sessions),
        "invalid_rows": sum(r["valid"] is not True for r in all_rows),
        "schema_errors": 0, "duplicate_samples": 0,
        "duplicate_measurements": 0,
        "normalized_mdeium_to_medium": normalized_speed,
        "scenario_counts": dict(sorted(Counter(str(r.get("scenario")) for r in all_rows).items())),
        "speed_counts": dict(sorted(Counter(str(r.get("speed_level")) for r in all_rows).items())),
        "sessions_detail": sessions, "metadata_warnings": warnings,
    }
    out.with_suffix(".summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge real collection datasets for AI training")
    parser.add_argument("--input-root", default="data/datasets")
    parser.add_argument("--out", default="data/processed/real-all-20261009.jsonl")
    args = parser.parse_args()
    summary = prepare(args.input_root, args.out)
    print(json.dumps({k: summary[k] for k in ("output", "rows", "sessions", "invalid_rows", "dataset_sha256", "metadata_warnings")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
