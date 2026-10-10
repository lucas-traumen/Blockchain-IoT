"""Integration coverage for ingestion, LSTM prediction doubles and safety/session state.

The predictor receives a deterministic injected LSTM model/scaler so these
tests cover the real ingestion path without training or loading a legacy model.
"""

import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker

from iot_backend.inference.predictor import SafetyPredictor
from iot_backend.ingestion import PROCESSOR_VERSION, IngestionService, Store
from iot_backend.safety import SafetyConfigVersion, load_safety_configs
from scripts.synthetic_dataset import METADATA_KEYS, generate_sessions

REPO_ROOT = Path(__file__).resolve().parents[1]

HORIZONS_MS = [100, 200, 300, 400, 500]
SP_MM = 440
N_SAMPLES = 40
SESSION_SEED = 7
SCENARIO = "approach"
TOPIC = "hrc/v1/devices/esp32_sim/telemetry"


@pytest.fixture(scope="module")
def model_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("lstm_integration_double")


@pytest.fixture(scope="module")
def model_config() -> dict:
    return yaml.safe_load(
        (REPO_ROOT / "configs" / "model.yaml").read_text(encoding="utf-8")
    )


@pytest.fixture(scope="module")
def safety_configs():
    """Cấu hình safety thật dùng chung cho các test integration."""
    return load_safety_configs(REPO_ROOT / "configs" / "safety.yaml")


def make_lstm_predictor(model_dir, model_config):
    from tests.test_inference import FakeLstmModel, FakeScaler, MANIFEST

    def loader(_directory):
        return FakeLstmModel(), FakeScaler(), MANIFEST

    return SafetyPredictor(
        model_dir, model_config, loader, model_type="lstm"
    )

def session_lines() -> list[dict]:
    """1 session approach 40 mẫu (seed 7, không mẫu lỗi) đã TÁCH nhãn metadata.

    Telemetry_v1 khai ``additionalProperties: false`` nên 3 trường nhãn
    (synthetic/scenario/session_label) phải bỏ trước khi ingest — cùng việc
    ``ai_model/prepare.load_jsonl_dataset`` làm khi train. Session approach
    của seed 7 chắc chắn có pha dưới Sp 440 mm (mẫu 25..31).
    """
    lines = generate_sessions(
        1, N_SAMPLES, SESSION_SEED, scenario=SCENARIO, error_prob=0.0
    )
    return [{k: v for k, v in line.items() if k not in METADATA_KEYS} for line in lines]


def records_by_sequence(store: Store, processor_version: str) -> dict[int, dict]:
    """Map sequence → record assessment (parsed result_json) của một version."""
    rows = store.all_processed(processor_version, limit=100000)
    return {row["sequence"]: json.loads(row["result_json"]) for row in rows}


# ------------------------------------------------- live: WARMUP → OK + safety


def test_live_with_predictor_warmup_ok_violation(
    tmp_path, model_dir, model_config, safety_configs
):
    """40 mẫu live qua handle_message: WARMUP 9 mẫu đầu, OK từ mẫu 10,
    VIOLATION đúng ngưỡng Sp, schema assessment_v1 PASS toàn bộ."""
    store = Store(tmp_path / "integ_live.db")
    try:
        service = IngestionService(
            store, safety_configs, predictor=make_lstm_predictor(model_dir, model_config)
        )
        lines = session_lines()
        device_id = lines[0]["device_id"]
        boot_id = lines[0]["boot_id"]
        for line in lines:
            result = service.handle_message(TOPIC, json.dumps(line))
            assert result["action"] == "stored"

        records = records_by_sequence(store, PROCESSOR_VERSION)
        assert len(records) == N_SAMPLES
        assert set(records) == set(range(1, N_SAMPLES + 1))

        # 9 mẫu đầu (< window_size 10): WARMUP, mọi field tương lai null.
        for seq in range(1, 10):
            pred = records[seq]["prediction"]
            assert pred["status"] == "WARMUP", f"seq={seq}"
            assert pred["model_version"] is None
            assert pred["distance_m"] is None
            assert pred["early_warning"] is None
            assert pred["window_ref"] is None
            assert pred["horizons_ms"] == [100, 200, 300, 400, 500]
            assert pred["mode"] == "live"

        # Từ mẫu 10 (mẫu valid liên tiếp — error_prob=0): OK, dự báo 5 float,
        # window_ref trượt đúng cửa sổ 10 mẫu cuối.
        for seq in range(10, N_SAMPLES + 1):
            pred = records[seq]["prediction"]
            assert pred["status"] == "OK", f"seq={seq}"
            assert pred["model_version"] is not None
            assert isinstance(pred["distance_m"], list)
            assert len(pred["distance_m"]) == 5
            assert all(isinstance(value, float) for value in pred["distance_m"])
            assert pred["window_ref"]["sequence_last"] == seq, f"seq={seq}"
            assert pred["window_ref"]["sequence_first"] == seq - 9, f"seq={seq}"
            assert pred["window_ref"]["sample_count"] == 10
            assert pred["window_ref"]["max_age_ms"] == 900.0  # 9 × 0.1 s

        # Safety tích hợp: mẫu distance ≤ Sp 440 mm → VIOLATION đúng vị trí;
        # prediction vẫn OK (đủ window) và early_warning False khi current
        # đã ≤ Sp (việc cảnh báo sớm là của hiện tại > Sp).
        violations = sorted(
            seq
            for seq, record in records.items()
            if record["safety"]["status"] == "VIOLATION"
        )
        expected = [
            line["sequence"]
            for line in lines
            if line["distance_mm"] is not None and line["distance_mm"] <= SP_MM
        ]
        assert expected, "dataset seed 7 phải có pha dưới Sp để test có nghĩa"
        assert violations == expected
        for seq in violations:
            pred = records[seq]["prediction"]
            assert pred["status"] == "OK", f"seq={seq}"
            assert pred["early_warning"] is False, f"seq={seq}"
        clear = [seq for seq, r in records.items() if r["safety"]["status"] == "CLEAR"]
        assert clear, "session approach phải còn mẫu CLEAR ngoài pha chạm ngưỡng"

        # Mọi record live PASS schema assessment_v1 (Draft 2020-12 + FormatChecker).
        schema = json.loads(
            (REPO_ROOT / "schemas" / "assessment_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        for seq, record in records.items():
            assert list(validator.iter_errors(record)) == [], f"seq={seq}"

        # Tra điểm qua get_processed: mẫu 10 là ranh giới WARMUP → OK.
        row = store.get_processed(device_id, boot_id, 10, PROCESSOR_VERSION)
        assert row is not None
        assert json.loads(row["result_json"])["prediction"]["status"] == "OK"
    finally:
        store.close()


# --------------------------------------------- replay offline với predictor riêng


def test_replay_offline_with_own_predictor(
    tmp_path, model_dir, model_config, safety_configs
):
    """Replay offline qua service RIÊNG (instance predictor MỚI): record mode
    offline, prediction.mode 'offline', stale False; chạy lần 2 → 0 insert
    mới (idempotent, INSERT OR IGNORE theo processor_version)."""
    db_path = tmp_path / "integ_replay.db"
    store = Store(db_path)
    try:
        live_service = IngestionService(
            store, safety_configs, predictor=make_lstm_predictor(model_dir, model_config)
        )
        lines = session_lines()
        device_id = lines[0]["device_id"]
        boot_id = lines[0]["boot_id"]
        for line in lines:
            assert live_service.handle_message(TOPIC, json.dumps(line))["action"] == (
                "stored"
            )

        # Service replay: instance SafetyPredictor MỚI — buffer riêng, không
        # chia sẻ state với predictor của đường live phía trên.
        replay_service = IngestionService(
            store, safety_configs, predictor=make_lstm_predictor(model_dir, model_config)
        )
        rows = store.all_raw_rows()
        assert len(rows) == N_SAMPLES
        inserted = replay_service.process_raw_rows(rows, "replay-safety-x", "offline")
        assert inserted == N_SAMPLES

        replay_records = records_by_sequence(store, "replay-safety-x")
        assert set(replay_records) == set(range(1, N_SAMPLES + 1))
        for seq, record in replay_records.items():
            pred = record["prediction"]
            assert pred["mode"] == "offline", f"seq={seq}"
            assert pred["stale"] is False, f"seq={seq}"
            if seq >= 10:
                assert pred["status"] == "OK", f"seq={seq}"
                assert len(pred["distance_m"]) == 5
        # Safety không phụ thuộc predictor: VIOLATION khớp giữa live và replay.
        live_records = records_by_sequence(store, PROCESSOR_VERSION)
        for seq in replay_records:
            assert (
                replay_records[seq]["safety"]["status"]
                == live_records[seq]["safety"]["status"]
            ), f"seq={seq}"

        # Replay lần 2 cùng processor_version: 0 insert mới (idempotent — M3).
        inserted_again = replay_service.process_raw_rows(
            rows, "replay-safety-x", "offline"
        )
        assert inserted_again == 0
        assert len(records_by_sequence(store, "replay-safety-x")) == N_SAMPLES
    finally:
        store.close()


# ------------------------------------------------- guard: không predictor (cũ)


def test_service_without_predictor_model_unavailable(tmp_path, safety_configs):
    """Service dựng không predictor (mặc định None) giữ hành vi cũ: khối
    prediction MODEL_UNAVAILABLE với horizons_ms hardcode, mọi field null."""
    store = Store(tmp_path / "integ_no_model.db")
    try:
        service = IngestionService(store, safety_configs)
        payload = {
            "schema_version": "1.0",
            "device_id": "esp32_sim",
            "boot_id": "sim_00000007_0",
            "sequence": 1,
            "monotonic_us": 1_000_000,
            "measured_at": "2026-01-01T00:00:00.000Z",
            "time_synced": True,
            "distance_mm": 643,
            "echo_us": 3746,
            "valid": True,
            "error_code": None,
        }
        result = service.handle_message(TOPIC, json.dumps(payload))
        assert result["action"] == "stored"

        row = store.get_processed(
            payload["device_id"], payload["boot_id"], 1, PROCESSOR_VERSION
        )
        assert row is not None
        pred = json.loads(row["result_json"])["prediction"]
        assert pred["status"] == "MODEL_UNAVAILABLE"
        assert pred["horizons_ms"] == [100, 200, 300, 400, 500]
        assert pred["model_version"] is None
        assert pred["distance_m"] is None
        assert pred["early_warning"] is None
        assert pred["window_ref"] is None
        assert pred["mode"] == "live"
    finally:
        store.close()
