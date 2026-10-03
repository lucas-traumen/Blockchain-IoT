"""Kiểm thử M10:data — dataset LSTM, split chống leakage, scaler train-only,
hash/manifest helpers (ai_model/prepare.py + ai_model/preprocessing.py).

Contract kiểm tra (PLAN mục 4 + 7, dòng M10):

- split theo DANH SÁCH session trước khi tạo/tập hợp tensor; mỗi session đúng
  một split; session thiếu/None bị từ chối;
- X ``(N, 10, 2)`` từ ``windows_to_lstm_array`` (M9) và y ``(N, 5)`` mét,
  cùng thứ tự windows; KHÔNG shuffle trước split;
- scaler fit CHỈ trên train; val/test outlier không đổi mean/std;
  transform/inverse round-trip; nonfinite/empty/zero-std bị từ chối;
  scaler.json round-trip số thuần, ``fitted_on: train_only``,
  ``preprocessing_version: pp-lstm-v1``;
- SHA-256 file/JSON canonical deterministic; split manifest tái lập khi
  created_at cố định, không timestamp tự sinh trong hash;
- module preprocessing không import sklearn/TensorFlow/pandas/pickle;
- RF dataset tests cũ (tests/test_dataset.py) vẫn pass nguyên trạng.

Dataset test sinh trong ``tmp_path`` — không artifact nào được ghi vào repo.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import numpy as np
import pytest
import yaml

from ai_model import preprocessing
from ai_model.features import LSTM_FEATURE_NAMES
from ai_model.prepare import (
    SPLIT_NAMES,
    build_dataset_split_manifest,
    build_lstm_dataset,
    build_split_manifest,
    normalize_rows,
    split_by_session,
    validate_split,
)
from ai_model.preprocessing import (
    FITTED_ON,
    PP_LSTM_VERSION,
    LSTMScaler,
    canonical_json,
    dataset_sha256,
    scaler_json_sha256,
    sha256_bytes,
    sha256_file,
    sha256_json,
    split_manifest_sha256,
    write_scaler_json,
    write_split_manifest,
)
from ai_model.windows import WindowConfig, extract_windows

REPO_ROOT = Path(__file__).resolve().parents[1]

# Khớp khối windowing dùng chung của configs/model.yaml.
_MODEL_CONFIG_DICT = {
    "window_size": 10,
    "delta_s": 0.1,
    "horizons_ms": [100, 200, 300, 400, 500],
    "max_time_jitter_s": 0.03,
    "max_gap_s": 0.25,
}

N_SESSIONS = 6
N_SAMPLES = 40  # mỗi session: 40 - 14 = 26 cửa sổ


@pytest.fixture(scope="module")
def window_config() -> WindowConfig:
    return WindowConfig.from_dict(_MODEL_CONFIG_DICT)


@pytest.fixture(scope="module")
def model_config() -> dict:
    """Config YAML thật của repo (không copy số liệu vào test)."""
    return yaml.safe_load(
        (REPO_ROOT / "configs" / "model.yaml").read_text(encoding="utf-8")
    )


def _session_rows(
    session_id: str,
    n: int = N_SAMPLES,
    start_mm: float = 1000.0,
    step_mm: float = 1.0,
    scenario: str = "approach",
    speed_level: str | None = "v02",
    target_type: str | None = "box",
) -> list[dict]:
    """Session chuẩn: sequence 1..n, Δ=0.1s đều, distance tăng đều, hợp lệ."""
    return [
        {
            "session_id": session_id,
            "device_id": "esp32_01",
            "boot_id": f"boot_{session_id}",
            "sequence": i + 1,
            "monotonic_us": 10_000_000 + i * 100_000,
            "measured_at": None,
            "time_synced": False,
            "distance_mm": start_mm + i * step_mm,
            "echo_us": 3000,
            "valid": True,
            "error_code": None,
            **({"scenario": scenario} if scenario is not None else {}),
            **({"speed_level": speed_level} if speed_level is not None else {}),
            **({"target_type": target_type} if target_type is not None else {}),
        }
        for i in range(n)
    ]


# ------------------------------------------------------- normalize_rows


def test_normalize_rows_extracts_metadata_keeps_raw_errors():
    rows = _session_rows("s1", n=8)
    rows[2]["valid"] = False
    rows[2]["distance_mm"] = None
    records = normalize_rows(rows)
    assert len(records) == 8  # raw lỗi VẪN GIỮ — windowing mới bỏ
    rec = records[0]
    assert set(rec) == {"row", "meta"}
    assert rec["row"]["session_id"] == "s1"  # session_id giữ trong row
    assert rec["meta"] == {
        "scenario": "approach",
        "speed_level": "v02",
        "target_type": "box",
    }
    # Metadata được tách KHỎI row (không trộn vào payload).
    assert "scenario" not in rec["row"]
    # Input không mutate.
    assert "scenario" in rows[0] and rows[0]["scenario"] == "approach"
    assert records[2]["row"]["valid"] is False


def test_normalize_rows_rejects_missing_or_none_session():
    bad = _session_rows("s1", n=3)
    del bad[1]["session_id"]
    with pytest.raises(ValueError, match="session_id"):
        normalize_rows(bad)
    bad2 = _session_rows("s2", n=3)
    bad2[0]["session_id"] = None
    with pytest.raises(ValueError, match="session_id"):
        normalize_rows(bad2)
    with pytest.raises(ValueError, match="mapping"):
        normalize_rows(["khong_phai_dict"])


# --------------------------------------------- split trước windowing


def test_build_lstm_dataset_shapes_and_alignment(window_config, model_config):
    rows = [r for i in range(N_SESSIONS) for r in _session_rows(f"s{i}")]
    ds = build_lstm_dataset(rows, window_config, model_config=model_config, seed=42)

    expected_windows = N_SAMPLES - 14  # window 10 + target offset 5
    for name in SPLIT_NAMES:
        part = ds.splits[name]
        assert part.X.shape == (part.n_windows, 10, 2)
        assert part.y.shape == (part.n_windows, 5)
        assert part.y.dtype == np.float64
        assert part.X.dtype == np.float64
        # n_rows khớp số mẫu của các session trong split (invariant thật).
        assert part.n_rows == N_SAMPLES * len(part.session_ids)
        # Alignment: window đầu của split có đúng meta + feature.
        w0 = part.windows[0]
        assert np.allclose(part.X[0][:, 0], w0.features)
        assert part.X[0][0][1] == 0.0  # delta[0] = 0 (contract M9)
        assert np.allclose(part.y[0], w0.targets)
        assert part.stats.n_windows == expected_windows * len(part.session_ids)
        assert part.dropped == {"error": 0, "gap": 0, "jitter": 0, "short": 0}

    # Tổng windows + n_rows khớp toàn bộ dữ liệu.
    assert sum(p.n_windows for p in ds.splits.values()) == N_SESSIONS * expected_windows
    assert sum(p.n_rows for p in ds.splits.values()) == N_SESSIONS * N_SAMPLES

    # Metadata session giữ cho M12 breakdown.
    all_meta = {}
    for part in ds.splits.values():
        all_meta.update(part.session_meta)
    assert set(all_meta) == {f"s{i}" for i in range(N_SESSIONS)}
    assert all_meta["s0"]["scenario"] == "approach"
    assert all_meta["s0"]["speed_level"] == "v02"


def test_split_no_leakage_with_near_identical_windows(window_config):
    """Sessions có windows gần GIỐNG HỆT nhau (dữ liệu trùng, chỉ khác
    session/boot): nếu windowing/split sai thứ tự thì window của session này
    có thể lọt split của session khác. Mỗi window phải thuộc đúng split của
    session sinh ra nó."""
    # s_even/s_odd có dữ liệu giống nhau 100% — phân biệt chỉ bằng session_id.
    rows = []
    for i in range(N_SESSIONS):
        rows.extend(_session_rows(f"s{i}", start_mm=1000.0, step_mm=1.0))
    ds = build_lstm_dataset(rows, window_config, seed=42)

    split_of_session = {
        name: ds.splits[name].session_ids for name in SPLIT_NAMES
    }
    # Không giao, union đủ, mỗi session đúng một split.
    counts = validate_split(
        {n: set(split_of_session[n]) for n in SPLIT_NAMES},
        expected_sessions={f"s{i}" for i in range(N_SESSIONS)},
    )
    assert sum(counts.values()) == N_SESSIONS
    for name in SPLIT_NAMES:
        part = ds.splits[name]
        # Mọi window trong split thuộc session của ĐÚNG split đó.
        for w in part.windows:
            assert w.meta.session_id in part.session_ids
        # Không window nào của session split khác lọt vào đây.
        other_sessions = set()
        for other in SPLIT_NAMES:
            if other != name:
                other_sessions |= split_of_session[other]
        assert {w.meta.session_id for w in part.windows}.isdisjoint(other_sessions)


def test_build_lstm_dataset_deterministic(window_config):
    """Cùng seed → cùng split, cùng tensor, cùng manifest hash; seed khác →
    split khác (đã xáo trộn thật)."""
    rows = [r for i in range(N_SESSIONS) for r in _session_rows(f"s{i}")]
    ds1 = build_lstm_dataset(rows, window_config, seed=42)
    ds2 = build_lstm_dataset(rows, window_config, seed=42)
    assert split_manifest_sha256(ds1.split_manifest) == split_manifest_sha256(
        ds2.split_manifest
    )
    for name in SPLIT_NAMES:
        assert ds1.splits[name].session_ids == ds2.splits[name].session_ids
        assert np.array_equal(ds1.splits[name].X, ds2.splits[name].X)

    ds3 = build_lstm_dataset(rows, window_config, seed=7)
    different = any(
        ds1.splits[name].session_ids != ds3.splits[name].session_ids
        for name in SPLIT_NAMES
    )
    assert different, "seed khác phải cho split khác (nếu không là chưa shuffle)"


def test_build_lstm_dataset_rejects_orphan_rows(window_config):
    """Row không session_id → ValueError (không lọt split)."""
    rows = _session_rows("s1", n=20)
    del rows[0]["session_id"]
    with pytest.raises(ValueError, match="session_id"):
        build_lstm_dataset(rows, window_config, seed=42)
    with pytest.raises(ValueError):
        build_lstm_dataset([], window_config, seed=42)


def test_build_lstm_dataset_accepts_record_form(window_config, model_config):
    """Records ``{"row", "meta"}`` (ví dụ từ normalize_rows) dùng được trực tiếp."""
    records = normalize_rows(
        [r for i in range(6) for r in _session_rows(f"s{i}", n=20)]
    )
    ds = build_lstm_dataset(records, window_config, model_config=model_config, seed=42)
    assert {s for p in ds.splits.values() for s in p.session_ids} == {
        f"s{i}" for i in range(6)
    }


def test_validate_split_checks():
    ok = {"train": {"a", "b"}, "val": {"c"}, "test": {"d"}}
    assert validate_split(ok, expected_sessions={"a", "b", "c", "d"}) == {
        "train": 2,
        "val": 1,
        "test": 1,
    }
    with pytest.raises(ValueError, match="leakage"):
        validate_split({"train": {"a"}, "val": {"a"}, "test": set()})
    with pytest.raises(ValueError, match="thiếu split"):
        validate_split({"train": {"a"}})
    with pytest.raises(ValueError, match="lệch tập"):
        validate_split(ok, expected_sessions={"a", "b", "c", "d", "e"})
    # Backward-compat: split_by_session cũ + build_split_manifest cũ vẫn chạy.
    keys = [{"session_label": f"sess_{i}"} for i in range(10)]
    old = split_by_session(keys, seed=42)
    assert (len(old["train"]), len(old["val"]), len(old["test"])) == (7, 2, 1)
    manifest = build_split_manifest(old, seed=42, created_at="2026-01-01T00:00:00.000Z")
    assert manifest["n_sessions"] == 10


# ------------------------------------------------------------ scaler


@pytest.fixture(scope="module")
def fitted_data(window_config, model_config):
    rows = [r for i in range(N_SESSIONS) for r in _session_rows(f"s{i}")]
    ds = build_lstm_dataset(rows, window_config, model_config=model_config, seed=42)
    return ds


def test_scaler_fit_train_only_and_outlier_immune(fitted_data):
    ds = fitted_data
    scaler = LSTMScaler.fit(ds.train.X, ds.train.y)

    # Tham số khớp tính tay TRÊN CHỈ train (kênh distance + delta + target).
    dist = ds.train.X[:, :, 0]
    delta = ds.train.X[:, :, 1]
    assert scaler.means["distance_m"] == pytest.approx(float(dist.mean()))
    assert scaler.stds["distance_m"] == pytest.approx(float(dist.std()))
    assert scaler.means["delta_distance_m"] == pytest.approx(float(delta.mean()))
    assert scaler.stds["delta_distance_m"] == pytest.approx(float(delta.std()))
    assert scaler.means["target_distance_m"] == pytest.approx(float(ds.train.y.mean()))
    assert scaler.stds["target_distance_m"] == pytest.approx(float(ds.train.y.std()))
    assert scaler.n_samples_fit == len(ds.train.X)

    # Outlier val/test KHÔNG đổi tham số: fit lại trên train + outlier y hệt.
    outlier = ds.val.X.copy()
    outlier[:, :, 0] += 100.0
    outlier_y = ds.val.y + 100.0
    combined_X = np.concatenate([ds.train.X, outlier], axis=0)
    combined_y = np.concatenate([ds.train.y, outlier_y], axis=0)
    polluted = LSTMScaler.fit(combined_X, combined_y)
    assert polluted.means != scaler.means  # outlier SẼ đổi mean nếu bị fit
    # Scaler pipeline (fit trên ds.train duy nhất) vẫn giữ tham số gốc:
    assert scaler.means == LSTMScaler.fit(ds.train.X, ds.train.y).means
    # transform của val/test dùng tham số train-only, không bị outlier kéo.
    transformed = scaler.transform_X(ds.val.X)
    expected = (ds.val.X[:, :, 0] - scaler.means["distance_m"]) / scaler.stds[
        "distance_m"
    ]
    assert np.allclose(transformed[:, :, 0], expected)


def test_scaler_transform_inverse_round_trip(fitted_data):
    ds = fitted_data
    scaler = LSTMScaler.fit(ds.train.X, ds.train.y)

    x_in = ds.train.X
    x_before = x_in.copy()
    xt = scaler.transform_X(x_in)
    assert np.allclose(x_in, x_before)  # không mutate input
    assert np.allclose(xt[:, :, 0].mean(), 0.0, atol=1e-12)
    assert np.allclose(xt[:, :, 0].std(), 1.0)
    # Round-trip X bằng inverse thủ công.
    x_back = xt * np.array(
        [scaler.stds["distance_m"], scaler.stds["delta_distance_m"]]
    ) + np.array(
        [scaler.means["distance_m"], scaler.means["delta_distance_m"]]
    )
    assert np.allclose(x_back, x_in)

    y_in = ds.train.y
    yt = scaler.transform_y(y_in)
    assert np.allclose(scaler.inverse_y(yt), y_in)
    # inverse của dự báo trả về mét (đơn vị SI) — không round.
    inv = scaler.inverse_y(yt)
    assert np.allclose(inv, y_in)


def test_scaler_rejects_nonfinite_empty_zero_std():
    x = np.linspace(0.5, 0.6, 20).reshape(2, 10, 1) * np.ones((1, 1, 2))
    y = np.linspace(0.5, 0.7, 10).reshape(2, 5)

    with pytest.raises(ValueError, match="rỗng"):
        LSTMScaler.fit(np.zeros((0, 10, 2)), np.zeros((0, 5)))
    x_nan = x.copy()
    x_nan[0, 0, 0] = np.nan
    with pytest.raises(ValueError, match="không hữu hạn"):
        LSTMScaler.fit(x_nan, y)
    y_inf = y.copy()
    y_inf[1, 4] = np.inf
    with pytest.raises(ValueError, match="không hữu hạn"):
        LSTMScaler.fit(x, y_inf)
    with pytest.raises(ValueError, match="shape"):
        LSTMScaler.fit(x.reshape(20, 2), y)

    # std = 0 → policy ValueError (không thay 1.0 âm thầm).
    # 0.5 biểu diễn chính xác trong float64 → std đúng bằng 0 (0.7 sinh
    # nhiễu ~1e-16 do mean không thể hiện chính xác).
    flat = np.full((2, 10, 2), 0.5)
    with pytest.raises(ValueError, match="std"):
        LSTMScaler.fit(flat, y)
    # y hằng nhưng X biến thiên → cũng từ chối (target std = 0).
    with pytest.raises(ValueError, match="std"):
        LSTMScaler.fit(x, np.full((2, 5), 0.9))

    scaler = LSTMScaler.fit(x, y)
    with pytest.raises(ValueError, match="không hữu hạn"):
        scaler.transform_X(np.full((1, 10, 2), np.nan))
    with pytest.raises(ValueError, match="không hữu hạn"):
        scaler.transform_y(np.array([[np.nan] * 5]))
    with pytest.raises(ValueError, match="không hữu hạn"):
        scaler.inverse_y(np.array([[np.inf] * 5]))


def test_scaler_json_round_trip(fitted_data, tmp_path):
    ds = fitted_data
    scaler = LSTMScaler.fit(ds.train.X, ds.train.y)

    d = scaler.to_dict()
    assert d["fitted_on"] == "train_only"
    assert d["preprocessing_version"] == PP_LSTM_VERSION == "pp-lstm-v1"
    assert d["features"] == LSTM_FEATURE_NAMES == ["distance_m", "delta_distance_m"]
    assert d["target"] == "target_distance_m"
    # Số thuần: mọi giá trị là int/float Python (JSON-safe, không numpy).
    for section in ("means", "stds"):
        for value in d[section].values():
            assert isinstance(value, float)
    json.dumps(d)  # serialize được bằng json thuần

    scaler2 = LSTMScaler.from_dict(json.loads(json.dumps(d)))
    x = ds.val.X
    assert np.array_equal(scaler2.transform_X(x), scaler.transform_X(x))
    assert np.array_equal(scaler2.inverse_y(scaler.transform_y(ds.val.y)),
                          scaler.inverse_y(scaler.transform_y(ds.val.y)))

    path = tmp_path / "scaler.json"
    sha = scaler.save(path)
    assert sha == sha256_file(path)
    loaded = LSTMScaler.load(path)
    assert loaded.means == scaler.means
    assert loaded.stds == scaler.stds

    # Sai version / fitted_on / features → ValueError rõ ràng.
    bad = dict(d, preprocessing_version="pp-v1")
    with pytest.raises(ValueError, match="preprocessing_version"):
        LSTMScaler.from_dict(bad)
    with pytest.raises(ValueError, match="fitted_on"):
        LSTMScaler.from_dict(dict(d, fitted_on="all"))
    with pytest.raises(ValueError, match="features"):
        LSTMScaler.from_dict(dict(d, features=["a", "b"]))
    with pytest.raises(ValueError, match="std"):
        LSTMScaler.from_dict(
            dict(d, stds=dict(d["stds"], distance_m=0.0))
        )


# ------------------------------------------------- hash / manifest helpers


def test_sha_helpers_deterministic(tmp_path):
    data = b"hrc-dataset-bytes"
    assert sha256_bytes(data) == hashlib.sha256(data).hexdigest()
    assert len(sha256_bytes(data)) == 64

    f = tmp_path / "dataset.jsonl"
    f.write_bytes(data + data)
    assert sha256_file(f) == hashlib.sha256(data + data).hexdigest()
    assert dataset_sha256(f) == sha256_file(f)  # không bịa, hash file thật

    # Canonical JSON: thứ tự chèn key không đổi hash; NaN/inf bị từ chối
    # bởi json.dumps mặc định (allow_nan) — dùng số thuần trong manifest.
    a = {"b": 1, "a": [1, 2], "c": {"z": True, "y": "ń"}}
    b = {"c": {"y": "ń", "z": True}, "a": [1, 2], "b": 1}
    assert canonical_json(a) == canonical_json(b)
    assert sha256_json(a) == sha256_json(b)
    assert sha256_json(a) == hashlib.sha256(canonical_json(a).encode()).hexdigest()
    assert scaler_json_sha256(a) == sha256_json(a)


def test_split_manifest_deterministic_and_required_fields(fitted_data):
    ds = fitted_data
    m = ds.split_manifest
    # Required fields theo M10.
    assert m["preprocessing_version"] == PP_LSTM_VERSION
    assert m["seed"] == 42
    assert m["ratios"] == [0.7, 0.15, 0.15]
    assert m["n_sessions"] == N_SESSIONS
    assert m["n_rows"] == N_SESSIONS * N_SAMPLES
    assert m["n_windows"] == sum(p.n_windows for p in ds.splits.values())
    assert set(m["sessions"]) == set(SPLIT_NAMES)
    for name in SPLIT_NAMES:
        assert m["sessions"][name] == sorted(ds.splits[name].session_ids)
        info = m["splits"][name]
        assert info["n_sessions"] == len(ds.splits[name].session_ids)
        assert info["n_rows"] == ds.splits[name].n_rows
        assert info["n_windows"] == ds.splits[name].n_windows
        assert info["dropped"] == ds.splits[name].dropped
    # Không timestamp tự sinh — hash tái lập được.
    assert "created_at" not in m
    assert split_manifest_sha256(m) == split_manifest_sha256(
        json.loads(json.dumps(m))
    )

    # created_at chỉ nhúng khi truyền tường minh → hash đổi theo nó.
    m2 = build_dataset_split_manifest(
        split_counts=validate_split(
            {n: ds.splits[n].session_ids for n in SPLIT_NAMES}
        ),
        sessions={n: ds.splits[n].session_ids for n in SPLIT_NAMES},
        split_info={
            n: {
                "n_rows": ds.splits[n].n_rows,
                "n_windows": ds.splits[n].n_windows,
                "dropped": ds.splits[n].dropped,
            }
            for n in SPLIT_NAMES
        },
        seed=42,
        created_at="2026-01-01T00:00:00.000Z",
    )
    assert m2["created_at"] == "2026-01-01T00:00:00.000Z"
    assert split_manifest_sha256(m2) != split_manifest_sha256(m)
    # Shape (N,10,2) khớp contract — ghi vào manifest qua M11 được kiểm ở đây.
    assert ds.train.X.shape[1:] == (10, 2)
    assert ds.train.y.shape[1] == 5


def test_write_split_manifest_file_hash_matches(tmp_path, fitted_data):
    ds = fitted_data
    path = tmp_path / "split_manifest.json"
    sha = write_split_manifest(ds.split_manifest, path)
    assert sha == split_manifest_sha256(ds.split_manifest)
    assert sha == sha256_file(path)
    assert json.loads(path.read_text(encoding="utf-8")) == ds.split_manifest
    # write_scaler_json tương tự.
    scaler = LSTMScaler.fit(ds.train.X, ds.train.y)
    sp = tmp_path / "scaler.json"
    sha2 = write_scaler_json(scaler.to_dict(), sp)
    assert sha2 == scaler_json_sha256(scaler.to_dict()) == sha256_file(sp)


# --------------------------------------------------- hygiene của module


def test_preprocessing_module_import_hygiene():
    """preprocessing.py không import sklearn/TensorFlow/pandas/pickle —
    artifact scaler phải là JSON số thuần, không pickle; module dùng được
    ở runtime backend không kéo dependency nặng."""
    source = (REPO_ROOT / "ai_model" / "preprocessing.py").read_text(
        encoding="utf-8"
    )
    for banned in ("sklearn", "tensorflow", "keras", "pandas", "pickle", "joblib"):
        assert not re.search(
            rf"^\s*(import|from)\s+{re.escape(banned)}\b", source, re.M
        ), f"preprocessing.py import {banned}"
    # Nạp module không kéo các thư viện đó vào sys.modules.
    fresh = {
        name
        for name in list(sys.modules)
        if name.split(".")[0] in {"sklearn", "tensorflow", "pandas", "joblib"}
    }
    import importlib

    importlib.reload(preprocessing)
    leaked = {
        name
        for name in sys.modules
        if name.split(".")[0] in {"sklearn", "tensorflow", "pandas", "joblib"}
    } - fresh
    assert not leaked, f"preprocessing kéo dependency cấm: {sorted(leaked)}"


def test_model_yaml_versions_match_constants(model_config):
    """pp-lstm-v1 khớp configs/model.yaml (không hardcode lệch nhau)."""
    assert model_config["lstm"]["preprocessing_version"] == PP_LSTM_VERSION
    assert model_config["lstm"]["features"] == LSTM_FEATURE_NAMES


# ------------------------------------ MAJOR-1: JSONL record-form + session inject


def _make_jsonl_dataset(tmp_path, n_sessions=4, n_samples=40, seed=42):
    """Sinh JSONL synthetic qua generator thật, trả records từ
    ``load_jsonl_dataset`` (record-form: row KHÔNG có session_id)."""
    from ai_model.prepare import load_jsonl_dataset
    from scripts.synthetic_dataset import generate_sessions, write_jsonl

    lines = generate_sessions(n_sessions, n_samples, seed, scenario="all")
    path = tmp_path / "sim_m10.jsonl"
    write_jsonl(lines, path)
    return load_jsonl_dataset(path)


def test_jsonl_records_session_label_injected_no_cross_session(tmp_path, window_config):
    """Record-form từ ``load_jsonl_dataset``: payload không có session_id.
    boot_id bị TÁI SỬ DỤNG giữa sessions (bắt buộc tình huống leakage của
    MAJOR-1): nếu không inject session_id, extract_windows nhóm theo
    ``(None, device, boot)`` và nối window QUA session. Sau fix: mỗi
    Window.meta.session_id = session_label, thuộc đúng split của nó, không
    cross session; metadata alignment giữ nguyên."""
    records = _make_jsonl_dataset(tmp_path, n_sessions=6)
    labels = sorted({rec["meta"]["session_label"] for rec in records})
    scenario_of = {
        rec["meta"]["session_label"]: rec["meta"]["scenario"] for rec in records
    }
    # Tái sử dụng boot_id giữa sessions — dữ liệu phân biệt CHỈ bằng nhãn.
    for rec in records:
        rec["row"]["boot_id"] = "shared_boot"
    rows_before = [dict(rec["row"]) for rec in records]

    ds = build_lstm_dataset(records, window_config, model_config=None, seed=42)

    # Input không mutate: row gốc vẫn không có session_id.
    for rec, before in zip(records, rows_before):
        assert rec["row"] == before
        assert "session_id" not in rec["row"]

    # Mỗi window có session_id = session_label và thuộc ĐÚNG split của nó.
    seen_windows = 0
    for name in SPLIT_NAMES:
        part = ds.splits[name]
        split_labels = {
            rec["meta"]["session_label"] for rec in records
            if rec["meta"]["session_label"] in part.session_ids
        }
        window_sessions = {w.meta.session_id for w in part.windows}
        assert window_sessions == split_labels  # đủ windows cho mọi session
        assert window_sessions.isdisjoint(
            {l for l in labels} - split_labels
        )  # không cross session
        seen_windows += len(part.windows)
    assert seen_windows == sum(
        p.n_windows for p in ds.splits.values()
    )
    # Metadata alignment: session_meta đủ nhãn + scenario khớp JSONL.
    all_meta = {}
    for part in ds.splits.values():
        all_meta.update(part.session_meta)
    assert set(all_meta) == set(labels)
    for label in labels:
        assert all_meta[label]["scenario"] == scenario_of[label]


def test_jsonl_conflicting_session_id_vs_session_label_rejected(tmp_path, window_config):
    """Row có session_id LỆCH meta.session_label → ValueError (không âm thầm
    chọn bên nào)."""
    records = _make_jsonl_dataset(tmp_path, n_sessions=2)
    rec = records[0]
    label = rec["meta"]["session_label"]
    rec["row"]["session_id"] = label + "_khat_biet"
    with pytest.raises(ValueError, match="lệch"):
        build_lstm_dataset(records, window_config, model_config=None, seed=42)


# ------------------------------------ MAJOR-2: guards split/window rỗng


def test_build_lstm_dataset_rejects_empty_split_with_positive_ratio(window_config):
    """2 sessions với ratios 70/15/15 → val/test quota 0 → ValueError nêu rõ
    tên split (mirror train.py guard), không trả dataset âm thầm."""
    rows = _session_rows("a") + _session_rows("b")
    with pytest.raises(ValueError, match="val"):
        build_lstm_dataset(rows, window_config, seed=42)
    # Ratio 0 cho split rỗng là HỢP LỆ: 2 sessions, 100/0/0.
    ds = build_lstm_dataset(rows, window_config, seed=42, ratios=(1.0, 0.0, 0.0))
    assert ds.val.n_windows == 0 and ds.test.n_windows == 0
    assert ds.train.n_windows > 0


def test_build_lstm_dataset_rejects_zero_total_windows(window_config):
    """Mọi mẫu invalid → tổng 0 cửa sổ → ValueError nêu tổng, không trả
    dataset rỗng âm thầm."""
    rows = []
    for i in range(3):
        session = _session_rows(f"s{i}", n=30)
        for r in session:
            r["valid"] = False
            r["distance_mm"] = None
        rows.extend(session)
    with pytest.raises(ValueError, match="tổng 0 cửa sổ"):
        build_lstm_dataset(rows, window_config, seed=42)


# ------------------------------------ minors: generator, horizons, NaN, from_dict


def test_build_lstm_dataset_accepts_generator_input(window_config):
    """Generator đầu vào không bị tiêu bởi probe — materialize ngay đầu."""
    rows = (r for i in range(6) for r in _session_rows(f"g{i}"))
    ds = build_lstm_dataset(rows, window_config, seed=42)
    assert sum(p.n_windows for p in ds.splits.values()) == 6 * 26


def test_build_lstm_dataset_rejects_horizons_mismatch(window_config):
    """model_config.lstm.horizons_ms lệch window_config → ValueError (chặn
    drift y/manifest)."""
    rows = [r for i in range(6) for r in _session_rows(f"h{i}")]
    bad = {"lstm": {"horizons_ms": [100, 200, 300]}}
    with pytest.raises(ValueError, match="horizons_ms"):
        build_lstm_dataset(rows, window_config, model_config=bad, seed=42)
    # Khớp → chạy bình thường.
    ok = {"lstm": {"horizons_ms": [100, 200, 300, 400, 500]}}
    build_lstm_dataset(rows, window_config, model_config=ok, seed=42)


def test_canonical_json_rejects_nan_inf():
    """allow_nan=False: NaN/inf trong manifest → ValueError (fail fast)."""
    with pytest.raises(ValueError):
        canonical_json({"a": float("nan")})
    with pytest.raises(ValueError):
        canonical_json({"a": float("inf")})


def test_scaler_from_dict_requires_fields_explicitly(fitted_data, tmp_path):
    """Thiếu means/stds/n_samples_fit → ValueError rõ ràng, không default
    0/KeyError."""
    ds = fitted_data
    d = LSTMScaler.fit(ds.train.X, ds.train.y).to_dict()
    for field in ("means", "stds", "n_samples_fit"):
        broken = {k: v for k, v in d.items() if k != field}
        with pytest.raises(ValueError, match=field):
            LSTMScaler.from_dict(broken)
