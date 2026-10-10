"""Export held-out LSTM forecasts and measured targets for report figures."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import numpy as np
import yaml

from ai_model.baselines import predict_constant_velocity, predict_persistence
from ai_model.lstm_train import load_lstm_artifact, validate_lstm_config
from ai_model.prepare import build_lstm_dataset, load_jsonl_dataset
from ai_model.preprocessing import sha256_file
from ai_model.windows import WindowConfig


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--config", default="configs/model.yaml")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    wc = WindowConfig.from_dict(config)
    model, scaler, manifest = load_lstm_artifact(args.model_dir)
    validate_lstm_config(manifest, config)
    if sha256_file(args.dataset) != manifest["dataset_sha256"]:
        raise ValueError("Dataset hash differs from trained artifact")
    split_manifest = json.loads((Path(args.model_dir) / "split_manifest.json").read_text(encoding="utf-8"))
    records = load_jsonl_dataset(args.dataset)
    ds = build_lstm_dataset(
        records,
        wc,
        config,
        seed=manifest["random_seed"],
        ratios=split_manifest["ratios"],
        session_split=split_manifest,
    )
    for name in ("train", "val", "test"):
        if sorted(getattr(ds, name).session_ids) != split_manifest["sessions"][name]:
            raise ValueError(f"Split differs: {name}")

    test = ds.test
    distance = test.X[:, :, 0]
    predictions = {
        "lstm": scaler.inverse_y(model.predict(scaler.transform_X(test.X), verbose=0)),
        "persistence": np.asarray([predict_persistence(x.tolist(), len(wc.horizons_ms)) for x in distance]),
        "constant_velocity": np.asarray([predict_constant_velocity(x.tolist(), wc.delta_s, wc.horizons_ms) for x in distance]),
    }
    if any(p.shape != test.y.shape or not np.isfinite(p).all() for p in predictions.values()):
        raise ValueError("Invalid prediction shape/value")
    raw = {(r["row"]["device_id"], r["row"]["boot_id"], r["row"]["sequence"]): r["row"] for r in records}
    starts = {sid: min(w.meta.sequence_last for w in test.windows if w.meta.session_id == sid) for sid in test.session_ids}
    output = []
    for i, window in enumerate(test.windows):
        meta = window.meta
        row = raw[(meta.device_id, meta.boot_id, meta.sequence_last)]
        first = raw[(meta.device_id, meta.boot_id, starts[meta.session_id])]
        values = {
            "session_id": meta.session_id,
            "scenario": test.session_meta[meta.session_id].get("scenario"),
            "device_id": meta.device_id,
            "boot_id": meta.boot_id,
            "sequence_last": meta.sequence_last,
            "monotonic_us": row["monotonic_us"],
            "time_s": (row["monotonic_us"] - first["monotonic_us"]) / 1e6,
            "current_m": float(distance[i, -1]),
        }
        for j, horizon in enumerate(wc.horizons_ms):
            values[f"target_{horizon}ms_m"] = float(test.y[i, j])
            for name, matrix in predictions.items():
                values[f"{name}_{horizon}ms_m"] = float(matrix[i, j])
        output.append(values)
    if not output:
        raise ValueError("test split không có window")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(output[0]))
        writer.writeheader()
        writer.writerows(output)
    print(json.dumps({"output": str(out), "windows": len(output), "seed": manifest["random_seed"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
