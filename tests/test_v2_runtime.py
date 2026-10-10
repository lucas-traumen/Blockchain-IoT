import json
from dataclasses import replace

from fastapi.testclient import TestClient

from iot_backend.ingestion.store import Store
from iot_backend.main import create_app
from iot_backend.settings import load_settings
from tests.test_inference import make_sample


def test_edge_prediction_is_validated_and_deduplicated(tmp_path):
    with Store(tmp_path / "edge.db") as store:
        from iot_backend.ingestion.service import IngestionService
        from iot_backend.safety import load_safety_configs

        service = IngestionService(store, load_safety_configs("configs/safety.yaml"))
        payload = {
            "schema_version": "1.0",
            "sample_ref": {"device_id": "sim-v2", "boot_id": "b1", "sequence": 1},
            "source": "esp32-edge",
            "simulation_trained": True,
            "model_version": "lstm-synthetic-square40-v1",
            "model_sha256": "a" * 64,
            "scaler_sha256": "b" * 64,
            "preprocessing_version": "pp-lstm-v1",
            "threshold_m": 0.04,
            "horizons_ms": [100, 200, 300, 400, 500],
            "distance_m": [0.05, 0.04, 0.03, 0.02, 0.01],
            "predicted_violation_ms": 200,
            "early_warning": True,
            "inference_us": 8200,
            "valid": True,
        }
        topic = "hrc/v1/devices/sim-v2/edge-prediction"
        assert service.handle_edge_prediction(topic, json.dumps(payload))["action"] == "stored"
        assert service.handle_edge_prediction(topic, json.dumps(payload))["action"] == "duplicate"
        assert store.edge_prediction("sim-v2", "b1", 1) is not None
        bad = dict(payload, model_sha256="bad")
        assert service.handle_edge_prediction(topic, json.dumps(bad))["action"] == "rejected"


def test_live_endpoints_return_runtime_data_and_chain_status(tmp_path):
    settings = replace(load_settings(), mqtt_enabled=False, db_path=str(tmp_path / "api.db"), device_id="esp32_01")
    with TestClient(create_app(settings)) as client:
        client.app.state.service.handle_message(
            "hrc/v1/devices/esp32_01/telemetry",
            json.dumps(make_sample(1, 100_000, 300, device_id="esp32_01")),
            received_at="2026-10-10T00:00:00.000Z",
        )
        latest = client.get("/live/latest")
        assert latest.status_code == 200
        assert latest.json()["latest"]["assessment"]["sample_ref"]["sequence"] == 1
        assert latest.json()["source_mode"] == "esp32"
        assert latest.json()["source_label"] == "HARDWARE EXPERIMENTAL"
        assert latest.json()["model"]["threshold_verified_on_hardware"] is False
        history = client.get("/live/history?seconds=60")
        assert len(history.json()["records"]) == 1
        event = client.app.state.store.list_events("safety-v1")[0]
        chain = client.get(f"/events/{event['event_id']}/chain")
        assert chain.status_code == 200
        assert chain.json()["status"] == "OUTBOX_PENDING"
        assert client.get("/models/active").status_code == 200
        assert client.get("/system/health").json()["status"] == "ok"


def test_source_mode_is_explicit_not_inferred_from_artifact_or_device(tmp_path):
    settings = replace(
        load_settings(),
        mqtt_enabled=False,
        db_path=str(tmp_path / "source.db"),
        device_id="esp32_01",
        model_artifact_dir="ai_model/artifacts/lstm-v1",
        source_mode="simulator",
    )
    with TestClient(create_app(settings)) as client:
        live = client.get("/live/latest").json()
        assert live["source_mode"] == "simulator"
        assert live["source_label"] == "SYNTHETIC STREAM"
        assert live["simulation_mode"] is True


def test_chain_outbox_payload_is_immutable_and_retry_is_authorized(tmp_path):
    settings = replace(load_settings(), mqtt_enabled=False, db_path=str(tmp_path / "api.db"), device_id="esp32_01", chain_ingest_token="operator-secret")
    with TestClient(create_app(settings)) as client:
        client.app.state.service.handle_message(
            "hrc/v1/devices/esp32_01/telemetry",
            json.dumps(make_sample(1, 100_000, 300, device_id="esp32_01")),
        )
        event = client.app.state.store.list_events("safety-v1")[0]
        row = client.app.state.store.get_chain_outbox(event["event_id"])
        original = row["payload_json"]
        assert client.post(f"/events/{event['event_id']}/chain/retry").status_code == 403
        response = client.post(f"/events/{event['event_id']}/chain/retry", headers={"X-HRC-Chain-Retry": "operator-secret"})
        assert response.status_code == 200
        assert client.app.state.store.get_chain_outbox(event["event_id"])["payload_json"] == original
