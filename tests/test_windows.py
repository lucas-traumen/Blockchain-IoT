"""Kiểm thử trích cửa sổ từ rows (windows.py).

Fixture dựng tay list dict (không cần DB); một test phụ dùng sqlite3.Row
thật để chứng minh helper ``_get`` hỗ trợ cả hai kiểu input.
"""

import sqlite3
from pathlib import Path

import pytest
import yaml

from ai_model.windows import (
    Window,
    WindowConfig,
    WindowStats,
    extract_windows,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# Khớp configs/model.yaml (test 1 đối chiếu trực tiếp với file thật).
_MODEL_CONFIG_DICT = {
    "window_size": 10,
    "delta_s": 0.1,
    "horizons_ms": [100, 200, 300, 400, 500],
    "max_time_jitter_s": 0.03,
    "max_gap_s": 0.25,
}

_BASE_MONO_US = 10_000_000
_UNSET = object()


def _mono_us(sequence: int) -> int:
    """monotonic_us của chuỗi chuẩn: Δ = 0.1 s = 100_000 µs giữa hai mẫu liền."""
    return _BASE_MONO_US + (sequence - 1) * 100_000


def _row(
    sequence: int,
    *,
    device_id: str = "esp32_01",
    boot_id: str = "b1",
    session_id: str | None = "s1",
    valid: bool = True,
    distance_mm=_UNSET,
    monotonic_us=_UNSET,
    measured_at=_UNSET,
) -> dict:
    """Một mẫu raw dạng dict; mặc định chuỗi chuẩn Δ=0.1s, distance tăng 1mm/mẫu."""
    return {
        "device_id": device_id,
        "boot_id": boot_id,
        "session_id": session_id,
        "sequence": sequence,
        "monotonic_us": _mono_us(sequence) if monotonic_us is _UNSET else monotonic_us,
        "measured_at": (
            f"2026-09-21T12:00:{sequence:02d}Z"
            if measured_at is _UNSET
            else measured_at
        ),
        "distance_mm": 1000 + sequence if distance_mm is _UNSET else distance_mm,
        "echo_us": 3746,
        "valid": valid,
        "error_code": None,
    }


def _chain(count: int, **overrides) -> list[dict]:
    """Chuỗi `count` mẫu hợp lệ liên tiếp: sequence 1..count."""
    return [_row(i + 1, **overrides) for i in range(count)]


@pytest.fixture
def config() -> WindowConfig:
    return WindowConfig.from_dict(_MODEL_CONFIG_DICT)


# ------------------------------------------------------------------ config


def test_config_from_project_yaml(config):
    """WindowConfig.from_dict khớp configs/model.yaml thật của dự án."""
    raw = yaml.safe_load(
        (REPO_ROOT / "configs" / "model.yaml").read_text(encoding="utf-8")
    )
    from_yaml = WindowConfig.from_dict(raw)
    assert from_yaml.window_size == 10
    assert from_yaml.delta_s == pytest.approx(0.1)
    assert from_yaml.horizons_ms == (100, 200, 300, 400, 500)
    assert from_yaml.max_time_jitter_s == pytest.approx(0.03)
    assert from_yaml.max_gap_s == pytest.approx(0.25)
    assert from_yaml.target_offsets == (1, 2, 3, 4, 5)


def test_config_rejects_non_multiple_horizon(config):
    bad = dict(_MODEL_CONFIG_DICT, horizons_ms=[100, 150, 300, 400, 500])
    with pytest.raises(ValueError, match="bội nguyên"):
        WindowConfig.from_dict(bad).target_offsets


# ------------------------------------------------------- chuỗi chuẩn (run)


def test_fifteen_samples_one_window(config):
    """15 mẫu chuẩn → 1 cửa sổ: X = mẫu 1..10, y = mẫu 11..15, không drop."""
    windows, stats = extract_windows(_chain(15), config)
    assert stats == WindowStats(
        n_windows=1,
        n_dropped_error=0,
        n_dropped_gap=0,
        n_dropped_jitter=0,
        n_dropped_short=0,
    )
    (w,) = windows
    assert isinstance(w, Window)
    assert w.features == pytest.approx([(1000 + i) / 1000 for i in range(1, 11)])
    assert w.targets == pytest.approx([(1000 + i) / 1000 for i in range(11, 16)])
    assert w.meta.device_id == "esp32_01"
    assert w.meta.boot_id == "b1"
    assert w.meta.session_id == "s1"
    assert w.meta.sequence_first == 1
    assert w.meta.sequence_last == 10
    assert w.meta.sequence_target_last == 15
    assert w.meta.measured_at == "2026-09-21T12:00:10Z"


def test_sixteen_samples_two_windows(config):
    """16 mẫu → 2 cửa sổ (kết thúc tại mẫu 10 và 11), trượt 1 mẫu."""
    windows, stats = extract_windows(_chain(16), config)
    assert stats.n_windows == 2
    assert stats.n_dropped_short == 0
    assert [w.meta.sequence_first for w in windows] == [1, 2]
    assert [w.meta.sequence_last for w in windows] == [10, 11]
    assert [w.meta.sequence_target_last for w in windows] == [15, 16]


def test_input_reverse_order_same_result(config):
    """Hàm tự sắp theo sequence — đảo ngược input cho cùng kết quả."""
    rows = _chain(15)[::-1]
    windows, stats = extract_windows(rows, config)
    assert stats.n_windows == 1
    assert windows[0].meta.sequence_first == 1
    assert windows[0].meta.sequence_target_last == 15


def test_empty_rows(config):
    assert extract_windows([], config) == (
        [],
        WindowStats(0, 0, 0, 0, 0),
    )


# ---------------------------------------------------------- cắt chuỗi (cut)


@pytest.mark.parametrize(
    ("field", "value"),
    [("valid", False), ("distance_mm", None)],
    ids=["valid_false", "missing_distance"],
)
def test_invalid_sample_cuts_chain_error(config, field, value):
    """Mẫu lỗi giữa chuỗi → cắt, drop lý do 'error'; run sau 9 mẫu → short."""
    rows = _chain(15)
    rows[5][field] = value  # mẫu sequence 6
    windows, stats = extract_windows(rows, config)
    assert stats.n_windows == 0
    assert stats.n_dropped_error == 1
    assert stats.n_dropped_gap == 0
    assert stats.n_dropped_jitter == 0
    assert stats.n_dropped_short == 1  # run sau (mẫu 7..15 = 9 mẫu) không đủ


def test_gap_cuts_chain(config):
    """dt = 0.5 s > max_gap_s (0.25) → cắt, lý do 'gap'."""
    rows = _chain(15)
    # Dịch mono toàn bộ phần sau điểm đứt: dt 7→8 = 0.5 s, sau đó nhịp chuẩn 0.1 s.
    for r in rows[7:]:
        r["monotonic_us"] += 400_000
    windows, stats = extract_windows(rows, config)
    assert stats.n_windows == 0
    assert stats.n_dropped_gap == 1
    assert stats.n_dropped_error == 0
    assert stats.n_dropped_jitter == 0
    assert stats.n_dropped_short == 1  # run sau (mẫu 8..15 = 8 mẫu) không đủ


def test_jitter_cuts_chain(config):
    """dt = 0.15 s: chưa vượt gap (0.25) nhưng lệch nhịp quá jitter (0.03) → 'jitter'."""
    rows = _chain(15)
    # Dịch mono toàn bộ phần sau điểm lệch: dt 7→8 = 0.15 s, sau đó nhịp chuẩn.
    for r in rows[7:]:
        r["monotonic_us"] += 50_000
    windows, stats = extract_windows(rows, config)
    assert stats.n_windows == 0
    assert stats.n_dropped_jitter == 1
    assert stats.n_dropped_gap == 0
    assert stats.n_dropped_error == 0
    assert stats.n_dropped_short == 1


def test_missing_sample_sequence_jump_is_gap(config):
    """Mất mẫu 8 (dt = 0.2 s vẫn <= max_gap) nhưng sequence không +1 → 'gap'."""
    rows = _chain(15)
    del rows[7]
    windows, stats = extract_windows(rows, config)
    assert stats.n_windows == 0
    assert stats.n_dropped_gap == 1
    assert stats.n_dropped_error == 0
    assert stats.n_dropped_jitter == 0


def test_short_chain_12_samples(config):
    """12 mẫu: đủ X (10) thiếu y (cần 15) → 0 cửa sổ, lý do 'short'."""
    windows, stats = extract_windows(_chain(12), config)
    assert stats.n_windows == 0
    assert stats.n_dropped_short == 1
    assert stats.n_dropped_error == 0
    assert stats.n_dropped_gap == 0
    assert stats.n_dropped_jitter == 0


def test_stats_mixed_reasons(config):
    """Lỗi + gap + run đủ 15 mẫu: đếm đúng từng loại, run đủ ra 1 cửa sổ."""
    rows = _chain(3)
    rows.append(_row(4, valid=False))  # error → cắt run 1 (3 mẫu)
    rows += [_row(s) for s in range(5, 8)]  # run 2: mẫu 5..7
    # Gap tại 7→9: dt = 0.7 s (> max_gap); run 3 = 15 mẫu (9..23), mono dịch
    # đồng bộ +500_000 giữ nhịp nội bộ chuẩn 0.1 s.
    rows += [_row(s, monotonic_us=_mono_us(s) + 500_000) for s in range(9, 24)]
    windows, stats = extract_windows(rows, config)
    assert stats.n_windows == 1
    assert stats.n_dropped_error == 1
    assert stats.n_dropped_gap == 1
    assert stats.n_dropped_jitter == 0
    assert stats.n_dropped_short == 0
    assert windows[0].meta.sequence_first == 9
    assert windows[0].meta.sequence_target_last == 23


def test_run_cut_after_full_window_still_yields_windows(config):
    """Run đủ 15 mẫu rồi gặp mẫu lỗi → cửa sổ trong run vẫn được tạo."""
    rows = _chain(16)
    rows[15]["valid"] = False  # lỗi sau khi run 1 đã đủ 15 mẫu
    windows, stats = extract_windows(rows, config)
    assert stats.n_windows == 1
    assert stats.n_dropped_error == 1
    assert windows[0].meta.sequence_target_last == 15


# ------------------------------------------------------- ranh giới nhóm


@pytest.mark.parametrize("field", ["session_id", "boot_id", "device_id"])
def test_different_group_not_merged(config, field):
    """30 mẫu xen kẽ 2 giá trị của field → không ghép: mỗi nhóm tự có cửa sổ."""
    rows: list[dict] = []
    for i in range(15):
        rows.append(_row(i + 1, **{field: "A"}))
        rows.append(_row(i + 1, **{field: "B"}))
    windows, stats = extract_windows(rows, config)
    assert stats.n_windows == 2
    assert {getattr(w.meta, field) for w in windows} == {"A", "B"}
    for w in windows:
        assert w.meta.sequence_first == 1
        assert w.meta.sequence_target_last == 15


def test_null_session_is_own_group(config):
    """session_id=None là nhóm riêng: không ghép với session có id."""
    rows: list[dict] = []
    for i in range(15):
        rows.append(_row(i + 1, session_id=None))
        rows.append(_row(i + 1, session_id="s1"))
    windows, stats = extract_windows(rows, config)
    assert stats.n_windows == 2
    assert {w.meta.session_id for w in windows} == {None, "s1"}


# ------------------------------------------------------------- meta / feature


def test_measured_at_null_kept_as_none(config):
    """measured_at=None → meta giữ None (không bịa); có giá trị → giữ nguyên mẫu cuối X."""
    rows_null = _chain(15)
    for r in rows_null:
        r["measured_at"] = None
    windows, _ = extract_windows(rows_null, config)
    assert windows[0].meta.measured_at is None

    rows = _chain(15)
    windows, _ = extract_windows(rows, config)
    assert windows[0].meta.measured_at == rows[9]["measured_at"]


def test_feature_order_oldest_to_newest(config):
    """d0 cũ nhất → d9 mới nhất, đúng distance_mm/1000 (mẫu 1..10 tăng dần)."""
    windows, _ = extract_windows(_chain(15), config)
    feats = windows[0].features
    assert len(feats) == 10
    assert feats[0] == pytest.approx(1001 / 1000)
    assert feats[-1] == pytest.approx(1010 / 1000)
    assert all(a < b for a, b in zip(feats, feats[1:]))




# ------------------------------------------------------------- sqlite3.Row


def test_extract_from_sqlite3_rows(config):
    """Input sqlite3.Row (shape như iot_backend/ingestion/store) hoạt động như dict."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE t (device_id TEXT, boot_id TEXT, session_id TEXT, "
        "sequence INTEGER, monotonic_us INTEGER, measured_at TEXT, "
        "distance_mm INTEGER, valid INTEGER)"
    )
    for r in _chain(15):
        conn.execute(
            "INSERT INTO t VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                r["device_id"], r["boot_id"], r["session_id"], r["sequence"],
                r["monotonic_us"], r["measured_at"], r["distance_mm"],
                int(r["valid"]),
            ),
        )
    db_rows = conn.execute(
        "SELECT * FROM t ORDER BY device_id, boot_id, sequence"
    ).fetchall()
    windows, stats = extract_windows(db_rows, config)
    assert stats.n_windows == 1
    assert windows[0].features[0] == pytest.approx(1001 / 1000)
    assert windows[0].meta.session_id == "s1"


def test_rows_without_session_id_column(config):
    """Row thiếu cột session_id (bảng raw_samples thuần) → nhóm None, vẫn window được."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE raw (device_id TEXT, boot_id TEXT, sequence INTEGER, "
        "monotonic_us INTEGER, measured_at TEXT, distance_mm INTEGER, valid INTEGER)"
    )
    for r in _chain(15):
        conn.execute(
            "INSERT INTO raw VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                r["device_id"], r["boot_id"], r["sequence"], r["monotonic_us"],
                r["measured_at"], r["distance_mm"], int(r["valid"]),
            ),
        )
    db_rows = conn.execute("SELECT * FROM raw ORDER BY sequence").fetchall()
    windows, stats = extract_windows(db_rows, config)
    assert stats.n_windows == 1
    assert windows[0].meta.session_id is None
