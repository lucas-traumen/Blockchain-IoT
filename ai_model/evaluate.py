"""Đánh giá model v1 + baseline trên split test (M5d2) — PLAN.md mục 4.

Pipeline ``evaluate_models`` (API hàm + CLI ``python -m ai_model.evaluate``):

1. Nạp YAML cấu hình (``configs/model.yaml``) → ``WindowConfig`` + seed;
   ``load_model`` (train.py) nạp artifact RF từ thư mục do operator chỉ định
   (đường dẫn cấu hình tin cậy — kiểm manifest + feature names trước khi dùng).
2. ``load_jsonl_dataset`` (prepare.py) đọc JSONL; mỗi row gắn
   ``session_id = meta["session_label"]`` (khớp train.py; thiếu nhãn →
   ``split_by_session`` từ chối ở bước sau, không fallback im lặng).
3. ``extract_windows`` → cửa sổ; ``split_by_session`` trên session của cửa sổ
   (ratios mặc định 70/15/15, seed từ config) → session sets. Cùng dataset +
   cùng seed nên ra đúng split đã dùng khi train; CHỈ split test được dùng
   để báo cáo (test không tham gia train/chọn hyperparameter).
4. MAE từng horizon cho 3 mô hình trên mọi cửa sổ test: RF (dự báo cả batch
   một lần bằng ``pandas.DataFrame(..., columns=FEATURE_NAMES)`` — khớp
   ``feature_names_in_`` của model, tránh warning sklearn), persistence
   (d(t) lặp) và constant-velocity (ngoại suy tuyến tính từ cửa sổ).
5. Sự kiện + cảnh báo tính TRÊN TỪNG SESSION test (không dựng chuỗi xuyên
   session), rồi gom episode/event của mọi session vào MỘT lần gọi
   ``event_metrics``.

Cách dựng dãy distance thực theo thời gian của một session — từ chính cửa
sổ, KHÔNG đọc lại raw:

- Mỗi cửa sổ cho các mẫu: ``features[k]`` là khoảng cách tại sequence
  ``meta.sequence_first + k`` (k = 0..N−1); ``targets[j]`` là khoảng cách
  tại sequence ``meta.sequence_last + target_offsets[j]``.
- Gom mọi cặp (sequence, distance) của các cửa sổ trong session, khử trùng
  theo sequence (``setdefault`` — giữ giá trị ĐẦU gặp; các cửa sổ liền kề
  cùng run chia sẻ mẫu nên giá trị trùng khớp nhau). Sắp theo sequence →
  dãy distance của session.
- ``time_s`` của mẫu: ``WindowMeta`` không lưu ``monotonic_us`` nên suy từ
  sequence — ``(sequence − seq_đầu) × delta_s``. Nhịp lệch tối đa
  ``max_time_jitter_s`` (điều kiện trích cửa sổ) nên cách suy này lệch tối
  đa jitter so với thời gian thật; mẫu lỗi/gap không nằm trong cửa sổ nào
  nên không xuất hiện trong dãy (đoạn thời gian tương ứng bị nén).
- Khi GOM nhiều session, time của session được cộng thêm offset tích lũy
  (độ dài session + horizon_s + delta_s) để các session KHÔNG chồng khoảng
  thời gian. Lý do: ``event_metrics`` ghép Episode với Event theo cửa sổ
  ``[start, end + horizon_s]`` — nếu các session cùng bắt đầu từ ~0 thì một
  event của session này có thể khớp nhầm episode của session khác (ghép tùy
  ý với sự kiện xa hơn — điều PLAN.md mục 4 cấm). Trong phạm vi một session,
  time vẫn đúng ``(sequence − seq_đầu) × delta_s``.

Cảnh báo (warning points → episodes) tính cho dự báo RF: cửa sổ i cảnh báo
iff current = d(t) (phần tử CUỐI của features; ``features[9]`` với
window_size = 10) đang CLEAR và min dự báo [0..horizon_index] ≤ Sp — đúng
định nghĩa ``ai_model/events.py``. Baseline chỉ đối chiếu MAE, không tham
gia phát cảnh báo.

Không bịa số liệu: mọi giá trị lấy từ tính toán thật trên split test; không
có sự kiện → các trường tương ứng ``None`` như ``event_metrics`` quy định.
Dataset không có nhãn synthetic → ``synthetic: false`` (không phân biệt xử
lý, chỉ ghi nhận nguồn dữ liệu trong report).
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from statistics import fmean
from typing import Callable, Sequence

import numpy as np
import pandas as pd
import yaml

from ai_model.baselines import predict_constant_velocity, predict_persistence
from ai_model.events import (
    Episode,
    Event,
    event_metrics,
    event_metrics_extended,
    find_events,
    find_warning_points,
    merge_warning_points,
)
from ai_model.features import FEATURE_NAMES, window_to_features
from ai_model.prepare import (
    SPLIT_NAMES,
    build_lstm_dataset,
    load_jsonl_dataset,
    split_by_session,
)
from ai_model.preprocessing import sha256_file
from ai_model.train import load_model
from ai_model.windows import Window, WindowConfig, extract_windows

__all__ = [
    "event_metrics_grouped",
    "mae_per_horizon",
    "regression_metrics",
    "regression_metrics_grouped",
    "rmse_per_horizon",
    "evaluate_models",
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
    LSTM cùng contract với RF/baseline.
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


def evaluate_models(
    dataset_path: str | Path,
    model_dir: str | Path,
    config_path: str | Path = "configs/model.yaml",
    sp_m: float = 0.44,
    horizon_index: int = 4,
) -> dict:
    """Đánh giá RF + 2 baseline trên split test; trả report dict.

    Tham số:

    - ``dataset_path``: file JSONL sinh bởi ``scripts/synthetic_dataset.py``
      (mỗi dòng có nhãn ``session_label`` — bắt buộc để split theo session,
      cùng contract train.py).
    - ``model_dir``: thư mục artifact (``model.joblib`` + ``manifest.json``)
      do operator chỉ định — nạp qua ``load_model``.
    - ``config_path``: YAML cấu hình (window + seed; seed dùng cho split —
      cùng seed lúc train nên split test trùng khớp).
    - ``sp_m``: ngưỡng an toàn Sp (mét) cho phát hiện sự kiện/cảnh báo.
    - ``horizon_index``: chỉ số horizon (0-based theo horizons_ms) dùng cho
      cảnh báo và lead time; mặc định 4 = horizon 500 ms của model v1.

    Cách dựng dãy distance theo session + quy ước time xem module docstring.
    Trả::

        {
          "mae": {"rf": [5], "persistence": [5], "constant_velocity": [5]},
          "n_test_windows": int,
          "event_metrics": {...},   # dict của ai_model.events.event_metrics
          "sp_m": float,
          "horizon_index": int,
          "synthetic": bool,
        }

    Không có cửa sổ test → ValueError (không trả report rỗng bịa). Số
    horizon mỗi dự báo = số phần tử ``horizons_ms`` của config.
    """
    with Path(config_path).open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    window_config = WindowConfig.from_dict(config)
    seed = int(config["random_seed"])
    horizons_ms = window_config.horizons_ms
    delta_s = window_config.delta_s
    if not 0 <= horizon_index < len(horizons_ms):
        raise ValueError(
            f"horizon_index phải trong [0, {len(horizons_ms) - 1}] "
            f"(horizons_ms {list(horizons_ms)}), nhận {horizon_index}"
        )

    model, _manifest = load_model(model_dir)

    records = load_jsonl_dataset(dataset_path)
    rows: list[dict] = []
    for record in records:
        row = dict(record["row"])
        row["session_id"] = record["meta"].get("session_label")
        rows.append(row)
    synthetic = any(bool(record["meta"].get("synthetic")) for record in records)

    windows, _stats = extract_windows(rows, window_config)
    split = split_by_session(windows, seed=seed)
    test_sessions = split["test"]
    test_windows = [w for w in windows if w.meta.session_id in test_sessions]
    if not test_windows:
        raise ValueError(
            f"{dataset_path} không có cửa sổ nào thuộc split test — "
            "không có gì để đánh giá"
        )

    # Feature/target của mọi cửa sổ test (thứ tự extract_windows).
    x_rows = [window_to_features(w) for w in test_windows]
    y_rows = [[float(t) for t in w.targets] for w in test_windows]

    # RF dự báo cả batch một lần; DataFrame với cột FEATURE_NAMES khớp
    # feature_names_in_ của model (load_model đã kiểm manifest) → không
    # warning sklearn "feature names".
    frame = pd.DataFrame(x_rows, columns=FEATURE_NAMES)
    rf_rows = [[float(v) for v in out] for out in model.predict(frame)]
    persistence_rows = [
        predict_persistence(f, n_horizons=len(horizons_ms)) for f in x_rows
    ]
    cv_rows = [predict_constant_velocity(f, delta_s, horizons_ms) for f in x_rows]

    mae = {
        "rf": mae_per_horizon(rf_rows, y_rows),
        "persistence": mae_per_horizon(persistence_rows, y_rows),
        "constant_velocity": mae_per_horizon(cv_rows, y_rows),
    }

    # Sự kiện + cảnh báo TRÊN TỪNG SESSION test (không gộp xuyên session).
    offsets = window_config.target_offsets
    horizon_s = horizons_ms[horizon_index] / 1000.0
    by_session: dict[str, list[int]] = {}
    for idx, window in enumerate(test_windows):
        by_session.setdefault(window.meta.session_id, []).append(idx)

    events_all: list[Event] = []
    episodes_all: list[Episode] = []
    time_offset_s = 0.0
    for session in sorted(by_session):
        # Cửa sổ của session theo thứ tự thời gian (mẫu kết thúc cửa sổ).
        idxs = sorted(
            by_session[session],
            key=lambda i: (
                test_windows[i].meta.sequence_last,
                test_windows[i].meta.sequence_first,
            ),
        )
        session_windows = [test_windows[i] for i in idxs]
        samples = _collect_session_samples(session_windows, offsets)
        seq_min = min(samples)
        ordered_seqs = sorted(samples)
        distances = [samples[s] for s in ordered_seqs]
        times = [time_offset_s + (s - seq_min) * delta_s for s in ordered_seqs]
        events_all.extend(find_events(distances, times, sp_m))

        currents = [w.features[-1] for w in session_windows]
        preds = [rf_rows[i] for i in idxs]
        window_times = [
            time_offset_s + (w.meta.sequence_last - seq_min) * delta_s
            for w in session_windows
        ]
        points = find_warning_points(preds, currents, sp_m, horizon_index)
        episodes_all.extend(merge_warning_points(points, delta_s, window_times))

        # Session kế chiếm khoảng thời gian không giao cửa sổ match
        # [start, end + horizon_s] của session này (xem module docstring).
        session_span_s = (max(samples) - seq_min) * delta_s
        time_offset_s += session_span_s + horizon_s + delta_s

    return {
        "mae": mae,
        "n_test_windows": len(test_windows),
        "event_metrics": event_metrics(
            episodes_all, events_all, horizon_s=horizon_s
        ),
        "sp_m": sp_m,
        "horizon_index": horizon_index,
        "synthetic": synthetic,
    }


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
    giống ``evaluate_models`` (module docstring) — samples khử trùng theo
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
    ``event_metrics`` = ``event_metrics_extended`` (không dùng legacy
    ``event_metrics`` vì legacy có thể trả ``inf``). Event/Episode dựng qua
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
    sp_m: float = 0.44,
    horizon_index: int = 4,
    rf_model_dir: str | Path | None = None,
    *,
    lstm_loader: Callable[[str | Path], tuple] | None = None,
    rf_loader: Callable[[str | Path], tuple] | None = None,
) -> dict:
    """Đánh giá OFFLINE LSTM artifact (+ baseline, + RF tùy chọn) trên đúng
    split test của artifact — API M12b, TEST-ONLY: KHÔNG ``model.fit``,
    KHÔNG đọc/fit train/val, KHÔNG tuning; evaluator chỉ inference + report.

    Tham số:

    - ``dataset_path``: file JSONL (``load_jsonl_dataset``). SHA-256 file
      phải khớp ``manifest["dataset_sha256"]`` của artifact — lệch →
      ``ValueError`` TRƯỚC khi tính report (chống đánh giá nhầm dataset).
    - ``lstm_model_dir``: thư mục 5 artifact LSTM (operator tin cậy);
      ``split_manifest.json`` cung cấp ratios, ``manifest.json`` cung cấp
      seed + ``n_test_windows``. Split tái lập bằng ``build_lstm_dataset``
      với ĐÚNG seed/ratios đó — session lists/counts/``n_test_windows``
      lệch split manifest → ``ValueError`` (không âm thầm đánh giá split
      khác seed). Chỉ ``dataset.test`` được dùng.
    - ``config_path``: YAML cấu hình (windowing + khối lstm).
    - ``sp_m``: ngưỡng an toàn (mét), phải hữu hạn.
    - ``horizon_index``: 0-based theo horizons_ms, dùng cho cảnh báo/lead.
    - ``rf_model_dir``: None = không đánh giá RF; nếu truyền, nạp RF legacy
      (``load_model``) và đánh giá trên CHÍNH các cửa sổ test LSTM (cùng
      session split — không tự split lại seed khác).
    - ``lstm_loader``/``rf_loader``: test seam — callable nhận đường dẫn dir
      và trả tuple artifact (``(model, scaler, manifest)`` /
      ``(model, manifest)``); None → resolve loader thật bên trong hàm
      (``ai_model.lstm_train.load_lstm_artifact`` /
      ``ai_model.train.load_model``) để import module này không kéo
      TensorFlow.

    Test ``X`` phải ``(N,10,2)`` raw mét và ``y`` ``(N,5)`` raw mét; scaler
    chỉ ``transform_X(test.X)`` rồi ``inverse_y`` sau predict — dự báo về
    mét, KHÔNG round/clamp trước metric/Sp; NaN/inf trong output →
    ``ValueError``.

    Trả report JSON-safe (``json.dumps(report, allow_nan=False)`` luôn được;
    mọi số là finite float/int hoặc None)::

        {
          "models": {"lstm" | "persistence" | "constant_velocity" [, "rf"]:
                     {"regression": ..., "event_metrics": ...}},
          "n_test_windows": int,
          "test_sessions": [str],
          "horizons_ms": [int],
          "sp_m": float, "horizon_index": int, "synthetic": bool,
          "breakdown": {"scenario": {...}, "target_type": {...}},
        }

    Breakdown group: metadata từ M10 ``dataset.test.session_meta``; field
    thiếu → group ``"unknown"``; group không có Event → recall/miss None;
    ``n_test_windows`` các group cộng đúng tổng.
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
    seed = int(manifest["random_seed"])
    ratios = split_manifest["ratios"]
    dataset = build_lstm_dataset(
        records,
        window_config,
        model_config=config,
        seed=seed,
        ratios=ratios,
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

    # RF tùy chọn: đánh giá trên CHÍNH các cửa sổ test LSTM (cùng session
    # split — không tự split lại seed khác); DataFrame cột FEATURE_NAMES
    # khớp feature_names_in_ (không warning sklearn, không đổi RF formula).
    if rf_model_dir is not None:
        if rf_loader is None:
            rf_loader = load_model
        rf_model, rf_manifest = rf_loader(rf_model_dir)
        if rf_manifest.get("dataset_sha256") != dataset_hash:
            raise ValueError("RF dataset_sha256 không khớp; train RF trên cùng dataset trước khi so sánh")
        for name in SPLIT_NAMES:
            rf_sessions = rf_manifest.get("split_manifest", {}).get("sessions", {}).get(name)
            if rf_sessions is None or sorted(rf_sessions) != sorted(split_manifest["sessions"][name]):
                raise ValueError(f"RF split {name} khác LSTM — không so sánh khi RF có thể thấy test")
        for key, value in (("window_size",window_config.window_size),
                           ("delta_s",delta_s),("horizons_ms",list(horizons_ms))):
            if rf_manifest.get(key) != value:
                raise ValueError(f"RF artifact {key} không khớp evaluator")
        x_rows = [window_to_features(w) for w in test.windows]
        frame = pd.DataFrame(x_rows, columns=FEATURE_NAMES)
        predictions_by_model["rf"] = [
            [float(v) for v in out] for out in rf_model.predict(frame)
        ]

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
    """CLI evaluate: ``python -m ai_model.evaluate --dataset <file.jsonl>
    --model-dir <dir>``; in report JSON ra stdout."""
    parser = argparse.ArgumentParser(
        description="Đánh giá offline RF/LSTM và baseline trên split test."
    )
    parser.add_argument("--model-type", choices=("rf", "lstm"), default="rf")
    parser.add_argument("--rf-model-dir", help="RF tùy chọn trên cùng split LSTM")
    parser.add_argument("--output", help="Ghi report JSON ra file thay vì stdout")
    parser.add_argument(
        "--dataset",
        required=True,
        help="Đường dẫn file JSONL dataset (bắt buộc)",
    )
    parser.add_argument(
        "--model-dir",
        default="ai_model/artifacts",
        help="Thư mục artifact model (mặc định: ai_model/artifacts)",
    )
    parser.add_argument(
        "--config",
        default="configs/model.yaml",
        help="File YAML cấu hình (mặc định: configs/model.yaml)",
    )
    parser.add_argument(
        "--sp",
        type=float,
        default=0.44,
        help=(
            "Ngưỡng an toàn Sp (mét). Sp demo từ configs/safety.yaml "
            "fixture 0.44"
        ),
    )
    parser.add_argument(
        "--horizon-index",
        type=int,
        default=4,
        help=(
            "Chỉ số horizon dùng cho cảnh báo/lead time, 0-based theo "
            "horizons_ms (mặc định: 4 = 500 ms)"
        ),
    )
    args = parser.parse_args(argv)
    if args.rf_model_dir and args.model_type != "lstm":
        parser.error("--rf-model-dir chỉ dùng với --model-type lstm")
    evaluator = evaluate_lstm_models if args.model_type == "lstm" else evaluate_models
    kwargs = {"rf_model_dir": args.rf_model_dir} if args.model_type == "lstm" else {}
    report = evaluator(
        args.dataset, args.model_dir, config_path=args.config,
        sp_m=args.sp, horizon_index=args.horizon_index, **kwargs,
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
