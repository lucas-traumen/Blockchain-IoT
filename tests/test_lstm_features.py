"""Kiểm thử M9: representation LSTM từ Window — dùng chung train (M11) + runtime (M13).

Contract kiểm tra (PLAN mục 4 + PROJECT_MEMORY):

- mỗi timestep ``[distance_m, delta_distance_m]`` (mét);
- shape một window ``(10, 2)``, batch ``(N, 10, 2)``;
- ``delta[0] = 0``, ``delta[i] = distance[i] - distance[i-1]`` theo thứ tự
  cũ → mới;
- input không bị mutate;
- sai shape/size bị từ chối rõ ràng (ValueError);
- ``window_size``/``lstm.features`` đọc từ configs/model.yaml, không hardcode.

RF legacy (``window_to_features``, d0..d9) đã test trong tests/test_windows.py.
"""

from pathlib import Path

import numpy as np
import pytest
import yaml

from ai_model.features import (
    FEATURE_NAMES,
    LSTM_FEATURE_NAMES,
    lstm_feature_names,
    window_to_features,
    window_to_lstm_features,
    windows_to_lstm_array,
)
from ai_model.windows import Window, WindowConfig, extract_windows

REPO_ROOT = Path(__file__).resolve().parents[1]

# Khớp khối windowing dùng chung của configs/model.yaml.
_MODEL_CONFIG_DICT = {
    "window_size": 10,
    "delta_s": 0.1,
    "horizons_ms": [100, 200, 300, 400, 500],
    "max_time_jitter_s": 0.03,
    "max_gap_s": 0.25,
}


@pytest.fixture
def config() -> WindowConfig:
    return WindowConfig.from_dict(_MODEL_CONFIG_DICT)


def _chain_window(n: int = 15, step_mm: float = 1.0) -> Window:
    """Window chuẩn từ chuỗi `n` mẫu, distance tăng `step_mm` mỗi mẫu (Δ=0.1s)."""
    rows = [
        {
            "device_id": "esp32_01",
            "boot_id": "b1",
            "session_id": "s1",
            "sequence": i + 1,
            "monotonic_us": 10_000_000 + i * 100_000,
            "measured_at": None,
            "distance_mm": 1000 + i * step_mm,
            "valid": True,
        }
        for i in range(n)
    ]
    windows, stats = extract_windows(rows, WindowConfig.from_dict(_MODEL_CONFIG_DICT))
    assert stats.n_windows == 1
    return windows[0]


# --------------------------------------------------------- window (10, 2)


def test_lstm_features_shape_and_delta(config):
    """Shape (10, 2); delta[0]=0; delta[i] = d[i]-d[i-1] (dãy tăng 1mm/mẫu)."""
    w = _chain_window()
    ts = window_to_lstm_features(w, _MODEL_CONFIG_DICT)
    assert len(ts) == 10
    assert all(len(t) == 2 for t in ts)
    distances = w.features
    assert ts[0] == pytest.approx([distances[0], 0.0])
    for i in range(1, 10):
        assert ts[i] == pytest.approx(
            [distances[i], distances[i] - distances[i - 1]]
        )
    # 1 mm/mẫu @ 0.1 s → delta không đổi 0.001 m.
    assert all(t[1] == pytest.approx(0.001) for t in ts[1:])


def test_lstm_features_flat_increasing_decreasing(config):
    """Dãy phẳng → delta toàn 0; giảm dần → delta âm đúng hiệu."""
    flat = _chain_window(step_mm=0.0)
    ts_flat = window_to_lstm_features(flat, _MODEL_CONFIG_DICT)
    assert all(t[1] == 0.0 for t in ts_flat)
    # delta[0]=0 quy ước, không phải hiệu thật của dãy phẳng.

    dec = _chain_window(step_mm=-2.0)  # giảm 2mm/mẫu
    ts_dec = window_to_lstm_features(dec, _MODEL_CONFIG_DICT)
    assert ts_dec[0][1] == 0.0
    assert all(t[1] == pytest.approx(-0.002) for t in ts_dec[1:])


def test_lstm_features_does_not_mutate_input(config):
    """window.features không bị thay đổi sau khi chuyển sang LSTM features."""
    w = _chain_window()
    before = list(w.features)
    window_to_lstm_features(w, _MODEL_CONFIG_DICT)
    assert w.features == before


def test_lstm_features_without_config(config):
    """Không truyền config → fallback window_size = len(FEATURE_NAMES) (10)."""
    ts = window_to_lstm_features(_chain_window())
    assert len(ts) == 10
    assert ts[0][1] == 0.0


def test_lstm_features_rejects_wrong_window_size():
    """Window sai độ dài → ValueError rõ ràng."""
    bad = Window(features=[0.5] * 9, targets=[0.5] * 5, meta=None)
    with pytest.raises(ValueError, match="window_size"):
        window_to_lstm_features(bad, _MODEL_CONFIG_DICT)
    with pytest.raises(ValueError, match="window_size"):
        window_to_lstm_features(bad)  # fallback cũng là 10


# ------------------------------------------------------------------ batch


def test_batch_array_shape_and_values(config):
    """Batch (N, 10, 2); thứ tự window giữ nguyên; dtype float; numpy.ndarray."""
    w1 = _chain_window()
    w2 = _chain_window(step_mm=2.0)
    arr = windows_to_lstm_array([w1, w2], _MODEL_CONFIG_DICT)
    assert isinstance(arr, np.ndarray)
    assert arr.shape == (2, 10, 2)
    assert arr.dtype == np.float64
    expected = np.asarray(
        window_to_lstm_features(w1, _MODEL_CONFIG_DICT), dtype="float64"
    )
    assert arr[0] == pytest.approx(expected)
    expected2 = np.asarray(
        window_to_lstm_features(w2, _MODEL_CONFIG_DICT), dtype="float64"
    )
    assert arr[1] == pytest.approx(expected2)
    # Batch không mutate window input.
    assert w1.features == list(arr[0][:, 0])


def test_batch_empty(config):
    arr = windows_to_lstm_array([], _MODEL_CONFIG_DICT)
    assert isinstance(arr, np.ndarray)
    assert arr.shape == (0, 10, 2)


def test_batch_rejects_mixed_window_sizes(config):
    """Một window sai size trong batch → ValueError (không im lặng cắt bớt)."""
    bad = Window(features=[0.5] * 11, targets=[0.5] * 5, meta=None)
    with pytest.raises(ValueError, match="window_size"):
        windows_to_lstm_array([_chain_window(), bad], _MODEL_CONFIG_DICT)


# ------------------------------------------------------- names / config


def test_lstm_feature_names_contract():
    assert LSTM_FEATURE_NAMES == ["distance_m", "delta_distance_m"]
    assert lstm_feature_names() == LSTM_FEATURE_NAMES
    assert lstm_feature_names(None) == LSTM_FEATURE_NAMES
    # Khai đúng contract → chấp nhận; lệch/swapped → ValueError (M-1).
    assert lstm_feature_names(
        {"lstm": {"features": ["distance_m", "delta_distance_m"]}}
    ) == LSTM_FEATURE_NAMES
    with pytest.raises(ValueError, match="lstm.features"):
        lstm_feature_names({"lstm": {"features": ["a", "b"]}})
    with pytest.raises(ValueError, match="lstm.features"):
        lstm_feature_names(
            {"lstm": {"features": ["delta_distance_m", "distance_m"]}}
        )


def test_lstm_feature_names_malformed_config():
    """Block sai kiểu / sai kiểu danh sách → ValueError, không fallback âm thầm."""
    with pytest.raises(ValueError, match="lstm"):
        lstm_feature_names({"lstm": "khong_phai_mapping"})
    with pytest.raises(ValueError, match="lstm"):
        window_to_lstm_features(_chain_window(), {"lstm": 42})
    with pytest.raises(ValueError, match="lstm.features"):
        lstm_feature_names({"lstm": {"features": "distance_m"}})


def test_lstm_names_match_project_yaml():
    """lstm.features trong configs/model.yaml thật khớp fallback (2 tên)."""
    raw = yaml.safe_load(
        (REPO_ROOT / "configs" / "model.yaml").read_text(encoding="utf-8")
    )
    assert lstm_feature_names(raw) == LSTM_FEATURE_NAMES
    assert _lstm_window_size_of(raw) == 10


def _lstm_window_size_of(config):
    from ai_model.features import _lstm_window_size

    return _lstm_window_size(config)


def test_config_window_size_mismatch_rejected():
    """lstm.window_size lệch window_size dùng chung → ValueError."""
    bad = dict(_MODEL_CONFIG_DICT, lstm={"window_size": 12})
    with pytest.raises(ValueError, match="lstm.window_size"):
        window_to_lstm_features(_chain_window(), bad)


def test_lstm_window_size_null_rejected():
    """lstm.window_size: null → ValueError rõ, không TypeError."""
    bad = dict(_MODEL_CONFIG_DICT, lstm={"window_size": None})
    with pytest.raises(ValueError, match="lstm.window_size"):
        window_to_lstm_features(_chain_window(), bad)


def test_non_finite_distance_rejected(config):
    """NaN/inf trong distance → ValueError; không ép về 0, không mutate."""
    w = _chain_window()
    bad_nan = Window(features=list(w.features), targets=list(w.targets), meta=w.meta)
    bad_nan.features[3] = float("nan")
    before = list(bad_nan.features)
    with pytest.raises(ValueError, match="không hữu hạn"):
        window_to_lstm_features(bad_nan, _MODEL_CONFIG_DICT)
    with pytest.raises(ValueError, match="không hữu hạn"):
        windows_to_lstm_array([bad_nan], _MODEL_CONFIG_DICT)
    assert bad_nan.features == before  # không bị sửa

    bad_inf = Window(features=list(w.features), targets=list(w.targets), meta=w.meta)
    bad_inf.features[0] = float("inf")
    with pytest.raises(ValueError, match="không hữu hạn"):
        window_to_lstm_features(bad_inf, _MODEL_CONFIG_DICT)

    # Delta vô hạn do overflow hiệu hai giá trị hữu hạn cũng bị chặn.
    bad_delta = Window(
        features=list(w.features), targets=list(w.targets), meta=w.meta
    )
    bad_delta.features[0] = 1.7e308
    bad_delta.features[1] = -1.7e308
    with pytest.raises(ValueError, match="không hữu hạn"):
        window_to_lstm_features(bad_delta, _MODEL_CONFIG_DICT)


# ----------------------------------------------------- RF không hồi quy


def test_rf_adapter_unchanged(config):
    """RF adapter: window_to_features vẫn vector phẳng 10 phần tử d0..d9."""
    w = _chain_window()
    feats = window_to_features(w)
    assert feats == w.features
    assert len(feats) == len(FEATURE_NAMES)
    assert FEATURE_NAMES == [f"d{i}" for i in range(10)]


def test_targets_untouched_by_lstm_features(config):
    """Target vẫn 5 khoảng cách mét theo horizons — không có delta trong target."""
    w = _chain_window()
    window_to_lstm_features(w, _MODEL_CONFIG_DICT)
    windows_to_lstm_array([w], _MODEL_CONFIG_DICT)
    assert len(w.targets) == 5
    # Chuỗi 15 mẫu (seq 1..15) tăng 1mm kể từ seq 1: target = mẫu 11..15.
    assert w.targets == pytest.approx([(1000 + i) / 1000 for i in range(10, 15)])


@pytest.mark.parametrize("block", [None,{"features":["delta_distance_m","distance_m"]}])
def test_feature_producer_rejects_misdeclared_semantics(block):
    with pytest.raises(ValueError):
        window_to_lstm_features(_chain_window(),dict(_MODEL_CONFIG_DICT,lstm=block))


@pytest.mark.parametrize("size", [0,True,10.5,"10"])
def test_feature_window_size_does_not_coerce(size):
    with pytest.raises(ValueError,match="window_size"):
        window_to_lstm_features(_chain_window(),dict(_MODEL_CONFIG_DICT,window_size=size))
