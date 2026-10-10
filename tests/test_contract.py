"""Kiểm thử contract cấu hình LSTM và JSON Schema (TV3–TV4).

Kiểm tra identity LSTM ở top-level, kiến trúc/windowing, assessment status
branches và manifest shape/hash. Validate bằng Draft 2020-12 + FormatChecker.
"""

import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker


REPO_ROOT = Path(__file__).resolve().parents[1]

HORIZONS_MS = [100, 200, 300, 400, 500]


# ----------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def model_config() -> dict:
    return yaml.safe_load(
        (REPO_ROOT / "configs" / "model.yaml").read_text(encoding="utf-8")
    )


def _validator(name: str) -> Draft202012Validator:
    schema = json.loads(
        (REPO_ROOT / "schemas" / name).read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


@pytest.fixture(scope="module")
def assessment_validator() -> Draft202012Validator:
    return _validator("assessment_v1.schema.json")


@pytest.fixture(scope="module")
def manifest_validator() -> Draft202012Validator:
    return _validator("model_manifest_v1.schema.json")


def _base_record() -> dict:
    """Bản ghi assessment không cần field preprocessing mở rộng."""
    return {
        "schema_version": "1.0",
        "sample_ref": {"device_id": "esp32_01", "boot_id": "b1", "sequence": 125},
        "session_id": "run_001",
        "measured_at": "2026-09-21T12:00:12.500Z",
        "computed_at": "2026-09-21T12:00:12.700Z",
        "received_at": "2026-09-21T12:00:12.600Z",
        "distance_m": 0.643,
        "safety": {
            "status": "CLEAR",
            "sensor_valid": True,
            "assessment_valid": True,
            "sp_m": 0.44,
            "margin_m": 0.203,
            "config_version": "demo-v1",
            "config_time_source": "measured_at",
            "components": {
                "sh_m": 0.30, "sr_m": 0.02, "ss_m": 0.02,
                "c_m": 0.05, "zd_m": 0.03, "zr_m": 0.02,
            },
            "config_fit_warnings": [],
        },
        "prediction": {
            "status": "OK",
            "model_version": "lstm-v1",
            "horizons_ms": HORIZONS_MS,
            "distance_m": [0.60, 0.56, 0.51, 0.47, 0.42],
            "early_warning": True,
            "stale": False,
            "window_ref": {
                "device_id": "esp32_01", "boot_id": "b1",
                "sequence_first": 116, "sequence_last": 125,
                "sample_count": 10, "max_age_ms": 900,
            },
            "mode": "live",
        },
    }


def _lstm_record(prediction_overrides: dict | None = None) -> dict:
    """Bản ghi assessment LSTM mới: thêm 2 field optional trong prediction."""
    record = _base_record()
    record["prediction"] = {
        **record["prediction"],
        "model_version": "lstm-v1",
        "preprocessing_version": "pp-lstm-v1",
        "predicted_violation_ms": 300,
    }
    if prediction_overrides:
        record["prediction"].update(prediction_overrides)
    return record


# ------------------------------------------------------------ configs/model.yaml


def test_config_yaml_parses(model_config):
    """YAML parse được và là object."""
    assert isinstance(model_config, dict)


def test_config_lstm_target_values(model_config):
    """Khối lstm đích đúng spec TV3–TV4, từng giá trị một."""
    lstm = model_config["lstm"]
    assert lstm["model_type"] == "lstm"
    assert lstm["model_version"] == "lstm-v1"
    assert lstm["preprocessing_version"] == "pp-lstm-v1"
    assert lstm["window_size"] == 10
    assert lstm["delta_s"] == pytest.approx(0.1)
    assert lstm["horizons_ms"] == HORIZONS_MS
    assert lstm["features"] == ["distance_m", "delta_distance_m"]
    assert lstm["input_shape"] == [10, 2]
    assert lstm["output_shape"] == [5]
    # Key kiến trúc đặt lstm_units (không phải units) để không ambiguity với
    # `units: m` (đơn vị mét) trong model_manifest_v1.schema.json.
    assert "units" not in lstm
    assert lstm["lstm_units"] == 32
    assert lstm["dropout"] == pytest.approx(0.2)
    assert lstm["dense_units"] == 32
    assert lstm["dense_activation"] == "relu"
    assert lstm["batch_size"] == 32
    assert lstm["max_epochs"] == 100
    assert lstm["learning_rate"] == pytest.approx(0.001)
    assert lstm["early_stopping_patience"] == 10


def test_config_lstm_loss_and_callbacks(model_config):
    """Loss Huber theo PLAN mục 4; đủ 3 callback nhất quán với nhau."""
    lstm = model_config["lstm"]
    assert lstm["loss"] == "huber"
    assert lstm["optimizer"] == "adam"

    callbacks = lstm["callbacks"]
    es = callbacks["early_stopping"]
    assert es["monitor"] == "val_loss"
    assert es["mode"] == "min"
    assert es["patience"] == lstm["early_stopping_patience"]
    assert es["restore_best_weights"] is True

    rlrop = callbacks["reduce_lr"]
    assert rlrop["monitor"] == "val_loss"
    assert rlrop["mode"] == "min"
    assert rlrop["factor"] == pytest.approx(0.5)
    assert rlrop["patience"] == 5
    assert rlrop["min_lr"] == pytest.approx(0.000001)

    ckpt = callbacks["model_checkpoint"]
    assert ckpt["monitor"] == "val_loss"
    assert ckpt["mode"] == "min"
    assert ckpt["save_best_only"] is True
    # Artifact-relative placeholder, không đường dẫn tuyệt đối.
    assert ckpt["filepath"] == "model.keras"


def test_config_lstm_no_internal_contradiction(model_config):
    """Key trong khối lstm không mâu thuẫn khối dùng chung / callback."""
    lstm = model_config["lstm"]
    # Window khai lại trong khối phải trùng khối dùng chung.
    for key in ("window_size", "delta_s", "horizons_ms"):
        assert lstm[key] == model_config[key], key
    # Callback early_stopping nhất quán với early_stopping_patience.
    es = lstm["callbacks"]["early_stopping"]
    assert es["patience"] == lstm["early_stopping_patience"]
    assert es["monitor"] == "val_loss"
    assert es["mode"] == "min"
    assert es["restore_best_weights"] is True
    # Loss/optimizer/metrics khai tường minh, không trống.
    assert lstm["loss"]
    assert lstm["optimizer"]
    assert lstm["metrics"]


def test_config_lstm_top_level_identity(model_config):
    assert model_config["model_type"] == "lstm"
    assert model_config["model_version"] == "lstm-v1"
    assert model_config["preprocessing_version"] == "pp-lstm-v1"


def test_config_shared_window_keys(model_config):
    """Khối dùng chung giữ đủ key window + chất lượng dữ liệu cho runtime."""
    assert model_config["window_size"] == 10
    assert model_config["delta_s"] == pytest.approx(0.1)
    assert model_config["horizons_ms"] == HORIZONS_MS
    assert model_config["max_time_jitter_s"] == pytest.approx(0.03)
    assert model_config["max_gap_s"] == pytest.approx(0.25)
    assert model_config["stale_after_s"] == pytest.approx(0.5)
    assert model_config["random_seed"] == 42


# ----------------------------------------------- schemas/assessment_v1.schema.json


def test_assessment_record_without_optional_fields_still_valid(assessment_validator):
    """Bản ghi không có field mở rộng vẫn hợp lệ — backward compatible."""
    assert list(assessment_validator.iter_errors(_base_record())) == []


def test_assessment_lstm_record_with_violation_ms_valid(assessment_validator):
    """Bản ghi LSTM mới: preprocessing_version + predicted_violation_ms=300."""
    errors = list(assessment_validator.iter_errors(_lstm_record()))
    assert errors == []


def test_assessment_predicted_violation_ms_null_valid(assessment_validator):
    """predicted_violation_ms=null (không vi phạm / chưa dự báo được) hợp lệ,
    kể cả khi status OK và cả khi không có preprocessing_version."""
    null_ms = _lstm_record({"predicted_violation_ms": None})
    assert list(assessment_validator.iter_errors(null_ms)) == []

    base_null_ms = _base_record()
    base_null_ms["prediction"]["predicted_violation_ms"] = None
    assert list(assessment_validator.iter_errors(base_null_ms)) == []


def test_assessment_rejects_extra_property(assessment_validator):
    """additionalProperties: false vẫn chặn field lạ ở mọi cấp."""
    extra_prediction = _lstm_record({"khong_hop_le": True})
    errors = list(assessment_validator.iter_errors(extra_prediction))
    assert errors and "khong_hop_le" in str(errors[0].message)

    extra_record = _lstm_record()
    extra_record["field_lam"] = 1
    errors = list(assessment_validator.iter_errors(extra_record))
    assert errors and "field_lam" in str(errors[0].message)


def test_assessment_non_ok_prediction_null_fields(assessment_validator):
    """status != OK: các field dự báo (cũ lẫn mới) phải null — có mặt thì
    phải là null; distance_m khác null bị từ chối."""
    non_ok = _lstm_record(
        {
            "status": "MODEL_UNAVAILABLE",
            "model_version": None,
            "preprocessing_version": None,
            "distance_m": None,
            "early_warning": None,
            "window_ref": None,
            "predicted_violation_ms": None,
        }
    )
    assert list(assessment_validator.iter_errors(non_ok)) == []

    # Field mới có giá trị khi status != OK → vi phạm nhánh "then".
    bad = _lstm_record(
        {
            "status": "WARMUP",
            "predicted_violation_ms": 300,
        }
    )
    errors = list(assessment_validator.iter_errors(bad))
    assert errors

    # distance_m khác null khi status != OK → từ chối (contract hiện tại).
    bad_distance = _lstm_record(
        {
            "status": "WARMUP",
            "distance_m": [0.5] * 5,
        }
    )
    errors = list(assessment_validator.iter_errors(bad_distance))
    assert errors


def test_assessment_ok_prediction_requires_valid_fields(assessment_validator):
    """status OK: distance_m phải là mảng, early_warning boolean,
    window_ref object — null bị từ chối."""
    ok_null_distance = _lstm_record({"distance_m": None})
    errors = list(assessment_validator.iter_errors(ok_null_distance))
    assert errors

    ok_null_warning = _lstm_record({"early_warning": None})
    assert list(assessment_validator.iter_errors(ok_null_warning))

    ok_null_window = _lstm_record({"window_ref": None})
    assert list(assessment_validator.iter_errors(ok_null_window))


def test_assessment_horizons_ms_fixed_five(assessment_validator):
    """horizons_ms luôn đúng 5 phần tử (khớp config) — ít hơn bị từ chối."""
    short = _lstm_record({"horizons_ms": [100, 200, 300]})
    errors = list(assessment_validator.iter_errors(short))
    assert errors


def test_assessment_predicted_violation_ms_must_be_a_horizon(assessment_validator):
    """predicted_violation_ms phải là một trong các horizon của config;
    giá trị ngoài [100,200,300,400,500] (kể cả 0 và 250) bị từ chối."""
    for bad in (0, 50, 250, 600):
        record = _lstm_record({"predicted_violation_ms": bad})
        errors = list(assessment_validator.iter_errors(record))
        assert errors, f"predicted_violation_ms={bad} phải bị từ chối"
    # Mỗi horizon hợp lệ đều chấp nhận.
    for good in (100, 200, 300, 400, 500):
        record = _lstm_record({"predicted_violation_ms": good})
        assert list(assessment_validator.iter_errors(record)) == [], good


# --------------------------------------------- schemas/model_manifest_v1.schema.json


def _lstm_manifest() -> dict:
    return {
        "schema_version": "1.0",
        "model_type": "lstm",
        "model_version": "lstm-v1",
        "preprocessing_version": "pp-lstm-v1",
        "framework": {
            "name": "tensorflow",
            "version": "2.20.0",
            "keras_version": "3.15.1",
        },
        "input_shape": [10, 2],
        "output_shape": [5],
        "features": ["distance_m", "delta_distance_m"],
        "units": "m",
        "window_size": 10,
        "delta_s": 0.1,
        "horizons_ms": HORIZONS_MS,
        "random_seed": 42,
        "model_file_sha256": "a" * 64,
        "dataset_sha256": "b" * 64,
        "split_manifest_sha256": "c" * 64,
        "created_at": "2026-09-30T00:00:00.000Z",
    }


def test_manifest_lstm_valid(manifest_validator, model_config):
    """Manifest LSTM mẫu (khớp config đích) hợp lệ qua Draft 2020-12."""
    manifest = _lstm_manifest()
    lstm = model_config["lstm"]
    # Đồng bộ với config đích (chặn manifest với config lệch nhau).
    assert manifest["model_version"] == lstm["model_version"]
    assert manifest["preprocessing_version"] == lstm["preprocessing_version"]
    assert manifest["features"] == lstm["features"]
    assert manifest["input_shape"] == lstm["input_shape"]
    assert manifest["output_shape"] == lstm["output_shape"]
    assert manifest["window_size"] == lstm["window_size"]
    assert manifest["horizons_ms"] == lstm["horizons_ms"]
    assert manifest["random_seed"] == model_config["random_seed"]
    assert list(manifest_validator.iter_errors(manifest)) == []


def test_manifest_rejects_bad_shape(manifest_validator):
    """input_shape [10,3] hoặc output_shape [4] → từ chối."""
    bad_input = _lstm_manifest()
    bad_input["input_shape"] = [10, 3]
    assert list(manifest_validator.iter_errors(bad_input))

    bad_output = _lstm_manifest()
    bad_output["output_shape"] = [4]
    assert list(manifest_validator.iter_errors(bad_output))


def test_manifest_rejects_bad_sha256_and_model_type(manifest_validator):
    """sha256 sai định dạng hoặc model_type khác lstm → từ chối."""
    bad_sha = _lstm_manifest()
    bad_sha["model_file_sha256"] = "khong-phai-sha256"
    assert list(manifest_validator.iter_errors(bad_sha))

    bad_type = _lstm_manifest()
    bad_type["model_type"] = "gru"
    assert list(manifest_validator.iter_errors(bad_type))


def test_manifest_requires_dataset_and_split_sha256(manifest_validator):
    """dataset_sha256 + split_manifest_sha256 là bắt buộc, pattern 64 hex
    (PLAN mục 4)."""
    assert "dataset_sha256" in manifest_validator.schema["required"]
    assert "split_manifest_sha256" in manifest_validator.schema["required"]

    missing = _lstm_manifest()
    del missing["dataset_sha256"]
    assert list(manifest_validator.iter_errors(missing))

    bad_hex = _lstm_manifest()
    bad_hex["split_manifest_sha256"] = "XYZ"
    assert list(manifest_validator.iter_errors(bad_hex))


def test_manifest_rejects_window_size_and_horizons_drift(
    manifest_validator, model_config
):
    """window_size khác 10 hoặc horizons lệch 5 phần tử/giá trị config →
    từ chối (không mâu thuẫn input/output shape)."""
    bad_window = _lstm_manifest()
    bad_window["window_size"] = 12
    assert list(manifest_validator.iter_errors(bad_window))

    bad_count = _lstm_manifest()
    bad_count["horizons_ms"] = [100, 200, 300, 400]
    assert list(manifest_validator.iter_errors(bad_count))

    bad_value = _lstm_manifest()
    bad_value["horizons_ms"] = [100, 200, 250, 400, 500]
    assert list(manifest_validator.iter_errors(bad_value))

    # Giá trị đúng phải khớp config đích.
    assert _lstm_manifest()["horizons_ms"] == model_config["lstm"]["horizons_ms"]
    assert _lstm_manifest()["window_size"] == model_config["lstm"]["window_size"]


def test_manifest_rejects_extra_property(manifest_validator):
    """additionalProperties: false chặn field lạ."""
    manifest = _lstm_manifest()
    manifest["tu_do_them"] = 1
    errors = list(manifest_validator.iter_errors(manifest))
    assert errors and "tu_do_them" in str(errors[0].message)
