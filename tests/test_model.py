"""Kiểm thử M5c: baselines (persistence/constant-velocity) + train_model
(RandomForest multi-output) + artifact manifest + load_model.

Dataset nhỏ sinh bằng ``scripts.synthetic_dataset.generate_sessions`` vào
``tmp_path`` (6 session × 50 mẫu, seed 42, error_prob=0 — mọi mẫu hợp lệ,
nhanh). Train chạy với grid RÚT GỌN qua ``config_overrides``
(``n_estimators_grid=[10]``, ``max_depth_grid=[4]`` — 1 tổ hợp) để toàn bộ
test chạy trong vài giây. Artifact ghi vào tmp_path — KHÔNG đụng
``ai_model/artifacts/`` của repo.
"""

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from ai_model.baselines import predict_constant_velocity, predict_persistence
from ai_model.features import FEATURE_NAMES
from ai_model.prepare import SPLIT_NAMES, load_jsonl_dataset
from ai_model.train import REQUIRED_MANIFEST_FIELDS, load_model, train_model
from ai_model.windows import WindowConfig, extract_windows
from scripts.synthetic_dataset import generate_sessions, write_jsonl

REPO_ROOT = Path(__file__).resolve().parents[1]

N_SESSIONS = 6
N_SAMPLES = 50
SEED = 42
FAST_GRID = {"n_estimators_grid": [10], "max_depth_grid": [4]}
HORIZONS_MS = [100, 200, 300, 400, 500]


@pytest.fixture(scope="module")
def dataset_path(tmp_path_factory) -> Path:
    """Dataset nhỏ 6×50, seed 42, không mẫu lỗi — sinh bằng đúng generator."""
    lines = generate_sessions(
        N_SESSIONS, N_SAMPLES, SEED, scenario="all", error_prob=0.0
    )
    path = tmp_path_factory.mktemp("m5c_dataset") / "sim_small.jsonl"
    write_jsonl(lines, path)
    return path


@pytest.fixture(scope="module")
def trained(tmp_path_factory, dataset_path):
    """Một lần train dùng chung cho module: (summary, thư mục artifact)."""
    out_dir = tmp_path_factory.mktemp("m5c_artifacts")
    summary = train_model(
        dataset_path,
        REPO_ROOT / "configs" / "model.yaml",
        out_dir,
        config_overrides=dict(FAST_GRID),
    )
    return summary, out_dir


# --------------------------------------------------------------- baselines


def test_persistence_repeats_last_value():
    """Persistence: 5 giá trị = d(t) (phần tử cuối), đúng số horizon."""
    features = [1.0, 0.95, 0.9, 0.85, 0.8]
    assert predict_persistence(features) == [0.8] * 5
    # Số horizon khác → lặp đúng số đó (mặc định 5 = số horizon model v1).
    assert predict_persistence(features, n_horizons=3) == [0.8] * 3
    with pytest.raises(ValueError):
        predict_persistence([])


def test_constant_velocity_decreasing_hand_checked():
    """Giảm đều 0.1 m mỗi mẫu, Δ = 0.1 s → v = −1.0 m/s; kiểm bằng tay:
    d(t) = 0.6, h = 0.1..0.5 s → 0.5, 0.4, 0.3, 0.2, 0.1."""
    features = [1.0, 0.9, 0.8, 0.7, 0.6]
    predictions = predict_constant_velocity(features, 0.1, HORIZONS_MS)
    assert predictions == pytest.approx([0.5, 0.4, 0.3, 0.2, 0.1])


def test_constant_velocity_increasing_and_flat():
    """Tăng đều → dự báo tiếp tục tăng đúng công thức; dằng ra → v = 0."""
    up = predict_constant_velocity([0.6, 0.7, 0.8, 0.9, 1.0], 0.1, HORIZONS_MS)
    assert up == pytest.approx([1.1, 1.2, 1.3, 1.4, 1.5])  # v = +1.0 m/s
    flat = predict_constant_velocity([0.7] * 10, 0.1, HORIZONS_MS)
    assert flat == pytest.approx([0.7] * 5)


def test_constant_velocity_no_clamp_and_guards():
    """Không clamp: giảm qua 0 cho dự báo âm; input sai → ValueError."""
    predictions = predict_constant_velocity(
        [0.5, 0.4, 0.3, 0.2, 0.1], 0.1, HORIZONS_MS
    )
    assert predictions[-1] == pytest.approx(-0.4)
    with pytest.raises(ValueError):
        predict_constant_velocity([0.5], 0.1, HORIZONS_MS)  # < 2 mẫu
    with pytest.raises(ValueError):
        predict_constant_velocity([0.5, 0.4], 0.0, HORIZONS_MS)  # Δ <= 0


# ------------------------------------------------------ artifact / manifest


def test_artifacts_exist_and_manifest_fields(trained):
    """model.joblib + manifest.json tồn tại; manifest đủ field bắt buộc;
    feature_names đúng d0..d9; sha256 khớp split_manifest nhúng trong manifest."""
    summary, out_dir = trained
    assert (out_dir / "model.joblib").is_file()
    manifest_path = out_dir / "manifest.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    for field in REQUIRED_MANIFEST_FIELDS:
        assert field in manifest, f"manifest thiếu field {field}"

    assert manifest["feature_names"] == [f"d{i}" for i in range(10)]
    assert manifest["feature_names"] == FEATURE_NAMES
    assert manifest["units"] == "m"
    assert manifest["model_version"] == "rf-v1"
    assert manifest["preprocessing_version"] == "pp-v1"
    assert manifest["random_seed"] == SEED
    assert manifest["horizons_ms"] == HORIZONS_MS
    assert manifest["window_size"] == 10

    expected_sha = hashlib.sha256(
        json.dumps(manifest["split_manifest"], sort_keys=True).encode("utf-8")
    ).hexdigest()
    assert manifest["split_manifest_sha256"] == expected_sha

    # Tổng cửa sổ 3 split = số cửa sổ trích được; khớp summary.
    assert (
        manifest["n_train_windows"]
        + manifest["n_val_windows"]
        + manifest["n_test_windows"]
        == summary["window_stats"]["n_windows"]
    )
    assert summary["n_windows"] == {
        "train": manifest["n_train_windows"],
        "val": manifest["n_val_windows"],
        "test": manifest["n_test_windows"],
    }


def test_summary_grid_and_best(trained):
    """Bảng grid đủ tổ hợp; best_params trùng một entry của grid; MAE val
    của model tốt nhất hữu hạn, đúng 5 horizon."""
    summary, _ = trained
    assert len(summary["grid"]) == 1  # grid rút gọn cho test
    entry = summary["grid"][0]
    assert summary["best_params"] == {
        "n_estimators": entry["n_estimators"],
        "max_depth": entry["max_depth"],
    }
    assert len(summary["val_mae_per_horizon"]) == len(HORIZONS_MS)
    assert all(np.isfinite(summary["val_mae_per_horizon"]))
    assert np.isfinite(summary["val_mae_mean"])


def test_load_model_predict_shape_and_finite(trained, dataset_path):
    """load_model trả model dự báo shape (n, 5), giá trị hữu hạn; đường dẫn
    thiếu artifact → FileNotFoundError."""
    _, out_dir = trained
    model, manifest = load_model(out_dir)

    # Feature thật: trích lại cửa sổ từ dataset bằng đúng pipeline train.
    config = yaml.safe_load(
        (REPO_ROOT / "configs" / "model.yaml").read_text(encoding="utf-8")
    )
    rows = []
    for record in load_jsonl_dataset(dataset_path):
        row = dict(record["row"])
        row["session_id"] = record["meta"]["session_label"]
        rows.append(row)
    windows, _ = extract_windows(rows, WindowConfig.from_dict(config))
    assert windows

    one = pd.DataFrame([windows[0].features], columns=manifest["feature_names"])
    prediction = model.predict(one)
    assert prediction.shape == (1, 5)
    assert np.all(np.isfinite(prediction))

    many = pd.DataFrame(
        [w.features for w in windows[:4]], columns=manifest["feature_names"]
    )
    assert model.predict(many).shape == (4, 5)

    with pytest.raises(FileNotFoundError):
        load_model(out_dir / "khong_ton_tai")


# ---------------------------------------------------------------- leakage


def test_no_session_leakage_between_splits(trained):
    """Train/val/test session id đôi một rỗng giao; union đủ mọi session."""
    summary, _ = trained
    sessions = summary["split_sessions"]
    assert set(sessions) == set(SPLIT_NAMES)
    train, val, test = (set(sessions[name]) for name in SPLIT_NAMES)
    assert not (train & val)
    assert not (train & test)
    assert not (val & test)
    assert train | val | test == {f"sess_{i}" for i in range(N_SESSIONS)}
    # Mọi split đều có session (6 session → 4/1/1 theo largest remainder).
    assert all(sessions[name] for name in SPLIT_NAMES)


# ------------------------------------------------------------ deterministic


def test_train_same_seed_reproducible(tmp_path, dataset_path):
    """Cùng seed train 2 lần: manifest giống nhau NGOẠI TRỪ created_at
    (giờ chạy thật) — bao hàm best_params + split + sha256 giống nhau."""
    out_a, out_b = tmp_path / "run_a", tmp_path / "run_b"
    train_model(
        dataset_path,
        REPO_ROOT / "configs" / "model.yaml",
        out_a,
        config_overrides=dict(FAST_GRID),
    )
    train_model(
        dataset_path,
        REPO_ROOT / "configs" / "model.yaml",
        out_b,
        config_overrides=dict(FAST_GRID),
    )
    manifest_a = json.loads((out_a / "manifest.json").read_text(encoding="utf-8"))
    manifest_b = json.loads((out_b / "manifest.json").read_text(encoding="utf-8"))
    manifest_a.pop("created_at")
    manifest_b.pop("created_at")
    assert manifest_a == manifest_b


# ------------------------------------------------------------------ guards


def test_train_rejects_unknown_override(tmp_path, dataset_path):
    """config_overrides key lạ → ValueError, trước khi ghi bất kỳ artifact."""
    with pytest.raises(ValueError, match="config_overrides"):
        train_model(
            dataset_path,
            REPO_ROOT / "configs" / "model.yaml",
            tmp_path / "never_created",
            config_overrides={"khong_hop_le": [1]},
        )
    assert not (tmp_path / "never_created").exists()
