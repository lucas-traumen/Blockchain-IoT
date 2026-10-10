"""Tiện ích chung: thời gian ISO-8601 UTC."""

from datetime import datetime, timezone

__all__ = ["utc_now_iso", "parse_iso_utc"]


def utc_now_iso() -> str:
    """Thời điểm hiện tại UTC, ISO-8601 độ chính xác millisecond, hậu tố 'Z'."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def parse_iso_utc(ts: str) -> datetime:
    """Parse chuỗi ISO-8601 thành datetime UTC.

    Chuỗi thiếu múi giờ (tz-naive) được coi là UTC. Sai định dạng hoặc
    không phải chuỗi → ``ValueError``.
    """
    if not isinstance(ts, str) or not ts.strip():
        raise ValueError(f"Timestamp không hợp lệ: {ts!r}")
    try:
        dt = datetime.fromisoformat(ts.strip())
    except ValueError as exc:
        raise ValueError(
            f"Timestamp {ts!r} không đúng định dạng ISO-8601"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
