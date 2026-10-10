"""Kiểm thử M12a: metric primitives regression + event metric mở rộng.

Bao phủ: ``ai_model/evaluate.py`` (``rmse_per_horizon``,
``regression_metrics``, ``regression_metrics_grouped``,
``event_metrics_grouped``) và ``ai_model/events.py``
(``event_metrics_extended``). Dữ liệu dựng tay, số liệu mong đợi tính
bằng tay; baseline kiểm tra công thức KHÔNG đổi so với M5c.
"""

import json
import math

import pytest

from ai_model.baselines import predict_constant_velocity, predict_persistence
from ai_model.events import Episode, Event, event_metrics, event_metrics_extended
from ai_model.evaluate import (
    event_metrics_grouped,
    mae_per_horizon,
    regression_metrics,
    regression_metrics_grouped,
    rmse_per_horizon,
)


# ------------------------------------------------------------- rmse/regression


def test_rmse_per_horizon_hand_check():
    """RMSE từng cột tính tay: [1,2] vs [0,0] → sqrt((1+4)/2)=sqrt(2.5);
    cột 2: [3,4] vs [1,1] → sqrt((4+9)/2)=sqrt(6.5)."""
    preds = [[1.0, 3.0], [2.0, 4.0]]
    targets = [[0.0, 1.0], [0.0, 1.0]]
    rmse = rmse_per_horizon(preds, targets)
    assert rmse == [pytest.approx(math.sqrt(2.5)), pytest.approx(math.sqrt(6.5))]


def test_rmse_per_horizon_matches_mae_on_constant_error():
    """Sai số đều |e| → RMSE = MAE = |e| theo mọi cột."""
    preds = [[0.7, 0.7], [0.3, 0.3]]
    targets = [[0.2, 0.2], [0.8, 0.8]]  # sai số +0.5 và −0.5 → |e| đều 0.5
    assert rmse_per_horizon(preds, targets) == pytest.approx(
        mae_per_horizon(preds, targets)
    )
    assert rmse_per_horizon(preds, targets) == pytest.approx([0.5, 0.5])


def test_regression_metrics_full_dict():
    """regression_metrics trả đủ keys, MAE/RMSE khớp tay, n_samples đúng."""
    preds = [[1.0, 3.0], [2.0, 4.0]]
    targets = [[0.0, 1.0], [0.0, 1.0]]
    m = regression_metrics(preds, targets)
    assert set(m) == {
        "mae_per_horizon",
        "rmse_per_horizon",
        "mae_mean",
        "rmse_mean",
        "n_samples",
    }
    assert m["mae_per_horizon"] == pytest.approx([1.5, 2.5])
    assert m["rmse_per_horizon"] == pytest.approx(
        [math.sqrt(2.5), math.sqrt(6.5)]
    )
    assert m["mae_mean"] == pytest.approx(2.0)
    assert m["rmse_mean"] == pytest.approx((math.sqrt(2.5) + math.sqrt(6.5)) / 2)
    assert m["n_samples"] == 2


@pytest.mark.parametrize(
    "preds,targets",
    [
        # lệch số hàng
        ([[1.0]], [[1.0], [2.0]]),
        # rỗng
        ([], []),
        # hàng lệch số horizon
        ([[1.0, 2.0]], [[1.0]]),
        # NaN / inf trong predictions và targets
        ([[float("nan")]], [[1.0]]),
        ([[float("inf")]], [[1.0]]),
        ([[1.0]], [[float("-inf")]]),
        ([[1.0]], [[float("nan")]]),
    ],
)
def test_regression_guards_reject_bad_input(preds, targets):
    """Shape sai / rỗng / NaN / inf → ValueError cho cả 3 hàm."""
    with pytest.raises(ValueError):
        mae_per_horizon(preds, targets)
    with pytest.raises(ValueError):
        rmse_per_horizon(preds, targets)
    with pytest.raises(ValueError):
        regression_metrics(preds, targets)


def test_regression_metrics_grouped_isolation_and_reject():
    """Metric theo group tính riêng; group rỗng → ValueError (không drop)."""
    grouped = {
        "approach": ([[1.0], [2.0]], [[0.0], [0.0]]),
        "retreat": ([[0.5]], [[0.0]]),
    }
    out = regression_metrics_grouped(grouped)
    assert set(out) == {"approach", "retreat"}
    assert out["approach"]["mae_per_horizon"] == pytest.approx([1.5])
    assert out["approach"]["n_samples"] == 2
    assert out["retreat"]["mae_per_horizon"] == pytest.approx([0.5])
    assert out["retreat"]["n_samples"] == 1

    with pytest.raises(ValueError):
        regression_metrics_grouped({"empty": ([], [])})
    with pytest.raises(ValueError):
        regression_metrics_grouped({})


# ------------------------------------------------- event_metrics_extended


def test_event_metrics_extended_f1_and_rates():
    """2 episodes (1 TP) + 3 events (2 match): precision 0.5, recall 2/3,
    F1 = 2·0.5·(2/3)/(0.5+2/3) = 4/7; FWR 0.5; miss 1/3; lead ms = ×1000."""
    episodes = [
        Episode(1, 3, 0.25, 0.75),
        Episode(5, 6, 1.25, 1.5),
    ]
    events = [Event(2, 0.5), Event(4, 1.0), Event(12, 3.0)]
    m = event_metrics_extended(episodes, events, horizon_s=0.5)
    assert m["precision"] == pytest.approx(0.5)
    assert m["recall"] == pytest.approx(2 / 3)
    assert m["f1"] == pytest.approx(2 * 0.5 * (2 / 3) / (0.5 + 2 / 3))
    assert m["false_warning_rate"] == pytest.approx(0.5)
    assert m["miss_rate"] == pytest.approx(1 / 3)
    assert m["lead_time_s"]["mean"] == pytest.approx(0.25)
    assert m["lead_time_ms"]["mean"] == pytest.approx(250.0)
    assert m["lead_time_ms"]["min"] == pytest.approx(250.0)
    assert m["lead_time_ms"]["max"] == pytest.approx(250.0)
    assert m["undefined_reason"] is None
    # FA/min giữ semantics cũ: 1 FA / (1.25/60 phút) = 48
    assert m["false_alarms_per_min"] == pytest.approx(48.0)
    # Đếm khớp event_metrics cũ
    old = event_metrics(episodes, events, horizon_s=0.5)
    for key in (
        "n_events",
        "n_episodes",
        "n_tp_episodes",
        "n_matched_events",
        "precision",
        "recall",
        "false_alarms_per_min",
        "lead_time_s",
    ):
        assert m[key] == old[key]


def test_event_metrics_extended_no_tp_with_events_all_zero_f1():
    """Có episode nhưng không TP và có event: precision=recall=f1=0."""
    episodes = [Episode(0, 1, 0.0, 0.25)]
    events = [Event(10, 2.0)]
    m = event_metrics_extended(episodes, events, horizon_s=0.5)
    assert m["precision"] == 0.0
    assert m["recall"] == 0.0
    assert m["f1"] == 0.0
    assert m["false_warning_rate"] == 1.0
    assert m["miss_rate"] == 1.0
    assert m["lead_time_s"] is None
    assert m["lead_time_ms"] is None


def test_event_metrics_extended_no_events_null_semantics():
    """Không có event → recall/miss_rate/f1 None; precision vẫn tính."""
    m = event_metrics_extended([Episode(0, 2, 0.0, 0.5)], [], horizon_s=0.5)
    assert m["recall"] is None
    assert m["miss_rate"] is None
    assert m["f1"] is None
    assert m["precision"] == pytest.approx(0.0)
    assert m["false_warning_rate"] == pytest.approx(1.0)
    assert m["lead_time_s"] is None
    assert m["lead_time_ms"] is None


def test_event_metrics_extended_no_episodes_zero_f1_with_events():
    """Có event nhưng model không phát episode: precision None, F1=0."""
    m = event_metrics_extended([], [Event(1, 0.5)], horizon_s=0.5)
    assert m["precision"] is None
    assert m["false_warning_rate"] is None
    assert m["f1"] == pytest.approx(0.0)
    assert m["recall"] == pytest.approx(0.0)
    assert m["miss_rate"] == pytest.approx(1.0)
    assert m["false_alarms_per_min"] == 0.0
    assert m["undefined_reason"] is None


def test_event_metrics_extended_empty_inputs_all_none():
    """Không episode, không event → mọi metric undefined None."""
    m = event_metrics_extended([], [], horizon_s=0.5)
    assert m["n_events"] == 0 and m["n_episodes"] == 0
    assert m["precision"] is None
    assert m["recall"] is None
    assert m["f1"] is None
    assert m["false_warning_rate"] is None
    assert m["miss_rate"] is None
    assert m["false_alarms_per_min"] == 0.0
    assert m["lead_time_s"] is None and m["lead_time_ms"] is None


def test_event_metrics_extended_zero_span_fa_returns_none_not_inf():
    """span=0 với FA>0 → FA/min None + undefined_reason; KHÔNG inf."""
    ep = Episode(3, 3, 0.5, 0.5)  # Episode điểm
    m = event_metrics_extended([ep], [], horizon_s=0.0)
    assert m["n_tp_episodes"] == 0
    assert m["false_alarms_per_min"] is None
    assert m["undefined_reason"] is not None
    assert "span" in m["undefined_reason"]


def test_event_metrics_extended_output_json_finite():
    """Mọi số trong output đều finite float hoặc None — JSON an toàn."""
    episodes = [Episode(1, 3, 0.25, 0.75)]
    events = [Event(2, 0.5), Event(4, 1.0)]
    m = event_metrics_extended(episodes, events, horizon_s=0.5)
    for value in m.values():
        if value is None:
            continue
        if isinstance(value, dict):
            for sub in value.values():
                assert math.isfinite(sub)
        else:
            assert math.isfinite(value)


def test_event_metrics_extended_negative_horizon_raises():
    with pytest.raises(ValueError):
        event_metrics_extended([], [], horizon_s=-0.1)


# --------------------------------------------------- event_metrics_grouped


def test_event_metrics_grouped_isolation_and_null_recall():
    """Match chỉ chạy phạm vi group: event group A không cứu episode group
    B; group không event → recall None (không đổi thành 0)."""
    grouped = {
        "s1": ([Episode(0, 1, 0.0, 0.25)], [Event(2, 0.5)]),
        "s2": ([Episode(0, 1, 0.0, 0.25)], []),  # cùng thời điểm, khác group
    }
    out = event_metrics_grouped(grouped, horizon_s=0.5)
    assert set(out) == {"s1", "s2"}
    # s1: event 0.5 trong [0.0, 0.75] → TP; recall = 1
    assert out["s1"]["n_tp_episodes"] == 1
    assert out["s1"]["recall"] == pytest.approx(1.0)
    # s2: episode giống hệt nhưng event thuộc group s1 → không match
    assert out["s2"]["n_tp_episodes"] == 0
    assert out["s2"]["n_events"] == 0
    assert out["s2"]["recall"] is None
    assert out["s2"]["miss_rate"] is None
    assert out["s2"]["precision"] == pytest.approx(0.0)

    with pytest.raises(ValueError):
        event_metrics_grouped({}, horizon_s=0.5)


def test_event_metrics_grouped_negative_horizon_raises():
    with pytest.raises(ValueError):
        event_metrics_grouped({"g": ([], [])}, horizon_s=-1.0)


# ------------------------------------------------- baseline formulas (M5c)


def test_baseline_formulas_unchanged():
    """Công thức baseline giữ nguyên so với M5c: persistence lặp d(t);
    constant-velocity ngoại suy d(t) + v·h với v = (d(t)−d0)/((N−1)·Δ)."""
    features = [1.0, 1.1, 1.2, 1.3, 1.4]
    assert predict_persistence(features, n_horizons=3) == [1.4, 1.4, 1.4]
    delta_s = 0.1
    velocity = (1.4 - 1.0) / ((len(features) - 1) * delta_s)  # = 1.0 m/s
    assert predict_constant_velocity(
        features, delta_s, [100, 200, 300, 400, 500]
    ) == pytest.approx([1.5, 1.6, 1.7, 1.8, 1.9])


# --------------------------------------------- M12a gap: legacy inf pinning


def test_legacy_event_metrics_zero_span_fa_returns_inf():
    """Legacy event_metrics keeps its zero-span infinity behavior."""
    ep = Episode(3, 3, 0.5, 0.5)  # Episode điểm → span 0
    m = event_metrics([ep], [Event(10, 2.0)], horizon_s=0.0)
    assert m["false_alarms_per_min"] == float("inf")
    assert math.isinf(m["false_alarms_per_min"])


def test_event_metrics_extended_zero_span_json_safe():
    """Gap M12a: ``event_metrics_extended`` zero-span FA → JSON-safe qua
    ``allow_nan=False``; FA/min None + undefined_reason giải thích."""
    ep = Episode(3, 3, 0.5, 0.5)
    m = event_metrics_extended([ep], [Event(10, 2.0)], horizon_s=0.0)
    json.dumps(m, allow_nan=False)  # không raise
    assert m["false_alarms_per_min"] is None
    assert m["undefined_reason"] is not None
    assert "span" in m["undefined_reason"]


def test_event_metrics_grouped_empty_group_not_dropped():
    """Gap M12a: group ``([], [])`` (rỗng hoàn toàn) vẫn xuất trong output
    với recall/miss_rate None — không silently drop group."""
    out = event_metrics_grouped(
        {"a": ([Episode(0, 1, 0.0, 0.25)], [Event(0.1, 0.2)]), "b": ([], [])},
        horizon_s=0.5,
    )
    assert set(out) == {"a", "b"}
    assert out["b"]["n_events"] == 0
    assert out["b"]["n_episodes"] == 0
    assert out["b"]["recall"] is None
    assert out["b"]["miss_rate"] is None
    assert out["b"]["f1"] is None
    json.dumps(out, allow_nan=False)
