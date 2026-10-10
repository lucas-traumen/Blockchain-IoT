"""Biểu diễn đầu vào LSTM dùng chung cho train và runtime.

Mỗi timestep là [distance_m, delta_distance_m] theo thứ tự cũ → mới.
Delta của timestep đầu bằng 0; window mặc định có 10 timestep.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from ai_model.windows import Window

__all__ = [
    "LSTM_FEATURE_NAMES",
    "lstm_feature_names",
    "window_to_lstm_features",
    "windows_to_lstm_array",
]

try:  # numpy tùy chọn: window_to_lstm_features trả list thuần nếu thiếu.
    import numpy as _np
except ImportError:  # pragma: no cover - .venv iot_backend luôn có numpy
    _np = None  # type: ignore[assignment]

# Fallback window LSTM khi không truyền config.
_LSTM_WINDOW_SIZE_FALLBACK = 10
LSTM_FEATURE_NAMES: list[str] = ["distance_m", "delta_distance_m"]


def _lstm_block(config: Mapping[str, Any] | None) -> Any:
    """Lấy khối ``lstm`` từ config; block có mặt nhưng không phải Mapping
    (ví dụ string/number/null rõ ràng sai kiểu YAML) → ValueError, không
    fallback âm thầm. Config rỗng/None → None.
    """
    if not config:
        return None
    lstm_block = config.get("lstm")
    if lstm_block is None:
        if "lstm" in config:
            raise ValueError("config 'lstm' không được null")
        return None
    if not isinstance(lstm_block, Mapping):
        raise ValueError(
            f"config 'lstm' phải là mapping, nhận {type(lstm_block).__name__}"
        )
    return lstm_block


def _lstm_window_size(config: Mapping[str, Any] | None) -> int:
    """Window size phải là integer dương; khối LSTM và shared phải khớp."""
    lstm = _lstm_block(config)
    def validate(value, name):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} phải là số nguyên dương, nhận {value!r}")
        return value
    shared = validate(config["window_size"], "window_size") if config and "window_size" in config else _LSTM_WINDOW_SIZE_FALLBACK
    if lstm is not None and "window_size" in lstm:
        size = validate(lstm["window_size"], "lstm.window_size")
        if size != shared:
            raise ValueError(f"lstm.window_size ({size}) lệch window_size ({shared})")
        return size
    return shared


def lstm_feature_names(config: Mapping[str, Any] | None = None) -> list[str]:
    """Tên feature LSTM mỗi timestep theo thứ tự chuẩn.

    Contract: cột tensor do ``window_to_lstm_features`` sinh luôn là
    ``[distance_m, delta_distance_m]`` theo đúng thứ tự đó. Nếu config khai
    ``lstm.features`` thì phải khớp CHÍNH XÁC tên và thứ tự
    ``LSTM_FEATURE_NAMES`` — lệch, thiếu, swapped hoặc sai số phần tử →
    ValueError (nếu không, manifest M11 có thể mô tả sai semantics tensor).
    Không khai (hoặc block thiếu key) → fallback ``LSTM_FEATURE_NAMES``.
    """
    if not config:
        return list(LSTM_FEATURE_NAMES)
    lstm_block = _lstm_block(config)
    from_config = lstm_block.get("features") if lstm_block else None
    if from_config is None:
        return list(LSTM_FEATURE_NAMES)
    if isinstance(from_config, str) or not isinstance(from_config, Sequence):
        raise ValueError(
            "config 'lstm.features' phải là danh sách tên, nhận "
            f"{type(from_config).__name__}"
        )
    names = [str(name) for name in from_config]
    if list(names) != list(LSTM_FEATURE_NAMES):
        raise ValueError(
            f"config 'lstm.features' {names} không khớp contract "
            f"{list(LSTM_FEATURE_NAMES)} — tên và thứ tự phải đúng "
            "[distance_m, delta_distance_m] vì tensor sinh cố định cột này"
        )
    return list(names)


def window_to_lstm_features(
    window: Window, config: Mapping[str, Any] | None = None
) -> list[list[float]]:
    """Timestep features LSTM của một window — trả list thuần ``(window_size, 2)``.

    Mỗi dòng là ``[distance_m, delta_distance_m]`` (đơn vị mét), theo thứ tự
    thời gian cũ → mới. ``delta[0] = 0`` cố định (không có mẫu trước đó);
    ``delta[i] = distance[i] - distance[i-1]`` cho ``i >= 1``. Input không bị
    thay đổi (bản sao nội bộ). Target không nằm ở đây — vẫn lấy
    ``window.targets`` (5 khoảng cách mét theo horizons_ms).

    ``config`` (tùy chọn): dict YAML nạp từ configs/model.yaml — dùng để lấy
    ``window_size``/``lstm.window_size`` thay vì hardcode 10. Sai độ dài hoặc
    khoảng cách không hữu hạn (NaN/±inf) → ValueError (không ép về 0, không
    loại âm thầm — boundary train/runtime chung, dữ liệu bẩn phải lộ ở đây).
    """
    lstm_feature_names(config)  # chặn tên/ordering drift ngay tại producer
    distances = list(window.features)  # bản sao — không mutate input
    window_size = _lstm_window_size(config)
    if len(distances) != window_size:
        raise ValueError(
            f"window có {len(distances)} features, kỳ vọng window_size "
            f"{window_size}"
        )
    result = [
        [distances[i], 0.0 if i == 0 else distances[i] - distances[i - 1]]
        for i in range(len(distances))
    ]
    for i, (d, delta) in enumerate(result):
        if not (math.isfinite(d) and math.isfinite(delta)):
            raise ValueError(
                f"window chứa giá trị không hữu hạn tại timestep {i} "
                f"({d}, {delta}) — từ chối, không ép giá trị"
            )
    return result


def windows_to_lstm_array(
    windows: Sequence[Window], config: Mapping[str, Any] | None = None
):
    """Batch tensor LSTM ``(N, window_size, 2)`` từ danh sách window.

    Mỗi window chuyển qua ``window_to_lstm_features`` (cùng contract:
    delta[0]=0, đơn vị mét, cũ → mới); thứ tự window giữ nguyên input.
    Trả ``numpy.ndarray`` (dtype float64); nếu numpy không có, trả nested
    list ``(N, window_size, 2)``. Danh sách rỗng → shape ``(0, W, 2)``.
    Sai độ dài window → ValueError (từ hàm window đơn).
    """
    batch = [window_to_lstm_features(w, config) for w in windows]
    window_size = _lstm_window_size(config)
    if _np is not None:
        return _np.asarray(batch, dtype="float64").reshape(
            len(batch), window_size, 2
        )
    return batch
