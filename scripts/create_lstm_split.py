"""Create a clean session split for the LSTM report.

The split is made before windowing and keeps every session in one partition.
The test partition is stratified by scenario and by the presence of a raw
crossing at the configured safety boundary, so event metrics have support
without selecting a split from model predictions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections import defaultdict
from pathlib import Path
from typing import Any

from ai_model.prepare import _largest_remainder_counts


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _crossings(rows: list[dict[str, Any]], threshold_mm: float) -> int:
    ordered = sorted(rows, key=lambda row: int(row["sequence"]))
    distances = [
        float(row["distance_mm"])
        for row in ordered
        if row.get("valid") is True and row.get("distance_mm") is not None
    ]
    return sum(a > threshold_mm and b <= threshold_mm for a, b in zip(distances, distances[1:]))


def _choose(candidates: list[str], rng: random.Random) -> str:
    if not candidates:
        raise ValueError("không đủ session để tạo split stratified")
    candidates = sorted(candidates)
    rng.shuffle(candidates)
    return candidates.pop()


def create_split(
    dataset: str | Path,
    out_dataset: str | Path,
    out_split: str | Path,
    *,
    threshold_m: float = 0.04,
    seed: int = 20261010,
    ratios: tuple[float, float, float] = (0.70, 0.15, 0.15),
    window_size: int = 10,
    horizon_count: int = 5,
    excluded_scenarios: tuple[str, ...] = ("sensor_check",),
    out_fault_dataset: str | Path | None = None,
) -> dict[str, Any]:
    source = Path(dataset)
    rows = _read_jsonl(source)
    by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        sid = row.get("session_label")
        if not isinstance(sid, str) or not sid:
            raise ValueError("dataset có row thiếu session_label")
        by_session[sid].append(row)

    min_rows = window_size + horizon_count
    info: dict[str, dict[str, Any]] = {}
    eligible: dict[str, dict[str, Any]] = {}
    for sid, session_rows in sorted(by_session.items()):
        ordered = sorted(session_rows, key=lambda row: int(row["sequence"]))
        valid_rows = [
            row for row in ordered
            if row.get("valid") is True and row.get("distance_mm") is not None
        ]
        meta = {
            "scenario": ordered[0].get("scenario", "unknown"),
            "target_type": ordered[0].get("target_type", "unknown"),
            "speed_level": ordered[0].get("speed_level"),
        }
        record = {
            **meta,
            "rows": len(ordered),
            "valid_rows": len(valid_rows),
            "crossings": _crossings(ordered, threshold_m * 1000.0),
            "eligible": len(valid_rows) >= min_rows and meta["scenario"] not in excluded_scenarios,
            "exclusion_reason": (
                f"scenario_excluded:{meta['scenario']}"
                if meta["scenario"] in excluded_scenarios
                else ("too_short_for_window" if len(valid_rows) < min_rows else None)
            ),
        }
        info[sid] = record
        if record["eligible"]:
            eligible[sid] = record

    counts = _largest_remainder_counts(len(eligible), ratios)
    if counts[1] < 7 or counts[2] < 7:
        raise ValueError(f"không đủ session cho validation/test: {counts}")
    rng = random.Random(seed)
    unused = set(eligible)
    test: list[str] = []

    # Event-focused coverage: one crossing session per movement scenario.
    # Sensor-check sessions are kept in the separate fault dataset.
    # This split does not provide whole-session negative-event coverage.
    event_scenarios = ("approach", "oscillation", "recede", "stop", "stationary")
    for scenario in event_scenarios:
        candidates = [
            sid for sid in unused
            if eligible[sid]["scenario"] == scenario and eligible[sid]["crossings"] > 0
        ]
        sid = _choose(candidates, rng)
        test.append(sid)
        unused.remove(sid)
    # Fill the remaining test slots from event-bearing sessions using the same
    # deterministic seed; this is based only on raw labels, before training.
    candidates = [sid for sid in unused if eligible[sid]["crossings"] > 0]
    while len(test) < counts[2]:
        candidates = [sid for sid in unused if eligible[sid]["crossings"] > 0]
        sid = _choose(candidates, rng)
        test.append(sid)
        unused.remove(sid)

    val: list[str] = []
    for scenario in ("approach", "oscillation", "recede", "stop", "stationary"):
        candidates = [sid for sid in unused if eligible[sid]["scenario"] == scenario]
        if not candidates:
            continue
        event_candidates = [sid for sid in candidates if eligible[sid]["crossings"] > 0]
        sid = _choose(event_candidates or candidates, rng)
        val.append(sid)
        unused.remove(sid)
    while len(val) < counts[1]:
        sid = _choose(sorted(unused), rng)
        val.append(sid)
        unused.remove(sid)
    if len(test) != counts[2] or len(val) != counts[1] or len(unused) != counts[0]:
        raise AssertionError((len(unused), len(val), len(test), counts))

    sessions = {"train": sorted(unused), "val": sorted(val), "test": sorted(test)}
    active_rows = [row for row in rows if row["session_label"] in set().union(*map(set, sessions.values()))]
    active_rows.sort(key=lambda row: (row["session_label"], row["device_id"], row["boot_id"], int(row["sequence"])))
    output = Path(out_dataset)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "".join(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in active_rows),
        encoding="utf-8",
    )
    excluded = {sid: value for sid, value in info.items() if not value["eligible"]}
    if out_fault_dataset is not None:
        fault_rows = [row for row in rows if row.get("scenario") in set(excluded_scenarios)]
        fault_path = Path(out_fault_dataset)
        fault_path.parent.mkdir(parents=True, exist_ok=True)
        fault_path.write_text(
            "".join(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in fault_rows),
            encoding="utf-8",
        )
    split = {
        "schema_version": "lstm-session-split-v1",
        "dataset": str(output),
        "dataset_sha256": _sha256(output),
        "source_dataset": str(source),
        "source_dataset_sha256": _sha256(source),
        "seed": seed,
        "ratios": list(ratios),
        "threshold_m": threshold_m,
        "threshold_mm": threshold_m * 1000.0,
        "window_size": window_size,
        "horizon_count": horizon_count,
        "min_valid_rows": min_rows,
        "excluded_scenarios": list(excluded_scenarios),
        "fault_dataset": str(out_fault_dataset) if out_fault_dataset is not None else None,
        "strategy": "session_stratified_by_scenario_and_raw_crossing",
        "excluded_sessions": excluded,
        "sessions": sessions,
        "session_counts": {name: len(values) for name, values in sessions.items()},
        "coverage": {
            name: {
                "sessions": len(values),
                "rows": sum(info[sid]["rows"] for sid in values),
                "crossing_sessions": sum(info[sid]["crossings"] > 0 for sid in values),
                "raw_crossings": sum(info[sid]["crossings"] for sid in values),
                "scenarios": dict(sorted({scenario: sum(info[sid]["scenario"] == scenario for sid in values) for scenario in {info[sid]["scenario"] for sid in values}}.items())),
            }
            for name, values in sessions.items()
        },
        "session_info": {sid: info[sid] for sid in sorted(eligible)},
    }
    split_path = Path(out_split)
    split_path.parent.mkdir(parents=True, exist_ok=True)
    split_path.write_text(json.dumps(split, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return split


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--out-dataset", required=True)
    parser.add_argument("--out-split", required=True)
    parser.add_argument("--threshold-m", type=float, default=0.04)
    parser.add_argument("--seed", type=int, default=20261010)
    parser.add_argument("--exclude-scenario", action="append", default=["sensor_check"])
    parser.add_argument("--out-fault-dataset")
    args = parser.parse_args()
    result = create_split(
        args.dataset, args.out_dataset, args.out_split,
        threshold_m=args.threshold_m, seed=args.seed,
        excluded_scenarios=tuple(args.exclude_scenario),
        out_fault_dataset=args.out_fault_dataset,
    )
    print(json.dumps({
        "dataset": result["dataset"],
        "dataset_sha256": result["dataset_sha256"],
        "sessions": result["session_counts"],
        "coverage": result["coverage"],
        "excluded_sessions": result["excluded_sessions"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
