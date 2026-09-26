"""Kiểm thử M3: replay offline — kết quả safety giống live, summary, idempotent."""

import json
from pathlib import Path

import pytest

from backend.ingestion.service import IngestionService, Store
from backend.safety import SafetyConfigVersion, load_safety_configs
from scripts.replay import main as replay_main

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


@pytest.fixture()
def db_path(tmp_path):
    """Đường dẫn DB tạm — replay mở connection riêng trên cùng file."""
    return tmp_path / "replay.db"


@pytest.fixture()
def store(db_path):
    s = Store(db_path)
    yield s
    s.close()


@pytest.fixture()
def safety_configs() -> list[SafetyConfigVersion]:
    """Cấu hình safety thật của repo (demo-v1, valid_from 2026-01-01)."""
    return load_safety_configs(SAFETY_YAML_PATH)


def run_replay(db_path: Path) -> int:
    """Chạy scripts.replay.main với DB/config tạm, processor_version mặc định."""
    return replay_main(
        [
            "--db",
            str(db_path),
            "--safety-config",
            str(SAFETY_YAML_PATH),
        ]
    )


# ---------------------------------------------------------------------------
# 17. Replay tái tạo đúng kết quả safety của live (khác version/mode/computed_at)
# ---------------------------------------------------------------------------


def test_replay_identical_safety_fields(store, safety_configs, db_path):
    service = IngestionService(store, safety_configs)
    for seq in range(1, 6):
        result = service.handle_message(TOPIC, json.dumps(make_payload(sequence=seq)))
        assert result["action"] == "stored"

    assert run_replay(db_path) == 0

    live_rows = store.fetch_processed("esp32_01", "b1", "safety-v1")
    offline_rows = store.fetch_processed("esp32_01", "b1", "replay-safety-v1")
    assert len(live_rows) == len(offline_rows) == 5

    live = {r["sequence"]: json.loads(r["result_json"]) for r in live_rows}
    offline = {r["sequence"]: json.loads(r["result_json"]) for r in offline_rows}
    for seq in live:
        # Safety block phải bằng nhau TỪNG field.
        for key, value in live[seq]["safety"].items():
            assert offline[seq]["safety"][key] == value, f"seq={seq} key={key}"
        # Khác processor_version/mode: live và offline ghi đúng cột + record.
        assert offline[seq]["prediction"]["mode"] == "offline"
        assert live[seq]["prediction"]["mode"] == "live"
    for row in offline_rows:
        assert row["processor_version"] == "replay-safety-v1"
        assert row["mode"] == "offline"
    for row in live_rows:
        assert row["mode"] == "live"


# ---------------------------------------------------------------------------
# 18. Summary output: exit 0, JSON có processed + by_status + mode
# ---------------------------------------------------------------------------


def test_replay_summary_output(db_path, capsys):
    store = Store(db_path)
    try:
        for seq in range(1, 6):
            store.insert_raw(make_payload(sequence=seq), RECEIVED_AT)
    finally:
        store.close()

    exit_code = run_replay(db_path)

    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out.strip())
    assert summary["processed"] == 5
    assert summary["mode"] == "offline"
    assert summary["by_status"] == {"CLEAR": 5, "VIOLATION": 0, "UNKNOWN": 0}


# ---------------------------------------------------------------------------
# 19. Idempotent: replay lần 2 cùng processor_version → processed = 0
# ---------------------------------------------------------------------------


def test_replay_idempotent(db_path, capsys):
    store = Store(db_path)
    try:
        for seq in range(1, 6):
            store.insert_raw(make_payload(sequence=seq), RECEIVED_AT)
    finally:
        store.close()

    assert run_replay(db_path) == 0
    first = json.loads(capsys.readouterr().out.strip())
    assert first["processed"] == 5

    assert run_replay(db_path) == 0
    second = json.loads(capsys.readouterr().out.strip())
    assert second["processed"] == 0
