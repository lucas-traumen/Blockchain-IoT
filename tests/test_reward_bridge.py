"""Tests for the optional IoT → blockchain_api reward bridge."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from fastapi.testclient import TestClient

from iot_backend.main import create_app
from iot_backend.rewards.client import RewardSettlementError
from iot_backend.settings import Settings

from iot_backend.rewards.client import RewardClient, evidence_hash

REPO_ROOT = Path(__file__).resolve().parents[1]
TOPIC = "hrc/v1/devices/esp32_01/telemetry"


def _payload(sequence: int = 1) -> dict:
    return {
        "schema_version": "1.0",
        "device_id": "esp32_01",
        "boot_id": "bridge-test-boot",
        "sequence": sequence,
        "monotonic_us": sequence * 100_000,
        "measured_at": "2026-10-10T00:00:00.000Z",
        "time_synced": True,
        "distance_mm": None,
        "echo_us": None,
        "valid": False,
        "error_code": "ECHO_TIMEOUT",
    }


def _bridge_settings(tmp_path: Path) -> Settings:
    return Settings(
        mqtt_host="localhost",
        mqtt_port=1883,
        db_path=str(tmp_path / "bridge.db"),
        device_id="esp32_01",
        safety_config_path=str(REPO_ROOT / "configs" / "safety.yaml"),
        model_config_path=str(REPO_ROOT / "configs" / "model.yaml"),
        model_artifact_dir=str(REPO_ROOT / "ai_model" / "artifacts"),
        mqtt_enabled=False,
        ack_timeout_s=0.5,
    )


def test_evidence_hash_is_deterministic_and_bytes32():
    session = {"session_id": "s1", "status": "closed"}
    stats = {"sample_count": 20, "violation_count": 0}
    result = {"outcome": "VALID_NO_VIOLATION", "policyVersion": "session-reward-v2"}
    first = evidence_hash(session, stats, result)
    second = evidence_hash(dict(reversed(list(session.items()))), stats, result)
    assert first == second
    assert len(first) == 66
    assert first.startswith("0x")


def test_reward_client_posts_start_and_settlement():
    requests: list[tuple[str, dict, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - stdlib handler API
            size = int(self.headers["Content-Length"])
            body = json.loads(self.rfile.read(size))
            requests.append((self.path, body, self.headers["Authorization"]))
            response = {"success": True, "path": self.path}
            encoded = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = RewardClient(
            f"http://127.0.0.1:{server.server_address[1]}",
            "i" * 40,
            "0x0000000000000000000000000000000000000001",
        )
        assert client.start_session("s1")["success"] is True
        result = {"outcome": "SENSOR_FAULT", "policyVersion": "session-reward-v2"}
        session = {"session_id": "s1", "ended_at": "2026-10-10T00:00:30.500Z"}
        assert client.settle("s1", session, {"sample_count": 1}, result)["success"] is True
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert requests[0][0] == "/api/session-start"
    assert requests[1][0] == "/api/session-outcome"
    assert requests[0][2] == "Bearer " + "i" * 40
    assert requests[1][1]["outcome"] == "SENSOR_FAULT"
    assert requests[1][1]["endedAt"] == int(datetime(2026, 10, 10, 0, 0, 30, 500000, tzinfo=UTC).timestamp())
    assert len(requests[1][1]["evidenceHash"]) == 66



def test_settlement_outbox_retries_with_same_evidence_and_is_idempotent(tmp_path):
    """A failed write is retryable, while a settled session never writes twice."""

    class FakeRewardClient:
        beneficiary = "0x0000000000000000000000000000000000000001"

        def __init__(self):
            self.start_calls = []
            self.settle_calls = []
            self.fail_once = True

        def start_session(self, session_id):
            self.start_calls.append(session_id)
            return {"success": True, "duplicate": len(self.start_calls) > 1}

        def settle(self, session_id, session, stats, result, *, evidence=None, ended_at=None):
            self.settle_calls.append((session_id, evidence, result["outcome"], ended_at))
            if self.fail_once:
                self.fail_once = False
                raise RewardSettlementError("temporary signer outage", status_code=503)
            return {
                "success": True,
                "duplicate": False,
                "sessionId": session_id,
                "settlementStatus": "NO_REWARD",
                "evidenceHash": evidence,
            }

    app = create_app(_bridge_settings(tmp_path))
    with TestClient(app) as client:
        fake = FakeRewardClient()
        app.state.reward_client = fake
        started = client.post("/sessions/start", json={"session_id": "bridge-outbox-001"})
        assert started.status_code == 200
        app.state.service.handle_message(TOPIC, json.dumps(_payload()))
        stopped = client.post("/sessions/bridge-outbox-001/stop")
        assert stopped.status_code == 200

        first = client.post("/sessions/bridge-outbox-001/settle")
        assert first.status_code == 200
        assert first.json()["status"] == "ERROR"
        saved_after_error = app.state.store.get_reward_settlement("bridge-outbox-001")
        assert saved_after_error["status"] == "ERROR"
        first_evidence = saved_after_error["evidence_hash"]
        first_ended_at = json.loads(saved_after_error["response_json"])["endedAt"]

        second = client.post("/sessions/bridge-outbox-001/settle")
        assert second.status_code == 200
        assert second.json()["status"] == "SETTLED"
        saved_after_success = app.state.store.get_reward_settlement("bridge-outbox-001")
        assert saved_after_success["status"] == "SETTLED"
        assert saved_after_success["evidence_hash"] == first_evidence
        assert fake.settle_calls[0][1] == fake.settle_calls[1][1] == first_evidence
        assert fake.settle_calls[0][3] == fake.settle_calls[1][3] == first_ended_at
        assert json.loads(saved_after_success["response_json"])["endedAt"] == first_ended_at

        third = client.post("/sessions/bridge-outbox-001/settle")
        assert third.status_code == 200
        assert third.json() == second.json()
        assert len(fake.settle_calls) == 2
        assert len(fake.start_calls) == 2
