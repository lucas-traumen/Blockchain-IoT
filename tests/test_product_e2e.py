"""Local product flow: safety remains available while chain/reward are down."""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi.testclient import TestClient

from iot_backend.events.chain_bridge import ChainEventBridge
from iot_backend.main import create_app
from iot_backend.rewards.client import RewardSettlementError
from iot_backend.settings import load_settings
from tests.test_inference import make_sample


class _ChainHandler(BaseHTTPRequestHandler):
    responses: list[tuple[int, dict]] = []
    payloads: list[str] = []

    def do_POST(self):  # noqa: N802 - stdlib handler API
        size = int(self.headers.get("Content-Length", "0"))
        payload = self.rfile.read(size).decode("utf-8")
        self.__class__.payloads.append(payload)
        status, body = self.__class__.responses.pop(0)
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_args):
        return


class _RewardClient:
    beneficiary = "0x0000000000000000000000000000000000000001"

    def __init__(self):
        self.start_calls: list[str] = []
        self.settle_calls: list[tuple[str, str | None]] = []
        self.fail_once = True

    def start_session(self, session_id: str) -> dict:
        self.start_calls.append(session_id)
        return {"success": True, "duplicate": len(self.start_calls) > 1}

    def settle(self, session_id, session, stats, result, *, evidence=None):
        self.settle_calls.append((session_id, evidence))
        if self.fail_once:
            self.fail_once = False
            raise RewardSettlementError("simulated chain outage", status_code=503)
        return {
            "success": True,
            "duplicate": False,
            "sessionId": session_id,
            "settlementStatus": "PENALTY",
            "evidenceHash": evidence,
        }


def _server():
    _ChainHandler.responses = []
    _ChainHandler.payloads = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ChainHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_local_e2e_confirms_safety_and_recovers_async_bridges(tmp_path):
    chain_server, chain_thread = _server()
    try:
        settings = replace(
            load_settings(),
            mqtt_enabled=False,
            db_path=str(tmp_path / "e2e.db"),
            device_id="esp32_01",
            source_mode="simulator",
            violation_confirmation_enabled=True,
        )
        with TestClient(create_app(settings)) as client:
            reward = _RewardClient()
            client.app.state.reward_client = reward
            started = client.post(
                "/sessions/start", json={"session_id": "e2e-local-001", "scenario": "confirmed-violation"}
            )
            assert started.status_code == 200
            session_id = started.json()["session_id"]
            # Make the test session satisfy the duration policy without sleeping.
            client.app.state.store._conn.execute(
                "UPDATE sessions SET started_at=? WHERE session_id=?",
                ("2026-10-10T00:00:00Z", session_id),
            )

            for sequence in range(1, 41):
                distance = 300 if sequence in (21, 22) else 900
                payload = make_sample(
                    sequence,
                    sequence * 100_000,
                    distance,
                    device_id="esp32_01",
                )
                result = client.app.state.service.handle_message(
                    "hrc/v1/devices/esp32_01/telemetry", json.dumps(payload)
                )
                assert result["action"] == "stored"

            stopped = client.post(f"/sessions/{session_id}/stop")
            assert stopped.status_code == 200
            outcome = client.get(f"/sessions/{session_id}/outcome")
            assert outcome.status_code == 200
            assert outcome.json()["result"]["outcome"] == "VIOLATION_CONFIRMED"

            events = client.get("/events", params={"session_id": session_id}).json()
            assert len(events) == 1
            event_id = events[0]["event_id"]
            outbox = client.app.state.store.get_chain_outbox(event_id)
            assert outbox["status"] == "OUTBOX_PENDING"

            _ChainHandler.responses = [
                (503, {"success": False, "error": "RPC unavailable"}),
                (
                    200,
                    {
                        "success": True,
                        "transactionStatus": "confirmed",
                        "transactionHash": "0x" + "a" * 64,
                        "blockNumber": 12,
                        "dataHash": "0x" + "b" * 64,
                    },
                ),
            ]
            bridge = ChainEventBridge(
                client.app.state.store,
                f"http://127.0.0.1:{chain_server.server_port}",
                "chain-token",
                max_attempts=4,
            )
            bridge._attempt(client.app.state.store.get_chain_outbox(event_id))
            assert client.app.state.store.get_chain_outbox(event_id)["status"] == "RETRYABLE_ERROR"
            bridge._attempt(client.app.state.store.get_chain_outbox(event_id))
            assert client.app.state.store.get_chain_outbox(event_id)["status"] == "CONFIRMED"
            assert _ChainHandler.payloads[0] == _ChainHandler.payloads[1]

            first = client.post(f"/sessions/{session_id}/settle")
            second = client.post(f"/sessions/{session_id}/settle")
            third = client.post(f"/sessions/{session_id}/settle")
            assert first.json()["status"] == "ERROR"
            assert second.json()["status"] == "SETTLED"
            assert third.json() == second.json()
            assert reward.settle_calls[0][1] == reward.settle_calls[1][1]
            assert len(reward.settle_calls) == 2
            assert client.app.state.store.get_reward_settlement(session_id)["status"] == "SETTLED"
    finally:
        chain_server.shutdown()
        chain_thread.join(timeout=2)
        chain_server.server_close()
