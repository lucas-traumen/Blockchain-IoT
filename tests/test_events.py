"""Kiểm thử M5d1: logic sự kiện + cảnh báo cho đánh giá model (events.py).

Dữ liệu dựng tay, không cần model/DB. Số liệu mong đợi tính bằng tay theo
định nghĩa chốt trong docstring của ``ai_model/events.py`` (nguồn spec
PLAN.md mục 4). Thời gian chọn bội của 0.25/0.5 s để so sánh biên chính
xác trong số học float.
"""

import pytest

from ai_model.events import (
    Episode,
    Event,
    event_metrics,
    find_events,
    find_warning_points,
    merge_warning_points,
)

SP = 0.44


def _times(n: int, dt: float = 0.1) -> list[float]:
    """Thời điểm đều nhịp dt bắt đầu từ 0."""
    return [i * dt for i in range(n)]


# ---------------------------------------------------------------- find_events


def test_find_events_crossing_down_once():
    """Một crossing từ trên xuống: [0.5, 0.45, 0.43, 0.44, 0.45], sp=0.44."""
    distances = [0.5, 0.45, 0.43, 0.44, 0.45]
    events = find_events(distances, _times(len(distances)), SP)
    assert len(events) == 1
    assert events[0].index == 2
    assert events[0].time_s == pytest.approx(0.2)


def test_find_events_flat_below_threshold_no_new_event():
    """Đi ngang dưới ngưỡng không thêm sự kiện — chỉ crossing đầu."""
    distances = [0.5, 0.43, 0.42, 0.43, 0.42, 0.43]
    events = find_events(distances, _times(len(distances)), SP)
    assert [e.index for e in events] == [1]


def test_find_events_equal_sp_counts_as_touch_then_second_crossing():
    """Bằng Sp tính là chạm (<= Sp); chạm đúng Sp lúc xuống cũng là sự kiện."""
    distances = [0.5, 0.44, 0.44, 0.45, 0.44]
    events = find_events(distances, _times(len(distances)), SP)
    assert [e.index for e in events] == [1, 4]


def test_find_events_down_up_down_two_events():
    """Xuống, lên trên rồi xuống lại → 2 sự kiện."""
    distances = [0.5, 0.43, 0.45, 0.43]
    events = find_events(distances, _times(len(distances)), SP)
    assert [e.index for e in events] == [1, 3]


def test_find_events_below_from_start_no_events():
    """Dưới ngưỡng từ đầu → không có sự kiện nào."""
    distances = [0.4, 0.43, 0.3, 0.44]
    assert find_events(distances, _times(len(distances)), SP) == []


def test_find_events_empty_and_single_sample():
    """Danh sách rỗng hoặc 1 mẫu → không sự kiện (i=0 không bao giờ là event)."""
    assert find_events([], [], SP) == []
    assert find_events([0.5], [0.0], SP) == []


def test_find_events_length_mismatch_raises():
    """distances/times lệch độ dài → ValueError."""
    with pytest.raises(ValueError):
        find_events([0.5, 0.4], [0.0], SP)


# -------------------------------------------------------- find_warning_points


def test_find_warning_points_clear_and_forecast_touch():
    """CLEAR + dự báo [0..horizon_index] chạm ngưỡng → cảnh báo; ngược lại không."""
    current = [0.50, 0.40, 0.50, 0.60, 0.44]
    predictions = [
        [0.45, 0.43, 0.46, 0.44],  # min 3 dự báo đầu = 0.43 <= sp → cảnh báo
        [0.43, 0.42, 0.41, 0.40],  # current <= sp (VIOLATION) → không cảnh báo
        [0.46, 0.45, 0.47, 0.42],  # chỉ chạm ở horizon 3 > horizon_index → không
        [0.44, 0.44, 0.44, 0.44],  # chạm đúng biên (<= sp) → cảnh báo
        [0.40, 0.42, 0.41, 0.43],  # current == sp (không > sp) → không cảnh báo
    ]
    assert find_warning_points(predictions, current, SP, horizon_index=2) == [0, 3]


def test_find_warning_points_horizon_zero_only_first_forecast():
    """horizon_index=0 chỉ xét dự báo đầu; tăng horizon_index mở rộng cửa sổ xét."""
    predictions = [[0.45, 0.40], [0.43, 0.30]]
    current = [0.50, 0.50]
    # horizon 0: cửa sổ 0 không chạm (0.45 > sp), cửa sổ 1 chạm (0.43 <= sp)
    assert find_warning_points(predictions, current, SP, horizon_index=0) == [1]
    # horizon 1: cả hai đều có dự báo chạm
    assert find_warning_points(predictions, current, SP, horizon_index=1) == [0, 1]


def test_find_warning_points_no_windows():
    """Không có cửa sổ nào → rỗng, không raise."""
    assert find_warning_points([], [], SP, horizon_index=2) == []


def test_find_warning_points_length_mismatch_raises():
    """predictions/current lệch độ dài → ValueError."""
    with pytest.raises(ValueError):
        find_warning_points([[0.4]], [0.5, 0.5], SP, horizon_index=0)


def test_find_warning_points_horizon_out_of_row_raises():
    """horizon_index vượt số dự báo của một hàng → ValueError (không fallback)."""
    with pytest.raises(ValueError):
        find_warning_points([[0.45]], [0.5], SP, horizon_index=3)


# ------------------------------------------------------- merge_warning_points


def test_merge_consecutive_points_one_episode():
    """[3, 4, 5] liên tiếp → 1 Episode từ 3 đến 5."""
    episodes = merge_warning_points([3, 4, 5], 0.1, _times(8))
    assert len(episodes) == 1
    ep = episodes[0]
    assert (ep.start_index, ep.end_index) == (3, 5)
    assert ep.start_time_s == pytest.approx(0.3)
    assert ep.end_time_s == pytest.approx(0.5)


def test_merge_gap_breaks_episode():
    """[3, 4, 6, 7] ngắt ở 5 → 2 Episode."""
    episodes = merge_warning_points([3, 4, 6, 7], 0.1, _times(8))
    assert [(e.start_index, e.end_index) for e in episodes] == [(3, 4), (6, 7)]
    assert episodes[0].end_time_s == pytest.approx(0.4)
    assert episodes[1].start_time_s == pytest.approx(0.6)


def test_merge_empty_and_single_point():
    """[] → []; điểm đơn → Episode điểm (start == end)."""
    assert merge_warning_points([], 0.1, []) == []
    episodes = merge_warning_points([2], 0.1, _times(5))
    assert [(e.start_index, e.end_index) for e in episodes] == [(2, 2)]


def test_merge_rejects_bad_input():
    """points không tăng dần hoặc delta_s <= 0 → ValueError."""
    with pytest.raises(ValueError):
        merge_warning_points([4, 3], 0.1, _times(6))
    with pytest.raises(ValueError):
        merge_warning_points([1, 2], 0.0, _times(4))


# ---------------------------------------------------------------- event_metrics


def test_event_metrics_hand_built_two_episodes_three_events():
    """2 episodes (1 TP) + 3 events (2 match, 1 ngoài): precision 0.5, recall 2/3.

    Episode A [0.25, 0.75], horizon 0.5 → cửa sổ match [0.25, 1.25]: chứa
    event 0.5 và 1.0 → TP, event đầu tiên 0.5 → lead 0.25. Episode B
    [1.25, 1.5], cửa sổ [1.25, 2.0]: không chứa event nào → FA. Event 3.0
    ngoài mọi cửa sổ.
    """
    times = [0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5]
    episodes = [
        Episode(1, 3, times[1], times[3]),
        Episode(5, 6, times[5], times[6]),
    ]
    events = [Event(2, 0.5), Event(4, 1.0), Event(12, 3.0)]
    m = event_metrics(episodes, events, horizon_s=0.5)
    assert m["n_events"] == 3
    assert m["n_episodes"] == 2
    assert m["n_tp_episodes"] == 1
    assert m["n_matched_events"] == 2
    assert m["precision"] == pytest.approx(0.5)
    assert m["recall"] == pytest.approx(2 / 3)
    assert m["lead_time_s"]["mean"] == pytest.approx(0.25)
    assert m["lead_time_s"]["min"] == pytest.approx(0.25)
    assert m["lead_time_s"]["max"] == pytest.approx(0.25)
    # span = (0.75−0.25) + (1.5−1.25) = 0.75 s; + horizon 0.5 s = 1.25 s
    # FA/min = 1 FA / (1.25/60 phút) = 48
    assert m["false_alarms_per_min"] == pytest.approx(48.0)


def test_event_metrics_event_in_two_episodes_counts_once():
    """Một event rơi vào 2 cửa sổ → 2 TP nhưng recall chỉ đếm event 1 lần."""
    episodes = [
        Episode(0, 1, 0.0, 0.25),  # cửa sổ [0.0, 0.75]
        Episode(2, 3, 0.5, 0.75),  # cửa sổ [0.5, 1.25]
    ]
    m = event_metrics(episodes, [Event(2, 0.6)], horizon_s=0.5)
    assert m["n_tp_episodes"] == 2
    assert m["n_matched_events"] == 1
    assert m["precision"] == pytest.approx(1.0)
    assert m["recall"] == pytest.approx(1.0)
    # lead: 0.6 − 0.0 = 0.6 và 0.6 − 0.5 = 0.1
    assert m["lead_time_s"]["min"] == pytest.approx(0.1)
    assert m["lead_time_s"]["max"] == pytest.approx(0.6)
    assert m["lead_time_s"]["mean"] == pytest.approx(0.35)


def test_event_metrics_boundary_inclusive():
    """Event đúng biên [start, end+horizon] là TP; event trước start không match."""
    times = [0.0, 0.25, 0.5, 0.75, 1.0, 1.25]
    ep = Episode(1, 3, times[1], times[3])  # cửa sổ match [0.25, 1.25]

    # Đúng biên dưới (== start): TP, lead 0.0
    m = event_metrics([ep], [Event(1, 0.25)], horizon_s=0.5)
    assert m["n_tp_episodes"] == 1
    assert m["lead_time_s"]["min"] == pytest.approx(0.0)

    # Đúng biên trên (== end + horizon = 1.25): TP, lead 1.0
    m = event_metrics([ep], [Event(5, 1.25)], horizon_s=0.5)
    assert m["n_tp_episodes"] == 1
    assert m["lead_time_s"]["max"] == pytest.approx(1.0)

    # Event trước start → không match: precision 0, recall 0 (có event nên xác định)
    m = event_metrics([ep], [Event(0, 0.0)], horizon_s=0.5)
    assert m["n_tp_episodes"] == 0
    assert m["n_matched_events"] == 0
    assert m["precision"] == pytest.approx(0.0)
    assert m["recall"] == pytest.approx(0.0)
    assert m["lead_time_s"] is None


def test_event_metrics_no_events_recall_none():
    """Không có event → recall None (không bịa), precision vẫn tính."""
    ep = Episode(0, 2, 0.0, 0.5)
    m = event_metrics([ep], [], horizon_s=0.5)
    assert m["n_events"] == 0
    assert m["n_episodes"] == 1
    assert m["n_tp_episodes"] == 0
    assert m["n_matched_events"] == 0
    assert m["precision"] == pytest.approx(0.0)
    assert m["recall"] is None
    assert m["lead_time_s"] is None
    # span 0.5 s + horizon 0.5 s = 1.0 s → 1 FA / (1/60 phút) = 60
    assert m["false_alarms_per_min"] == pytest.approx(60.0)


def test_event_metrics_empty_inputs():
    """Không episode, không event → precision/recall None, FA/min 0."""
    m = event_metrics([], [], horizon_s=0.5)
    assert m["n_events"] == 0
    assert m["n_episodes"] == 0
    assert m["n_tp_episodes"] == 0
    assert m["n_matched_events"] == 0
    assert m["precision"] is None
    assert m["recall"] is None
    assert m["false_alarms_per_min"] == 0.0
    assert m["lead_time_s"] is None


def test_event_metrics_negative_horizon_raises():
    """horizon_s âm → ValueError."""
    with pytest.raises(ValueError):
        event_metrics([], [], horizon_s=-0.1)
