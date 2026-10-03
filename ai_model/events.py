"""Logic sự kiện + cảnh báo cho đánh giá model — thuần tính toán, không I/O.

Nguồn spec: PLAN.md mục 4 (M5). Định nghĩa chốt, dùng xuyên suốt pipeline
đánh giá:

- **Sự kiện (``Event``)**: một lần chạm ngưỡng — crossing từ trên xuống:
  ``d[i-1] > Sp`` và ``d[i] <= Sp``. Chỉ số 0 không bao giờ là sự kiện
  (thiếu mẫu trước để xác định hướng crossing).
- **Điểm cảnh báo**: tại cửa sổ kết thúc tại mẫu t_i (hiện tại
  ``current[i]``): cảnh báo iff đang CLEAR (``current[i] > Sp``) và ít
  nhất một dự báo từ horizon 0 đến horizon đã chọn chạm ngưỡng
  (``min(predictions[i][0..horizon_index]) <= Sp``).
- **Episode**: các điểm cảnh báo LIÊN TIẾP (chỉ số cách nhau đúng 1)
  gộp thành một đợt cảnh báo trước khi đếm; ngắt → Episode mới.
- **Một cảnh báo đúng iff có lần chạm trong chân trời dự báo đã chọn —
  không ghép với sự kiện xa hơn**: Episode là TP iff tồn tại Event với
  ``event.time_s ∈ [episode.start_time_s, episode.end_time_s + horizon_s]``
  (biên gồm cả hai đầu). Không có cơ chế ghép Episode với Event ngoài
  khoảng này.

Số liệu không xác định ghi ``None``, không bịa: không có Event → recall
``None`` (precision vẫn tính được); không có cặp Episode–Event match →
lead time ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import fmean
from typing import Any, Sequence

__all__ = [
    "Episode",
    "Event",
    "event_metrics",
    "event_metrics_extended",
    "find_events",
    "find_warning_points",
    "merge_warning_points",
]


@dataclass(frozen=True)
class Event:
    """Một lần chạm ngưỡng: crossing từ trên xuống tại mẫu ``index``."""

    index: int
    time_s: float


@dataclass(frozen=True)
class Episode:
    """Một đợt cảnh báo đã gộp từ các điểm cảnh báo liên tiếp."""

    start_index: int
    end_index: int
    start_time_s: float
    end_time_s: float


def find_events(
    distances: Sequence[float], times: Sequence[float], sp_m: float
) -> list[Event]:
    """Tìm mọi lần chạm ngưỡng: crossing từ trên xuống của ``distances``.

    Sự kiện tại i (i >= 1) iff ``distances[i-1] > sp_m`` và
    ``distances[i] <= sp_m``. Đi ngang dưới ngưỡng (kể cả bằng Sp) không
    sinh thêm sự kiện — phải lên trên ngưỡng rồi xuống lại mới đếm lần
    tiếp theo. i = 0 không bao giờ là sự kiện.

    ``times[i]`` là thời điểm của ``distances[i]``; hai dãy phải cùng độ
    dài (ValueError nếu lệch).
    """
    if len(distances) != len(times):
        raise ValueError(
            f"distances ({len(distances)}) và times ({len(times)}) "
            "phải cùng độ dài"
        )
    events: list[Event] = []
    for i in range(1, len(distances)):
        if distances[i - 1] > sp_m and distances[i] <= sp_m:
            events.append(Event(index=i, time_s=float(times[i])))
    return events


def find_warning_points(
    predictions: Sequence[Sequence[float]],
    current: Sequence[float],
    sp_m: float,
    horizon_index: int,
) -> list[int]:
    """Chỉ số các cửa sổ phát cảnh báo sớm.

    Cửa sổ i (kết thúc tại mẫu t_i, hiện tại ``current[i]``) được cảnh
    báo iff ``current[i] > sp_m`` (đang CLEAR; đang VIOLATION thì giữ
    nhãn, không cảnh báo) và ``min(predictions[i][: horizon_index + 1])
    <= sp_m`` — một dự báo từ horizon 0 đến horizon_index chạm ngưỡng;
    dự báo chỉ chạm ở horizon sau horizon_index không tính.

    ``predictions[i]`` là dự báo của cửa sổ i theo từng horizon (một giá
    trị mỗi horizon, cùng thứ tự horizons_ms). ``horizon_index`` phải
    >= 0 và hợp lệ trong mọi hàng của ``predictions``; ``predictions`` và
    ``current`` phải cùng độ dài — sai → ValueError (không fallback im
    lặng). Không có cửa sổ → [].
    """
    if len(predictions) != len(current):
        raise ValueError(
            f"predictions ({len(predictions)}) và current ({len(current)}) "
            "phải cùng độ dài"
        )
    if horizon_index < 0:
        raise ValueError(f"horizon_index phải >= 0, nhận {horizon_index}")
    for i, row in enumerate(predictions):
        if horizon_index >= len(row):
            raise ValueError(
                f"predictions[{i}] có {len(row)} dự báo, không đủ cho "
                f"horizon_index = {horizon_index}"
            )
    points: list[int] = []
    for i in range(len(current)):
        if current[i] > sp_m and min(predictions[i][: horizon_index + 1]) <= sp_m:
            points.append(i)
    return points


def merge_warning_points(
    points: Sequence[int], delta_s: float, times: Sequence[float]
) -> list[Episode]:
    """Gộp các điểm cảnh báo LIÊN TIẾP thành ``Episode``.

    Quy tắc gộp theo CHỈ SỐ mẫu: hai điểm cách nhau đúng 1 thuộc cùng
    Episode; ngắt (chênh ≥ 2) → Episode mới. ``delta_s`` (nhịp mẫu, giây)
    là tham số giao diện của pipeline đánh giá, không tham gia quy tắc
    gộp; phải > 0. ``points`` phải tăng dần (đầu ra của
    ``find_warning_points`` thỏa sẵn) — sai → ValueError. Rỗng → [].
    """
    if delta_s <= 0:
        raise ValueError(f"delta_s phải > 0, nhận {delta_s}")
    for prev, p in zip(points, points[1:]):
        if p <= prev:
            raise ValueError(f"points phải tăng dần, thấy {prev} trước {p}")
    episodes: list[Episode] = []
    run_start: int | None = None
    prev: int | None = None
    for p in points:
        if run_start is None:
            run_start = p
        elif p - prev != 1:
            episodes.append(_run_episode(run_start, prev, times))
            run_start = p
        prev = p
    if run_start is not None:
        episodes.append(_run_episode(run_start, prev, times))
    return episodes


def _run_episode(start: int, end: int, times: Sequence[float]) -> Episode:
    """Episode từ một run điểm cảnh báo [start..end] (start <= end)."""
    return Episode(
        start_index=start,
        end_index=end,
        start_time_s=float(times[start]),
        end_time_s=float(times[end]),
    )


def _match_episodes_events(
    episodes: Sequence[Episode], events: Sequence[Event], horizon_s: float
) -> tuple[int, set[Event], list[float]]:
    """Đếm TP/matched/lead time theo định nghĩa match của module.

    Trả ``(n_tp, matched, leads)``: số Episode TP; tập Event bị match
    (mỗi Event đếm 1 lần cho recall dù rơi vào nhiều Episode); lead time
    của mỗi Episode TP (Event ĐẦU TIÊN trong khoảng của nó). Hàm nội bộ
    dùng chung cho ``event_metrics`` và ``event_metrics_extended``.
    """
    ordered = sorted(events, key=lambda e: (e.time_s, e.index))
    n_tp = 0
    matched: set[Event] = set()
    leads: list[float] = []
    for episode in episodes:
        window_end = episode.end_time_s + horizon_s
        hits = [e for e in ordered if episode.start_time_s <= e.time_s <= window_end]
        if not hits:
            continue
        n_tp += 1
        matched.update(hits)
        leads.append(hits[0].time_s - episode.start_time_s)
    return n_tp, matched, leads


def event_metrics(
    episodes: Sequence[Episode], events: Sequence[Event], horizon_s: float
) -> dict[str, Any]:
    """Đối chiếu Episode với Event theo định nghĩa chốt của module.

    Match: Episode là TP iff tồn tại Event với ``event.time_s`` nằm trong
    ``[episode.start_time_s, episode.end_time_s + horizon_s]`` (biên gồm
    cả hai đầu) — không ghép với sự kiện xa hơn. Một Event rơi vào nhiều
    Episode vẫn đếm 1 lần cho recall; mỗi Episode TP ghép với Event ĐẦU
    TIÊN (sớm nhất) trong khoảng của nó để tính lead time
    ``event.time_s − episode.start_time_s`` (≥ 0).

    Trả dict:

    - ``n_events``, ``n_episodes``, ``n_tp_episodes``, ``n_matched_events``;
    - ``precision`` = tp / n_episodes (None nếu không có Episode);
    - ``recall`` = matched / n_events (None nếu không có Event);
    - ``false_alarms_per_min`` = (n_episodes − tp) chia (tổng độ dài mọi
      Episode + horizon_s, đổi ra phút); 0 nếu không có Episode hoặc
      không có cảnh báo sai. Span tổng = 0 với FA > 0 → ``inf`` (trường
      hợp lý thuyết: Episode điểm + horizon 0);
    - ``lead_time_s`` = dict mean/min/max của các cặp match; None nếu
      không có cặp nào.

    ``horizon_s`` phải >= 0 (ValueError nếu âm).
    """
    if horizon_s < 0:
        raise ValueError(f"horizon_s phải >= 0, nhận {horizon_s}")
    n_episodes = len(episodes)
    n_events = len(events)

    n_tp, matched, leads = _match_episodes_events(episodes, events, horizon_s)

    precision = n_tp / n_episodes if n_episodes else None
    recall = len(matched) / n_events if n_events else None

    n_fa = n_episodes - n_tp
    if n_fa == 0:
        false_alarms_per_min = 0.0
    else:
        span_s = sum(e.end_time_s - e.start_time_s for e in episodes) + horizon_s
        if span_s > 0:
            false_alarms_per_min = n_fa / (span_s / 60.0)
        else:
            false_alarms_per_min = float("inf")

    lead_time_s = (
        {"mean": fmean(leads), "min": min(leads), "max": max(leads)}
        if leads
        else None
    )

    return {
        "n_events": n_events,
        "n_episodes": n_episodes,
        "n_tp_episodes": n_tp,
        "n_matched_events": len(matched),
        "precision": precision,
        "recall": recall,
        "false_alarms_per_min": false_alarms_per_min,
        "lead_time_s": lead_time_s,
    }


def event_metrics_extended(
    episodes: Sequence[Episode], events: Sequence[Event], horizon_s: float
) -> dict[str, Any]:
    """Superset số liệu episode/event cho M12b — cùng định nghĩa match với
    ``event_metrics`` (xem docstring hàm đó), cộng thêm F1, false-warning
    rate, miss rate, lead time ms; KHÔNG để ``inf`` trong output.

    Quy ước (mọi giá trị None nghĩa là "không xác định — không bịa"):

    - ``precision`` = TP episodes / tổng episodes; không có Episode → None.
      Có Episode nhưng không TP → 0.0.
    - ``recall`` = matched unique events / tổng events; không có Event →
      None. Có Event nhưng không match → 0.0.
    - ``f1`` = harmonic mean 2·p·r/(p+r) khi cả precision và recall đều
      xác định và (p+r) > 0; thiếu một trong hai → None. Trường hợp có
      Episode nhưng không TP VÀ có Event: precision=0, recall=0, f1=0.
    - ``false_warning_rate`` = (episodes − TP) / tổng episodes; không có
      Episode → None.
    - ``miss_rate`` = 1 − recall khi có Event; không có Event → None.
    - ``false_alarms_per_min``: giữ semantics ``event_metrics`` (n_fa chia
      tổng span mọi Episode + horizon_s, đổi ra phút; 0.0 khi n_fa = 0)
      NHƯNG nếu span = 0 với n_fa > 0 → trả None (không ``inf``) và đặt
      ``undefined_reason`` giải thích; các lý do khác → undefined_reason
      là None.
    - ``lead_time_s`` / ``lead_time_ms``: dict mean/min/max của các cặp
      match (ms = s × 1000, cùng giá trị); không có cặp match → None.

    ``horizon_s`` phải >= 0 (ValueError nếu âm). Hàm thuần: KHÔNG ghép
    Episode với Event xuyên session — caller phải truyền các Episode/Event
    đã cùng phạm vi (xem ``ai_model/evaluate.py``).
    """
    if horizon_s < 0:
        raise ValueError(f"horizon_s phải >= 0, nhận {horizon_s}")
    n_episodes = len(episodes)
    n_events = len(events)
    n_tp, matched, leads = _match_episodes_events(episodes, events, horizon_s)

    precision: float | None = None
    if n_episodes:
        precision = n_tp / n_episodes
    recall: float | None = None
    if n_events:
        recall = len(matched) / n_events

    f1: float | None = None
    if precision is not None and recall is not None:
        total = precision + recall
        f1 = 0.0 if total == 0 else 2.0 * precision * recall / total

    false_warning_rate: float | None = None
    if n_episodes:
        false_warning_rate = (n_episodes - n_tp) / n_episodes

    miss_rate: float | None = None
    if n_events:
        miss_rate = 1.0 - recall  # recall xác định khi có Event

    n_fa = n_episodes - n_tp
    undefined_reason: str | None = None
    if n_fa == 0:
        false_alarms_per_min: float | None = 0.0
    else:
        span_s = sum(e.end_time_s - e.start_time_s for e in episodes) + horizon_s
        if span_s > 0:
            false_alarms_per_min = n_fa / (span_s / 60.0)
        else:
            # Trường hợp lý thuyết (Episode điểm + horizon 0): không định
            # nghĩa được FA/min — trả None, không ``inf`` trong report JSON.
            false_alarms_per_min = None
            undefined_reason = (
                "false_alarms_per_min không xác định: span tổng = 0 giây "
                f"với {n_fa} cảnh báo sai"
            )

    lead_time_s: dict[str, float] | None = (
        {"mean": fmean(leads), "min": min(leads), "max": max(leads)}
        if leads
        else None
    )
    lead_time_ms: dict[str, float] | None = (
        {key: value * 1000.0 for key, value in lead_time_s.items()}
        if lead_time_s is not None
        else None
    )

    return {
        "n_events": n_events,
        "n_episodes": n_episodes,
        "n_tp_episodes": n_tp,
        "n_matched_events": len(matched),
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "false_warning_rate": false_warning_rate,
        "miss_rate": miss_rate,
        "false_alarms_per_min": false_alarms_per_min,
        "undefined_reason": undefined_reason,
        "lead_time_s": lead_time_s,
        "lead_time_ms": lead_time_ms,
    }
