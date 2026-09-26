"""Trích cửa sổ training/inference từ danh sách mẫu raw — dùng chung M5c + M6.

Theo PLAN.md mục 4: windowing/features dùng chung train và runtime.
Đơn vị nội bộ SI (mét, giây); payload gốc mm + µs, chuyển đổi tại đây
(``distance_m = distance_mm / 1000``, ``dt = Δmonotonic_us / 1e6``).

Quy tắc một chuỗi (run) hợp lệ:

- cùng nhóm ``(session_id, device_id, boot_id)`` — ``session_id=None`` là
  một giá trị nhóm riêng (các mẫu không có session ghép với nhau theo
  device+boot, không ghép với session có id);
- mẫu ``valid=True`` và có ``distance_mm``;
- ``sequence`` liên tiếp đúng (+1);
- nhịp thời gian giữa hai mẫu liền tính theo ``monotonic_us`` (µs, cùng
  boot nên so sánh được): ``abs(dt - delta_s) <= max_time_jitter_s``;
  ``dt > max_gap_s`` → cắt run (gap); ``dt <= max_gap_s`` nhưng lệch nhịp
  quá jitter → cắt run (jitter). Mẫu ``valid=False`` hoặc thiếu
  ``distance_mm`` → cắt run (error).

Cửa sổ kết thúc tại mẫu t: ``X = [d(t-(N-1)Δ), ..., d(t)]`` (N =
window_size), ``y`` theo ``horizons_ms`` (mỗi horizon phải là bội nguyên
của ``delta_s``). Cửa sổ không vắt qua điểm cắt run.

Thống kê drop (đếm mức sự kiện, không đếm trùng một mẫu hai lần):

- ``n_dropped_error``: mỗi mẫu không dùng được (valid khác True, thiếu
  ``distance_mm``/``sequence``/``monotonic_us``) đếm 1;
- ``n_dropped_gap``: mỗi điểm cắt do sequence không +1 hoặc
  ``dt > max_gap_s`` đếm 1;
- ``n_dropped_jitter``: mỗi điểm cắt do lệch nhịp quá
  ``max_time_jitter_s`` đếm 1;
- ``n_dropped_short``: mỗi run kết thúc tự nhiên (hết mẫu của nhóm) mà
  không đủ dài để tạo bất kỳ cửa sổ nào đếm 1; run bị cắt bởi sự kiện
  (error/gap/jitter) chỉ tính vào lý do sự kiện đó.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

__all__ = [
    "WindowConfig",
    "WindowMeta",
    "Window",
    "WindowStats",
    "extract_windows",
]

# Hằng số đơn vị µs → giây (không phải tham số cấu hình mô hình).
US_PER_S = 1_000_000


def _get(row: Any, key: str, default: Any = None) -> Any:
    """Đọc ``key`` từ dict hoặc sqlite3.Row; trả ``default`` nếu thiếu hoặc None.

    sqlite3.Row raise IndexError cho key không tồn tại, dict raise KeyError —
    chuẩn hóa cả hai. Row không có cột ``session_id`` (bảng raw_samples) →
    None, tức nhóm "không session".
    """
    try:
        value = row[key]
    except (KeyError, IndexError):
        return default
    if value is None:
        return default
    return value


@dataclass(frozen=True)
class WindowConfig:
    """Tham số cửa sổ — mọi hằng số lấy từ config truyền vào (configs/model.yaml).

    Không có giá trị mặc định: bắt buộc dựng qua ``from_dict`` từ dict YAML
    đã nạp, tránh cứng hóa số liệu trong code.
    """

    window_size: int
    delta_s: float
    horizons_ms: tuple[int, ...]
    max_time_jitter_s: float
    max_gap_s: float

    @classmethod
    def from_dict(cls, config: Mapping[str, Any]) -> "WindowConfig":
        """Dựng từ dict đã nạp YAML; thiếu key bắt buộc → KeyError (không fallback)."""
        horizons_ms = config["horizons_ms"]
        if not horizons_ms:
            raise ValueError("horizons_ms rỗng — cần ít nhất một horizon")
        window_size = int(config["window_size"])
        if window_size < 1:
            raise ValueError(f"window_size phải >= 1, nhận {window_size}")
        return cls(
            window_size=window_size,
            delta_s=float(config["delta_s"]),
            horizons_ms=tuple(int(h) for h in horizons_ms),
            max_time_jitter_s=float(config["max_time_jitter_s"]),
            max_gap_s=float(config["max_gap_s"]),
        )

    @property
    def target_offsets(self) -> tuple[int, ...]:
        """Số bước mẫu Δ của từng horizon, theo thứ tự horizons_ms.

        horizon_ms phải là bội nguyên của delta_s*1000; sai → ValueError
        (không làm tròn âm thầm).
        """
        step_ms = self.delta_s * 1000.0
        offsets: list[int] = []
        for horizon_ms in self.horizons_ms:
            exact = horizon_ms / step_ms
            offset = round(exact)
            if offset < 1 or abs(exact - offset) > 1e-9:
                raise ValueError(
                    f"horizon {horizon_ms} ms không là bội nguyên của "
                    f"delta_s = {self.delta_s} s"
                )
            offsets.append(int(offset))
        return tuple(offsets)


@dataclass(frozen=True)
class WindowMeta:
    """Nguồn gốc một cửa sổ.

    ``measured_at`` là của mẫu cuối X (mẫu t); None nếu mẫu đó không có
    giá trị — không bịa thay thế.
    """

    device_id: str | None
    boot_id: str | None
    session_id: str | None
    sequence_first: int
    sequence_last: int
    sequence_target_last: int
    measured_at: str | None


@dataclass(frozen=True)
class Window:
    """Một cửa sổ train/inference.

    ``features``: N khoảng cách (mét), thứ tự d0 (cũ nhất) → d(N-1) (mới
    nhất). ``targets``: giá trị tương lai (mét) theo thứ tự horizons_ms.
    """

    features: list[float]
    targets: list[float]
    meta: WindowMeta


@dataclass(frozen=True)
class WindowStats:
    """Thống kê trích cửa sổ của một lần gọi ``extract_windows``."""

    n_windows: int
    n_dropped_error: int
    n_dropped_gap: int
    n_dropped_jitter: int
    n_dropped_short: int


def _sequence_sort_key(row: Any) -> tuple[int, int]:
    """Sắp mẫu theo sequence trong nhóm; sequence None đẩy xuống cuối."""
    sequence = _get(row, "sequence")
    if sequence is None:
        return (1, 0)
    return (0, int(sequence))


def _windows_from_run(
    run: list[tuple[int, int, float, str | None]],
    config: WindowConfig,
    offsets: Sequence[int],
    max_offset: int,
    session_id: str | None,
    device_id: str | None,
    boot_id: str | None,
) -> list[Window]:
    """Tạo mọi cửa sổ nằm trọn trong một run.

    ``run``: danh sách (sequence, monotonic_us, distance_m, measured_at)
    theo thứ tự thời gian. Cửa sổ kết thúc tại chỉ số ``end`` cần
    ``end + max_offset <= len(run) - 1`` — không vắt qua điểm cắt.
    """
    windows: list[Window] = []
    n = len(run)
    for end in range(config.window_size - 1, n):
        if end + max_offset > n - 1:
            break
        start = end - config.window_size + 1
        features = [run[start + k][2] for k in range(config.window_size)]
        targets = [run[end + offset][2] for offset in offsets]
        meta = WindowMeta(
            device_id=device_id,
            boot_id=boot_id,
            session_id=session_id,
            sequence_first=run[start][0],
            sequence_last=run[end][0],
            sequence_target_last=run[end + max_offset][0],
            measured_at=run[end][3],
        )
        windows.append(Window(features=features, targets=targets, meta=meta))
    return windows


def extract_windows(
    rows: Sequence[Any], config: WindowConfig
) -> tuple[list[Window], WindowStats]:
    """Trích cửa sổ từ các row (dict hoặc sqlite3.Row).

    ``rows`` có thể ở bất kỳ thứ tự nào: hàm tự nhóm theo
    ``(session_id, device_id, boot_id)`` rồi sắp theo ``sequence`` trong
    từng nhóm. Trả về ``(windows, stats)``.
    """
    offsets = config.target_offsets
    max_offset = max(offsets)

    groups: dict[tuple[str | None, str | None, str | None], list[Any]] = {}
    for row in rows:
        key = (
            _get(row, "session_id"),
            _get(row, "device_id"),
            _get(row, "boot_id"),
        )
        groups.setdefault(key, []).append(row)

    windows: list[Window] = []
    n_error = 0
    n_gap = 0
    n_jitter = 0
    n_short = 0

    for (session_id, device_id, boot_id), group_rows in groups.items():
        group_rows.sort(key=_sequence_sort_key)
        # Run hiện tại: (sequence, monotonic_us, distance_m, measured_at).
        run: list[tuple[int, int, float, str | None]] = []

        for row in group_rows:
            valid = _get(row, "valid")
            distance_mm = _get(row, "distance_mm")
            sequence = _get(row, "sequence")
            monotonic_us = _get(row, "monotonic_us")

            # Mẫu không dùng được → cắt run, đếm sự kiện error.
            if (
                not valid
                or distance_mm is None
                or sequence is None
                or monotonic_us is None
            ):
                windows.extend(
                    _windows_from_run(
                        run, config, offsets, max_offset,
                        session_id, device_id, boot_id,
                    )
                )
                run = []
                n_error += 1
                continue

            distance_m = float(distance_mm) / 1000.0

            if run:
                dt_s = (int(monotonic_us) - run[-1][1]) / US_PER_S
                # Thiếu mẫu (sequence không +1) hoặc quá lớn → gap.
                if int(sequence) != run[-1][0] + 1 or dt_s > config.max_gap_s:
                    windows.extend(
                        _windows_from_run(
                            run, config, offsets, max_offset,
                            session_id, device_id, boot_id,
                        )
                    )
                    run = []
                    n_gap += 1
                # Còn trong giới hạn gap nhưng lệch nhịp → jitter.
                elif abs(dt_s - config.delta_s) > config.max_time_jitter_s:
                    windows.extend(
                        _windows_from_run(
                            run, config, offsets, max_offset,
                            session_id, device_id, boot_id,
                        )
                    )
                    run = []
                    n_jitter += 1

            run.append(
                (
                    int(sequence),
                    int(monotonic_us),
                    distance_m,
                    _get(row, "measured_at"),
                )
            )

        # Hết mẫu của nhóm: run cuối kết thúc tự nhiên — thiếu mới đếm short.
        tail_windows = _windows_from_run(
            run, config, offsets, max_offset, session_id, device_id, boot_id
        )
        if tail_windows:
            windows.extend(tail_windows)
        elif run:
            n_short += 1

    stats = WindowStats(
        n_windows=len(windows),
        n_dropped_error=n_error,
        n_dropped_gap=n_gap,
        n_dropped_jitter=n_jitter,
        n_dropped_short=n_short,
    )
    return windows, stats
