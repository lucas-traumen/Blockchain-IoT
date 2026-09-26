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
from pathlib import Path
from typing import Sequence

import pandas as pd
import yaml

from ai_model.baselines import predict_constant_velocity, predict_persistence
from ai_model.events import (
    Episode,
    Event,
    event_metrics,
    find_events,
    find_warning_points,
    merge_warning_points,
)
from ai_model.features import FEATURE_NAMES, window_to_features
from ai_model.prepare import load_jsonl_dataset, split_by_session
from ai_model.train import load_model
from ai_model.windows import Window, WindowConfig, extract_windows

__all__ = ["mae_per_horizon", "evaluate_models", "main"]


def mae_per_horizon(
    predictions: Sequence[Sequence[float]], targets: Sequence[Sequence[float]]
) -> list[float]:
    """MAE (mét) từng horizon: trung bình |dự báo − thực tế| theo cột.

    ``predictions`` và ``targets``: list[list] n×H (mỗi hàng một cửa sổ,
    mỗi phần tử một horizon theo thứ tự horizons_ms). Hai input phải cùng
    số hàng và mọi hàng cùng số cột — sai → ValueError; rỗng → ValueError.
    Trả list H số float, thứ tự khớp horizons_ms.
    """
    if len(predictions) != len(targets):
        raise ValueError(
            f"predictions ({len(predictions)}) và targets ({len(targets)}) "
            "phải cùng số hàng"
        )
    if not predictions:
        raise ValueError("predictions rỗng — không có gì để tính MAE")
    n_horizons = len(predictions[0])
    if n_horizons < 1:
        raise ValueError("mỗi hàng dự báo phải có ít nhất 1 horizon")
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


def main(argv: list[str] | None = None) -> int:
    """CLI evaluate: ``python -m ai_model.evaluate --dataset <file.jsonl>
    --model-dir <dir>``; in report JSON ra stdout."""
    parser = argparse.ArgumentParser(
        description=(
            "Đánh giá model RF + baseline (persistence, constant-velocity) "
            "trên split test: MAE từng horizon + precision/recall/lead time "
            "cảnh báo; in report JSON ra stdout."
        )
    )
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
    report = evaluate_models(
        args.dataset,
        args.model_dir,
        config_path=args.config,
        sp_m=args.sp,
        horizon_index=args.horizon_index,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
