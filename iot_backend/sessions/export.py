"""Export raw samples của session ra JSONL (payload nguyên vẹn) hoặc CSV."""

import csv
import json
import sqlite3
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import TextIO

__all__ = ["CSV_COLUMNS", "export_csv", "export_jsonl", "iter_jsonl"]

# 11 trường của payload telemetry v1 (header CSV, thứ tự chuẩn).
CSV_COLUMNS = [
    "schema_version",
    "device_id",
    "boot_id",
    "sequence",
    "monotonic_us",
    "measured_at",
    "time_synced",
    "distance_mm",
    "echo_us",
    "valid",
    "error_code",
]


def iter_jsonl(rows: Iterable[sqlite3.Row]) -> Iterator[str]:
    """Sinh từng dòng JSONL từ raw rows: payload_json NGUYÊN VẸN, không chỉnh sửa."""
    for row in rows:
        yield row["payload_json"]


def _write_lines(rows: Iterable[sqlite3.Row], handle: TextIO) -> int:
    """Ghi JSONL vào handle đang mở, trả về số dòng."""
    count = 0
    for line in iter_jsonl(rows):
        handle.write(line)
        handle.write("\n")
        count += 1
    return count


def export_jsonl(rows: Iterable[sqlite3.Row], out: str | Path) -> int:
    """Ghi raw rows ra file JSONL (mỗi dòng payload_json nguyên vẹn); trả số dòng."""
    count = 0
    with open(out, "w", encoding="utf-8", newline="\n") as handle:
        count = _write_lines(rows, handle)
    return count


def export_csv(rows: Iterable[sqlite3.Row], out: str | Path) -> int:
    """Ghi raw rows ra CSV: header 11 trường, giá trị parse từ payload_json.

    Trả về số dòng data (không tính header). Giá trị thiếu → chuỗi rỗng.
    Line terminator chuẩn hóa '\n' để khớp giữa file và API streaming.
    """
    count = 0
    with open(out, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(CSV_COLUMNS)
        for row in rows:
            payload = json.loads(row["payload_json"])
            writer.writerow([payload.get(column) for column in CSV_COLUMNS])
            count += 1
    return count
