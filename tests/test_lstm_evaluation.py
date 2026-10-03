"""Kiểm thử M12b1: evaluator offline LSTM (ai_model/evaluate.evaluate_lstm_models).

Contract kiểm tra (pure/fake — KHÔNG cần TensorFlow):

- import module không kéo TensorFlow; report đúng contract keys/models;
- cùng test windows của artifact (split tái lập seed/ratios đúng, khớp
  split manifest + model manifest); dataset hash lệch → ValueError;
- baselines giữ công thức M5c; RF tùy chọn đánh giá trên chính các cửa sổ
  test LSTM; breakdown scenario/target_type cô lập, metadata thiếu →
  group "unknown", tổng n_test_windows đúng;
- guards: split mismatch, output shape/NaN/inf, horizon/sp_m sai, test rỗng;
- không fit: fake model/scaler đếm ``fit`` — evaluator chỉ inference.

Integration (TF 2.20.0 trong AI env riêng, skip khi thiếu): train artifact
thật rồi evaluate bằng loader thật; không commit artifact (mọi file ở tmp).
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

from ai_model.evaluate import mae_per_horizon, regression_metrics
from ai_model.preprocessing import sha256_file, write_split_manifest
from ai_model.windows import WindowConfig

REPO_ROOT = Path(__file__).resolve().parents[1]


def _tf_available() -> bool:
    return importlib.util.find_spec("tensorflow") is not None


def _config() -> dict:
    return yaml.safe_load(
        (REPO_ROOT / "configs" / "model.yaml").read_text(encoding="utf-8")
    )


def _write_jsonl(
    path: Path,
    n_sessions: int = 6,
    n_samples: int = 40,
    scenario_of_session=None,
) -> None:
    """JSONL synthetic nhỏ (cùng contract test_lstm_train): telemetry đủ
    field cho windowing; scenario theo session (mặc định một group)."""
    lines: list[str] = []
    for s in range(n_sessions):
        label = f"sess_{s}"
        scenario = (
            scenario_of_session(s) if scenario_of_session else "approach"
        )
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
                        "scenario": scenario,
                        "session_label": label,
                    }
                )
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class FakeScaler:
    """Scaler giả: transform/inverse identity (raw mét — M12b contract)."""

    def __init__(self):
        self.fit_calls = 0

    def transform_X(self, X):
        return np.asarray(X, dtype="float64")

    def inverse_y(self, y):
        return np.asarray(y, dtype="float64")


class FakeLstmModel:
    """Model giả: predict = khoảng cách cuối cửa sổ lặp 5 horizon; đếm fit."""

    def __init__(self, output_fn=None):
        self.fit_calls = 0
        self._output_fn = output_fn

    def fit(self, *args, **kwargs):
        self.fit_calls += 1
        raise AssertionError("evaluator KHÔNG được gọi model.fit")

    def predict(self, X, **kwargs):
        x = np.asarray(X, dtype="float64")
        if self._output_fn is not None:
            return self._output_fn(x)
        return np.repeat(x[:, -1:, 0], 5, axis=1)  # (N,5), finite mét


class FakeRfModel:
    """RF giả: predict nhận DataFrame, trả dự báo persistence-ish (N,5)."""

    def __init__(self):
        self.fit_calls = 0

    def fit(self, *args, **kwargs):
        self.fit_calls += 1
        raise AssertionError("evaluator KHÔNG được gọi model.fit")

    def predict(self, frame):
        d9 = frame["d9"].to_numpy(dtype="float64")
        return np.repeat(d9[:, None], 5, axis=1)


def _build_artifact(
    tmp_path: Path,
    n_sessions: int = 6,
    n_samples: int = 40,
    ratios=(0.7, 0.15, 0.15),
    dataset_sha256: str | None = None,
    scenario_of_session=None,
):
    """Dựng dataset + thư mục artifact giả (split_manifest.json + manifest
   .json với seed/hash/counts thật từ build_lstm_dataset). Trả tuple."""
    dataset_path = tmp_path / "ds.jsonl"
    _write_jsonl(
        dataset_path,
        n_sessions=n_sessions,
        n_samples=n_samples,
        scenario_of_session=scenario_of_session,
    )
    from ai_model.prepare import build_lstm_dataset, load_jsonl_dataset

    config = _config()
    records = load_jsonl_dataset(dataset_path)
    dataset = build_lstm_dataset(
        records,
        WindowConfig.from_dict(config),
        model_config=config,
        seed=42,
        ratios=ratios,
    )
    model_dir = tmp_path / "lstm-v1"
    model_dir.mkdir()
    write_split_manifest(
        dataset.split_manifest, model_dir / "split_manifest.json"
    )
    manifest = {
        "model_type": "lstm", "model_version": "lstm-v1",
        "preprocessing_version": "pp-lstm-v1", "units": "m",
        "features": ["distance_m", "delta_distance_m"],
        "window_size": 10, "delta_s": .1,
        "horizons_ms": [100,200,300,400,500],
        "random_seed": 42,
        "dataset_sha256": (
            dataset_sha256
            if dataset_sha256 is not None
            else sha256_file(dataset_path)
        ),
        "n_test_windows": int(dataset.test.n_windows),
        "input_shape": [10, 2],
        "output_shape": [5],
    }
    (model_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return dataset_path, model_dir, dataset, manifest


def _fake_lstm_loader(model, scaler, manifest):
    def loader(_model_dir):
        return model, scaler, manifest

    return loader


def _evaluate(tmp_path, **kwargs):
    """evaluate_lstm_models với fake loader (không TF)."""
    from ai_model.evaluate import evaluate_lstm_models

    dataset_path, model_dir, dataset, manifest = _build_artifact(tmp_path)
    scaler = FakeScaler()
    model = FakeLstmModel()
    overrides = {
        "lstm_loader": _fake_lstm_loader(model, scaler, manifest)
    }
    overrides.update(kwargs)
    report = evaluate_lstm_models(dataset_path, model_dir, **overrides)
    return report, model, scaler, dataset, manifest


# ------------------------------------------------- import + report contract


def test_module_import_without_tensorflow():
    """Fresh process kiểm lazy import kể cả khi pytest đã nạp TensorFlow."""
    result = subprocess.run(
        [sys.executable, "-c",
         "import ai_model.evaluate; import sys; assert 'tensorflow' not in sys.modules"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_report_contract_keys_and_models(tmp_path):
    """Report đủ keys contract; 3 model mặc định; breakdown đủ 2 chiều."""
    report, _model, _scaler, dataset, _manifest = _evaluate(tmp_path)
    assert set(report) == {
        "models",
        "n_test_windows",
        "test_sessions",
        "horizons_ms",
        "sp_m",
        "horizon_index",
        "synthetic",
        "breakdown",
    }
    assert set(report["models"]) == {"lstm", "persistence", "constant_velocity"}
    for model in report["models"].values():
        assert set(model) == {"regression", "event_metrics"}
        assert set(model["regression"]) == {
            "mae_per_horizon",
            "rmse_per_horizon",
            "mae_mean",
            "rmse_mean",
            "n_samples",
        }
        assert set(model["event_metrics"]) >= {"precision", "recall", "f1"}
    assert report["n_test_windows"] == dataset.test.n_windows
    assert report["test_sessions"] == sorted(dataset.test.session_ids)
    assert report["horizons_ms"] == [100, 200, 300, 400, 500]
    assert report["sp_m"] == 0.44
    assert report["horizon_index"] == 4
    assert report["synthetic"] is True
    assert set(report["breakdown"]) == {"scenario", "target_type"}


def test_report_json_safe(tmp_path):
    """json.dumps(report, allow_nan=False) không raise — JSON-safe contract."""
    report, _model, _scaler, _dataset, _manifest = _evaluate(tmp_path)
    json.dumps(report, allow_nan=False)


def test_same_test_windows_as_artifact_split(tmp_path):
    """Evaluator tái lập ĐÚNG split artifact: n windows + sessions test."""
    from ai_model.prepare import build_lstm_dataset, load_jsonl_dataset

    dataset_path, model_dir, dataset, manifest = _build_artifact(tmp_path)
    from ai_model.evaluate import evaluate_lstm_models

    report = evaluate_lstm_models(
        dataset_path,
        model_dir,
        lstm_loader=_fake_lstm_loader(
            FakeLstmModel(), FakeScaler(), manifest
        ),
    )
    # Tái lập độc lập từ dataset để đối chiếu n_test_windows.
    config = _config()
    rebuilt = build_lstm_dataset(
        load_jsonl_dataset(dataset_path),
        WindowConfig.from_dict(config),
        model_config=config,
        seed=42,
    )
    assert report["n_test_windows"] == rebuilt.test.n_windows
    assert report["test_sessions"] == sorted(rebuilt.test.session_ids)
    assert report["n_test_windows"] == manifest["n_test_windows"]


def test_baseline_metrics_match_direct_computation(tmp_path):
    """Persistence/constant-velocity metric khớp tính trực tiếp trên raw
    distance channel test.X[:,:,0] (công thức M5c không đổi)."""
    report, _model, _scaler, dataset, _manifest = _evaluate(tmp_path)
    x = np.asarray(dataset.test.X)
    y_rows = [[float(t) for t in w.targets] for w in dataset.test.windows]
    from ai_model.baselines import predict_constant_velocity, predict_persistence

    horizons_ms = [100, 200, 300, 400, 500]
    persistence = [
        predict_persistence([float(v) for v in x[i, :, 0]], n_horizons=5)
        for i in range(len(x))
    ]
    cv = [
        predict_constant_velocity(
            [float(v) for v in x[i, :, 0]], 0.1, horizons_ms
        )
        for i in range(len(x))
    ]
    assert report["models"]["persistence"]["regression"][
        "mae_per_horizon"
    ] == pytest.approx(mae_per_horizon(persistence, y_rows))
    assert report["models"]["constant_velocity"]["regression"][
        "mae_per_horizon"
    ] == pytest.approx(mae_per_horizon(cv, y_rows))


def test_lstm_prediction_pipeline_no_clamp(tmp_path):
    """Fake model predict = d9 lặp → LSTM metric khớp persistence (identity
    scaler, không round/clamp can thiệp)."""
    report, _model, _scaler, _dataset, _manifest = _evaluate(tmp_path)
    assert report["models"]["lstm"]["regression"] == pytest.approx(
        report["models"]["persistence"]["regression"]
    )


def test_rf_optional_on_same_test_windows(tmp_path):
    """rf_model_dir truyền vào → report thêm 'rf', n_samples bằng số cửa sổ
    test LSTM (cùng split, không split lại); rf_model_dir None → không 'rf'."""
    from ai_model.evaluate import evaluate_lstm_models

    dataset_path, model_dir, dataset, manifest = _build_artifact(tmp_path)
    rf_loader_calls = []

    def rf_loader(_dir):
        rf_loader_calls.append(_dir)
        return FakeRfModel(), {
            "dataset_sha256": manifest["dataset_sha256"],
            "split_manifest": dataset.split_manifest,
            "window_size":10,"delta_s":.1,"horizons_ms":[100,200,300,400,500],
        }

    report = evaluate_lstm_models(
        dataset_path,
        model_dir,
        rf_model_dir="rf-legacy",
        rf_loader=rf_loader,
        lstm_loader=_fake_lstm_loader(FakeLstmModel(), FakeScaler(), manifest),
    )
    assert set(report["models"]) == {
        "lstm",
        "persistence",
        "constant_velocity",
        "rf",
    }
    assert len(rf_loader_calls) == 1
    n = report["n_test_windows"]
    assert report["models"]["rf"]["regression"]["n_samples"] == n
    assert report["models"]["lstm"]["regression"]["n_samples"] == n
    for dim in ("scenario", "target_type"):
        for group in report["breakdown"][dim].values():
            assert set(group["models"]) == set(report["models"])


# ------------------------------------------------------- breakdown semantics


def test_breakdown_isolation_and_unknown_metadata(tmp_path):
    """Breakdown cô lập: 2 scenario group; tổng n_test_windows đúng;
    target_type không có trong metadata → duy nhất group 'unknown' với đủ
    số cửa sổ (không silently drop)."""
    report, _model, _scaler, dataset, _manifest = _evaluate(
        tmp_path,
        # artifact dựng trong _evaluate; override scenario qua builder riêng
    )
    assert report["breakdown"]["target_type"].keys() == {"unknown"}
    total = sum(
        g["n_test_windows"] for g in report["breakdown"]["target_type"].values()
    )
    assert total == report["n_test_windows"]
    total = sum(
        g["n_test_windows"] for g in report["breakdown"]["scenario"].values()
    )
    assert total == report["n_test_windows"]


def test_breakdown_scenario_two_groups_isolated(tmp_path):
    """2 scenario → 2 group riêng biệt, không ghép event/metric xuyên group."""
    from ai_model.evaluate import evaluate_lstm_models

    dataset_path, model_dir, dataset, manifest = _build_artifact(
        tmp_path,
        n_sessions=8,
        ratios=(0.5, 0.25, 0.25),  # 4/2/2 session — test có ≥2 session
        scenario_of_session=lambda s: "approach" if s % 2 == 0 else "retreat",
    )
    report = evaluate_lstm_models(
        dataset_path,
        model_dir,
        lstm_loader=_fake_lstm_loader(FakeLstmModel(), FakeScaler(), manifest),
    )
    groups = report["breakdown"]["scenario"]
    assert set(groups) == {"approach", "retreat"}
    assert (
        groups["approach"]["n_test_windows"]
        + groups["retreat"]["n_test_windows"]
        == report["n_test_windows"]
    )
    # Regression per-group: tổng n_samples các group = tổng windows.
    for group in groups.values():
        for model in group["models"].values():
            assert model["regression"]["n_samples"] == group["n_test_windows"]


# ------------------------------------------------------------------- guards


def test_dataset_hash_mismatch_raises(tmp_path):
    """Dataset hash lệch manifest → ValueError TRƯỚC report."""
    from ai_model.evaluate import evaluate_lstm_models

    dataset_path, model_dir, _dataset, manifest = _build_artifact(
        tmp_path, dataset_sha256="0" * 64
    )
    with pytest.raises(ValueError, match="dataset_sha256"):
        evaluate_lstm_models(
            dataset_path,
            model_dir,
            lstm_loader=_fake_lstm_loader(
                FakeLstmModel(), FakeScaler(), manifest
            ),
        )


def test_split_manifest_session_mismatch_raises(tmp_path):
    """Split manifest bị sửa session list → ValueError (không âm thầm đánh
    giá split khác)."""
    from ai_model.evaluate import evaluate_lstm_models

    dataset_path, model_dir, _dataset, manifest = _build_artifact(tmp_path)
    split_manifest = json.loads(
        (model_dir / "split_manifest.json").read_text(encoding="utf-8")
    )
    split_manifest["sessions"]["test"] = ["sess_khong_ton_tai"]
    (model_dir / "split_manifest.json").write_text(
        json.dumps(split_manifest), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="session lists"):
        evaluate_lstm_models(
            dataset_path,
            model_dir,
            lstm_loader=_fake_lstm_loader(
                FakeLstmModel(), FakeScaler(), manifest
            ),
        )


def test_lstm_output_nonfinite_raises(tmp_path):
    """Predict chứa NaN/inf → ValueError."""
    from ai_model.evaluate import evaluate_lstm_models

    dataset_path, model_dir, _dataset, manifest = _build_artifact(tmp_path)
    bad = FakeLstmModel(
        output_fn=lambda x: np.full((len(x), 5), float("nan"))
    )
    with pytest.raises(ValueError, match="NaN|inf|không khớp|finite"):
        evaluate_lstm_models(
            dataset_path,
            model_dir,
            lstm_loader=_fake_lstm_loader(bad, FakeScaler(), manifest),
        )


def test_lstm_output_shape_mismatch_raises(tmp_path):
    """Predict shape (N,3) lệch output_shape (N,5) → ValueError."""
    from ai_model.evaluate import evaluate_lstm_models

    dataset_path, model_dir, _dataset, manifest = _build_artifact(tmp_path)
    bad = FakeLstmModel(
        output_fn=lambda x: np.repeat(x[:, -1:, 0], 3, axis=1)
    )
    with pytest.raises(ValueError, match="shape"):
        evaluate_lstm_models(
            dataset_path,
            model_dir,
            lstm_loader=_fake_lstm_loader(bad, FakeScaler(), manifest),
        )


def test_invalid_horizon_index_raises(tmp_path):
    from ai_model.evaluate import evaluate_lstm_models

    dataset_path, model_dir, _dataset, manifest = _build_artifact(tmp_path)
    with pytest.raises(ValueError, match="horizon_index"):
        evaluate_lstm_models(
            dataset_path,
            model_dir,
            horizon_index=5,
            lstm_loader=_fake_lstm_loader(
                FakeLstmModel(), FakeScaler(), manifest
            ),
        )


def test_nonfinite_sp_m_raises(tmp_path):
    from ai_model.evaluate import evaluate_lstm_models

    dataset_path, model_dir, _dataset, manifest = _build_artifact(tmp_path)
    with pytest.raises(ValueError, match="sp_m"):
        evaluate_lstm_models(
            dataset_path,
            model_dir,
            sp_m=float("nan"),
            lstm_loader=_fake_lstm_loader(
                FakeLstmModel(), FakeScaler(), manifest
            ),
        )


def test_empty_test_split_raises(tmp_path):
    """Ratios test = 0 → split test rỗng → ValueError, không report rỗng."""
    from ai_model.evaluate import evaluate_lstm_models

    dataset_path, model_dir, _dataset, manifest = _build_artifact(
        tmp_path, ratios=(0.8, 0.2, 0.0)
    )
    assert manifest["n_test_windows"] == 0
    with pytest.raises(ValueError, match="test"):
        evaluate_lstm_models(
            dataset_path,
            model_dir,
            lstm_loader=_fake_lstm_loader(
                FakeLstmModel(), FakeScaler(), manifest
            ),
        )


def test_lstm_loader_bad_tuple_raises(tmp_path):
    from ai_model.evaluate import evaluate_lstm_models

    dataset_path, model_dir, _dataset, _manifest = _build_artifact(tmp_path)
    with pytest.raises(ValueError, match="tuple"):
        evaluate_lstm_models(
            dataset_path,
            model_dir,
            lstm_loader=lambda _dir: ("không phải tuple",),
        )


def test_no_fit_called_evaluator_inference_only(tmp_path):
    """Evaluator chỉ inference: fake scaler/model không bị gọi fit; toàn bộ
    dữ liệu dùng là test split (train/val không đụng)."""
    report, model, scaler, dataset, _manifest = _evaluate(tmp_path)
    assert model.fit_calls == 0
    assert scaler.fit_calls == 0
    assert report["n_test_windows"] == dataset.test.n_windows
    assert report["n_test_windows"] < sum(
        s.n_windows for s in dataset.splits.values()
    )


# -------------------------------------------- integration (AI env có TF)


@pytest.mark.skipif(not _tf_available(), reason="TensorFlow không có trong env")
class TestIntegrationRealArtifact:
    """Integration AI env riêng (TF 2.20.0): train artifact thật trong tmp
    rồi evaluate bằng loader thật — không commit artifact."""

    def test_evaluate_trained_artifact(self, tmp_path):
        from ai_model.evaluate import evaluate_lstm_models
        from ai_model.lstm_train import train_lstm

        dataset_path = tmp_path / "ds.jsonl"
        _write_jsonl(dataset_path)
        artifact_dir = tmp_path / "lstm-v1"
        train_lstm(
            dataset_path,
            config_path=REPO_ROOT / "configs" / "model.yaml",
            out_dir=artifact_dir,
            config_overrides={"max_epochs": 1, "batch_size": 8, "verbose": 0},
        )
        report = evaluate_lstm_models(dataset_path, artifact_dir)
        assert set(report["models"]) == {
            "lstm",
            "persistence",
            "constant_velocity",
        }
        assert report["n_test_windows"] > 0
        for name in ("lstm", "persistence", "constant_velocity"):
            regression = report["models"][name]["regression"]
            assert regression["rmse_mean"] >= 0.0
            assert regression["mae_mean"] >= 0.0
            assert regression["n_samples"] == report["n_test_windows"]
            event = report["models"][name]["event_metrics"]
            assert "precision" in event and "recall" in event
        total = sum(
            g["n_test_windows"]
            for g in report["breakdown"]["scenario"].values()
        )
        assert total == report["n_test_windows"]
        json.dumps(report, allow_nan=False)


def test_cli_selects_lstm_and_writes_json(tmp_path, monkeypatch, capsys):
    from ai_model import evaluate
    calls = []
    def fake(*args, **kwargs):
        calls.append((args, kwargs))
        return {"models":{"lstm":{}},"synthetic":True}
    monkeypatch.setattr(evaluate,"evaluate_lstm_models",fake)
    output=tmp_path/"report.json"
    assert evaluate.main(["--dataset","input","--model-dir","lstm",
        "--model-type","lstm","--rf-model-dir","rf","--output",str(output)]) == 0
    assert calls[0][1]["rf_model_dir"] == "rf"
    assert json.loads(output.read_text())["synthetic"] is True
    assert capsys.readouterr().out == ""


def test_evaluator_rejects_artifact_horizon_drift(tmp_path):
    from ai_model.evaluate import evaluate_lstm_models
    data,directory,dataset,manifest=_build_artifact(tmp_path)
    wrong=dict(manifest,horizons_ms=[500,400,300,200,100])
    with pytest.raises(ValueError,match="horizons_ms"):
        evaluate_lstm_models(data,directory,lstm_loader=_fake_lstm_loader(FakeLstmModel(),FakeScaler(),wrong))


def test_rf_split_mismatch_cannot_leak_test(tmp_path):
    from ai_model.evaluate import evaluate_lstm_models
    data,directory,dataset,manifest=_build_artifact(tmp_path)
    wrong={"dataset_sha256":manifest["dataset_sha256"],"split_manifest":{
        "sessions":{"train":list(dataset.train.session_ids|dataset.test.session_ids),
                    "val":list(dataset.val.session_ids),"test":[]}}}
    with pytest.raises(ValueError,match="RF split"):
        evaluate_lstm_models(data,directory,rf_model_dir="rf",
            rf_loader=lambda _: (FakeRfModel(),wrong),
            lstm_loader=_fake_lstm_loader(FakeLstmModel(),FakeScaler(),manifest))


def test_events_never_match_across_boot_or_gap():
    from ai_model.evaluate import _collect_events_and_episodes
    from ai_model.windows import Window, WindowMeta
    def window(first,boot,features,targets):
        return Window(features,targets,WindowMeta('device',boot,'same-session',first,first+9,first+14,None))
    first=window(1,'b1',[.8]*10,[.3]*5)
    second=window(1,'b2',[.8]*10,[.3]*5)
    events,episodes=_collect_events_and_episodes([first,second],{'lstm':[[.3]*5,[.3]*5]},
        (1,2,3,4,5),.1,.5,4,.44)
    assert len(events)==2
    assert len(episodes['lstm'])==2
    clear=window(1,'b1',[.8]*10,[.8]*5)
    after_gap=window(100,'b1',[.3]*10,[.3]*5)
    events,_=_collect_events_and_episodes([clear,after_gap],{'lstm':[[.8]*5,[.3]*5]},
        (1,2,3,4,5),.1,.5,4,.44)
    assert events==[]
