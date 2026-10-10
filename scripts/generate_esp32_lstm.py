#!/usr/bin/env python3
"""Embed a Keras .keras LSTM artifact into the ESP32 C implementation.

The script replaces only the marked model-weight block in lstm_inference.c.
It does not train or convert the model, and it intentionally leaves runtime
inference code under source control. The generated C uses the exact float32
arrays saved by Keras 3's model.weights.h5.

The build machine needs h5py and numpy. Neither package is needed on ESP32.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import zipfile
from pathlib import Path

import h5py
import numpy as np

BEGIN = "/* BEGIN GENERATED MODEL WEIGHTS */"
END = "/* END GENERATED MODEL WEIGHTS */"
SCALER_BEGIN = "/* BEGIN GENERATED SCALER */"
SCALER_END = "/* END GENERATED SCALER */"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_arrays(artifact: Path) -> dict[str, np.ndarray]:
    with zipfile.ZipFile(artifact) as archive:
        config = json.loads(archive.read("config.json"))
        weights = archive.read("model.weights.h5")

    layers = config["config"]["layers"]
    expected = {
        "input_shape": [10, 2],
        "lstm_units": 32,
        "dense_units": 32,
        "output_units": 5,
        "gate_order": ["i", "f", "c", "o"],
    }
    if config.get("class_name") != "Sequential":
        raise ValueError("artifact không phải Keras Sequential")
    input_layer = layers[0]["config"]
    lstm = layers[1]["config"]
    dense = layers[3]["config"]
    output = layers[4]["config"]
    if input_layer.get("batch_shape", [None, None, None])[1:] != expected["input_shape"]:
        raise ValueError("input shape artifact không phải (10, 2)")
    if lstm.get("units") != expected["lstm_units"] or lstm.get("activation") != "tanh":
        raise ValueError("LSTM architecture không khớp")
    if lstm.get("recurrent_activation") != "sigmoid":
        raise ValueError("LSTM recurrent activation không khớp")
    if dense.get("units") != expected["dense_units"] or dense.get("activation") != "relu":
        raise ValueError("Dense(32) architecture không khớp")
    if output.get("units") != expected["output_units"] or output.get("activation") != "linear":
        raise ValueError("Dense(5) architecture không khớp")

    with h5py.File(io.BytesIO(weights), "r") as root:
        arrays = {
            "s_lstm_kernel": np.asarray(root["layers/lstm/cell/vars/0"], dtype=np.float32),
            "s_lstm_recurrent_kernel": np.asarray(root["layers/lstm/cell/vars/1"], dtype=np.float32),
            "s_lstm_bias": np.asarray(root["layers/lstm/cell/vars/2"], dtype=np.float32),
            "s_dense_kernel": np.asarray(root["layers/dense/vars/0"], dtype=np.float32),
            "s_dense_bias": np.asarray(root["layers/dense/vars/1"], dtype=np.float32),
            "s_output_kernel": np.asarray(root["layers/dense_1/vars/0"], dtype=np.float32),
            "s_output_bias": np.asarray(root["layers/dense_1/vars/1"], dtype=np.float32),
        }
    shapes = {
        "s_lstm_kernel": (2, 128),
        "s_lstm_recurrent_kernel": (32, 128),
        "s_lstm_bias": (128,),
        "s_dense_kernel": (32, 32),
        "s_dense_bias": (32,),
        "s_output_kernel": (32, 5),
        "s_output_bias": (5,),
    }
    for name, expected_shape in shapes.items():
        if arrays[name].shape != expected_shape:
            raise ValueError(f"{name} có shape {arrays[name].shape}, cần {expected_shape}")
        if not np.isfinite(arrays[name]).all():
            raise ValueError(f"{name} chứa NaN/inf")
    return arrays


def load_scaler(path: Path) -> dict[str, np.float32]:
    scaler = json.loads(path.read_text(encoding="utf-8"))
    if scaler.get("features") != ["distance_m", "delta_distance_m"]:
        raise ValueError("scaler features không khớp pp-lstm-v1")
    if scaler.get("target") != "target_distance_m" or scaler.get("fitted_on") != "train_only":
        raise ValueError("scaler target/fitted_on không khớp pp-lstm-v1")
    if scaler.get("preprocessing_version") != "pp-lstm-v1":
        raise ValueError("scaler preprocessing_version không khớp pp-lstm-v1")
    names = ("distance_m", "delta_distance_m", "target_distance_m")
    values: dict[str, np.float32] = {}
    for name in names:
        mean = np.float32(scaler["means"][name])
        std = np.float32(scaler["stds"][name])
        if not np.isfinite(mean) or not np.isfinite(std) or std <= 0.0:
            raise ValueError(f"scaler {name} không hữu hạn hoặc std <= 0")
        values[f"{name}_mean"] = mean
        values[f"{name}_std"] = std
    return values


def format_array(array: np.ndarray, name: str) -> str:
    def format_values(values: np.ndarray, indent: str) -> list[str]:
        rows: list[str] = []
        flat = values.reshape(-1)
        for start in range(0, len(flat), 8):
            row = ", ".join(f"{float(value):.9e}f" for value in flat[start : start + 8])
            rows.append(f"{indent}{row}")
        return rows

    rows: list[str] = []
    if array.ndim == 1:
        rows.extend(format_values(array, "    "))
        body = ",\n".join(rows)
    elif array.ndim == 2:
        for row in array:
            row_lines = format_values(row, "        ")
            rows.append("    {\n" + ",\n".join(row_lines) + "\n    }")
        body = ",\n".join(rows)
    else:
        raise ValueError(f"không hỗ trợ tensor {name} có {array.ndim} chiều")
    shape = "[" + "][".join(str(dim) for dim in array.shape) + "]"
    return f"static const float {name}{shape} = {{\n{body}\n}};"


def generated_block(arrays: dict[str, np.ndarray], artifact_hash: str) -> str:
    declarations = [
        f"/* Generated from model.keras sha256={artifact_hash}; do not edit by hand. */",
        format_array(arrays["s_lstm_kernel"], "s_lstm_kernel"),
        format_array(arrays["s_lstm_recurrent_kernel"], "s_lstm_recurrent_kernel"),
        format_array(arrays["s_lstm_bias"], "s_lstm_bias"),
        format_array(arrays["s_dense_kernel"], "s_dense_kernel"),
        format_array(arrays["s_dense_bias"], "s_dense_bias"),
        format_array(arrays["s_output_kernel"], "s_output_kernel"),
        format_array(arrays["s_output_bias"], "s_output_bias"),
    ]
    return "\n\n".join(declarations)


def generated_scaler_block(values: dict[str, np.float32], scaler_hash: str) -> str:
    return "\n".join(
        [
            f"/* Generated from scaler.json sha256={scaler_hash}; do not edit by hand. */",
            f"static const float s_distance_mean = {float(values['distance_m_mean']):.9e}f;",
            f"static const float s_distance_std = {float(values['distance_m_std']):.9e}f;",
            f"static const float s_delta_mean = {float(values['delta_distance_m_mean']):.9e}f;",
            f"static const float s_delta_std = {float(values['delta_distance_m_std']):.9e}f;",
            f"static const float s_target_mean = {float(values['target_distance_m_mean']):.9e}f;",
            f"static const float s_target_std = {float(values['target_distance_m_std']):.9e}f;",
        ]
    )


def replace_block(source: str, begin_marker: str, end_marker: str, block: str) -> str:
    begin = source.find(begin_marker)
    end = source.find(end_marker)
    if begin < 0 or end < 0 or end < begin:
        raise ValueError(f"không tìm thấy marker {begin_marker!r}/{end_marker!r}")
    end += len(end_marker)
    return source[:begin] + begin_marker + "\n" + block + "\n" + end_marker + source[end:]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--scaler", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check", action="store_true", help="chỉ kiểm tra artifact/marker")
    args = parser.parse_args()

    arrays = load_arrays(args.artifact)
    scaler = load_scaler(args.scaler)
    source = args.output.read_text(encoding="utf-8")
    updated = replace_block(
        source,
        SCALER_BEGIN,
        SCALER_END,
        generated_scaler_block(scaler, sha256(args.scaler)),
    )
    updated = replace_block(
        updated,
        BEGIN,
        END,
        generated_block(arrays, sha256(args.artifact)),
    )
    if args.check:
        if updated != source:
            raise SystemExit("generated model-weight block is stale")
        return 0
    args.output.write_text(updated, encoding="utf-8")
    print(f"generated {args.output} from {args.artifact}")
    print(f"artifact_sha256={sha256(args.artifact)}")
    print(f"scaler_sha256={sha256(args.scaler)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
