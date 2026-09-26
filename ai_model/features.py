"""Chuẩn hóa feature vector từ Window — contract dùng chung train + runtime.

Quy ước thứ tự (contract, không được đổi): ``d0`` là khoảng cách CŨ NHẤT
của cửa sổ (mẫu t-(N-1)Δ), ``d9`` là MỚI NHẤT (mẫu t). Đơn vị mét. Train
(M5c) và runtime inference (M6) phải dùng cùng module này để thứ tự
feature khớp model artifact (manifest.json: feature_names).
"""

from __future__ import annotations

from typing import Any, Mapping

from ai_model.windows import Window

__all__ = ["FEATURE_NAMES", "feature_names", "window_to_features"]

# Fallback cứng đúng thứ tự d0..d9 — chỉ dùng khi config không khai 'features'.
FEATURE_NAMES: list[str] = [
    "d0", "d1", "d2", "d3", "d4", "d5", "d6", "d7", "d8", "d9",
]


def feature_names(config: Mapping[str, Any] | None = None) -> list[str]:
    """Tên feature theo thứ tự chuẩn.

    Ưu tiên key ``features`` trong config (configs/model.yaml); thiếu hoặc
    rỗng → fallback ``FEATURE_NAMES``. Số phần tử từ config phải khớp
    fallback (10) — sai → ValueError.
    """
    from_config = (config or {}).get("features")
    if not from_config:
        return list(FEATURE_NAMES)
    names = [str(name) for name in from_config]
    if len(names) != len(FEATURE_NAMES):
        raise ValueError(
            f"config 'features' có {len(names)} phần tử, "
            f"kỳ vọng {len(FEATURE_NAMES)}"
        )
    return names


def window_to_features(window: Window) -> list[float]:
    """Feature vector của cửa sổ, đúng thứ tự d0 (cũ nhất) → d9 (mới nhất), mét.

    Contract dùng chung train (M5c) và runtime inference (M6): thứ tự phần
    tử trả về khớp ``FEATURE_NAMES``/``feature_names()`` và model artifact
    (manifest.json: feature_names). Không sắp lại, không thêm/bớt giá trị;
    sai độ dài → ValueError.
    """
    features = list(window.features)
    if len(features) != len(FEATURE_NAMES):
        raise ValueError(
            f"window có {len(features)} features, kỳ vọng {len(FEATURE_NAMES)}"
        )
    return features
