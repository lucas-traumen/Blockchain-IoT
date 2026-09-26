"""ai_model — windowing + features dùng chung cho train (M5c) và runtime inference (M6).

Package độc lập với backend: backend không được import ngược vào đây theo
PLAN.md mục 1; runtime M6 import ai_model (không đảo ngược). M5a chỉ chứa
windowing/features; prepare/split/train/evaluate thuộc M5c.
"""

from ai_model.features import FEATURE_NAMES, feature_names, window_to_features
from ai_model.windows import (
    Window,
    WindowConfig,
    WindowMeta,
    WindowStats,
    extract_windows,
)

__all__ = [
    "FEATURE_NAMES",
    "Window",
    "WindowConfig",
    "WindowMeta",
    "WindowStats",
    "extract_windows",
    "feature_names",
    "window_to_features",
]
