"""Kiểm thử SafetyPredictor LSTM: buffer trượt, fail-safe và schema output.

Các test dùng model/scaler LSTM nhỏ được inject qua loader để giữ coverage
runtime mà không cần TensorFlow hoặc artifact train thật.
"""

import json

import numpy as np
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker

from iot_backend.inference.predictor import SafetyPredictor

REPO_ROOT = Path(__file__).resolve().parents[1]

HORIZONS_MS = [100, 200, 300, 400, 500]
SP_M = 0.44
MEASURED_AT = "2026-01-01T00:00:00.000Z"
NOW_ISO = MEASURED_AT

MANIFEST = {
    "model_type": "lstm",
    "model_version": "lstm-v1",
    "preprocessing_version": "pp-lstm-v1",
    "input_shape": [10, 2],
    "output_shape": [5],
    "features": ["distance_m", "delta_distance_m"],
    "units": "m",
    "window_size": 10,
    "delta_s": 0.1,
    "horizons_ms": HORIZONS_MS,
}


class FakeScaler:
    def transform_X(self, values):
        return np.asarray(values, dtype="float64")

    def inverse_y(self, values):
        return np.asarray(values, dtype="float64")


class FakeLstmModel:
    """Deterministic injected LSTM double: constant-velocity forecast."""

    def __call__(self, values, training=False):
        assert training is False
        values = np.asarray(values, dtype="float64")
        distances = values[:, :, 0]
        velocity = (distances[:, -1] - distances[:, 0]) / (9 * 0.1)
        horizons = np.asarray(HORIZONS_MS, dtype="float64") / 1000.0
        return distances[:, -1, None] + velocity[:, None] * horizons


@pytest.fixture(scope="module")
def model_config() -> dict:
    return yaml.safe_load((REPO_ROOT / "configs" / "model.yaml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def model_dir(tmp_path_factory) -> Path:
    """Path placeholder; the injected loader supplies the LSTM artifact."""
    return tmp_path_factory.mktemp("lstm_runtime_double")


def make_lstm_predictor(model_dir, model_config, model=None):
    def loader(_directory):
        return model or FakeLstmModel(), FakeScaler(), MANIFEST

    return SafetyPredictor(
        model_dir, model_config, loader, model_type="lstm"
    )

def make_sample(seq: int, mono_us: int, distance_mm: int | None, **overrides) -> dict:
    """Payload telemetry_v1 11 field hợp lệ (echo_us xấp xỉ ~5.83 µs/mm)."""
    sample = {
        "schema_version": "1.0",
        "device_id": "esp32_sim",
        "boot_id": "b1",
        "sequence": seq,
        "monotonic_us": mono_us,
        "measured_at": MEASURED_AT,
        "time_synced": True,
        "distance_mm": distance_mm,
        "echo_us": None
        if distance_mm is None
        else max(1, int(round(distance_mm * 5.83))),
        "valid": True,
        "error_code": None,
    }
    sample.update(overrides)
    return sample


def push_run(predictor: SafetyPredictor, seqs, mono_start: int = 1_000_000) -> dict:
    """Push các mẫu liên tiếp (sequence +1, nhịp đúng 0.1 s, 900 mm);
    trả prediction của mẫu CUỐI."""
    result = None
    for seq in seqs:
        result = predictor.predict(
            make_sample(seq, mono_start + seq * 100_000, 900),
            None,
            SP_M,
            "live",
            now_iso=NOW_ISO,
        )
    assert result is not None
    return result


# ----------------------------------------------------------- WARMUP và OK


def test_warmup_below_window_size(model_dir, model_config):
    """9 mẫu đầu (< window_size 10) → WARMUP, mọi field tương lai null."""
    predictor = make_lstm_predictor(model_dir, model_config)
    for seq in range(1, 10):
        result = predictor.predict(
            make_sample(seq, 1_000_000 + seq * 100_000, 900),
            None,
            SP_M,
            "live",
            now_iso=NOW_ISO,
        )
        assert result["status"] == "WARMUP"
        assert result["model_version"] is None
        assert result["distance_m"] is None
        assert result["early_warning"] is None
        assert result["window_ref"] is None
        assert result["horizons_ms"] == HORIZONS_MS
        assert result["stale"] is False
        assert result["mode"] == "live"


def test_tenth_sample_ok_window_ref(model_dir, model_config):
    """Mẫu thứ 10 → OK: distance_m 5 float, window_ref đúng cửa sổ 1..10."""
    predictor = make_lstm_predictor(model_dir, model_config)
    result = push_run(predictor, range(1, 11))
    assert result["status"] == "OK"
    assert result["model_version"] is not None
    assert isinstance(result["distance_m"], list)
    assert len(result["distance_m"]) == 5
    assert all(isinstance(value, float) for value in result["distance_m"])
    assert result["window_ref"] == {
        "device_id": "esp32_sim",
        "boot_id": "b1",
        "sequence_first": 1,
        "sequence_last": 10,
        "sample_count": 10,
        "max_age_ms": 900.0,  # (window_size−1) * delta_s * 1000
    }


def test_window_slides_on_eleventh_sample(model_dir, model_config):
    """Mẫu 11 → vẫn OK, cửa sổ trượt: sequence_first 2, sequence_last 11."""
    predictor = make_lstm_predictor(model_dir, model_config)
    push_run(predictor, range(1, 11))
    result = predictor.predict(
        make_sample(11, 1_000_000 + 11 * 100_000, 900),
        None,
        SP_M,
        "live",
        now_iso=NOW_ISO,
    )
    assert result["status"] == "OK"
    assert len(result["distance_m"]) == 5
    assert result["window_ref"]["sequence_first"] == 2
    assert result["window_ref"]["sequence_last"] == 11


# --------------------------------------------------------- INVALID_WINDOW


def test_invalid_sample_resets_buffer(model_dir, model_config):
    """Mẫu invalid → INVALID_WINDOW; mẫu kế tiếp valid → WARMUP (đã reset)."""
    predictor = make_lstm_predictor(model_dir, model_config)
    push_run(predictor, range(1, 11))  # đủ window_size

    invalid = predictor.predict(
        make_sample(
            11, 1_000_000 + 11 * 100_000, None,
            valid=False, error_code="ECHO_TIMEOUT",
        ),
        None,
        SP_M,
        "live",
        now_iso=NOW_ISO,
    )
    assert invalid["status"] == "INVALID_WINDOW"
    assert invalid["model_version"] is None
    assert invalid["distance_m"] is None
    assert invalid["early_warning"] is None
    assert invalid["window_ref"] is None

    next_valid = predictor.predict(
        make_sample(12, 1_000_000 + 12 * 100_000, 900),
        None,
        SP_M,
        "live",
        now_iso=NOW_ISO,
    )
    # Sau reset buffer chỉ còn 1 mẫu (mẫu vừa nhận) → WARMUP, không OK.
    assert next_valid["status"] == "WARMUP"


def test_sequence_gap_resets_buffer(model_dir, model_config):
    """Sequence nhảy (+2) → INVALID_WINDOW; mẫu nối tiếp sau đó → WARMUP."""
    predictor = make_lstm_predictor(model_dir, model_config)
    push_run(predictor, range(1, 11))

    gap = predictor.predict(
        make_sample(12, 1_000_000 + 12 * 100_000, 900),
        None,
        SP_M,
        "live",
        now_iso=NOW_ISO,
    )
    assert gap["status"] == "INVALID_WINDOW"
    assert gap["distance_m"] is None
    assert gap["window_ref"] is None

    resumed = predictor.predict(
        make_sample(13, 1_000_000 + 13 * 100_000, 900),
        None,
        SP_M,
        "live",
        now_iso=NOW_ISO,
    )
    assert resumed["status"] == "WARMUP"


# ------------------------------------------------------- MODEL_UNAVAILABLE


def test_model_unavailable_empty_model_dir(tmp_path, model_config):
    """model_dir rỗng → available False, predict MODEL_UNAVAILABLE, không raise."""
    predictor = SafetyPredictor(tmp_path, model_config)
    assert predictor.available is False

    result = predictor.predict(
        make_sample(1, 1_100_000, 900), None, SP_M, "live", now_iso=NOW_ISO
    )
    assert result["status"] == "MODEL_UNAVAILABLE"
    assert result["model_version"] is None
    assert result["distance_m"] is None
    assert result["early_warning"] is None
    assert result["window_ref"] is None
    assert result["horizons_ms"] == HORIZONS_MS  # vẫn đúng từ config


# ------------------------------------------------------------ early_warning


def test_early_warning_before_crossing_sp(model_dir, model_config):
    """Chuỗi giảm 5 cm/mẫu từ 0.9 m: current > Sp mà min dự báo ≤ Sp → True;
    current đã ≤ Sp → False (đó là VIOLATION — việc của khối safety)."""
    predictor = make_lstm_predictor(model_dir, model_config)
    result = None
    for seq, mm in enumerate(range(900, 400, -50), start=1):  # 900..450
        result = predictor.predict(
            make_sample(seq, 1_000_000 + seq * 100_000, mm),
            None,
            SP_M,
            "live",
            now_iso=NOW_ISO,
        )
    # Mẫu 10: current 0.45 m > Sp 0.44, dự báo xa nhất chạm dưới Sp.
    assert result["status"] == "OK"
    assert result["early_warning"] is True

    below = predictor.predict(
        make_sample(11, 1_000_000 + 11 * 100_000, 400),
        None,
        SP_M,
        "live",
        now_iso=NOW_ISO,
    )
    # Mẫu 11: current 0.40 m ≤ Sp → False dù dự báo vẫn dưới Sp.
    assert below["status"] == "OK"
    assert below["early_warning"] is False


# ------------------------------------------------------------------ stale


def test_stale_flag(model_dir, model_config):
    """stale: tuổi mẫu > stale_after_s (0.5 s) → True; measured_at None →
    False; mode 'offline' → False (kể cả mẫu cũ)."""
    predictor = make_lstm_predictor(model_dir, model_config)
    old_now = "2026-01-01T00:05:00.000Z"  # age 300 s >> stale_after_s 0.5 s

    stale = predictor.predict(
        make_sample(1, 1_100_000, 900), None, SP_M, "live", now_iso=old_now
    )
    assert stale["stale"] is True

    unsynced = predictor.predict(
        make_sample(2, 1_200_000, 900, measured_at=None, time_synced=False),
        None,
        SP_M,
        "live",
        now_iso=old_now,
    )
    assert unsynced["stale"] is False

    offline = predictor.predict(
        make_sample(3, 1_300_000, 900), None, SP_M, "offline", now_iso=old_now
    )
    assert offline["stale"] is False
    assert offline["mode"] == "offline"


# ------------------------------------------------- buffer per (device, boot)


def test_buffers_separated_per_boot(model_dir, model_config):
    """Boot khác → buffer riêng: b1 đủ 10 mẫu OK; 1 mẫu b2 → WARMUP; b1
    không bị ảnh hưởng."""
    predictor = make_lstm_predictor(model_dir, model_config)
    result = push_run(predictor, range(1, 11))
    assert result["status"] == "OK"
    assert result["window_ref"]["boot_id"] == "b1"

    other_boot = predictor.predict(
        make_sample(1, 5_000_000, 900, boot_id="b2"),
        None,
        SP_M,
        "live",
        now_iso=NOW_ISO,
    )
    assert other_boot["status"] == "WARMUP"

    b1_next = predictor.predict(
        make_sample(11, 1_000_000 + 11 * 100_000, 900),
        None,
        SP_M,
        "live",
        now_iso=NOW_ISO,
    )
    assert b1_next["status"] == "OK"  # buffer của b1 nguyên vẹn
    assert b1_next["window_ref"]["boot_id"] == "b1"
    assert b1_next["window_ref"]["sequence_first"] == 2


# ---------------------------------------------------------- schema assessment


def test_ok_prediction_validates_assessment_v1_schema(model_dir, model_config):
    """Khối prediction OK nhét vào assessment đầy đủ phải PASS
    Draft 2020-12 (FormatChecker) với schemas/assessment_v1.schema.json."""
    predictor = make_lstm_predictor(model_dir, model_config)
    prediction = push_run(predictor, range(1, 11))
    assert prediction["status"] == "OK"

    # Contract đầu ra: đúng 8 field (schema khai additionalProperties: false).
    assert set(prediction) == {
        "status",
        "model_version",
        "horizons_ms",
        "distance_m",
        "early_warning",
        "stale",
        "window_ref",
        "mode",
        "preprocessing_version",
        "predicted_violation_ms",
    }

    record = {
        "schema_version": "1.0",
        "sample_ref": {"device_id": "esp32_sim", "boot_id": "b1", "sequence": 10},
        "session_id": None,
        "measured_at": MEASURED_AT,
        "computed_at": "2026-01-01T00:00:00.050Z",
        "received_at": MEASURED_AT,
        "distance_m": 0.9,
        "safety": {
            "status": "CLEAR",
            "sensor_valid": True,
            "assessment_valid": True,
            "sp_m": SP_M,
            "margin_m": 0.46,
            "config_version": "demo-v1",
            "config_time_source": "measured_at",
            "components": {
                "sh_m": 0.2,
                "sr_m": 0.1,
                "ss_m": 0.02,
                "c_m": 0.05,
                "zd_m": 0.03,
                "zr_m": 0.02,
            },
            "config_fit_warnings": [],
        },
        "prediction": prediction,
    }
    schema = json.loads(
        (REPO_ROOT / "schemas" / "assessment_v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    errors = list(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(
            record
        )
    )
    assert errors == []
