"""Kiểm thử M2: ingestion core — store, validate, service, catch_up, dedup/gap/late."""

import json
from pathlib import Path

import jsonschema
import pytest

from backend.ingestion import PROCESSOR_VERSION, IngestionService, Store
from backend.safety import SafetyConfigVersion, load_safety_configs
from backend.sessions import SessionManager

REPO_ROOT = Path(__file__).resolve().parents[1]
SAFETY_YAML_PATH = REPO_ROOT / "configs" / "safety.yaml"
ASSESSMENT_SCHEMA = json.loads(
    (REPO_ROOT / "schemas" / "assessment_v1.schema.json").read_text(encoding="utf-8")
)

TOPIC = "hrc/v1/devices/esp32_01/telemetry"
RECEIVED_AT = "2026-06-01T00:00:00.000Z"


def make_payload(**overrides) -> dict:
    """Payload telemetry hợp lệ (PLAN.md mục 2) với các tham số ghi đè."""
    payload = {
        "schema_version": "1.0",
        "device_id": "esp32_01",
        "boot_id": "b1",
        "sequence": 1,
        "monotonic_us": 1_000_000,
        "measured_at": "2026-06-01T00:00:00.000Z",
        "time_synced": True,
        "distance_mm": 643,
        "echo_us": 3746,
        "valid": True,
        "error_code": None,
    }
    payload.update(overrides)
    return payload


@pytest.fixture()
def store(tmp_path):
    """Store trên DB tạm trong tmp_path — không đụng data/ thật."""
    s = Store(tmp_path / "ingestion_test.db")
    yield s
    s.close()


@pytest.fixture()
def safety_configs() -> list[SafetyConfigVersion]:
    """Cấu hình safety thật của repo (demo-v1, valid_from 2026-01-01)."""
    return load_safety_configs(SAFETY_YAML_PATH)


@pytest.fixture()
def service(store, safety_configs) -> IngestionService:
    """IngestionService gắn với store tạm."""
    return IngestionService(store, safety_configs)


def _rejected_rows(store: Store) -> list:
    """Đọc toàn bộ rejected_messages (qua connection nội bộ, chỉ dùng trong test)."""
    return store._conn.execute(
        "SELECT * FROM rejected_messages ORDER BY id"
    ).fetchall()


def _processed_records(store: Store, device_id: str, boot_id: str) -> dict:
    """Map sequence → record assessment đã parse từ processed_samples."""
    rows = store.fetch_processed(device_id, boot_id, PROCESSOR_VERSION)
    return {row["sequence"]: json.loads(row["result_json"]) for row in rows}


# ---------------------------------------------------------------------------
# 1. Dedup QoS1: redelivery cùng payload không tạo row mới
# ---------------------------------------------------------------------------


def test_duplicate_qoS_redelivery_single_row(service, store):
    payload = make_payload(sequence=7)
    first = service.handle_message(TOPIC, json.dumps(payload))
    second = service.handle_message(TOPIC, json.dumps(payload))

    assert first["action"] == "stored"
    assert second["action"] == "duplicate"
    assert second["reason"] == "duplicate"
    rows = store.fetch_raw("esp32_01", "b1")
    assert len(rows) == 1
    assert rows[0]["sequence"] == 7


# ---------------------------------------------------------------------------
# 2. Phát hiện gap sequence
# ---------------------------------------------------------------------------


def test_gap_detection(store):
    for seq in (1, 2, 5, 6):
        store.insert_raw(make_payload(sequence=seq), RECEIVED_AT)
    assert store.gaps("esp32_01", "b1") == [(3, 4)]

    for seq in (1, 3, 5):
        store.insert_raw(make_payload(sequence=seq, boot_id="b2"), RECEIVED_AT)
    assert store.gaps("esp32_01", "b2") == [(2, 2), (4, 4)]


# ---------------------------------------------------------------------------
# 3. Mẫu đến muộn: vẫn lưu, cờ is_late, thứ tự fetch đúng theo sequence
# ---------------------------------------------------------------------------


def test_late_arrival_stored_flagged(service, store):
    for seq in (10, 11, 13):
        result = service.handle_message(TOPIC, json.dumps(make_payload(sequence=seq)))
        assert result["is_late"] is False

    late = service.handle_message(TOPIC, json.dumps(make_payload(sequence=12)))
    assert late["action"] == "stored"
    assert late["is_late"] is True

    rows = store.fetch_raw("esp32_01", "b1")
    assert [row["sequence"] for row in rows] == [10, 11, 12, 13]
    assert rows[2]["is_late"] == 1  # sequence 12


# ---------------------------------------------------------------------------
# 4. Restart: dữ liệu đã commit giữ nguyên khi mở lại DB
# ---------------------------------------------------------------------------


def test_restart_keeps_committed_data(tmp_path):
    db_path = tmp_path / "restart.db"
    store_a = Store(db_path)
    try:
        for seq in (1, 2, 3):
            store_a.insert_raw(make_payload(sequence=seq), RECEIVED_AT)
    finally:
        store_a.close()

    store_b = Store(db_path)
    try:
        rows = store_b.fetch_raw("esp32_01", "b1")
        assert [row["sequence"] for row in rows] == [1, 2, 3]
    finally:
        store_b.close()


# ---------------------------------------------------------------------------
# 5. Schema sai: thiếu boot_id → rejected kèm reason, không vào raw
# ---------------------------------------------------------------------------


def test_invalid_schema_rejected_with_reason(service, store):
    payload = make_payload()
    del payload["boot_id"]

    result = service.handle_message(TOPIC, json.dumps(payload))

    assert result["action"] == "rejected"
    assert "boot_id" in result["reason"]
    rejected = _rejected_rows(store)
    assert len(rejected) == 1
    assert "boot_id" in rejected[0]["reason"]
    assert json.loads(rejected[0]["payload_text"])["device_id"] == "esp32_01"
    assert store.fetch_raw("esp32_01", "b1") == []


# ---------------------------------------------------------------------------
# 6. Mẫu lỗi cảm biến: vẫn lưu raw đúng cột
# ---------------------------------------------------------------------------


def test_sensor_error_sample_stored_raw(service, store):
    payload = make_payload(
        valid=False, distance_mm=None, echo_us=None, error_code="ECHO_TIMEOUT"
    )
    result = service.handle_message(TOPIC, json.dumps(payload))

    assert result["action"] == "stored"
    rows = store.fetch_raw("esp32_01", "b1")
    assert len(rows) == 1
    row = rows[0]
    assert row["valid"] == 0
    assert row["distance_mm"] is None
    assert row["echo_us"] is None
    assert row["error_code"] == "ECHO_TIMEOUT"


# ---------------------------------------------------------------------------
# 7. Device lệch giữa topic và payload
# ---------------------------------------------------------------------------


def test_topic_mismatch_rejected(service, store):
    result = service.handle_message(
        "hrc/v1/devices/esp32_02/telemetry", json.dumps(make_payload())
    )

    assert result["action"] == "rejected"
    assert result["reason"] == "device_mismatch"
    assert store.fetch_raw("esp32_01", "b1") == []
    assert len(_rejected_rows(store)) == 1


# ---------------------------------------------------------------------------
# 8. JSON hỏng
# ---------------------------------------------------------------------------


def test_bad_json_rejected(service, store):
    result = service.handle_message(TOPIC, b"not json")

    assert result["action"] == "rejected"
    assert result["reason"] == "invalid_json"
    rejected = _rejected_rows(store)
    assert len(rejected) == 1
    assert rejected[0]["reason"] == "invalid_json"
    assert rejected[0]["payload_text"] == "not json"


# ---------------------------------------------------------------------------
# 9. Đánh giá CLEAR end-to-end qua service
# ---------------------------------------------------------------------------


def test_service_assessment_clear(service, store):
    payload = make_payload(distance_mm=643, measured_at="2026-06-01T00:00:00Z")
    result = service.handle_message(TOPIC, json.dumps(payload))

    assert result["action"] == "stored"
    assert result["safety_status"] == "CLEAR"
    assert result["assessment_valid"] is True

    rows = store.fetch_processed("esp32_01", "b1", PROCESSOR_VERSION)
    assert len(rows) == 1
    record = json.loads(rows[0]["result_json"])
    assert record["distance_m"] == pytest.approx(0.643)
    assert record["safety"]["status"] == "CLEAR"
    assert record["safety"]["sp_m"] == pytest.approx(0.44)
    assert record["safety"]["margin_m"] == pytest.approx(0.203)
    assert record["safety"]["config_version"] == "demo-v1"
    assert record["safety"]["config_time_source"] == "measured_at"


# ---------------------------------------------------------------------------
# 10. VIOLATION và UNKNOWN
# ---------------------------------------------------------------------------


def test_service_assessment_violation_and_unknown(service, store):
    ok = service.handle_message(
        TOPIC, json.dumps(make_payload(sequence=1, distance_mm=400))
    )
    bad = service.handle_message(
        TOPIC,
        json.dumps(
            make_payload(
                sequence=2,
                valid=False,
                distance_mm=None,
                echo_us=None,
                error_code="ECHO_TIMEOUT",
            )
        ),
    )

    assert ok["safety_status"] == "VIOLATION"
    assert ok["assessment_valid"] is True
    assert bad["safety_status"] == "UNKNOWN"
    assert bad["assessment_valid"] is False

    records = _processed_records(store, "esp32_01", "b1")
    assert records[1]["safety"]["status"] == "VIOLATION"
    assert records[2]["safety"]["status"] == "UNKNOWN"
    assert records[2]["safety"]["sp_m"] is None
    assert records[2]["safety"]["margin_m"] is None


# ---------------------------------------------------------------------------
# 11. Chưa sync giờ: fallback received_at, vẫn đánh giá được
# ---------------------------------------------------------------------------


def test_service_unsynced_uses_received_at(service, store):
    payload = make_payload(time_synced=False, measured_at=None, distance_mm=643)
    result = service.handle_message(TOPIC, json.dumps(payload))

    assert result["action"] == "stored"
    assert result["safety_status"] == "CLEAR"

    record = _processed_records(store, "esp32_01", "b1")[1]
    assert record["safety"]["config_time_source"] == "received_at"
    assert record["safety"]["config_version"] == "demo-v1"
    assert record["safety"]["status"] == "CLEAR"
    assert record["distance_m"] == pytest.approx(0.643)


# ---------------------------------------------------------------------------
# 12. Chưa có config hiệu lực tại thời điểm mẫu → UNKNOWN
# ---------------------------------------------------------------------------


def test_service_no_config_for_time_returns_unknown(service, store):
    payload = make_payload(
        measured_at="2020-01-01T00:00:00Z", time_synced=True, distance_mm=643
    )
    result = service.handle_message(TOPIC, json.dumps(payload))

    assert result["action"] == "stored"
    assert result["safety_status"] == "UNKNOWN"
    assert result["assessment_valid"] is False

    record = _processed_records(store, "esp32_01", "b1")[1]
    assert record["safety"]["config_version"] is None
    assert record["safety"]["sp_m"] is None
    assert record["safety"]["config_time_source"] == "measured_at"


# ---------------------------------------------------------------------------
# 13. result_json khớp schema assessment_v1
# ---------------------------------------------------------------------------


def test_result_matches_assessment_schema(service, store):
    payload = make_payload(distance_mm=643, measured_at="2026-06-01T00:00:00Z")
    service.handle_message(TOPIC, json.dumps(payload))

    row = store.fetch_processed("esp32_01", "b1", PROCESSOR_VERSION)[0]
    record = json.loads(row["result_json"])
    jsonschema.validate(record, ASSESSMENT_SCHEMA)
    assert record["schema_version"] == "1.0"
    assert record["sample_ref"] == {
        "device_id": "esp32_01",
        "boot_id": "b1",
        "sequence": 1,
    }
    assert record["session_id"] is None
    assert record["prediction"]["status"] == "MODEL_UNAVAILABLE"
    assert record["prediction"]["mode"] == "live"


# ---------------------------------------------------------------------------
# 14. catch_up: xử lý raw pending, idempotent lần 2
# ---------------------------------------------------------------------------


def test_catch_up_processes_pending(store, safety_configs):
    for seq in (1, 2, 3):
        store.insert_raw(make_payload(sequence=seq), RECEIVED_AT)
    assert store.fetch_processed("esp32_01", "b1", PROCESSOR_VERSION) == []

    service = IngestionService(store, safety_configs)
    assert service.catch_up() == 3
    rows = store.fetch_processed("esp32_01", "b1", PROCESSOR_VERSION)
    assert [row["sequence"] for row in rows] == [1, 2, 3]
    # catch_up lần 2: không còn pending → 0 (idempotent).
    assert service.catch_up() == 0


# ---------------------------------------------------------------------------
# 15. pending_raw theo processor_version
# ---------------------------------------------------------------------------


def test_pending_raw_new_processor_version(service, store):
    service.handle_message(TOPIC, json.dumps(make_payload(sequence=1)))
    service.handle_message(TOPIC, json.dumps(make_payload(sequence=2)))
    assert store.pending_raw(PROCESSOR_VERSION) == []

    pending_v2 = store.pending_raw("safety-v2")
    assert [row["sequence"] for row in pending_v2] == [1, 2]


# ---------------------------------------------------------------------------
# 16. Thứ tự đúng: không mẫu nào bị cờ is_late
# ---------------------------------------------------------------------------


def test_is_late_false_for_in_order(service, store):
    for seq in (1, 2, 3):
        result = service.handle_message(TOPIC, json.dumps(make_payload(sequence=seq)))
        assert result["action"] == "stored"
        assert result["is_late"] is False

    rows = store.fetch_raw("esp32_01", "b1")
    assert [row["is_late"] for row in rows] == [0, 0, 0]


# ---------------------------------------------------------------------------
# 17. M3b: service có session_assigner (luồng CLI ingest_jsonl) gán mẫu vào
#     session đang chạy; mẫu trước ranh giới start không bị gán.
# ---------------------------------------------------------------------------


def _service_with_session_manager(store, safety_configs):
    """Cặp (SessionManager, IngestionService) như luồng CLI ingest_jsonl.

    SessionManager control=None → ranh giới start theo db_fallback, giống
    cách ``scripts/ingest_jsonl.py`` dựng service.
    """
    manager = SessionManager(store, "esp32_01", control=None)
    service2 = IngestionService(store, safety_configs, session_assigner=manager)
    return manager, service2


def test_ingest_jsonl_assigns_running_session(store, safety_configs):
    """Start session (db_fallback, DB rỗng) TRƯỚC khi nạp: cả 2 mẫu thuộc session."""
    manager, service2 = _service_with_session_manager(store, safety_configs)
    info = manager.start_session()
    assert info.boundary_source == "db_fallback"

    for seq in (1, 2):
        result = service2.handle_message(
            TOPIC, json.dumps(make_payload(sequence=seq))
        )
        assert result["action"] == "stored"

    records = _processed_records(store, "esp32_01", "b1")
    assert records[1]["session_id"] == info.session_id
    assert records[2]["session_id"] == info.session_id
    assert store.session_stats(info.session_id)["sample_count"] == 2


def test_ingest_jsonl_sample_before_start_not_assigned(store, safety_configs):
    """Mẫu nạp TRƯỚC start_session (sequence < start_sequence) không thuộc session."""
    manager, service2 = _service_with_session_manager(store, safety_configs)
    # Nạp 1 mẫu trước khi start: chưa có session → session_id None.
    service2.handle_message(TOPIC, json.dumps(make_payload(sequence=1)))

    # db_fallback: start_sequence = 1 + 1 = 2 → chỉ mẫu sequence >= 2 gán được.
    info = manager.start_session()
    assert info.start_sequence == 2

    for seq in (2, 3):
        result = service2.handle_message(
            TOPIC, json.dumps(make_payload(sequence=seq))
        )
        assert result["action"] == "stored"

    records = _processed_records(store, "esp32_01", "b1")
    assert records[1]["session_id"] is None
    assert records[2]["session_id"] == info.session_id
    assert records[3]["session_id"] == info.session_id
    assert store.session_stats(info.session_id)["sample_count"] == 2


# ---------------------------------------------------------------------------
# 18. M3c m-5: CLI ingest_jsonl gọi catch_up sau khi nạp hết input — raw
#     pending của lần chạy trước (đã insert_raw nhưng chưa compute) tính nốt.
# ---------------------------------------------------------------------------


def test_ingest_jsonl_cli_catch_up_pending(tmp_path):
    from scripts.ingest_jsonl import main as ingest_main

    db_path = tmp_path / "cli.db"
    store = Store(db_path)
    try:
        # Mẫu của lần chạy trước: raw đã lưu, processed chưa kịp tính.
        store.insert_raw(make_payload(sequence=1), RECEIVED_AT)
    finally:
        store.close()

    input_file = tmp_path / "in.jsonl"
    input_file.write_text(
        json.dumps(make_payload(sequence=2)) + "\n", encoding="utf-8"
    )

    rc = ingest_main(
        [
            "--input", str(input_file),
            "--db", str(db_path),
            "--safety-config", str(SAFETY_YAML_PATH),
        ]
    )
    assert rc == 0

    store = Store(db_path)
    try:
        rows = store.fetch_processed("esp32_01", "b1", PROCESSOR_VERSION)
        assert [row["sequence"] for row in rows] == [1, 2]
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 19. M3c m-5: mqtt_listener.main() gọi catch_up đúng MỘT lần sau khi tạo
#     service, trước vòng lặp chờ (MqttListener.start/stop và time.sleep bị
#     thay để chạy không broker).
# ---------------------------------------------------------------------------


def test_mqtt_listener_main_catch_up_on_start(tmp_path, monkeypatch):
    import backend.ingestion.mqtt_listener as mqtt_listener_module

    db_path = tmp_path / "listener.db"
    store = Store(db_path)
    try:
        store.insert_raw(make_payload(sequence=1), RECEIVED_AT)
    finally:
        store.close()

    monkeypatch.setenv("HRC_DB_PATH", str(db_path))
    monkeypatch.setenv("HRC_MQTT_ENABLED", "0")

    original_catch_up = mqtt_listener_module.IngestionService.catch_up
    calls: list[bool] = []

    def spy_catch_up(self):
        calls.append(True)
        return original_catch_up(self)

    monkeypatch.setattr(
        mqtt_listener_module.IngestionService, "catch_up", spy_catch_up
    )
    monkeypatch.setattr(mqtt_listener_module.MqttListener, "start", lambda self: None)
    monkeypatch.setattr(mqtt_listener_module.MqttListener, "stop", lambda self: None)

    def _interrupt(_seconds):
        raise KeyboardInterrupt  # thoát vòng lặp chờ ngay lần sleep đầu

    monkeypatch.setattr(mqtt_listener_module.time, "sleep", _interrupt)

    mqtt_listener_module.main()

    assert calls == [True]
    store = Store(db_path)
    try:
        rows = store.fetch_processed("esp32_01", "b1", PROCESSOR_VERSION)
        assert [row["sequence"] for row in rows] == [1]
    finally:
        store.close()
