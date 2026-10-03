"""Chuẩn bị dữ liệu train (M5b): nạp JSONL synthetic + split theo session.

Trách nhiệm (train thật thuộc M5c — file này KHÔNG train/evaluate):

- ``load_jsonl_dataset``: đọc file JSONL do ``scripts/synthetic_dataset.py``
  sinh, tách nhãn metadata (synthetic/scenario/session_label/target_type/speed_level) khỏi
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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ai_model.preprocessing import PP_LSTM_VERSION

__all__ = [
    "METADATA_KEYS",
    "SPLIT_NAMES",
    "SESSION_META_KEYS",
    "load_jsonl_dataset",
    "normalize_rows",
    "split_by_session",
    "validate_split",
    "build_split_manifest",
    "build_dataset_split_manifest",
    "build_lstm_dataset",
    "LSTMSplit",
    "LSTMDataset",
]

# Khớp nhãn do scripts/synthetic_dataset.py ghi — nhãn mức JSONL, không thuộc
# payload telemetry_v1.
METADATA_KEYS = ("synthetic", "scenario", "session_label", "target_type", "speed_level")
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
            item.get("row") if isinstance(item.get("row"), Mapping) else None,
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


# ------------------------------------------------------------------ M10:data
#
# Đường chuẩn bị dataset LSTM từ rows generic (session export/SQLite hoặc
# bất kỳ nguồn nào giữ boundary: caller nạp rows, module này KHÔNG mở DB).
# Nguyên tắc leakage: split theo DANH SÁCH SESSION trước, extract window sau
# — mọi cửa sổ của một session nằm trọn trong một split.

# Metadata mức session giữ lại cho breakdown M12 (scenario/target_type/...).
# session_id không nằm ở đây: nó là trường nhóm của payload/row.
SESSION_META_KEYS = ("scenario", "speed_level", "target_type")


def normalize_rows(
    rows: Iterable[Any], metadata_keys: Sequence[str] = SESSION_META_KEYS
) -> list[dict]:
    """Chuẩn hóa rows generic (session export/SQLite) thành record
    ``{"row": payload, "meta": metadata}`` — cùng hình dạng output của
    ``load_jsonl_dataset`` để downstream dùng chung.

    ``rows``: mỗi item là Mapping có các trường telemetry (device_id, boot_id,
    sequence, monotonic_us, distance_mm, valid, ...) và BẮT BUỘC có
    ``session_id`` khác rỗng/None — không cho mẫu không xác định session lọt
    vào dataset (split theo session không thể an toàn nếu thiếu). Metadata
    trong ``metadata_keys`` (mặc định scenario/speed_level/target_type) được
    tách vào ``meta``; các trường còn lại giữ nguyên trong ``row`` (gồm cả
    ``session_id`` — ``extract_windows`` nhóm theo nó). Raw lỗi (valid=False,
    thiếu distance) VẪN GIỮ — việc bỏ thuộc windowing ở ``extract_windows``.

    Input không bị mutate (bản sao dict). Item không phải Mapping hoặc thiếu
    session_id → ``ValueError`` kèm vị trí.
    """
    records: list[dict] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError(
                f"row {index} không phải mapping, nhận {type(row).__name__}"
            )
        session_id = row.get("session_id")
        if session_id is None or str(session_id) == "":
            raise ValueError(
                f"row {index} thiếu session_id — dataset LSTM yêu cầu gán "
                "session trước khi chuẩn bị (không split mẫu mồ côi)"
            )
        meta = {key: row[key] for key in metadata_keys if key in row}
        payload = {
            key: value for key, value in row.items() if key not in metadata_keys
        }
        records.append({"row": payload, "meta": meta})
    return records


def validate_split(
    split: Mapping[str, Iterable[str]],
    expected_sessions: Iterable[str] | None = None,
) -> dict[str, int]:
    """Kiểm tra split theo session: đủ 3 split chuẩn, pairwise không giao,
    đếm tổng khớp (và nếu có ``expected_sessions``: union khớp chính xác tập
    đó, không thừa không thiếu). Trả về ``{"train": n, "val": n, "test": n}``.
    Vi phạm → ``ValueError``.
    """
    missing = [name for name in SPLIT_NAMES if name not in split]
    if missing:
        raise ValueError(f"thiếu split {missing}")
    unexpected = set(split) - set(SPLIT_NAMES)
    if unexpected:
        raise ValueError(f"split lạ {sorted(unexpected)}")
    sets = {name: set(split[name]) for name in SPLIT_NAMES}
    names = list(SPLIT_NAMES)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            overlap = sets[names[i]] & sets[names[j]]
            if overlap:
                raise ValueError(
                    f"leakage: session xuất hiện ở cả {names[i]} và "
                    f"{names[j]}: {sorted(overlap)[:5]}..."
                )
    if expected_sessions is not None:
        expected = set(expected_sessions)
        union = sets["train"] | sets["val"] | sets["test"]
        if union != expected:
            raise ValueError(
                f"union của split lệch tập session kỳ vọng: thiếu "
                f"{sorted(expected - union)[:5]}, thừa {sorted(union - expected)[:5]}"
            )
    return {name: len(sets[name]) for name in SPLIT_NAMES}


@dataclass(frozen=True)
class LSTMSplit:
    """Kết quả chuẩn bị một split: tensor + thống kê + metadata session.

    ``X`` shape ``(N, window_size, 2)`` (từ ``windows_to_lstm_array`` — M9),
    ``y`` shape ``(N, len(horizons_ms))`` khoảng cách tương lai (mét), cùng
    thứ tự windows. ``windows`` giữ Window gốc (meta + targets) cho truy
    vết; ``session_meta``: session_id → metadata (scenario/...) để M12
    breakdown; ``dropped``: thống kê drop của ``extract_windows``.
    """

    name: str
    X: Any
    y: Any
    windows: tuple
    stats: Any
    session_ids: frozenset
    session_meta: dict = field(default_factory=dict)
    n_rows: int = 0

    @property
    def n_windows(self) -> int:
        return self.stats.n_windows

    @property
    def dropped(self) -> dict[str, int]:
        return {
            "error": self.stats.n_dropped_error,
            "gap": self.stats.n_dropped_gap,
            "jitter": self.stats.n_dropped_jitter,
            "short": self.stats.n_dropped_short,
        }


@dataclass(frozen=True)
class LSTMDataset:
    """Dataset LSTM hoàn chỉnh sau split: ba LSTMSplit + split manifest."""

    train: LSTMSplit
    val: LSTMSplit
    test: LSTMSplit
    split_manifest: dict

    @property
    def splits(self) -> dict[str, LSTMSplit]:
        return {name: getattr(self, name) for name in SPLIT_NAMES}


def _as_records(rows: Iterable[Any]) -> list[dict]:
    """Chấp nhận cả record sẵn ``{"row", "meta"}`` lẫn raw rows: raw →
    ``normalize_rows``. Record sẵn vẫn phải có session key (split_by_session
    sẽ chặn item mồ côi). Input được materialize thành list NGAY ĐẦU để
    probe ``all(...)`` không tiêu generator trước khi dùng."""
    items = list(rows)  # materialize: an toàn với generator/iterator
    if items and all(
        isinstance(item, Mapping) and "row" in item and "meta" in item
        for item in items
    ):
        return items
    return normalize_rows(items)


def _prepared_row(rec: Mapping, index: int) -> Mapping:
    """Bản sao ``rec["row"]`` có ``session_id`` khớp session key, để
    ``extract_windows`` nhóm đúng theo session (MAJOR-1 fix).

    Record từ ``load_jsonl_dataset`` (JSONL synthetic) có ``session_label``
    ở ``meta`` nhưng payload không có ``session_id`` — nếu đưa thẳng vào
    ``extract_windows``, các mẫu nhóm theo ``(None, device, boot)`` và boot
    tái sử dụng giữa sessions sẽ nối window QUA session (leakage). Quy ước
    (giống RF train.py): khi row thiếu ``session_id``, inject bản sao
    ``row["session_id"] = session_label``; row đã có ``session_id`` khác
    session key → ``ValueError`` (không nối âm thầm). Input không mutate.
    """
    row = rec["row"]
    # Quy ước nguồn session: meta.session_label (nhãn mức JSONL) là nguồn
    # chính khi có; fallback row.session_id (record generic/SQLite).
    meta = rec.get("meta")
    label = meta.get("session_label") if isinstance(meta, Mapping) else None
    key = str(label) if label is not None else _extract_session_key(rec)
    if key is None:
        raise ValueError(
            f"record {index} không suy ra được session_label/session_id — "
            "không thể gán session"
        )
    row_session = row.get("session_id")
    if row_session is None:
        row = dict(row)
        row["session_id"] = key
    elif str(row_session) != key:
        raise ValueError(
            f"record {index}: session_id={row_session!r} trong row lệch "
            f"session key={key!r} (session_label/meta) — hai nguồn phải "
            "khớp nhau, không tự chọn thay caller"
        )
    return row


def build_dataset_split_manifest(
    split_counts: Mapping[str, int],
    sessions: Mapping[str, Iterable[str]],
    split_info: Mapping[str, Mapping[str, int]],
    seed: int,
    ratios: Sequence[float] = (0.7, 0.15, 0.15),
    created_at: str | None = None,
) -> dict:
    """Split manifest cho dataset LSTM — dùng làm ``split_manifest.json``.

    Khác ``build_split_manifest`` (M5b): có đếm rows/windows/dropped từng
    split và ``preprocessing_version``. ``created_at`` chỉ được nhúng khi
    caller truyền tường minh — mặc định KHÔNG có timestamp nên
    ``sha256_json(manifest)`` tái lập được (yêu cầu M10). ``sessions`` là
    mapping split → session ids (được sort trong manifest).
    """
    ratios = _validate_ratios(ratios)
    missing = [name for name in SPLIT_NAMES if name not in sessions]
    if missing:
        raise ValueError(f"thiếu split {missing} trong sessions")
    manifest = {
        "preprocessing_version": PP_LSTM_VERSION,
        "seed": int(seed),
        "ratios": list(ratios),
        "n_sessions": sum(int(split_counts[n]) for n in SPLIT_NAMES),
        "n_rows": sum(int(split_info[n]["n_rows"]) for n in SPLIT_NAMES),
        "n_windows": sum(int(split_info[n]["n_windows"]) for n in SPLIT_NAMES),
        "sessions": {
            name: sorted(sessions[name]) for name in SPLIT_NAMES
        },
        "splits": {
            name: {
                "n_sessions": int(split_counts[name]),
                "n_rows": int(split_info[name]["n_rows"]),
                "n_windows": int(split_info[name]["n_windows"]),
                "dropped": dict(split_info[name]["dropped"]),
            }
            for name in SPLIT_NAMES
        },
    }
    if created_at is not None:
        manifest["created_at"] = created_at
    return manifest


def build_lstm_dataset(
    rows: Sequence[Any],
    window_config: Any,
    model_config: Mapping[str, Any] | None = None,
    seed: int = 42,
    ratios: Sequence[float] = (0.7, 0.15, 0.15),
    group_key: Callable[[str], str] | None = None,
    created_at: str | None = None,
) -> LSTMDataset:
    """Chuẩn bị dataset LSTM: split theo session TRƯỚC, extract window SAU.

    Tham số:
      - ``rows``: raw rows generic (bắt buộc có ``session_id``) hoặc record
        ``{"row", "meta"}`` (ví dụ từ ``normalize_rows``). Raw lỗi vẫn giữ;
        chỉ windowing bỏ theo rules M9.
      - ``window_config``: ``WindowConfig`` (configs/model.yaml).
      - ``model_config``: dict YAML cho ``windows_to_lstm_array`` (khối
        lstm.window_size); None → fallback window_size của features.py. Nếu
        truyền, ``lstm.horizons_ms`` (khi khai) phải khớp
        ``window_config.horizons_ms`` — lệch → ValueError (chặn drift y/manifest).
      - ``seed``: seed split (configs/model.yaml: random_seed = 42).
      - ``ratios``: (train, val, test) mặc định 70/15/15.
      - ``group_key``: hook gom session theo buổi — như ``split_by_session``.
      - ``created_at``: truyền tường minh nếu muốn manifest có timestamp
        (hash sẽ lệch theo giá trị này); mặc định manifest không timestamp.

    Đầu vào: caller tự assemble rows (session export JSONL, SQLite
    ``session_raw_rows``/``all_raw_rows`` của ``backend.ingestion.store``,
    hoặc bất kỳ nguồn nào) — module này KHÔNG mở DB, chỉ nhận rows đã nạp.

    Guards: split có ratio > 0 nhưng không có session/window, hoặc tổng
    windows = 0 → ``ValueError`` nêu rõ split/tổng (ratio = 0 cho split
    rỗng là hợp lệ). Không trả dataset âm thầm khi thiếu dữ liệu.

    Quy trình: chuẩn hóa records → split danh sách session (seed, largest
    remainder) → validate overlap/union → inject session_id (bản sao, không
    mutate) → lọc rows theo split → ``extract_windows`` từng split (stats
    drop theo nguyên nhân) → ``windows_to_lstm_array`` X ``(N, W, 2)`` + y
    ``(N, H)`` giữ nguyên thứ tự windows (KHÔNG shuffle — shuffle batch nếu
    có phải về sau, sau split, với seed riêng ở M11). Không có window nào
    được tạo trước khi biết split.
    """
    try:
        import numpy as _np
    except ImportError:  # pragma: no cover - .venv luôn có numpy
        _np = None  # type: ignore[assignment]
    # Import cục bộ: prepare.py giữ nhẹ ở top-level (RF path không cần),
    # windows/features chỉ nạp khi dựng dataset LSTM.
    from ai_model.features import windows_to_lstm_array
    from ai_model.windows import extract_windows

    records = _as_records(rows)
    if not records:
        raise ValueError("rows rỗng — không có dữ liệu để chuẩn bị dataset")

    # Chuẩn bị rows (inject session_id bản sao, chặn conflict lệch nguồn)
    # NGAY TRƯỚC split — lỗi dữ liệu input phải lộ trước mọi guard split.
    prepared_rows = [
        _prepared_row(rec, index) for index, rec in enumerate(records)
    ]

    ratios_list = _validate_ratios(ratios)
    if model_config is not None:
        lstm_block = model_config.get("lstm")
        if isinstance(lstm_block, Mapping) and lstm_block.get("horizons_ms"):
            config_horizons = tuple(int(h) for h in lstm_block["horizons_ms"])
            if config_horizons != tuple(window_config.horizons_ms):
                raise ValueError(
                    f"model_config lstm.horizons_ms {config_horizons} lệch "
                    f"window_config.horizons_ms "
                    f"{tuple(window_config.horizons_ms)} — y sẽ drift khỏi "
                    "manifest, từ chối"
                )

    # 1) Split theo session TRƯỚC khi tạo bất kỳ window nào.
    split = split_by_session(records, seed=seed, ratios=ratios, group_key=group_key)
    session_keys = {_extract_session_key(rec) for rec in records}
    validate_split(split, expected_sessions=session_keys)
    key_to_split: dict[str, str] = {
        key: name for name in SPLIT_NAMES for key in split[name]
    }

    # 2) Inject session_id (bản sao) rồi lọc rows theo split, mới windowing.
    rows_by_split: dict[str, list[dict]] = {name: [] for name in SPLIT_NAMES}
    session_meta: dict[str, dict] = {name: {} for name in SPLIT_NAMES}
    for index, rec in enumerate(records):
        key = _extract_session_key(rec)
        name = key_to_split[key]
        rows_by_split[name].append(prepared_rows[index])
        meta = session_meta[name].setdefault(key, {})
        for k, v in rec["meta"].items():
            meta.setdefault(k, v)

    splits: dict[str, LSTMSplit] = {}
    split_info: dict[str, dict] = {}
    for name in SPLIT_NAMES:
        windows, stats = extract_windows(rows_by_split[name], window_config)
        X = windows_to_lstm_array(windows, model_config)
        targets = [w.targets for w in windows]
        if _np is not None:
            y = _np.asarray(targets, dtype="float64").reshape(
                len(targets), len(window_config.horizons_ms)
            )
        else:  # pragma: no cover
            y = targets
        splits[name] = LSTMSplit(
            name=name,
            X=X,
            y=y,
            windows=tuple(windows),
            stats=stats,
            session_ids=frozenset(split[name]),
            session_meta=session_meta[name],
            n_rows=len(rows_by_split[name]),
        )
        split_info[name] = {
            "n_rows": splits[name].n_rows,
            "n_windows": splits[name].n_windows,
            "dropped": splits[name].dropped,
        }

    # Guards (MAJOR-2): tổng windows = 0, hoặc split có ratio > 0 nhưng rỗng
    # — fail rõ nêu tên split, không trả dataset âm thầm. Ratio = 0 cho
    # split rỗng là hợp lệ.
    total_windows = sum(info["n_windows"] for info in split_info.values())
    if total_windows == 0:
        dropped_summary = {
            name: split_info[name]["dropped"] for name in SPLIT_NAMES
        }
        total_rows = sum(info["n_rows"] for info in split_info.values())
        raise ValueError(
            "build_lstm_dataset: tổng 0 cửa sổ trên toàn bộ dataset — kiểm "
            f"tra dữ liệu (n_rows={total_rows}, dropped={dropped_summary})"
        )
    for name, ratio in zip(SPLIT_NAMES, ratios_list):
        if ratio > 0 and split_info[name]["n_windows"] == 0:
            raise ValueError(
                f"build_lstm_dataset: split '{name}' có ratio {ratio} > 0 "
                f"nhưng 0 cửa sổ (n_sessions={len(split[name])}, n_rows="
                f"{split_info[name]['n_rows']}, dropped="
                f"{split_info[name]['dropped']}) — thêm dữ liệu hoặc chỉnh ratios"
            )

    manifest = build_dataset_split_manifest(
        split_counts=validate_split(split),
        sessions={name: split[name] for name in SPLIT_NAMES},
        split_info=split_info,
        seed=seed,
        ratios=ratios,
        created_at=created_at,
    )
    return LSTMDataset(
        train=splits["train"],
        val=splits["val"],
        test=splits["test"],
        split_manifest=manifest,
    )
