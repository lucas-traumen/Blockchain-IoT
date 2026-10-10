import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from iot_backend.events.chain_bridge import ChainEventBridge
from iot_backend.ingestion.store import Store


class _Handler(BaseHTTPRequestHandler):
    responses = []
    requests = []
    lock = threading.Lock()

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode()
        with self.lock:
            self.requests.append((self.path, self.headers.get("Authorization"), body))
            response = self.responses.pop(0) if self.responses else {
                "status": 200,
                "body": {"success": True, "transactionStatus": "confirmed", "transactionHash": "0x" + "a" * 64, "blockNumber": 7, "dataHash": "0x" + "b" * 64},
            }
        encoded = json.dumps(response["body"]).encode()
        self.send_response(response["status"])
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_args):
        return


def _server():
    _Handler.responses = []
    _Handler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _insert(store: Store, event_id="event-1"):
    payload = {
        "schemaVersion": "1.0", "eventId": event_id, "deviceId": "sim-v2",
        "bootId": "boot-1", "sessionId": None, "sequence": 3,
        "timestamp": 1790996000,
        "sensorData": {"distance": 0.03, "valid": True, "safetyThreshold": 0.04, "margin": -0.01},
        "aiResult": {"state": "VIOLATION", "riskLevel": 3, "earlyWarning": False, "predictedViolationMs": None, "model": "v2"},
    }
    now = "2026-10-10T00:00:00Z"
    store._conn.execute(
        "INSERT INTO chain_event_outbox(event_id,payload_json,payload_hash,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        (event_id, json.dumps(payload, sort_keys=True), "hash-1", "OUTBOX_PENDING", now, now),
    )


def test_bridge_confirms_and_reuses_exact_payload(tmp_path):
    server, thread = _server()
    try:
        with Store(tmp_path / "bridge.db") as store:
            _insert(store)
            bridge = ChainEventBridge(store, f"http://127.0.0.1:{server.server_port}", "secret", interval_s=0.1)
            bridge._attempt(store.get_chain_outbox("event-1"))
            row = store.get_chain_outbox("event-1")
            assert row["status"] == "CONFIRMED"
            assert row["attempts"] == 1
            assert _Handler.requests[0][0] == "/api/safety-event"
            assert _Handler.requests[0][1] == "Bearer secret"
            first_payload = _Handler.requests[0][2]
            bridge._attempt(row)
            assert _Handler.requests[1][2] == first_payload
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_bridge_handles_pending_retry_and_permanent_failure(tmp_path):
    server, thread = _server()
    try:
        with Store(tmp_path / "bridge.db") as store:
            _insert(store, "event-pending")
            _Handler.responses = [{"status": 202, "body": {"success": True, "transactionStatus": "pending", "transactionHash": "0x" + "c" * 64}}]
            bridge = ChainEventBridge(store, f"http://127.0.0.1:{server.server_port}", "secret", interval_s=0.1)
            bridge._attempt(store.get_chain_outbox("event-pending"))
            assert store.get_chain_outbox("event-pending")["status"] == "SUBMITTED"

            _insert(store, "event-bad")
            _Handler.responses = [{"status": 400, "body": {"success": False, "error": "bad payload"}}]
            bridge._attempt(store.get_chain_outbox("event-bad"))
            row = store.get_chain_outbox("event-bad")
            assert row["status"] == "DEAD_LETTER"
            assert row["last_http_status"] == 400
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
