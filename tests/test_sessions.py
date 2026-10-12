"""Kiểm thử M3: SessionManager, gán mẫu, fallback ranh giới, export, stats.

Không dùng broker MQTT thật — control transport là ``StubControlTransport``
trả ack canned (hoặc None) theo command qua constructor.
"""

import json
import threading
from pathlib import Path

import pytest

from iot_backend.ingestion.service import IngestionService, Store
from iot_backend.ingestion.service import PROCESSOR_VERSION
from iot_backend.safety import SafetyConfigVersion, load_safety_configs
from iot_backend.sessions import SessionManager
from iot_backend.sessions.manager import SessionAlreadyClosed
from iot_backend.sessions.export import (
    CSV_COLUMNS,
    export_csv,
    export_jsonl,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SAFETY_YAML_PATH = REPO_ROOT / "configs" / "safety.yaml"

TOPIC = "hrc/v1/devices/esp32_01/telemetry"
RECEIVED_AT = "2026-06-01T00:00:00.000Z"


class StubControlTransport:
    """Control transport giả: trả ack canned theo command (None nếu không khai báo)."""

    def __init__(self, acks: dict[str, dict | None] | None = None):
        self.acks = dict(acks or {})
        self.calls: list[tuple[str, str, str]] = []

    def session_boundary(
        self, device_id: str, command: str, session_id: str, timeout_s: float
    ) -> dict | None:
        self.calls.append((device_id, command, session_id))
        return self.acks.get(command)


class TimeoutRecordingControl:
    """Control giả ghi timeout iot_backend truyền cho boundary ACK."""

    def __init__(self):
        self.timeouts: list[float] = []

    def session_boundary(
        self, device_id: str, command: str, session_id: str, timeout_s: float
    ) -> dict:
        self.timeouts.append(timeout_s)
        return {"boot_id": "b1", "sequence": 100}


class BlockingControlTransport:
    """Control giả để giữ ACK và tái hiện telemetry bay qua boundary."""

    def __init__(self):
        self.entered: dict[str, threading.Event] = {
            "session_start": threading.Event(),
            "session_stop": threading.Event(),
        }
        self.release = threading.Event()

    def session_boundary(
        self, device_id: str, command: str, session_id: str, timeout_s: float
    ) -> dict | None:
        self.entered[command].set()
        assert self.release.wait(2.0), f"timeout chờ test release cho {command}"
        return {"boot_id": "b1", "sequence": 5 if command == "session_start" else 6}


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
    s = Store(tmp_path / "sessions_test.db")
    yield s
    s.close()


@pytest.fixture()
def safety_configs() -> list[SafetyConfigVersion]:
    """Cấu hình safety thật của repo (demo-v1, valid_from 2026-01-01)."""
    return load_safety_configs(SAFETY_YAML_PATH)


def make_service(
    store, safety_configs, manager: SessionManager | None = None
) -> IngestionService:
    """Service có/không có hook gán session."""
    return IngestionService(store, safety_configs, session_assigner=manager)


# ---------------------------------------------------------------------------
# 1. Start với ack thiết bị: ranh giới = ack.sequence + 1, gán đúng khoảng
# ---------------------------------------------------------------------------


def test_start_session_device_ack(store, safety_configs):
    stub = StubControlTransport({"session_start": {"boot_id": "b1", "sequence": 100}})
    manager = SessionManager(store, "esp32_01", control=stub)
    info = manager.start_session(scenario="demo")

    assert info.status == "running"
    row = store.get_session(info.session_id)
    assert row is not None
    assert row["start_boot_id"] == "b1"
    assert row["start_sequence"] == 101
    assert row["boundary_source"] == "device_ack"
    assert row["status"] == "running"

    service = make_service(store, safety_configs, manager)
    for seq in (101, 102, 100):
        service.handle_message(TOPIC, json.dumps(make_payload(sequence=seq)))

    rows = store.session_raw_rows(info.session_id)
    assert [r["sequence"] for r in rows] == [101, 102]  # 100 ngoài khoảng → không gán


def test_default_ack_timeout_allows_delayed_device_boundary(store):
    """Timeout mặc định đủ dài cho ACK muộn trong lúc MQTT reconnect."""
    control = TimeoutRecordingControl()
    manager = SessionManager(store, "esp32_01", control=control)

    manager.start_session(session_id="late-ack")

    assert control.timeouts == [pytest.approx(8.0)]


# ---------------------------------------------------------------------------
# 2. Start không ack: fallback DB — start_sequence = max đã nhận + 1
# ---------------------------------------------------------------------------


def test_start_session_db_fallback(store, safety_configs):
    manager = SessionManager(store, "esp32_01")  # không control
    service = make_service(store, safety_configs, manager)
    for seq in range(1, 6):
        service.handle_message(TOPIC, json.dumps(make_payload(sequence=seq)))

    info = manager.start_session()

    assert info.start_boot_id == "b1"
    assert info.start_sequence == 6
    assert info.boundary_source == "db_fallback"


# ---------------------------------------------------------------------------
# 3. Fallback khi DB rỗng: start NULL → mọi mẫu sau (boot bất kỳ) gán được
# ---------------------------------------------------------------------------


def test_start_session_db_fallback_empty_db(store, safety_configs):
    manager = SessionManager(store, "esp32_01")
    info = manager.start_session()

    assert info.start_boot_id is None
    assert info.start_sequence is None
    assert info.boundary_source == "db_fallback"

    service = make_service(store, safety_configs, manager)
    service.handle_message(TOPIC, json.dumps(make_payload(sequence=1, boot_id="bx")))

    rows = store.session_raw_rows(info.session_id)
    assert [(r["boot_id"], r["sequence"]) for r in rows] == [("bx", 1)]


# ---------------------------------------------------------------------------
# 4. Stop với ack thiết bị: stop_sequence = ack.sequence, status closed
# ---------------------------------------------------------------------------


def test_stop_session_records_boundary(store, safety_configs):
    stub = StubControlTransport(
        {
            "session_start": {"boot_id": "b1", "sequence": 100},
            "session_stop": {"boot_id": "b1", "sequence": 200},
        }
    )
    manager = SessionManager(store, "esp32_01", control=stub)
    info = manager.start_session()
    service = make_service(store, safety_configs, manager)
    service.handle_message(TOPIC, json.dumps(make_payload(sequence=101)))

    stopped = manager.stop_session(info.session_id)

    assert stopped.status == "closed"
    assert stopped.stop_sequence == 200
    assert stopped.ended_at is not None
    row = store.get_session(info.session_id)
    assert row["status"] == "closed"
    assert row["stop_sequence"] == 200


# ---------------------------------------------------------------------------
# 5. Stop không ack: fallback max sequence đã gán vào session
# ---------------------------------------------------------------------------


def test_stop_session_fallback_max_assigned(store, safety_configs):
    manager = SessionManager(store, "esp32_01")  # không control
    info = manager.start_session()
    service = make_service(store, safety_configs, manager)
    for seq in (101, 102, 103):
        service.handle_message(TOPIC, json.dumps(make_payload(sequence=seq)))

    stopped = manager.stop_session(info.session_id)

    assert stopped.stop_sequence == 103


# ---------------------------------------------------------------------------
# 6. Gán mẫu tôn trọng khoảng [start_sequence, stop_sequence] kể cả khi closed
# ---------------------------------------------------------------------------


def test_assignment_respects_range(store, safety_configs):
    stub = StubControlTransport(
        {
            "session_start": {"boot_id": "b1", "sequence": 100},
            "session_stop": {"boot_id": "b1", "sequence": 200},
        }
    )
    manager = SessionManager(store, "esp32_01", control=stub)
    info = manager.start_session()
    service = make_service(store, safety_configs, manager)
    service.handle_message(TOPIC, json.dumps(make_payload(sequence=101)))
    manager.stop_session(info.session_id)  # closed [101, 200]

    # 101 (duplicate raw, idempotent gán), 200 trong khoảng, 201 ngoài khoảng.
    service.handle_message(TOPIC, json.dumps(make_payload(sequence=101)))
    service.handle_message(TOPIC, json.dumps(make_payload(sequence=200)))
    service.handle_message(TOPIC, json.dumps(make_payload(sequence=201)))

    rows = store.session_raw_rows(info.session_id)
    assert [r["sequence"] for r in rows] == [101, 200]


# ---------------------------------------------------------------------------
# 7. Boot đổi giữa session: không gán + sessions.boot_change = 1
# ---------------------------------------------------------------------------


def test_boot_change_flagged(store, safety_configs):
    stub = StubControlTransport({"session_start": {"boot_id": "b1", "sequence": 100}})
    manager = SessionManager(store, "esp32_01", control=stub)
    info = manager.start_session()
    service = make_service(store, safety_configs, manager)

    service.handle_message(TOPIC, json.dumps(make_payload(sequence=101, boot_id="b2")))

    assert store.session_raw_rows(info.session_id) == []
    row = store.get_session(info.session_id)
    assert row["boot_change"] == 1


# ---------------------------------------------------------------------------
# 8. Đang running mà start tiếp → ValueError
# ---------------------------------------------------------------------------


def test_second_start_rejected(store):
    manager = SessionManager(store, "esp32_01")
    manager.start_session(scenario="first")

    with pytest.raises(ValueError, match="đang chạy"):
        manager.start_session(scenario="second")


# ---------------------------------------------------------------------------
# 9. Export JSONL: payload gốc nguyên vẹn từng dòng
# ---------------------------------------------------------------------------


def test_export_jsonl_raw_preserved(store, safety_configs, tmp_path):
    service = make_service(store, safety_configs)
    payloads = [make_payload(sequence=seq) for seq in (1, 2, 3)]
    for payload in payloads:
        service.handle_message(TOPIC, json.dumps(payload))

    rows = store.all_raw_rows()
    out = tmp_path / "export.jsonl"
    count = export_jsonl(rows, out)

    assert count == 3
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    for line, payload in zip(lines, payloads):
        assert json.loads(line) == payload  # nội dung raw không bị chỉnh sửa


# ---------------------------------------------------------------------------
# 10. Export CSV: header đúng 11 cột, giá trị khớp payload
# ---------------------------------------------------------------------------


def test_export_csv_header_and_rows(store, safety_configs, tmp_path):
    service = make_service(store, safety_configs)
    for seq in (1, 2, 3):
        service.handle_message(TOPIC, json.dumps(make_payload(sequence=seq)))

    rows = store.all_raw_rows()
    out = tmp_path / "export.csv"
    count = export_csv(rows, out)

    assert count == 3
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0].split(",") == CSV_COLUMNS
    assert len(lines) == 4
    first = lines[1].split(",")
    assert first[0] == "1.0"  # schema_version
    assert first[1] == "esp32_01"  # device_id
    assert first[2] == "b1"  # boot_id
    assert first[3] == "1"  # sequence
    assert first[4] == "1000000"  # monotonic_us
    assert first[6] == "True"  # time_synced
    assert first[7] == "643"  # distance_mm
    assert first[10] == ""  # error_code None → chuỗi rỗng


# ---------------------------------------------------------------------------
# 11. session_stats: đếm mẫu, gap, CLEAR/VIOLATION/UNKNOWN
# ---------------------------------------------------------------------------


def test_session_stats(store, safety_configs):
    manager = SessionManager(store, "esp32_01")  # DB rỗng → mọi mẫu gán được
    info = manager.start_session()
    service = make_service(store, safety_configs, manager)
    service.handle_message(TOPIC, json.dumps(make_payload(sequence=1, monotonic_us=1_000_000)))  # CLEAR
    service.handle_message(
        TOPIC, json.dumps(make_payload(sequence=2, monotonic_us=1_100_000, distance_mm=400))
    )  # VIOLATION
    service.handle_message(
        TOPIC,
        json.dumps(
            make_payload(
                sequence=3,
                monotonic_us=1_200_000,
                valid=False,
                distance_mm=None,
                echo_us=None,
                error_code="ECHO_TIMEOUT",
            )
        ),
    )  # UNKNOWN
    service.handle_message(TOPIC, json.dumps(make_payload(sequence=5, monotonic_us=1_300_000)))  # CLEAR, sequence gap

    stats = store.session_stats(info.session_id)
    assert stats["sample_count"] == 4
    assert stats["raw_count"] == 4
    assert stats["processed_count"] == 4
    assert stats["processed_coverage"] == 1.0
    assert stats["timing_span_s"] == pytest.approx(0.3)
    assert stats["max_timing_gap_s"] == pytest.approx(0.1)
    assert stats["mean_cadence_hz"] == pytest.approx(10.0)
    assert stats["late_count"] == 0
    assert stats["gap_count"] == 1
    assert stats["clear_count"] == 2
    assert stats["violation_count"] == 1
    assert stats["unknown_count"] == 1

    info = manager.get_session(info.session_id)
    assert info.sample_count == 4
    assert info.gap_count == 1


# ---------------------------------------------------------------------------
# 12. M3c M-1: stop lần 2 → SessionAlreadyClosed, KHÔNG gửi session_stop lần
#     nữa xuống device, stop_sequence/ended_at không bị ghi đè
# ---------------------------------------------------------------------------


def test_stop_twice_raises_and_keeps_boundary(store, safety_configs):
    stub = StubControlTransport(
        {
            "session_start": {"boot_id": "b1", "sequence": 100},
            "session_stop": {"boot_id": "b1", "sequence": 200},
        }
    )
    manager = SessionManager(store, "esp32_01", control=stub)
    info = manager.start_session()
    service = make_service(store, safety_configs, manager)
    service.handle_message(TOPIC, json.dumps(make_payload(sequence=101)))

    manager.stop_session(info.session_id)
    row_first = store.get_session(info.session_id)
    assert row_first["status"] == "closed"

    with pytest.raises(SessionAlreadyClosed):
        manager.stop_session(info.session_id)

    # Chỉ MỘT lệnh session_stop đã gửi xuống device (guard chặn trước transport).
    assert [call[1] for call in stub.calls].count("session_stop") == 1

    # Ranh giới không bị ghi đè lần hai.
    row_second = store.get_session(info.session_id)
    assert row_second["stop_sequence"] == row_first["stop_sequence"] == 200
    assert row_second["ended_at"] == row_first["ended_at"]


# ---------------------------------------------------------------------------
# 13. M3c m-2: latest_raw theo received_at — device reboot (boot mới đếm từ 1)
#     không bị MAX(sequence) xuyên boot đánh lừa; fallback start theo boot mới
# ---------------------------------------------------------------------------


def test_latest_raw_after_reboot_uses_new_boot(store, safety_configs):
    manager = SessionManager(store, "esp32_01")
    service = make_service(store, safety_configs, manager)
    for seq in range(1, 6):
        service.handle_message(TOPIC, json.dumps(make_payload(sequence=seq)))
    service.handle_message(TOPIC, json.dumps(make_payload(boot_id="b2", sequence=1)))

    latest = store.latest_raw("esp32_01")
    assert latest["boot_id"] == "b2"
    assert latest["sequence"] == 1

    info = manager.start_session()
    assert info.boundary_source == "db_fallback"
    assert info.start_boot_id == "b2"
    assert info.start_sequence == 2


# ---------------------------------------------------------------------------
# 14. M3c m-3: session NULL start_boot_id — mẫu đầu tiên pin start_boot_id,
#     mẫu boot khác sau đó không gán + boot_change=1
# ---------------------------------------------------------------------------


def test_null_start_boot_pins_first_sample_then_flags_change(store, safety_configs):
    manager = SessionManager(store, "esp32_01")
    info = manager.start_session()  # DB rỗng → start NULL/NULL
    assert info.start_boot_id is None
    assert info.start_sequence is None
    service = make_service(store, safety_configs, manager)

    # Mẫu đầu tiên: gán + pin start_boot_id thành boot của mẫu đó.
    service.handle_message(TOPIC, json.dumps(make_payload(boot_id="b1", sequence=1)))
    row = store.get_session(info.session_id)
    assert row["start_boot_id"] == "b1"

    # Mẫu thứ hai của boot khác: không gán + boot_change.
    service.handle_message(TOPIC, json.dumps(make_payload(boot_id="b2", sequence=1)))
    rows = store.session_raw_rows(info.session_id)
    assert [(r["boot_id"], r["sequence"]) for r in rows] == [("b1", 1)]
    row = store.get_session(info.session_id)
    assert row["boot_change"] == 1


# ---------------------------------------------------------------------------
# 15. M3c m-6: start trùng session_id (PK) → ValueError "đã tồn tại"
# ---------------------------------------------------------------------------


def test_duplicate_session_id_raises(store):
    manager = SessionManager(store, "esp32_01")
    manager.start_session(session_id="run_dup")
    manager.stop_session("run_dup")

    with pytest.raises(ValueError, match="đã tồn tại"):
        manager.start_session(session_id="run_dup")


# ---------------------------------------------------------------------------
# 16. M3c INFO: session_stats nhận processor_version (None = không lọc);
#     manager truyền PROCESSOR_VERSION — version khác không đếm vào stats
# ---------------------------------------------------------------------------


def test_session_stats_processor_version_filter(store, safety_configs):
    manager = SessionManager(store, "esp32_01")
    info = manager.start_session()
    service = make_service(store, safety_configs, manager)
    service.handle_message(
        TOPIC, json.dumps(make_payload(sequence=1, distance_mm=400))
    )  # VIOLATION

    assert store.session_stats(info.session_id)["violation_count"] == 1
    assert (
        store.session_stats(info.session_id, PROCESSOR_VERSION)["violation_count"] == 1
    )
    assert store.session_stats(info.session_id, "safety-v9")["violation_count"] == 0

    # Replay cùng mẫu dưới version khác: không lọc (None) đếm cả version
    # replay; lọc theo PROCESSOR_VERSION bỏ qua version đó.
    replayed = service.process_raw_rows(
        store.session_raw_rows(info.session_id), "replay-safety-x", mode="offline"
    )
    assert replayed == 1
    assert store.session_stats(info.session_id)["violation_count"] == 2
    assert (
        store.session_stats(info.session_id, PROCESSOR_VERSION)["violation_count"] == 1
    )
    assert (
        store.session_stats(info.session_id, "replay-safety-x")["violation_count"] == 1
    )


# ---------------------------------------------------------------------------
# 17. Boundary START/STOP không để telemetry bay bị tính sai session
# ---------------------------------------------------------------------------


def test_start_boundary_waits_for_ack_before_assigning_inflight_sample(
    store, safety_configs
):
    control = BlockingControlTransport()
    manager = SessionManager(store, "esp32_01", control=control)
    service = make_service(store, safety_configs, manager)
    started: dict[str, object] = {}

    start_thread = threading.Thread(
        target=lambda: started.setdefault("info", manager.start_session(session_id="start-race"))
    )
    start_thread.start()
    assert control.entered["session_start"].wait(1.0)

    ingested: dict[str, object] = {}
    ingest_thread = threading.Thread(
        target=lambda: ingested.setdefault(
            "result",
            service.handle_message(
                TOPIC, json.dumps(make_payload(sequence=6, monotonic_us=1_600_000))
            ),
        )
    )
    ingest_thread.start()
    # Raw đã commit nhưng compute/assign phải chờ boundary hoàn tất.
    for _ in range(100):
        if store.fetch_raw("esp32_01", "b1"):
            break
        threading.Event().wait(0.005)
    assert store.fetch_raw("esp32_01", "b1")
    assert "result" not in ingested

    control.release.set()
    start_thread.join(1.0)
    ingest_thread.join(1.0)
    assert not start_thread.is_alive()
    assert not ingest_thread.is_alive()
    assert started["info"].start_sequence == 6
    assert ingested["result"]["action"] == "stored"

    record = json.loads(
        store.get_processed("esp32_01", "b1", 6, PROCESSOR_VERSION)["result_json"]
    )
    assert record["session_id"] == "start-race"
    assert [r["sequence"] for r in store.session_raw_rows("start-race")] == [6]


def test_stop_boundary_waits_for_ack_before_assigning_inflight_sample(
    store, safety_configs
):
    control = BlockingControlTransport()
    manager = SessionManager(store, "esp32_01", control=control)
    service = make_service(store, safety_configs, manager)

    # Hoàn tất START trước, sau đó giữ ACK STOP.
    start_result: dict[str, object] = {}
    control.release.set()
    start_thread = threading.Thread(
        target=lambda: start_result.setdefault("info", manager.start_session(session_id="stop-race"))
    )
    start_thread.start()
    assert control.entered["session_start"].wait(1.0)
    start_thread.join(1.0)
    assert not start_thread.is_alive()
    control.release.clear()

    stop_result: dict[str, object] = {}
    stop_thread = threading.Thread(
        target=lambda: stop_result.setdefault("info", manager.stop_session("stop-race"))
    )
    stop_thread.start()
    assert control.entered["session_stop"].wait(1.0)

    ingested: dict[str, object] = {}
    ingest_thread = threading.Thread(
        target=lambda: ingested.setdefault(
            "result",
            service.handle_message(
                TOPIC, json.dumps(make_payload(sequence=6, monotonic_us=1_600_000))
            ),
        )
    )
    ingest_thread.start()
    for _ in range(100):
        if store.fetch_raw("esp32_01", "b1"):
            break
        threading.Event().wait(0.005)
    assert store.fetch_raw("esp32_01", "b1")
    assert "result" not in ingested

    control.release.set()
    stop_thread.join(1.0)
    ingest_thread.join(1.0)
    assert not stop_thread.is_alive()
    assert not ingest_thread.is_alive()
    assert stop_result["info"].stop_sequence == 6
    assert ingested["result"]["action"] == "stored"

    record = json.loads(
        store.get_processed("esp32_01", "b1", 6, PROCESSOR_VERSION)["result_json"]
    )
    assert record["session_id"] == "stop-race"
    assert [r["sequence"] for r in store.session_raw_rows("stop-race")] == [6]
