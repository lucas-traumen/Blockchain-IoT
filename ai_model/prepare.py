"""Chuẩn bị dữ liệu train (M5b): nạp JSONL synthetic + split theo session.

Trách nhiệm (train thật thuộc M5c — file này KHÔNG train/evaluate):

- ``load_jsonl_dataset``: đọc file JSONL do ``scripts/synthetic_dataset.py``
  sinh, tách 3 trường nhãn metadata (synthetic/scenario/session_label) khỏi
  payload. Lý do phải tách: ``schemas/telemetry_v1.schema.json`` khai
  ``additionalProperties: false`` nên payload phải đứng một mình khi
  validate/ingest; nhãn là thông tin mức JSONL. Trả về list
  ``{"row": <payload 11 field>, "meta": {<nhãn>}}``.

- ``split_by_session``: chia theo DANH SÁCH SESSION (không phải từng mẫu)
  thành train/val/test theo ``ratios`` (mặc định 70/15/15) — mọi cửa sổ của
  một session nằm trọn trong một split, tránh leakage train/test. Trả về
  ``{"train": set, "val": set, "test": set}`` các session id.

- ``build_split_manifest``: dict manifest ghi seed + ratios + danh sách
  session từng split + created_at, để hash (ví dụ ``sha256`` của
  ``json.dumps(manifest, sort_keys=True)``) và nhúng vào model manifest ở
  M5c.

Làm tròn số lượng split: phương pháp **largest remainder (Hamilton)** — lấy
floor của từng quota ``ratio × n_sessions``, các đơn vị dư (luôn có, vì tổng
floor < n) chia cho split có phần lẻ thập phân lớn nhất; hòa thì ưu tiên
thứ tự cố định train → val → test. Ví dụ: 10 session → 7/2/1; 30 session →
21/5/4. Tổng đếm luôn đủ ``n_sessions``.

Chia theo buổi: PLAN.md mục 4 yêu cầu "chia theo buổi nếu các lượt cùng buổi
gần giống". Dataset synthetic hiện KHÔNG có khái niệm buổi (mỗi session là
một boot độc lập, không gắn giờ chạy thật), nên mặc định chia ngẫu nhiên theo
session. Hook ``group_key`` (callable nhận session key, trả id buổi) được để
sẵn cho M7/experiments: khi cung cấp, các session cùng giá trị ``group_key``
gom thành một nhóm và phân bổ NGUYÊN NHÓM vào đúng một split — tỉ lệ chỉ
xấp xỉ khi nhóm lớn (nhóm không thể tách), ngược lại nhóm 1 session cho
đếm khớp chính xác quota.
"""

from __future__ import annotations

import json
import math
import random
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "METADATA_KEYS",
    "SPLIT_NAMES",
    "load_jsonl_dataset",
    "split_by_session",
    "build_split_manifest",
]

# Khớp nhãn do scripts/synthetic_dataset.py ghi — nhãn mức JSONL, không thuộc
# payload telemetry_v1.
METADATA_KEYS = ("synthetic", "scenario", "session_label")
SPLIT_NAMES = ("train", "val", "test")


def _utc_now_iso() -> str:
    """Thời điểm UTC hiện tại, millisecond, hậu tố Z (không phụ thuộc backend)."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def load_jsonl_dataset(path: str | Path) -> list[dict]:
    """Đọc JSONL synthetic; trả list ``{"row": payload, "meta": nhãn}``.

    Các trường trong ``METADATA_KEYS`` được tách vào ``meta``; phần còn lại
    của dòng là ``row`` (payload telemetry 11 field, sẵn sàng validate schema
    hoặc đưa vào ``extract_windows``). Dòng trắng bỏ qua; dòng JSON hỏng hoặc
    không phải object → ``ValueError`` kèm số dòng (dữ liệu train phải sạch,
    không bỏ qua âm thầm). Thứ tự dòng được giữ nguyên.
    """
    records: list[dict] = []
    with Path(path).open(encoding="utf-8") as handle:
        for line_no, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError as exc:
                raise ValueError(
                    f"Dòng {line_no} không parse được JSON: {exc}"
                ) from exc
            if not isinstance(item, dict):
                raise ValueError(f"Dòng {line_no} không phải JSON object")
            meta = {key: item[key] for key in METADATA_KEYS if key in item}
            row = {
                key: value for key, value in item.items() if key not in METADATA_KEYS
            }
            records.append({"row": row, "meta": meta})
    return records


def _extract_session_key(item: Any) -> str | None:
    """Session key của một item metadata: ưu tiên ``session_label`` (nhãn
    JSONL), fallback ``session_id`` (WindowMeta của cửa sổ). Không có → None.

    Nhận: dict phẳng có ``session_label`` (dòng JSONL), dict dạng
    ``{"row", "meta"}`` từ ``load_jsonl_dataset``, hoặc object
    Window/WindowMeta (đọc thuộc tính ``session_id``).
    """
    if isinstance(item, Mapping):
        meta = item.get("meta")
        candidates: tuple[Any, ...] = (
            item,
            meta if isinstance(meta, Mapping) else None,
        )
    else:
        candidates = (item, getattr(item, "meta", None))
    for obj in candidates:
        if obj is None:
            continue
        for attr in ("session_label", "session_id"):
            value = (
                obj.get(attr) if isinstance(obj, Mapping) else getattr(obj, attr, None)
            )
            if value is not None:
                return str(value)
    return None


def _validate_ratios(ratios: Sequence[float]) -> list[float]:
    """Kiểm tra ratios: đúng 3 giá trị, không âm, tổng bằng 1.0 (sai ±1e-9)."""
    if len(ratios) != len(SPLIT_NAMES):
        raise ValueError(
            f"cần đúng {len(SPLIT_NAMES)} ratio cho {SPLIT_NAMES}, nhận {len(ratios)}"
        )
    values = [float(r) for r in ratios]
    if any(r < 0 for r in values) or abs(sum(values) - 1.0) > 1e-9:
        raise ValueError(
            f"ratios phải không âm và tổng bằng 1.0 (±1e-9), nhận {values}"
        )
    return values


def _largest_remainder_counts(n: int, ratios: Sequence[float]) -> list[int]:
    """Đếm mỗi split theo largest remainder (xem module docstring).

    Tổng kết quả luôn bằng ``n``; thứ tự ưu tiên khi hòa phần lẻ: train →
    val → test (vị trí trong ``SPLIT_NAMES``).
    """
    exact = [r * n for r in ratios]
    counts = [int(math.floor(e)) for e in exact]
    remaining = n - sum(counts)
    order = sorted(
        range(len(ratios)), key=lambda i: (-(exact[i] - counts[i]), i)
    )
    for k in range(remaining):
        counts[order[k]] += 1
    return counts


def split_by_session(
    windows_meta: Sequence[Any],
    seed: int,
    ratios: Sequence[float] = (0.7, 0.15, 0.15),
    group_key: Callable[[str], str] | None = None,
) -> dict[str, set[str]]:
    """Chia danh sách session thành train/val/test; trả dict các ``set``.

    Tham số:
      - ``windows_meta``: list metadata của cửa sổ/mẫu — dict phẳng có
        ``session_label`` (dòng JSONL synthetic), dict dạng ``{"row",
        "meta"}`` từ ``load_jsonl_dataset``, hoặc object Window/WindowMeta
        (fallback ``session_id``). Item nào không suy ra được session key →
        ``ValueError`` (không cho mẫu không xác định session lọt vào split).
      - ``seed``: seed cho ``random.Random`` khi xáo trộn nhóm — cùng seed
        cho cùng kết quả (deterministic).
      - ``ratios``: (train, val, test), mặc định (0.7, 0.15, 0.15).
      - ``group_key``: tùy chọn, hook gom session theo buổi (xem module
        docstring); None → mỗi session là một nhóm độc lập.

    Thuật toán: gom nhóm → sắp nhóm deterministic theo session nhỏ nhất →
    xáo trộn nhóm bằng ``random.Random(seed)`` → phân bổ nguyên nhóm cho
    split đang thiếu hụt (deficit) nhiều nhất so với quota largest-remainder,
    hòa thì ưu tiên train → val → test. Với nhóm 1 session, đếm từng split
    khớp chính xác quota; với nhóm nhiều session, tỉ lệ chỉ xấp xỉ.
    """
    ratios = _validate_ratios(ratios)

    session_keys: set[str] = set()
    n_unkeyed = 0
    for item in windows_meta:
        key = _extract_session_key(item)
        if key is None:
            n_unkeyed += 1
        else:
            session_keys.add(key)
    if n_unkeyed:
        raise ValueError(
            f"{n_unkeyed} item không suy ra được session_label/session_id — "
            "không thể split an toàn"
        )
    if not session_keys:
        raise ValueError("windows_meta rỗng — không có session nào để split")

    # Gom nhóm buổi (mặc định: mỗi session một nhóm).
    groups: dict[str, set[str]] = {}
    for key in session_keys:
        gid = str(group_key(key)) if group_key is not None else key
        groups.setdefault(gid, set()).add(key)

    targets = _largest_remainder_counts(len(session_keys), ratios)
    group_list = sorted(groups.items(), key=lambda kv: min(kv[1]))
    rng = random.Random(seed)
    rng.shuffle(group_list)

    assigned: dict[str, set[str]] = {name: set() for name in SPLIT_NAMES}
    deficits = list(targets)
    for _gid, members in group_list:
        # Split đang thiếu nhiều nhất; hòa → ưu tiên train → val → test.
        best = max(range(len(SPLIT_NAMES)), key=lambda i: (deficits[i], -i))
        assigned[SPLIT_NAMES[best]].update(members)
        deficits[best] -= len(members)
    return assigned


def build_split_manifest(
    session_keys: Mapping[str, Iterable[str]],
    seed: int,
    ratios: Sequence[float] = (0.7, 0.15, 0.15),
    created_at: str | None = None,
) -> dict:
    """Dựng split manifest để hash + lưu vào model manifest (M5c).

    ``session_keys``: kết quả của ``split_by_session`` (mapping split → tập
    session id). Trả về::

        {
          "seed": int,
          "ratios": [float, float, float],
          "n_sessions": int,               # tổng số session mọi split
          "sessions": {"train": [sorted], "val": [sorted], "test": [sorted]},
          "created_at": ISO-8601 UTC,
        }

    ``created_at`` truyền sẵn (chuỗi ISO-8601 UTC) khi cần manifest
    deterministic để hash; mặc định là thời điểm gọi hàm. Thiếu split chuẩn
    hoặc có split lạ → ``ValueError``.
    """
    ratios = _validate_ratios(ratios)
    missing = [name for name in SPLIT_NAMES if name not in session_keys]
    if missing:
        raise ValueError(f"thiếu split {missing} trong session_keys")
    unexpected = set(session_keys) - set(SPLIT_NAMES)
    if unexpected:
        raise ValueError(
            f"session_keys có split lạ {sorted(unexpected)}, chỉ nhận "
            f"{SPLIT_NAMES}"
        )
    sessions = {name: sorted(session_keys[name]) for name in SPLIT_NAMES}
    return {
        "seed": int(seed),
        "ratios": list(ratios),
        "n_sessions": sum(len(ids) for ids in sessions.values()),
        "sessions": sessions,
        "created_at": created_at or _utc_now_iso(),
    }
