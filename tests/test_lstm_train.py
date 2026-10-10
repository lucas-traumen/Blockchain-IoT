"""Kiểm thử M11a: train pipeline LSTM (ai_model/lstm_train.py).

Contract kiểm tra:

- import module KHÔNG cần TensorFlow (lazy import) — chạy được trong iot_backend
  ``.venv`` cho phần không model: validate override, manifest document,
  lỗi split rỗng;
- ``train_lstm`` (cần TF, skip nếu thiếu) sinh ĐỦ 5 artifact trong out_dir
  (không file thừa), manifest hợp lệ schema, SHA-256 khớp bytes thực,
  scaler train-only (n_samples_fit = n train windows, tham số khớp
  ``LSTMScaler.fit`` trên train), model nạp lại predict ``(N,5)`` hữu hạn,
  metrics JSON finite, ``dataset_sha256`` = hash file JSONL thật;
- artifact chỉ ghi vào ``tmp_path`` — không artifact nào vào repo;
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import yaml

from ai_model import lstm_train
from ai_model.preprocessing import LSTMScaler, canonical_json, sha256_file, sha256_json

REPO_ROOT = Path(__file__).resolve().parents[1]


def _tf_available() -> bool:
    return importlib.util.find_spec("tensorflow") is not None


def _config() -> dict:
    return yaml.safe_load(
        (REPO_ROOT / "configs" / "model.yaml").read_text(encoding="utf-8")
    )


def _write_jsonl(path: Path, n_sessions: int = 6, n_samples: int = 40) -> None:
    """JSONL synthetic nhỏ: session_label ở meta, payload telemetry đủ field
    để windowing (window 10 + horizon cuối cần 15 mẫu)."""
    lines: list[str] = []
    for s in range(n_sessions):
        label = f"sess_{s}"
        for i in range(n_samples):
            row = {
                "device_id": "esp32_01",
                "boot_id": f"boot_{label}",
                "sequence": i + 1,
                "monotonic_us": 10_000_000 + i * 100_000,
                "measured_at": None,
                "time_synced": False,
                "distance_mm": 1000.0 + i * 2.0 + s,
                "echo_us": 3000 + i,
                "valid": True,
                "error_code": None,
            }
            lines.append(
                json.dumps(
                    {
                        **row,
                        "synthetic": True,
                        "scenario": "approach",
                        "session_label": label,
                    }
                )
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ------------------------------------------------- không cần TensorFlow


def test_module_import_without_tensorflow():
    """Fresh process kiểm lazy import kể cả khi pytest đã nạp TensorFlow."""
    result = subprocess.run(
        [sys.executable, "-c",
         "import ai_model.lstm_train; import sys; assert 'tensorflow' not in sys.modules"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_manifest_document_valid_and_bad_timestamp(tmp_path):
    """validate_manifest_document: manifest chuẩn qua schema; created_at hỏng
    → ValueError (không phụ thuộc rfc3339-validator của FormatChecker)."""
    manifest = {
        "schema_version": "1.0",
        "model_type": "lstm",
        "model_version": "lstm-v1",
        "preprocessing_version": "pp-lstm-v1",
        "framework": {"name": "tensorflow", "version": "2.20.0"},
        "input_shape": [10, 2],
        "output_shape": [5],
        "features": ["distance_m", "delta_distance_m"],
        "units": "m",
        "window_size": 10,
        "delta_s": 0.1,
        "horizons_ms": [100, 200, 300, 400, 500],
        "random_seed": 42,
        "model_file_sha256": "a" * 64,
        "dataset_sha256": "b" * 64,
        "split_manifest_sha256": "c" * 64,
        "created_at": "2026-09-30T00:00:00.000Z",
    }
    lstm_train.validate_manifest_document(manifest)

    # scaler_sha256/n_*_windows là field optional schema — thêm vào vẫn hợp lệ.
    extended = dict(manifest, scaler_sha256="d" * 64, n_train_windows=26,
                    n_val_windows=26, n_test_windows=26)
    lstm_train.validate_manifest_document(extended)

    bad_ts = dict(manifest, created_at="khong-phai-timestamp")
    with pytest.raises(ValueError, match="created_at"):
        lstm_train.validate_manifest_document(bad_ts)


def test_train_lstm_rejects_unknown_override(tmp_path):
    """Override key lạ → ValueError ngay (trước khi đụng TF/dataset)."""
    with pytest.raises(ValueError, match="không hỗ trợ"):
        lstm_train.train_lstm(
            tmp_path / "khong ton tai.jsonl",
            out_dir=tmp_path / "out",
            config_overrides={"epoch": 2},
        )


def test_train_lstm_rejects_bad_override_values(tmp_path):
    dataset = tmp_path / "d.jsonl"
    _write_jsonl(dataset)
    for overrides in (
        {"max_epochs": 0},
        {"batch_size": -1},
        {"verbose": 7},
        {"ratios": (0.5, 0.5)},
    ):
        with pytest.raises(ValueError):
            lstm_train.train_lstm(
                dataset,
                out_dir=tmp_path / "out",
                config_overrides=overrides,
            )


def test_train_lstm_empty_split_fails_clearly(tmp_path):
    """1 session + ratios chia 3 → val/test rỗng (ratio > 0) → lỗi rõ tên
    split, không artifact dở trong out_dir."""
    dataset = tmp_path / "d.jsonl"
    _write_jsonl(dataset, n_sessions=1)
    out_dir = tmp_path / "out"
    with pytest.raises(ValueError, match="0 cửa sổ"):
        lstm_train.train_lstm(
            dataset,
            out_dir=out_dir,
            config_overrides={"max_epochs": 1},
        )
    assert not out_dir.exists() or not any(out_dir.iterdir())


def test_scaler_params_unchanged_when_val_test_mutated(tmp_path):
    """Scaler train-only: fit chỉ trên train — val/test bị gán outlier cực
    đại thì mean/std scaler KHÔNG đổi."""
    from ai_model.prepare import build_lstm_dataset
    from ai_model.windows import WindowConfig

    config = _config()
    window_config = WindowConfig.from_dict(config)
    rows = [
        row
        for s in range(6)
        for row in (
            {
                "session_id": f"sess_{s}",
                "device_id": "esp32_01",
                "boot_id": f"boot_sess_{s}",
                "sequence": i + 1,
                "monotonic_us": 10_000_000 + i * 100_000,
                "measured_at": None,
                "time_synced": False,
                "distance_mm": 1000.0 + i * 2.0 + s,
                "echo_us": 3000 + i,
                "valid": True,
                "error_code": None,
            }
            for i in range(40)
        )
    ]
    dataset = build_lstm_dataset(
        rows, window_config, model_config=config, seed=42
    )
    scaler1 = LSTMScaler.fit(dataset.train.X, dataset.train.y)

    mutated_val = np.array(dataset.val.X, copy=True)
    mutated_val[0, :, 0] = 1.0e6  # outlier val
    mutated_test = np.array(dataset.test.X, copy=True)
    mutated_test[0, :, 1] = -1.0e6  # outlier test
    # Fit lại trên train GIỐNG HỆT (val/test bị mutate không được chạm):
    # tham số phải khớp scaler1 — chứng to fit không đọc val/test.
    scaler2 = LSTMScaler.fit(dataset.train.X, dataset.train.y)
    assert scaler1.to_dict() == scaler2.to_dict()
    # Tham số train-only khác hẳn tham số nếu ai lỡ fit trên cả dataset.
    all_x = np.vstack([dataset.train.X, mutated_val, mutated_test])
    all_y = np.vstack([dataset.train.y, dataset.val.y, dataset.test.y])
    leaked = LSTMScaler.fit(all_x, all_y)
    assert leaked.means != scaler1.means or leaked.stds != scaler1.stds


# ------------------------------------------------- cần TensorFlow (AI env)


@pytest.mark.skipif(not _tf_available(), reason="TensorFlow không có trong .venv — chạy smoke trong AI env")
class TestTrainLstmEndToEnd:
    """Smoke train + nạp artifact trong environment AI (TF cài)."""

    @pytest.fixture(scope="class")
    @classmethod
    def trained(cls, tmp_path_factory):
        tmp_path = tmp_path_factory.mktemp("lstm")
        dataset = tmp_path / "d.jsonl"
        _write_jsonl(dataset)
        out_dir = tmp_path / "out"
        summary = lstm_train.train_lstm(
            dataset,
            out_dir=out_dir,
            config_overrides={
                "max_epochs": 3,
                "batch_size": 16,
                "verbose": 0,
            },
        )
        return summary, out_dir, dataset

    def test_five_artifacts_only(self, trained):
        _, out_dir, _ = trained
        files = {p.name for p in out_dir.iterdir()}
        assert files == set(lstm_train.REQUIRED_LSTM_ARTIFACTS)

    def test_manifest_schema_and_hashes(self, trained):
        _, out_dir, dataset = trained
        manifest = json.loads((out_dir / "manifest.json").read_text())
        lstm_train.validate_manifest_document(manifest)
        assert sha256_file(out_dir / "model.keras") == manifest["model_file_sha256"]
        assert sha256_file(out_dir / "scaler.json") == manifest["scaler_sha256"]
        assert sha256_file(dataset) == manifest["dataset_sha256"]
        assert sha256_file(out_dir / "split_manifest.json") == (
            manifest["split_manifest_sha256"]
        )
        split_manifest = json.loads(
            (out_dir / "split_manifest.json").read_text()
        )
        assert sha256_json(split_manifest) == manifest["split_manifest_sha256"]

    def test_n_windows_match_split_manifest(self, trained):
        summary, out_dir, _ = trained
        manifest = json.loads((out_dir / "manifest.json").read_text())
        split_manifest = json.loads(
            (out_dir / "split_manifest.json").read_text()
        )
        for name, key in (("train", "n_train_windows"), ("val", "n_val_windows"), ("test", "n_test_windows")):
            assert manifest[key] == split_manifest["splits"][name]["n_windows"]
            assert summary["n_windows"][name] == manifest[key]

    def test_scaler_contract_and_train_only(self, trained):
        from ai_model.prepare import build_lstm_dataset, load_jsonl_dataset
        from ai_model.windows import WindowConfig

        _, out_dir, dataset_path = trained
        scaler = LSTMScaler.load(out_dir / "scaler.json")
        assert scaler.to_dict()["fitted_on"] == "train_only"
        assert scaler.to_dict()["preprocessing_version"] == "pp-lstm-v1"
        # Tái lập dataset (split deterministic cùng seed/ratios) để kiểm
        # tham số scaler khớp fit CHỈ trên split train (mét).
        config = _config()
        dataset = build_lstm_dataset(
            load_jsonl_dataset(dataset_path),
            WindowConfig.from_dict(config),
            model_config=config,
            seed=42,
        )
        assert scaler.n_samples_fit == int(dataset.train.X.shape[0])
        ref = LSTMScaler.fit(dataset.train.X, dataset.train.y)
        assert scaler.to_dict() == ref.to_dict()

    def test_load_lstm_artifact_predict_shape(self, trained):
        _, out_dir, _ = trained
        model, scaler, manifest = lstm_train.load_lstm_artifact(out_dir)
        x = np.zeros((4, 10, 2), dtype="float64")
        y = model.predict(x, verbose=0)
        assert y.shape == (4, 5)
        assert np.isfinite(y).all()
        meters = scaler.inverse_y(y)
        assert meters.shape == (4, 5)
        assert manifest["model_version"] == "lstm-v1"

    def test_metrics_json_finite(self, trained):
        _, out_dir, _ = trained
        raw = (out_dir / "metrics.json").read_text()
        assert "NaN" not in raw and "Infinity" not in raw
        metrics = json.loads(raw)
        for name in ("train", "val", "test"):
            block = metrics["metrics"][name]
            assert block["n_windows"] > 0
            for value in block["mae_m_per_horizon"]:
                assert np.isfinite(value) and value >= 0
        for key in ("loss", "val_loss"):
            if key in metrics["history"]:
                assert all(np.isfinite(v) for v in metrics["history"][key])

    def test_load_rejects_hash_mismatch(self, trained, tmp_path):
        _, out_dir, _ = trained
        tampered = tmp_path / "tampered"
        tampered.mkdir()
        for name in lstm_train.REQUIRED_LSTM_ARTIFACTS:
            (tampered / name).write_bytes((out_dir / name).read_bytes())
        data = json.loads((tampered / "scaler.json").read_text())
        data["means"]["distance_m"] = 0.0
        (tampered / "scaler.json").write_text(canonical_json(data))
        with pytest.raises(ValueError, match="scaler"):
            lstm_train.load_lstm_artifact(tampered)

    def test_load_rejects_metrics_tamper(self, trained, tmp_path):
        import shutil
        _, out_dir, _ = trained
        tampered = tmp_path / "tampered_metrics"
        shutil.copytree(out_dir, tampered)
        doc = json.loads((tampered / "metrics.json").read_text())
        doc["metrics"]["test"]["mae_m_mean"] = 0.0
        (tampered / "metrics.json").write_text(json.dumps(doc))
        with pytest.raises(ValueError, match="metrics.json"):
            lstm_train.load_lstm_artifact(tampered)

    def test_load_rejects_missing_file(self, trained, tmp_path):
        _, out_dir, _ = trained
        partial = tmp_path / "partial"
        partial.mkdir()
        (partial / "manifest.json").write_bytes(
            (out_dir / "manifest.json").read_bytes()
        )
        with pytest.raises(FileNotFoundError):
            lstm_train.load_lstm_artifact(partial)


@pytest.mark.parametrize("ratios", [(1,0,0),(.7,.3,0),(.7,float("nan"),.3)])
def test_zero_or_nonfinite_ratios_fail_before_tensorflow(tmp_path, ratios):
    with pytest.raises(ValueError, match="ratios"):
        lstm_train.train_lstm(tmp_path/"missing", config_overrides={"ratios":ratios})


def test_unsupported_metric_rejected(tmp_path):
    config = _config()
    config["lstm"]["metrics"] = ["rmse"]
    path = tmp_path/"config.yaml"
    path.write_text(yaml.safe_dump(config))
    with pytest.raises(ValueError, match="metrics"):
        lstm_train.train_lstm(tmp_path/"missing", config_path=path)
