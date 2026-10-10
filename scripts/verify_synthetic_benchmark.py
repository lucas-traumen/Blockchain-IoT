"""Verify a SYNTHETIC benchmark and replay backend 2/3 warning confirmation.

Uses stored forecasts; does not retrain, tune, or modify any real dataset/model.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path

import numpy as np

from ai_model.evaluate import regression_metrics
from ai_model.events import Episode, event_metrics_extended, find_events
from ai_model.prepare import validate_split
from iot_backend.events.detector import EventDetector
from iot_backend.ingestion.store import Store


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", required=True)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--report-dir", required=True)
    args = parser.parse_args(argv)
    dataset_dir, model_dir, root = map(Path, (args.dataset_dir, args.model_dir, args.report_dir))
    report = _read_json(root / "evaluation-500ms.json")
    summary = _read_json(dataset_dir / "generation.json")
    manifest = _read_json(model_dir / "manifest.json")
    split = _read_json(model_dir / "split_manifest.json")
    if report.get("synthetic") is not True or summary.get("synthetic") is not True:
        raise ValueError("Requires an explicitly synthetic report and dataset")
    source_rows = [json.loads(line) for line in (dataset_dir / "dataset.jsonl").read_text().splitlines() if line.strip()]
    assert all(row.get("synthetic") is True and row["boot_id"].startswith("synthetic_") for row in source_rows)
    digest = hashlib.sha256((dataset_dir / "dataset.jsonl").read_bytes()).hexdigest()
    assert digest == manifest["dataset_sha256"] == summary["dataset_sha256"]
    original_split = _read_json(dataset_dir / "split_manifest.json")
    assert split["sessions"] == original_split["sessions"]
    split_counts = validate_split(split["sessions"], {r["session_label"] for r in source_rows})
    assert set(report["test_sessions"]) == set(split["sessions"]["test"])
    with (root / "predictions.csv").open(newline="", encoding="utf-8") as handle:
        forecasts = list(csv.DictReader(handle))
    assert len(forecasts) == report["n_test_windows"] == manifest["n_test_windows"]
    assert {r["session_id"] for r in forecasts} == set(report["test_sessions"])
    prediction_keys = [(row["session_id"], int(row["sequence_last"])) for row in forecasts]
    assert len(prediction_keys) == len(set(prediction_keys))
    predicted = [[float(row[f"lstm_{h}ms_m"]) for h in report["horizons_ms"]] for row in forecasts]
    targets = [[float(row[f"target_{h}ms_m"]) for h in report["horizons_ms"]] for row in forecasts]
    recomputed = regression_metrics(predicted, targets)
    for key in ("mae_per_horizon", "rmse_per_horizon", "mae_mean", "rmse_mean"):
        np.testing.assert_allclose(recomputed[key], report["models"]["lstm"]["regression"][key], atol=1e-12, rtol=1e-10)

    streams: dict[str, list[dict]] = defaultdict(list)
    sources: dict[str, list[dict]] = defaultdict(list)
    for row in forecasts:
        streams[row["session_id"]].append(row)
    for row in source_rows:
        if row["session_label"] in streams:
            sources[row["session_label"]].append(row)
    all_events, confirmations, confirmed_evidence = [], [], []
    offset = 1.0
    with Store(":memory:") as store:
        detector = EventDetector(store)
        for sid in sorted(streams):
            stream = sorted(streams[sid], key=lambda r: int(r["sequence_last"]))
            origin = int(stream[0]["monotonic_us"])
            samples = sorted(sources[sid], key=lambda r: r["sequence"])
            all_events.extend(find_events([r["distance_mm"] / 1000 for r in samples],
                                          [offset + (r["monotonic_us"] - origin) / 1e6 for r in samples], report["sp_m"]))
            for row in stream:
                current = float(row["current_m"])
                distances = [float(row[f"lstm_{h}ms_m"]) for h in report["horizons_ms"]]
                ref = {"device_id": row["device_id"], "boot_id": row["boot_id"], "sequence": int(row["sequence_last"])}
                record = {
                    "sample_ref": ref, "session_id": sid,
                    "safety": {"status": "CLEAR" if current > report["sp_m"] else "VIOLATION",
                               "config_version": "synthetic-square40"},
                    "prediction": {"status": "OK", "model_version": manifest["model_version"],
                                   "distance_m": distances, "early_warning": any(d <= report["sp_m"] for d in distances),
                                   "stale": False, "mode": "offline"},
                }
                detector.observe(record, {"monotonic_us": int(row["monotonic_us"])}, "synthetic-replay")
            by_seq = {int(row["sequence_last"]): row for row in stream}
            for event in store.list_events("synthetic-replay", sid):
                if event["event_type"] != "PREDICTED_WARNING":
                    continue
                confirmed_sequence = event["confirmed_ref"]["sequence"]
                activation = offset + (int(by_seq[confirmed_sequence]["monotonic_us"]) - origin) / 1e6
                # Effective alarm time is confirmation time, not backdated first evidence.
                confirmations.append(Episode(confirmed_sequence, confirmed_sequence, activation, activation))
                confirmed_evidence.append({"session_id": sid, "trigger_sequence": event["trigger_ref"]["sequence"],
                                           "confirmed_sequence": confirmed_sequence, "synthetic": True})
            offset += samples[-1]["monotonic_us"] / 1e6 - samples[0]["monotonic_us"] / 1e6 + 1.0
    debounced = event_metrics_extended(confirmations, all_events, report["horizons_ms"][-1] / 1000)
    assert debounced["n_events"] == report["models"]["lstm"]["event_metrics"]["n_events"]
    debounce_doc = {"synthetic": True, "detector": "iot_backend.events.detector.EventDetector",
                    "definition": "2 of 3 warnings, matched at actual confirmation time; no live latency simulated",
                    "metrics": debounced, "confirmed_events": confirmed_evidence}
    (root / "evaluation-debounced.json").write_text(json.dumps(debounce_doc, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    before_path = root / "real-checksums-before.json"
    preserved = {}
    if before_path.is_file():
        preserved = {path: hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
                     for path, digest in _read_json(before_path).items()}
        assert all(preserved.values())
    result = {"synthetic": True, "dataset_sha256": digest, "source_rows": len(source_rows),
              "split_sessions": split_counts, "test_windows": len(forecasts),
              "checks": {"synthetic_labels": True, "artifact_dataset_hash": True, "split_unchanged": True,
                         "no_session_overlap": True, "regression_recomputed_from_csv": True},
              "real_files_preserved": preserved, "debounced": debounced}
    (root / "verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
