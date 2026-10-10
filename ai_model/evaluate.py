"""Đánh giá offline LSTM và các baseline trên split test của artifact.

Evaluator chỉ chạy inference trên artifact LSTM đã phát hành. Dataset được
tái dựng bằng danh sách session lưu trong ``split_manifest.json`` để mọi
window test đúng với lúc train; không tự chọn lại split bằng seed. Report
bao gồm LSTM, persistence và constant-velocity.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from statistics import fmean
from typing import Callable, Sequence

import numpy as np
import yaml

from ai_model.baselines import predict_constant_velocity, predict_persistence
from ai_model.events import (
    Episode,
    Event,
    event_metrics_extended,
    find_events,
    find_warning_points,
    merge_warning_points,
)
from ai_model.prepare import (
    SPLIT_NAMES,
    build_lstm_dataset,
    load_jsonl_dataset,
)
from ai_model.preprocessing import sha256_file
from ai_model.windows import Window, WindowConfig, extract_windows

__all__ = [
    "event_metrics_grouped",
    "mae_per_horizon",
    "regression_metrics",
    "regression_metrics_grouped",
    "rmse_per_horizon",
    "evaluate_lstm_models",
    "main",
]


def _validate_matrix_pair(
    predictions: Sequence[Sequence[float]], targets: Sequence[Sequence[float]]
) -> int:
    """Kiểm tra contract chung n×H của cặp predictions/targets.

    Cùng số hàng, không rỗng, mọi hàng cùng số cột H, mọi phần tử hữu hạn
    (không NaN/inf). Sai → ValueError. Trả H (số horizon).
    """
    if len(predictions) != len(targets):
        raise ValueError(
            f"predictions ({len(predictions)}) và targets ({len(targets)}) "
            "phải cùng số hàng"
        )
    if not predictions:
        raise ValueError("predictions rỗng — không có gì để tính metric")
    n_horizons = len(predictions[0])
    if n_horizons < 1:
        raise ValueError("mỗi hàng dự báo phải có ít nhất 1 horizon")
    for i, (row_p, row_t) in enumerate(zip(predictions, targets)):
        if len(row_p) != n_horizons or len(row_t) != n_horizons:
            raise ValueError(
                f"hàng {i} lệch số horizon: predictions {len(row_p)}, "
                f"targets {len(row_t)}, kỳ vọng {n_horizons}"
            )
        for value in list(row_p) + list(row_t):
            value = float(value)
            if value != value or value in (float("inf"), float("-inf")):
                raise ValueError(
                    f"hàng {i} chứa giá trị không hữu hạn (NaN/inf) — "
                    "không tính metric trên dữ liệu lỗi"
                )
    return n_horizons


def rmse_per_horizon(
    predictions: Sequence[Sequence[float]], targets: Sequence[Sequence[float]]
) -> list[float]:
    """RMSE (mét) từng horizon: căn bình phương trung bình sai số theo cột.

    ``predictions``/``targets``: n×H như ``mae_per_horizon``. Validate cùng
    số hàng, không rỗng, mọi hàng cùng H, mọi giá trị hữu hạn — sai →
    ValueError. KHÔNG clamp/không làm tròn: trả H số float thô, thứ tự
    khớp horizons_ms.
    """
    _validate_matrix_pair(predictions, targets)
    n_horizons = len(predictions[0])
    sq_sums = [0.0] * n_horizons
    for row_p, row_t in zip(predictions, targets):
        for j, (p, t) in enumerate(zip(row_p, row_t)):
            diff = float(p) - float(t)
            sq_sums[j] += diff * diff
    n = len(predictions)
    return [math.sqrt(total / n) for total in sq_sums]


def regression_metrics(
    predictions: Sequence[Sequence[float]], targets: Sequence[Sequence[float]]
) -> dict:
    """Bộ metric regression đầy đủ trên cặp n×H — API mới của M12a.

    Validate như ``rmse_per_horizon`` (cùng số hàng, không rỗng, mọi hàng
    cùng H, hữu hạn — sai → ValueError). Trả::

        {
          "mae_per_horizon": [H],   # mét, theo cột (giống mae_per_horizon)
          "rmse_per_horizon": [H],  # mét, theo cột, không clamp/round
          "mae_mean": float,        # trung bình MAE mọi horizon
          "rmse_mean": float,       # trung bình RMSE mọi horizon
          "n_samples": int,         # số hàng n
        }

    Hàm thuần, không TensorFlow/DB/I/O — M12b dùng để đánh giá dự báo
    LSTM và hai baseline khoảng cách.
    """
    _validate_matrix_pair(predictions, targets)
    mae = mae_per_horizon(predictions, targets)
    rmse = rmse_per_horizon(predictions, targets)
    return {
        "mae_per_horizon": mae,
        "rmse_per_horizon": rmse,
        "mae_mean": fmean(mae),
        "rmse_mean": fmean(rmse),
        "n_samples": len(predictions),
    }


def regression_metrics_grouped(
    grouped: dict[str, tuple[Sequence[Sequence[float]], Sequence[Sequence[float]]]]
) -> dict[str, dict]:
    """``regression_metrics`` tính RIÊNG theo group (scenario/target_type).

    Input: mapping group → ``(predictions, targets)`` — caller tự gán cửa
    sổ vào group theo metadata, hàm không tự ghép dữ liệu giữa các group
    (không leak test/train). Mọi group đều được tính và xuất ra output,
    KHÔNG silently drop; group rỗng → ValueError (không bịa metric).
    Output: mapping group → dict của ``regression_metrics``.
    """
    if not grouped:
        raise ValueError("grouped rỗng — không có group nào để tính")
    result: dict[str, dict] = {}
    for group, (predictions, targets) in grouped.items():
        if not predictions:
            raise ValueError(
                f"group {group!r} không có mẫu — không silently drop, "
                "caller phải gán dữ liệu đầy đủ"
            )
        result[group] = regression_metrics(predictions, targets)
    return result


def event_metrics_grouped(
    grouped: dict[str, tuple[Sequence[Episode], Sequence[Event]]],
    horizon_s: float,
) -> dict[str, dict]:
    """``event_metrics_extended`` tính RIÊNG theo group (vd session/scenario).

    Input: mapping group → ``(episodes, events)`` thuộc group đó; match
    Episode–Event chỉ chạy PHẠM VI group (không ghép xuyên session). Mọi
    group đều xuất ra output, không silently drop; group không có Event →
    ``recall``/``miss_rate`` None theo quy ước của
    ``event_metrics_extended``, KHÔNG tự đổi thành 0. Output: mapping
    group → dict của ``event_metrics_extended``. ``horizon_s`` >= 0.
    """
    if not grouped:
        raise ValueError("grouped rỗng — không có group nào để tính")
    result: dict[str, dict] = {}
    for group, (episodes, events) in grouped.items():
        result[group] = event_metrics_extended(episodes, events, horizon_s)
    return result


def mae_per_horizon(
    predictions: Sequence[Sequence[float]], targets: Sequence[Sequence[float]]
) -> list[float]:
    """MAE (mét) từng horizon: trung bình |dự báo − thực tế| theo cột.

    ``predictions`` và ``targets``: list[list] n×H (mỗi hàng một cửa sổ,
    mỗi phần tử một horizon theo thứ tự horizons_ms). Hai input phải cùng
    số hàng, không rỗng, mọi hàng cùng số cột và mọi giá trị hữu hạn —
    sai → ValueError. Trả list H số float, thứ tự khớp horizons_ms.
    """
    _validate_matrix_pair(predictions, targets)
    n_horizons = len(predictions[0])
    sums = [0.0] * n_horizons
    for i, (row_p, row_t) in enumerate(zip(predictions, targets)):
        if len(row_p) != n_horizons or len(row_t) != n_horizons:
            raise ValueError(
                f"hàng {i} lệch số horizon: predictions {len(row_p)}, "
                f"targets {len(row_t)}, kỳ vọng {n_horizons}"
            )
        for j, (p, t) in enumerate(zip(row_p, row_t)):
            sums[j] += abs(float(p) - float(t))
    return [total / len(predictions) for total in sums]


def _collect_session_samples(
    windows: Sequence[Window], target_offsets: Sequence[int]
) -> dict[int, float]:
    """(sequence → distance_m) của một session, gom từ features + targets.

    Xem module docstring ("Cách dựng dãy distance thực"): ``features[k]``
    ứng sequence ``meta.sequence_first + k``; ``targets[j]`` ứng sequence
    ``meta.sequence_last + target_offsets[j]``. Khử trùng theo sequence,
    giữ giá trị ĐẦU gặp — các cửa sổ cùng run chia sẻ mẫu nên giá trị trùng
    khớp nhau.
    """
    samples: dict[int, float] = {}
    for window in windows:
        first = window.meta.sequence_first
        for k, value in enumerate(window.features):
            samples.setdefault(first + k, float(value))
        last = window.meta.sequence_last
        for j, value in enumerate(window.targets):
            samples.setdefault(last + int(target_offsets[j]), float(value))
    return samples


def _collect_events_and_episodes(
    test_windows: Sequence[Window],
    predictions_by_model: dict[str, list[list[float]]],
    target_offsets: Sequence[int],
    delta_s: float,
    horizon_s: float,
    horizon_index: int,
    sp_m: float,
) -> tuple[list[Event], dict[str, list[Episode]]]:
    """Episode/Event của TỪNG model trên tập cửa sổ test — helper generic
    dùng chung cho report chính và breakdown (không copy logic lệch).

    Cách dựng dãy distance theo session + offset thời gian giữa sessions:
    theo cùng quy tắc session-offset ở evaluator — samples khử trùng theo
    sequence từ ``_collect_session_samples``, time session cộng offset tích
    lũy (span + horizon_s + delta_s) để không match xuyên session. Warning
    points tính RIÊNG cho từng model trong ``predictions_by_model`` (cùng
    currents = ``features[-1]``); Event (crossing >Sp → <=Sp trên dãy
    distance thực) dùng chung — Event thuộc dữ liệu, không thuộc model.

    Trả ``(events_all, episodes_by_model)``: Event gộp mọi session (đã
    offset) và Episode theo tên model.
    """
    events_all: list[Event] = []
    episodes_by_model: dict[str, list[Episode]] = {
        name: [] for name in predictions_by_model
    }
    streams: dict[tuple, list[int]] = {}
    for idx, window in enumerate(test_windows):
        key = (window.meta.session_id,window.meta.device_id,window.meta.boot_id)
        streams.setdefault(key, []).append(idx)
    runs = []
    for key in sorted(streams, key=str):
        ordered = sorted(streams[key], key=lambda i: test_windows[i].meta.sequence_last)
        run = []
        for idx in ordered:
            if run and test_windows[idx].meta.sequence_last != test_windows[run[-1]].meta.sequence_last + 1:
                runs.append(run)
                run = []
            run.append(idx)
        if run:
            runs.append(run)
    time_offset_s = 0.0
    for idxs in runs:
        session_windows = [test_windows[i] for i in idxs]
        samples = _collect_session_samples(session_windows, target_offsets)
        seq_min = min(samples)
        ordered_seqs = sorted(samples)
        distances = [samples[s] for s in ordered_seqs]
        times = [time_offset_s + (s - seq_min) * delta_s for s in ordered_seqs]
        events_all.extend(find_events(distances, times, sp_m))

        currents = [w.features[-1] for w in session_windows]
        window_times = [
            time_offset_s + (w.meta.sequence_last - seq_min) * delta_s
            for w in session_windows
        ]
        for name, rows in predictions_by_model.items():
            points = find_warning_points(
                [rows[i] for i in idxs], currents, sp_m, horizon_index
            )
            episodes_by_model[name].extend(
                merge_warning_points(points, delta_s, window_times)
            )
        # Session kế chiếm khoảng thời gian không giao cửa sổ match
        # [start, end + horizon_s] của session này.
        session_span_s = (max(samples) - seq_min) * delta_s
        time_offset_s += session_span_s + horizon_s + delta_s
    return events_all, episodes_by_model


def _model_metrics(
    test_windows: Sequence[Window],
    predictions_by_model: dict[str, list[list[float]]],
    window_config: WindowConfig,
    horizon_index: int,
    sp_m: float,
) -> dict[str, dict]:
    """Regression + event metrics của từng model trên CÙNG tập cửa sổ.

    Helper generic (dùng cho report chính và từng breakdown group):
    ``regression`` = ``regression_metrics`` trên targets mét của cửa sổ;
    ``event_metrics`` = ``event_metrics_extended``. Event/Episode dựng qua
    ``_collect_events_and_episodes`` — không pool xuyên session.
    """
    y_rows = [[float(t) for t in w.targets] for w in test_windows]
    horizon_s = window_config.horizons_ms[horizon_index] / 1000.0
    events_all, episodes_by_model = _collect_events_and_episodes(
        test_windows,
        predictions_by_model,
        window_config.target_offsets,
        window_config.delta_s,
        horizon_s,
        horizon_index,
        sp_m,
    )
    models: dict[str, dict] = {}
    for name, rows in predictions_by_model.items():
        models[name] = {
            "regression": regression_metrics(rows, y_rows),
            "event_metrics": event_metrics_extended(
                episodes_by_model[name], events_all, horizon_s
            ),
        }
    return models


def _group_window_indices(
    test_windows: Sequence[Window], session_meta: dict, field: str
) -> dict[str, list[int]]:
    """Gán mỗi cửa sổ test vào ĐÚNG MỘT group theo metadata session.

    ``session_meta``: session_id → metadata (M10 ``LSTMSplit.session_meta``).
    Giá trị field thiếu/rỗng → group ``"unknown"`` (không silently drop);
    kết quả: group → danh sách index cửa sổ (mỗi index xuất hiện đúng 1
    lần nên tổng ``n_test_windows`` các group luôn bằng tổng).
    """
    groups: dict[str, list[int]] = {}
    for idx, window in enumerate(test_windows):
        meta = session_meta.get(window.meta.session_id) or {}
        value = meta.get(field)
        group = str(value) if value not in (None, "") else "unknown"
        groups.setdefault(group, []).append(idx)
    return groups


def _breakdown_by(
    test_windows: Sequence[Window],
    predictions_by_model: dict[str, list[list[float]]],
    session_meta: dict,
    window_config: WindowConfig,
    horizon_index: int,
    sp_m: float,
    field: str,
) -> dict[str, dict]:
    """Breakdown metrics theo một metadata field (scenario/target_type).

    Mỗi group cô lập: windows + predictions cắt theo index group rồi gọi
    ``_model_metrics`` — event/regression KHÔNG ghép giữa các group.
    """
    out: dict[str, dict] = {}
    for group, idxs in _group_window_indices(
        test_windows, session_meta, field
    ).items():
        sub_windows = [test_windows[i] for i in idxs]
        sub_preds = {
            name: [rows[i] for i in idxs]
            for name, rows in predictions_by_model.items()
        }
        out[group] = {
            "n_test_windows": len(idxs),
            "models": _model_metrics(
                sub_windows, sub_preds, window_config, horizon_index, sp_m
            ),
        }
    return out


def evaluate_lstm_models(
    dataset_path: str | Path,
    lstm_model_dir: str | Path,
    config_path: str | Path = "configs/model.yaml",
    sp_m: float = 0.04,
    horizon_index: int = 4,
    *,
    lstm_loader: Callable[[str | Path], tuple] | None = None,
) -> dict:
    """Đánh giá artifact LSTM và hai baseline trên đúng split test đã phát hành.

    ``split_manifest.json`` là nguồn sự thật cho danh sách session của từng
    split. Evaluator truyền danh sách đó vào ``build_lstm_dataset`` qua
    ``session_split``; seed chỉ là metadata để đối chiếu, không được dùng để
    chọn lại session. Dataset hash và số window vẫn phải khớp manifest.

    Trả report gồm ``lstm``, ``persistence`` và ``constant_velocity`` cùng
    regression/event metrics, breakdown theo scenario và target_type.
    """
    if not math.isfinite(float(sp_m)):
        raise ValueError(f"sp_m phải hữu hạn, nhận {sp_m}")
    sp_m = float(sp_m)

    with Path(config_path).open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    window_config = WindowConfig.from_dict(config)
    horizons_ms = window_config.horizons_ms
    delta_s = window_config.delta_s
    if not 0 <= horizon_index < len(horizons_ms):
        raise ValueError(
            f"horizon_index phải trong [0, {len(horizons_ms) - 1}] "
            f"(horizons_ms {list(horizons_ms)}), nhận {horizon_index}"
        )

    # Nạp artifact LSTM (loader thật lazy-import để không kéo TensorFlow).
    if lstm_loader is None:
        from ai_model.lstm_train import load_lstm_artifact  # noqa: PLC0415

        lstm_loader = load_lstm_artifact
    loaded = lstm_loader(lstm_model_dir)
    if not isinstance(loaded, tuple) or len(loaded) != 3:
        raise ValueError(
            "lstm_loader phải trả tuple (model, scaler, manifest), nhận "
            f"{type(loaded).__name__}"
        )
    model, scaler, manifest = loaded
    from ai_model.lstm_train import validate_lstm_config
    validate_lstm_config(manifest, config)

    # Dataset phải chính là dataset lúc train (hash khớp manifest TRƯỚC
    # mọi tính toán).
    dataset_hash = sha256_file(dataset_path)
    if dataset_hash != manifest.get("dataset_sha256"):
        raise ValueError(
            f"SHA-256 dataset {dataset_hash} lệch manifest dataset_sha256 "
            f"{manifest.get('dataset_sha256')!r} — không phải dataset đã "
            "train, từ chối đánh giá"
        )

    records = load_jsonl_dataset(dataset_path)
    synthetic = any(
        bool(record["meta"].get("synthetic")) for record in records
    )

    split_manifest = json.loads(
        (Path(lstm_model_dir) / "split_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    ratios = split_manifest["ratios"]
    session_split = split_manifest["sessions"]
    dataset = build_lstm_dataset(
        records,
        window_config,
        model_config=config,
        seed=int(manifest["random_seed"]),
        ratios=ratios,
        session_split=session_split,
    )

    # Split integrity: session lists + counts từng split phải khớp split
    # manifest đã hash trong artifact; n_test_windows khớp cả hai manifest.
    for name in SPLIT_NAMES:
        expected_sessions = sorted(split_manifest["sessions"][name])
        actual_sessions = sorted(dataset.split_manifest["sessions"][name])
        if expected_sessions != actual_sessions:
            raise ValueError(
                f"split '{name}': session lists không khớp split manifest — "
                f"manifest={expected_sessions}, tái lập={actual_sessions} "
                "(seed/ratios/dataset lệch artifact)"
            )
        expected_windows = int(split_manifest["splits"][name]["n_windows"])
        actual_windows = getattr(dataset, name).n_windows
        if expected_windows != actual_windows:
            raise ValueError(
                f"split '{name}': n_windows {actual_windows} lệch split "
                f"manifest {expected_windows}"
            )
    test = dataset.test
    if test.n_windows != int(manifest["n_test_windows"]):
        raise ValueError(
            f"n_test_windows {test.n_windows} lệch model manifest "
            f"{int(manifest['n_test_windows'])}"
        )
    if test.n_windows == 0:
        raise ValueError(
            "split test LSTM rỗng — không có gì để đánh giá (kiểm ratios/"
            "dataset của artifact)"
        )

    # Contract shape: X (N,10,2) raw mét, y (N,5) raw mét.
    x_arr = np.asarray(test.X)
    y_arr = np.asarray(test.y)
    input_shape = [int(v) for v in manifest["input_shape"]]
    output_shape = [int(v) for v in manifest["output_shape"]]
    if list(x_arr.shape[1:]) != input_shape:
        raise ValueError(
            f"test.X shape {x_arr.shape} không khớp input_shape "
            f"{input_shape} của manifest"
        )
    if list(y_arr.shape[1:]) != output_shape:
        raise ValueError(
            f"test.y shape {y_arr.shape} không khớp output_shape "
            f"{output_shape} của manifest"
        )

    # LSTM: transform X (scaler train-only) → predict → inverse y về mét.
    # KHÔNG round/clamp trước metric hoặc Sp.
    prediction = np.asarray(model.predict(scaler.transform_X(x_arr), verbose=0))
    if list(prediction.shape) != [x_arr.shape[0]] + output_shape:
        raise ValueError(
            f"model.predict shape {prediction.shape} không khớp "
            f"({x_arr.shape[0]}, {output_shape})"
        )
    if not np.all(np.isfinite(prediction)):
        raise ValueError(
            "model.predict chứa NaN/inf — không tính metric trên output lỗi"
        )
    prediction_m = np.asarray(scaler.inverse_y(prediction))
    if not np.all(np.isfinite(prediction_m)):
        raise ValueError(
            "inverse_y chứa NaN/inf — scaler artifact lỗi, từ chối report"
        )
    lstm_rows = [[float(v) for v in row] for row in prediction_m]

    # Baselines trên raw distance channel X[:,:,0] (mét) — cùng horizons/delta.
    distance_rows = [[float(v) for v in x_arr[i, :, 0]] for i in range(len(x_arr))]
    predictions_by_model: dict[str, list[list[float]]] = {
        "lstm": lstm_rows,
        "persistence": [
            predict_persistence(f, n_horizons=len(horizons_ms))
            for f in distance_rows
        ],
        "constant_velocity": [
            predict_constant_velocity(f, delta_s, horizons_ms)
            for f in distance_rows
        ],
    }

    test_windows = list(test.windows)
    models = _model_metrics(
        test_windows, predictions_by_model, window_config, horizon_index, sp_m
    )
    report = {
        "models": models,
        "n_test_windows": int(test.n_windows),
        "test_sessions": sorted(test.session_ids),
        "horizons_ms": [int(h) for h in horizons_ms],
        "sp_m": sp_m,
        "horizon_index": int(horizon_index),
        "synthetic": bool(synthetic),
        "breakdown": {
            "scenario": _breakdown_by(
                test_windows,
                predictions_by_model,
                test.session_meta,
                window_config,
                horizon_index,
                sp_m,
                "scenario",
            ),
            "target_type": _breakdown_by(
                test_windows,
                predictions_by_model,
                test.session_meta,
                window_config,
                horizon_index,
                sp_m,
                "target_type",
            ),
        },
    }
    # JSON-safe là contract: NaN/inf lọt vào → fail ngay tại đây.
    json.dumps(report, allow_nan=False)
    return report


def main(argv: list[str] | None = None) -> int:
    """CLI evaluate: ``python -m ai_model.evaluate``."""
    parser = argparse.ArgumentParser(
        description="Đánh giá offline LSTM và baseline trên split test."
    )
    # Giữ option để các lệnh hiện có vẫn rõ ràng; chỉ LSTM được hỗ trợ.
    parser.add_argument("--model-type", choices=("lstm",), default="lstm")
    parser.add_argument("--output", help="Ghi report JSON ra file thay vì stdout")
    parser.add_argument(
        "--dataset", required=True, help="Đường dẫn file JSONL dataset (bắt buộc)"
    )
    parser.add_argument(
        "--model-dir", default="ai_model/artifacts/lstm-v1",
        help="Thư mục artifact LSTM (mặc định: ai_model/artifacts/lstm-v1)",
    )
    parser.add_argument(
        "--config", default="configs/model.yaml",
        help="File YAML cấu hình (mặc định: configs/model.yaml)",
    )
    parser.add_argument(
        "--sp", type=float, default=0.04,
        help="Ngưỡng vi phạm Sp (mét), mặc định 0.04 = 40 mm.",
    )
    parser.add_argument(
        "--horizon-index", type=int, default=4,
        help="Chỉ số horizon dùng cho cảnh báo/lead time (mặc định: 4).",
    )
    args = parser.parse_args(argv)
    report = evaluate_lstm_models(
        args.dataset, args.model_dir, config_path=args.config,
        sp_m=args.sp, horizon_index=args.horizon_index,
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
