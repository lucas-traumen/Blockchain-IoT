"""Kiểm thử M1: công thức Sp, assess_sample, loader cấu hình safety và JSON Schema v1."""

import copy
import json
from pathlib import Path

import jsonschema
import pytest
import yaml

from backend.safety import (
    SafetyParams,
    assess_sample,
    get_config_for_time,
    load_safety_configs,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ZONE_MIN_M = 0.10
ZONE_MAX_M = 1.00


def _fixture_params() -> SafetyParams:
    """Bộ tham số đối chiếu PLAN.md mục 3 → Sp = 0.44 m chính xác."""
    return SafetyParams(
        vh_m_s=1.0, vr_m_s=0.2, tr_s=0.1, ts_s=0.2, c_m=0.05, zd_m=0.03, zr_m=0.02
    )


def _version_dict(config_version: str, valid_from: str, **param_overrides: float) -> dict:
    """Tạo một phần tử 'versions' hợp lệ cho yaml tạm trong test."""
    params: dict[str, float] = {
        "vh": 1.0, "vr": 0.2, "tr_s": 0.1, "ts_s": 0.2,
        "c_m": 0.05, "zd_m": 0.03, "zr_m": 0.02,
    }
    params.update(param_overrides)
    return {
        "config_version": config_version,
        "valid_from": valid_from,
        "params": params,
        "measurement_zone_m": {"min": ZONE_MIN_M, "max": ZONE_MAX_M},
    }


def _telemetry_reference_payload() -> dict:
    """Payload telemetry mẫu đúng như PLAN.md mục 2."""
    return {
        "schema_version": "1.0",
        "device_id": "esp32_01",
        "boot_id": "unique-per-boot",
        "sequence": 125,
        "monotonic_us": 12500000,
        "measured_at": "2026-09-21T12:00:12.500Z",
        "time_synced": True,
        "distance_mm": 643,
        "echo_us": 3746,
        "valid": True,
        "error_code": None,
    }


def _assessment_reference_record() -> dict:
    """Bản ghi bàn giao TV2 mẫu đúng như PLAN.md mục 2 (assessment_v1)."""
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
            "model_version": "rf-v1",
            "horizons_ms": [100, 200, 300, 400, 500],
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


def _load_schema(name: str) -> dict:
    return json.loads((REPO_ROOT / "schemas" / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Công thức Sp (backend/safety/sp.py)
# ---------------------------------------------------------------------------


def test_fixture_sp_0_44():
    """Fixture PLAN.md mục 3: Sp = 0.44 m; từng thành phần đúng giá trị chuẩn."""
    params = _fixture_params()
    assert params.sp_m == pytest.approx(0.44)
    components = params.components()
    assert components["sh_m"] == pytest.approx(0.30)
    assert components["sr_m"] == pytest.approx(0.02)
    assert components["ss_m"] == pytest.approx(0.02)
    assert components["c_m"] == pytest.approx(0.05)
    assert components["zd_m"] == pytest.approx(0.03)
    assert components["zr_m"] == pytest.approx(0.02)
    assert set(components) == {"sh_m", "sr_m", "ss_m", "c_m", "zd_m", "zr_m"}


# ---------------------------------------------------------------------------
# assess_sample: ranh giới CLEAR/VIOLATION và chuyển đổi đơn vị
# ---------------------------------------------------------------------------


def test_boundary_at_sp_is_violation():
    """Khoảng cách đúng bằng Sp (440 mm = 0.44 m) là VIOLATION, margin = 0."""
    result = assess_sample(440, True, _fixture_params())
    assert result.status == "VIOLATION"
    assert result.margin_m == pytest.approx(0.0, abs=1e-9)
    assert result.assessment_valid is True
    assert result.sensor_valid is True


def test_clear_above_boundary():
    """441 mm → CLEAR với margin ≈ 0.001 m."""
    result = assess_sample(441, True, _fixture_params())
    assert result.status == "CLEAR"
    assert result.margin_m == pytest.approx(0.001)


def test_violation_below():
    """439 mm → VIOLATION với margin âm."""
    result = assess_sample(439, True, _fixture_params())
    assert result.status == "VIOLATION"
    assert result.margin_m < 0


def test_unit_conversion_mm_to_m():
    """643 mm = 0.643 m → CLEAR, margin ≈ 0.203 m; chống nhầm đơn vị mm↔m."""
    result = assess_sample(643, True, _fixture_params())
    assert result.status == "CLEAR"
    assert result.distance_m == pytest.approx(0.643)
    assert result.margin_m == pytest.approx(0.203)
    # Nếu nhầm 643 mm thành 643 m thì margin sẽ cỡ hàng trăm mét — phải < 1.0 m.
    assert result.margin_m < 1.0


# ---------------------------------------------------------------------------
# assess_sample: các nhánh UNKNOWN
# ---------------------------------------------------------------------------


def test_unknown_on_sensor_error():
    """Cảm biến lỗi, không có khoảng cách → UNKNOWN, không có Sp/margin."""
    result = assess_sample(None, False, _fixture_params())
    assert result.status == "UNKNOWN"
    assert result.sensor_valid is False
    assert result.assessment_valid is False
    assert result.sp_m is None
    assert result.margin_m is None
    assert result.components is None
    assert result.config_fit_warnings == []


def test_unknown_on_valid_false_with_distance():
    """Cảm biến lỗi nhưng vẫn có giá trị distance_mm → không tin cảm biến, distance_m = None."""
    result = assess_sample(500, False, _fixture_params())
    assert result.status == "UNKNOWN"
    assert result.distance_m is None
    assert result.sp_m is None
    assert result.assessment_valid is False


def test_unknown_on_missing_params():
    """Cảm biến tốt nhưng thiếu tham số cấu hình (params=None) → UNKNOWN."""
    result = assess_sample(500, True, None)
    assert result.status == "UNKNOWN"
    assert result.assessment_valid is False
    assert result.sp_m is None
    assert result.margin_m is None
    assert result.components is None


def test_sensor_valid_vs_assessment_valid():
    """sensor_valid phản ánh cảm biến, assessment_valid phản ánh khả năng đánh giá.

    Trong test này chỉ xét 2 trường hợp: mẫu tốt (cả hai True) và lỗi cảm biến
    (cả hai False). Không có trường hợp sensor_valid=True + assessment_valid=False
    ở đây vì nó chỉ xảy ra khi thiếu tham số cấu hình (params=None, cảm biến vẫn
    tin cậy) — đã được phủ riêng ở test_unknown_on_missing_params.
    """
    good = assess_sample(643, True, _fixture_params())
    assert good.sensor_valid is True
    assert good.assessment_valid is True

    bad = assess_sample(None, False, _fixture_params())
    assert bad.sensor_valid is False
    assert bad.assessment_valid is False


# ---------------------------------------------------------------------------
# Cấu hình safety: loader + tra cứu theo thời điểm
# ---------------------------------------------------------------------------


def test_config_yaml_load_and_time_lookup():
    """Load configs/safety.yaml thật từ repo; tra cứu theo thời điểm và từ chối thời điểm quá sớm."""
    configs = load_safety_configs(REPO_ROOT / "configs" / "safety.yaml")
    assert len(configs) == 1
    assert configs[0].config_version == "demo-v1"
    assert configs[0].valid_from == "2026-01-01T00:00:00Z"
    assert configs[0].zone_min_m == pytest.approx(0.10)
    assert configs[0].zone_max_m == pytest.approx(1.00)
    assert configs[0].params.sp_m == pytest.approx(0.44)

    selected = get_config_for_time(configs, "2026-06-01T00:00:00Z")
    assert selected.config_version == "demo-v1"

    # Trước valid_from của mọi phiên bản → không có cấu hình hiệu lực.
    with pytest.raises(ValueError):
        get_config_for_time(configs, "2025-01-01T00:00:00Z")


def test_config_time_boundary_inclusive(tmp_path):
    """Tại đúng valid_from của phiên bản sau (bao gồm biên) phải chọn phiên bản sau."""
    data = {
        "versions": [
            _version_dict("v-a", "2026-01-01"),
            _version_dict("v-b", "2026-02-01"),
        ]
    }
    path = tmp_path / "safety_boundary.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")

    configs = load_safety_configs(path)
    assert get_config_for_time(configs, "2026-01-01T00:00:00Z").config_version == "v-a"
    assert get_config_for_time(configs, "2026-01-31T23:59:59Z").config_version == "v-a"
    # Đúng biên: phiên bản sau đã có hiệu lực.
    assert get_config_for_time(configs, "2026-02-01T00:00:00Z").config_version == "v-b"


def test_config_duplicate_version_rejected(tmp_path):
    """Hai phiên bản trùng config_version → ValueError khi load."""
    data = {
        "versions": [
            _version_dict("dup", "2026-01-01T00:00:00Z"),
            _version_dict("dup", "2026-03-01T00:00:00Z"),
        ]
    }
    path = tmp_path / "safety_duplicate.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")

    with pytest.raises(ValueError, match="dup"):
        load_safety_configs(path)


# ---------------------------------------------------------------------------
# Cảnh báo cấu hình (config_fit_warnings) — không chặn, không giảm Sp
# ---------------------------------------------------------------------------


def test_zone_warning_sp_above_zone():
    """Sp vượt zone max → cảnh báo 'exceeds validated measurement zone', Sp không bị giảm."""
    params = SafetyParams(
        vh_m_s=4.0, vr_m_s=0.2, tr_s=0.1, ts_s=0.2, c_m=0.05, zd_m=0.03, zr_m=0.02
    )
    result = assess_sample(2000, True, params, zone_min_m=ZONE_MIN_M, zone_max_m=ZONE_MAX_M)
    assert result.status == "CLEAR"
    assert any("exceeds validated measurement zone" in w for w in result.config_fit_warnings)
    # Sp phải giữ nguyên giá trị 1.34 m (4.0*(0.1+0.2) + 0.02 + 0.02 + 0.05 + 0.03 + 0.02).
    assert result.sp_m == pytest.approx(1.34)


def test_zone_warning_no_observation_region():
    """Sp <= zone min → cảnh báo 'no observable region'."""
    params = SafetyParams(
        vh_m_s=0.05, vr_m_s=0.05, tr_s=0.05, ts_s=0.1, c_m=0.0, zd_m=0.0, zr_m=0.0
    )
    # Sp ≈ 0.0125 m <= 0.10 m.
    result = assess_sample(500, True, params, zone_min_m=ZONE_MIN_M, zone_max_m=ZONE_MAX_M)
    assert any("no observable region" in w for w in result.config_fit_warnings)


def test_no_warning_when_sp_in_zone():
    """Sp = 0.44 nằm trong zone (0.10, 1.00) → config_fit_warnings rỗng."""
    result = assess_sample(
        643, True, _fixture_params(), zone_min_m=ZONE_MIN_M, zone_max_m=ZONE_MAX_M
    )
    assert result.config_fit_warnings == []


# ---------------------------------------------------------------------------
# JSON Schema v1
# ---------------------------------------------------------------------------


def test_telemetry_schema_accepts_reference_payload():
    """Payload telemetry mẫu trong PLAN.md mục 2 phải validate OK qua jsonschema."""
    schema = _load_schema("telemetry_v1.schema.json")
    jsonschema.validate(_telemetry_reference_payload(), schema)


def test_telemetry_schema_rejects_bad_payload():
    """distance_mm = 50000 vượt miền 0..4500 → phải bị từ chối."""
    schema = _load_schema("telemetry_v1.schema.json")
    bad = _telemetry_reference_payload()
    bad["distance_mm"] = 50000
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


def test_assessment_control_ack_schemas_accept_spec_examples():
    """Các schema còn lại parse được và chấp nhận bản ghi mẫu theo PLAN.md mục 2."""
    # assessment_v1: bản ghi đủ (prediction OK).
    assessment_schema = _load_schema("assessment_v1.schema.json")
    jsonschema.validate(_assessment_reference_record(), assessment_schema)

    # assessment_v1: prediction không OK → các trường tương lai thành null vẫn hợp lệ.
    not_ok = copy.deepcopy(_assessment_reference_record())
    not_ok["prediction"] = {
        **not_ok["prediction"],
        "status": "MODEL_UNAVAILABLE",
        "model_version": None,
        "distance_m": None,
        "early_warning": None,
        "window_ref": None,
    }
    jsonschema.validate(not_ok, assessment_schema)

    # control_v1.
    control_schema = _load_schema("control_v1.schema.json")
    jsonschema.validate(
        {
            "schema_version": "1.0",
            "command": "session_start",
            "session_id": "run_001",
            "issued_at": "2026-09-21T12:00:00Z",
        },
        control_schema,
    )

    # ack_v1.
    ack_schema = _load_schema("ack_v1.schema.json")
    jsonschema.validate(
        {
            "schema_version": "1.0",
            "command": "session_start",
            "session_id": "run_001",
            "device_id": "esp32_01",
            "boot_id": "b1",
            "sequence": 125,
            "monotonic_us": 12500000,
            "time_synced": True,
            "accepted": True,
        },
        ack_schema,
    )
