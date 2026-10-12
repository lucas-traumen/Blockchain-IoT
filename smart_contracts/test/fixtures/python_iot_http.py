"""Isolated real FastAPI/SQLite ingestion fixture for V2 contract integration.

Only MQTT transport/device ACKs and wall time are deterministic. The production
session manager, ingestion/safety/detector, stats, outcome policy and HTTP routes
are not replaced. No production endpoint, active broker, DB or artifact is used.
Prediction is deliberately unavailable: these cases validate direct safety and
reward evidence, not model quality, radio behavior or physical hardware.
"""
from __future__ import annotations

import json
import math
import os
import socket
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

os.environ["HRC_APP_AUTOCREATE"] = "0"
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import uvicorn
from fastapi import HTTPException, Request
from pydantic import BaseModel
from iot_backend import main as iot_main
from iot_backend.sessions import manager as session_module
from iot_backend.settings import Settings

DEVICE = "esp32_01"
BOOT = "synthetic-v3-http-fixture"
clock = {"now": datetime.now(UTC), "sequence": 0, "commands": []}


def iso(value):
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class DeviceTransport:
    """Loopback ACK transport; no network MQTT client is created."""

    def __init__(self, service, host, port):
        self.handlers = []

    def add_wildcard_handler(self, pattern, handler):
        self.handlers.append(handler)

    def start(self):
        pass

    def stop(self):
        pass

    def publish_json(self, topic, control, qos=1):
        clock["commands"].append(control["command"])
        ack = {"schema_version": "1.0", "command": control["command"],
               "session_id": control["session_id"], "device_id": DEVICE,
               "boot_id": BOOT, "sequence": clock["sequence"],
               "monotonic_us": clock["sequence"] * 100_000,
               "time_synced": True, "accepted": True}
        for handler in self.handlers:
            handler(f"hrc/v1/devices/{DEVICE}/ack", json.dumps(ack))


# Injection exists only in this fixture process, never in production settings.
iot_main.MqttListener = DeviceTransport
session_module.utc_now_iso = lambda: iso(clock["now"])
settings = Settings(
    mqtt_host="unused", mqtt_port=0, db_path=sys.argv[1], device_id=DEVICE,
    safety_config_path=str(ROOT / "configs/safety.yaml"),
    model_config_path=str(ROOT / "configs/model.yaml"),
    model_artifact_dir=str(Path(sys.argv[1]).parent / "deliberately-unavailable-model"),
    mqtt_enabled=True, violation_confirmation_enabled=True,
    source_mode="esp32", ack_timeout_s=0.1,
)
app = iot_main.create_app(settings)


class Prepare(BaseModel):
    timestamp: int


class Collect(BaseModel):
    sessionId: str
    scenario: str = "safe"
    durationS: float = 120
    sampleCount: int | None = None
    gapS: float | None = None


def ingest(request, measured_at, distance=900, valid=True, *, monotonic_us=None, boot_id=BOOT):
    clock["sequence"] += 1
    sample = {"schema_version": "1.0", "device_id": DEVICE, "boot_id": boot_id,
              "sequence": clock["sequence"], "monotonic_us": monotonic_us if monotonic_us is not None else clock["sequence"] * 100_000,
              "measured_at": iso(measured_at), "time_synced": True,
              "distance_mm": distance if valid else None,
              "echo_us": 5248 if valid else None, "valid": valid,
              "error_code": None if valid else "ECHO_TIMEOUT"}
    result = request.app.state.service.handle_message(
        f"hrc/v1/devices/{DEVICE}/telemetry", json.dumps(sample), received_at=iso(measured_at))
    if result["action"] != "stored":
        raise HTTPException(500, detail=result)
    return sample


@app.post("/__fixture/prepare")
def prepare(body: Prepare, request: Request):
    if request.app.state.store.running_session(DEVICE):
        raise HTTPException(409, detail="Cannot change fixture start while a session is active")
    clock["now"] = datetime.fromtimestamp(body.timestamp, UTC)
    ingest(request, clock["now"])
    return {"prepared": True, "scope": "synthetic transport and clock; real IoT HTTP/SQLite"}


@app.post("/__fixture/collect")
def collect(body: Collect, request: Request):
    row = request.app.state.store.get_session(body.sessionId)
    if row is None or row["status"] != "running":
        raise HTTPException(409, detail="Collect only into the active sensor session")
    scenarios = {"safe", "spike", "repeated_spikes", "sustained_violation", "violation", "fault",
                 "insufficient", "sequence_gap", "boot_reset", "timing_gap", "moving", "stationary_noise", "distance_spike", "moving_then_still"}
    if body.scenario not in scenarios or not math.isfinite(body.durationS) or not 1 <= body.durationS <= 900:
        raise HTTPException(400, detail="Invalid fixture scenario")
    start = datetime.fromisoformat(row["started_at"].replace("Z", "+00:00"))
    count = body.sampleCount if body.sampleCount is not None else (20 if body.scenario == "insufficient" else math.floor(body.durationS * 10))
    if not 20 <= count <= 10800 or (body.gapS is not None and not 0 < body.gapS < body.durationS / 2):
        raise HTTPException(400, detail="Invalid fixture sample count or gap")
    # Monotonic time is independent of sequence: exercise actual SQLite stats.
    span = body.durationS - 0.1
    normal_interval = span / (count - 1)
    if body.gapS is not None:
        normal_interval = (span - body.gapS) / (count - 2)
    mono_start = clock["sequence"] * 100_000
    offset = 0.1
    for index in range(1, count + 1):
        if index > 1:
            offset += body.gapS if body.gapS is not None and index == 21 else normal_interval
        low = body.scenario in {"spike", "violation"} and index == 21
        low = low or (body.scenario == "violation" and index == 22)
        low = low or (body.scenario == "repeated_spikes" and index % 4 == 0)
        low = low or (body.scenario == "sustained_violation" and 21 <= index <= 60)
        if body.scenario == "sequence_gap" and index == 21:
            clock["sequence"] += 1
        distance = 300 if low else 900
        if body.scenario == "moving" or (body.scenario == "moving_then_still" and offset <= 5):
            distance = 900 + round(100 * math.sin(offset * math.pi / 2))
        elif body.scenario == "stationary_noise":
            distance = 900 + index % 7 - 3
        elif body.scenario == "distance_spike" and index == 21:
            distance = 1100
        ingest(request, start + timedelta(seconds=offset),
               distance=distance, valid=not (body.scenario == "fault" and index == 21),
               monotonic_us=mono_start + round(offset * 1_000_000),
               boot_id="synthetic-reset" if body.scenario == "boot_reset" and index == 21 else BOOT)
    clock["now"] = start + timedelta(seconds=body.durationS)
    return {"sampleCount": count, "endedAt": iso(clock["now"]), "scenario": body.scenario}


@app.get("/__fixture/diagnostics")
def diagnostics(request: Request):
    return {"commands": clock["commands"], "synthetic": True,
            "predictionScope": "unavailable; direct safety remains live"}


sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.bind(("127.0.0.1", 0))
sock.listen(128)
print(json.dumps({"port": sock.getsockname()[1]}), flush=True)
server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
server.run(sockets=[sock])
