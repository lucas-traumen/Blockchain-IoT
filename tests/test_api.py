"""Kiểm thử M3: FastAPI app — health, sessions lifecycle, export, assessments.

Không dùng broker. Đặt ``HRC_APP_AUTOCREATE=0`` TRƯỚC khi import iot_backend.main
để module không tự tạo app với settings môi trường thật; mỗi test tạo app qua
``create_app(settings)`` với settings tùy chỉnh (DB trong tmp_path).
"""

import os

os.environ.setdefault("HRC_APP_AUTOCREATE", "0")
os.environ.setdefault("HRC_MQTT_ENABLED", "0")

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from iot_backend.ingestion.store import Store
from iot_backend.main import create_app
from iot_backend.safety import load_safety_configs
from iot_backend.settings import Settings
from iot_backend.sessions.export import CSV_COLUMNS, export_csv, iter_jsonl

REPO_ROOT = Path(__file__).resolve().parents[1]
SAFETY_YAML_PATH = REPO_ROOT / "configs" / "safety.yaml"

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


def make_settings(tmp_path: Path, db_name: str = "api.db") -> Settings:
    """Settings tùy chỉnh: DB trong tmp_path, MQTT tắt, config safety của repo."""
    return Settings(
        mqtt_host="localhost",
        mqtt_port=1883,
        db_path=str(tmp_path / db_name),
        device_id="esp32_01",
        safety_config_path=str(SAFETY_YAML_PATH),
        model_config_path="configs/model.yaml",
        model_artifact_dir="ai_model/artifacts",
        mqtt_enabled=False,
        ack_timeout_s=0.5,
    )


@pytest.fixture()
def app(tmp_path):
    """App với settings tạm (chưa chạy lifespan)."""
    return create_app(make_settings(tmp_path))


@pytest.fixture()
def client(app):
    """TestClient trong with-block để lifespan chạy (open/cleanup đúng)."""
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# 12. GET /health: status ok, mqtt false (đã tắt qua settings)
# ---------------------------------------------------------------------------


def test_health(client, tmp_path):
    resp = client.get("/health")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["mqtt"] is False
    assert data["db"] == str(tmp_path / "api.db")
    assert data["processor_version"] == "safety-v1"


# ---------------------------------------------------------------------------
# 13. Vòng đời session qua API: start → ingest → stop → detail
# ---------------------------------------------------------------------------


def test_session_lifecycle_api(client, app):
    resp = client.post("/sessions/start", json={"scenario": "demo"})
    assert resp.status_code == 200
    info = resp.json()
    assert info["status"] == "running"
    session_id = info["session_id"]

    service = app.state.service
    for seq in (101, 102):
        result = service.handle_message(TOPIC, json.dumps(make_payload(sequence=seq)))
        assert result["action"] == "stored"

    resp = client.post(f"/sessions/{session_id}/stop")
    assert resp.status_code == 200
    assert resp.json()["status"] == "closed"

    resp = client.get(f"/sessions/{session_id}")
    assert resp.status_code == 200
    assert resp.json()["sample_count"] == 2


# ---------------------------------------------------------------------------
# 14. Export endpoints: csv và jsonl khớp nội dung export helper
# ---------------------------------------------------------------------------


def test_export_endpoints(client, app, tmp_path):
    start = client.post("/sessions/start", json={})
    session_id = start.json()["session_id"]
    service = app.state.service
    for seq in (101, 102):
        service.handle_message(TOPIC, json.dumps(make_payload(sequence=seq)))

    rows = app.state.store.session_raw_rows(session_id)

    csv_resp = client.get(f"/sessions/{session_id}/export?format=csv")
    assert csv_resp.status_code == 200
    assert csv_resp.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="' in csv_resp.headers["content-disposition"]
    expected_csv = tmp_path / "expected.csv"
    export_csv(rows, expected_csv)
    assert csv_resp.text == expected_csv.read_text(encoding="utf-8")
    assert csv_resp.text.splitlines()[0].split(",") == CSV_COLUMNS

    jsonl_resp = client.get(f"/sessions/{session_id}/export?format=jsonl")
    assert jsonl_resp.status_code == 200
    assert jsonl_resp.headers["content-type"].startswith("application/x-ndjson")
    expected_jsonl = "".join(line + "\n" for line in iter_jsonl(rows))
    assert jsonl_resp.text == expected_jsonl


# ---------------------------------------------------------------------------
# 15. Assessments theo session + sample lookup (404 cho mẫu lạ)
# ---------------------------------------------------------------------------


def test_assessments_and_sample_lookup(client, app):
    start = client.post("/sessions/start", json={})
    session_id = start.json()["session_id"]
    service = app.state.service
    service.handle_message(TOPIC, json.dumps(make_payload(sequence=101)))
    client.post(f"/sessions/{session_id}/stop")

    resp = client.get("/assessments", params={"session_id": session_id})
    assert resp.status_code == 200
    records = resp.json()
    assert len(records) == 1
    assert records[0]["safety"]["status"] == "CLEAR"
    assert records[0]["session_id"] == session_id

    resp = client.get(
        "/samples/esp32_01/b1/101", params={"processor_version": "safety-v1"}
    )
    assert resp.status_code == 200
    assert resp.json()["sample_ref"] == {
        "device_id": "esp32_01",
        "boot_id": "b1",
        "sequence": 101,
    }

    assert client.get("/samples/esp32_01/b1/999").status_code == 404
    assert (
        client.get("/assessments", params={"session_id": "khong-ton-tai"}).status_code
        == 404
    )


# ---------------------------------------------------------------------------
# 16. catch_up khi khởi động: raw có sẵn được tính ngay lúc start app
# ---------------------------------------------------------------------------


def test_catchup_on_startup(tmp_path):
    db_path = tmp_path / "catchup.db"
    store = Store(db_path)
    try:
        for seq in (1, 2):
            store.insert_raw(make_payload(sequence=seq), RECEIVED_AT)
        assert store.fetch_processed("esp32_01", "b1", "safety-v1") == []
    finally:
        store.close()

    app = create_app(make_settings(tmp_path, db_name="catchup.db"))
    with TestClient(app) as client:
        resp = client.get("/assessments")
        assert resp.status_code == 200
        records = resp.json()
        assert len(records) == 2
        assert {r["sample_ref"]["sequence"] for r in records} == {1, 2}


# ---------------------------------------------------------------------------
# 17. M3c M-1: stop session đã đóng qua API → 409 (không phải 404), ranh giới
#     stop_sequence/ended_at giữ nguyên; session lạ vẫn 404
# ---------------------------------------------------------------------------


def test_stop_closed_session_409_and_unknown_404(client, app):
    start = client.post("/sessions/start", json={})
    session_id = start.json()["session_id"]
    service = app.state.service
    service.handle_message(TOPIC, json.dumps(make_payload(sequence=101)))

    first = client.post(f"/sessions/{session_id}/stop")
    assert first.status_code == 200
    row_first = app.state.store.get_session(session_id)

    second = client.post(f"/sessions/{session_id}/stop")
    assert second.status_code == 409
    assert "đã đóng" in second.json()["detail"]

    row_second = app.state.store.get_session(session_id)
    assert row_second["stop_sequence"] == row_first["stop_sequence"] == 101
    assert row_second["ended_at"] == row_first["ended_at"]

    # Session không tồn tại vẫn 404 (phân biệt với 409 đã đóng).
    assert client.post("/sessions/khong-ton-tai/stop").status_code == 404


# ---------------------------------------------------------------------------
# 18. M3c m-6: start trùng session_id qua API → 409 (cả khi đang running lẫn
#     khi PK conflict sau khi session cũ đã đóng)
# ---------------------------------------------------------------------------


def test_start_duplicate_session_id_409(client):
    first = client.post("/sessions/start", json={"session_id": "run_dup"})
    assert first.status_code == 200

    # Session đang chạy → 409 (kiểm tra "đang chạy" trước khi insert).
    running_conflict = client.post("/sessions/start", json={"session_id": "run_dup"})
    assert running_conflict.status_code == 409

    assert client.post("/sessions/run_dup/stop").status_code == 200

    # Đã đóng → insert chạm PK sessions.session_id → ValueError → 409.
    pk_conflict = client.post("/sessions/start", json={"session_id": "run_dup"})
    assert pk_conflict.status_code == 409
    assert "đã tồn tại" in pk_conflict.json()["detail"]


# ---------------------------------------------------------------------------
# 19. M3c INFO: GET /sessions/{id} ghép stats lọc theo PROCESSOR_VERSION —
#     processed của version replay khác không đếm vào violation_count.
# ---------------------------------------------------------------------------


def test_session_detail_stats_filter_by_processor_version(client, app):
    start = client.post("/sessions/start", json={})
    session_id = start.json()["session_id"]
    service = app.state.service
    service.handle_message(
        TOPIC, json.dumps(make_payload(sequence=101, distance_mm=400))
    )  # VIOLATION
    client.post(f"/sessions/{session_id}/stop")

    # Replay cùng mẫu dưới version khác → processed_samples thêm 1 row.
    replayed = service.process_raw_rows(
        app.state.store.session_raw_rows(session_id), "replay-safety-x", mode="offline"
    )
    assert replayed == 1

    # API ghép stats của store (đã truyền PROCESSOR_VERSION) — không đếm replay.
    resp = client.get(f"/sessions/{session_id}")
    assert resp.status_code == 200
    assert resp.json()["violation_count"] == 1
