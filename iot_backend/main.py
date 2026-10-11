"""FastAPI app HRC Safety Log: health, sessions, export, assessments, sample lookup.

Chạy thật (nạp env từ shell hoặc file .env)::

    .venv/bin/uvicorn iot_backend.main:app --host 0.0.0.0 --port 8000

Lifespan: mở Store → load safety configs → IngestionService + SessionManager
(gán assigner) → MQTT listener + control transport (nếu ``HRC_MQTT_ENABLED``)
→ catch_up recovery → dọn dẹp khi tắt. Test đặt ``HRC_APP_AUTOCREATE=0`` rồi
gọi ``create_app(settings)`` với settings tùy chỉnh.
"""

import json
import logging
import os
import asyncio
import hashlib
from contextlib import asynccontextmanager
from collections.abc import Iterator
from pathlib import Path
from typing import Any, AsyncIterator

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import yaml

from iot_backend.inference.predictor import SafetyPredictor, build_predictor
from iot_backend.ingestion.mqtt_listener import MqttListener
from iot_backend.ingestion.service import PROCESSOR_VERSION, IngestionService
from iot_backend.ingestion.store import Store
from iot_backend.safety import load_safety_configs
from iot_backend.rewards import RewardSettlementError, build_reward_client, evaluate_session, evidence_hash
from iot_backend.events.chain_bridge import ChainEventBridge
from iot_backend.sessions import (
    MqttControlTransport,
    SessionManager,
    iter_jsonl,
)
from iot_backend.sessions.export import CSV_COLUMNS
from iot_backend.sessions.manager import SessionAlreadyClosed, snapshot_configs
from iot_backend.settings import Settings, load_settings
from iot_backend.util import utc_now_iso

__all__ = ["create_app", "app"]

logger = logging.getLogger(__name__)

DEFAULT_ASSESSMENT_LIMIT = 1000


class SessionStartBody(BaseModel):
    """Body POST /sessions/start — mọi trường tùy chọn."""

    scenario: str | None = None
    object: str | None = None
    zone_note: str | None = None
    session_id: str | None = None


def _build_predictor(settings: Settings) -> SafetyPredictor:
    return build_predictor(
        settings.model_artifact_dir, settings.model_config_path, settings.model_type
    )


def create_app(settings: Settings) -> FastAPI:
    """Tạo FastAPI app với settings truyền vào (inject được khi test)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Mở tài nguyên khi start, catch_up recovery, dọn dẹp khi tắt."""
        store = Store(settings.db_path)
        configs = load_safety_configs(settings.safety_config_path)
        manager = SessionManager(
            store,
            settings.device_id,
            ack_timeout_s=settings.ack_timeout_s,
            safety_configs_snapshot=snapshot_configs(configs),
        )
        predictor = _build_predictor(settings)
        service = IngestionService(
            store,
            configs,
            session_assigner=manager,
            predictor=predictor,
            violation_confirmation_enabled=settings.violation_confirmation_enabled,
        )

        listener: MqttListener | None = None
        chain_bridge: ChainEventBridge | None = None
        if settings.mqtt_enabled:
            listener = MqttListener(service, settings.mqtt_host, settings.mqtt_port)
            control = MqttControlTransport(listener)  # tự đăng ký handler ack wildcard
            manager.set_control(control)

        try:
            recovered = service.catch_up()
            if recovered:
                logger.info("catch_up: đã tính %d raw pending khi khởi động", recovered)
            if listener is not None:
                listener.start()
            app.state.settings = settings
            app.state.store = store
            app.state.service = service
            app.state.manager = manager
            app.state.listener = listener
            app.state.predictor = predictor
            app.state.reward_client = build_reward_client(settings)
            chain_bridge = ChainEventBridge(
                store,
                settings.chain_api_url,
                settings.chain_ingest_token,
                max_attempts=settings.chain_max_attempts,
            )
            app.state.chain_bridge = chain_bridge
            if settings.chain_auto_submit:
                chain_bridge.start()
            logger.info(
                "HRC iot_backend sẵn sàng: db=%s, mqtt=%s, reward_bridge=%s",
                settings.db_path,
                listener is not None,
                app.state.reward_client is not None,
            )
            yield
        finally:
            if listener is not None:
                listener.stop()
            if chain_bridge is not None:
                chain_bridge.stop()
            store.close()

    app = FastAPI(title="HRC Safety Log", version="1.0.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Accept", "Last-Event-ID", "X-HRC-Chain-Retry"],
        expose_headers=["Content-Type"],
    )

    # ------------------------------------------------------------------ health

    @app.get("/health")
    def health(request: Request) -> dict[str, Any]:
        """Trạng thái app: mqtt đang bật hay không, đường dẫn DB, processor version."""
        return {
            "status": "ok",
            "mqtt": request.app.state.listener is not None,
            "db": settings.db_path,
            "processor_version": PROCESSOR_VERSION,
            "model_type": settings.model_type,
            "model_available": request.app.state.predictor.available,
            "reward_bridge": request.app.state.reward_client is not None,
            "reward_auto_start": settings.reward_auto_start,
            "reward_auto_settle": settings.reward_auto_settle,
            "chain_bridge": {
                "configured": bool(settings.chain_api_url and settings.chain_ingest_token),
                "running": bool(getattr(request.app.state, "chain_bridge", None) and settings.chain_auto_submit),
            },
            "violation_confirmation": {
                "enabled": settings.violation_confirmation_enabled,
                "rule": "2_of_3_valid_consecutive" if settings.violation_confirmation_enabled else "legacy_transition",
            },
        }

    # ----------------------------------------------------------- live/runtime

    def _model_info(request: Request) -> dict[str, Any]:
        predictor = request.app.state.predictor
        manifest = getattr(predictor, "_manifest", None) or {}
        artifact_path = str(settings.model_artifact_dir)
        model_hash = manifest.get("model_file_sha256")
        training_data = "unknown"
        simulation_mode = False
        try:
            config = yaml.safe_load(Path(settings.model_config_path).read_text(encoding="utf-8"))
            if isinstance(config, dict):
                training_data = str(config.get("training_data", "unknown"))
                simulation_mode = bool(config.get("simulation_mode", False))
        except (OSError, TypeError, ValueError, yaml.YAMLError):
            logger.warning("Không đọc được model config provenance: %s", settings.model_config_path)
        metrics = {}
        metrics_path = Path(artifact_path) / "metrics.json"
        if metrics_path.exists():
            try:
                metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                metrics = {}
        return {
            "status": "ACTIVE" if predictor.available else "UNAVAILABLE",
            "model_type": settings.model_type,
            "model_version": manifest.get("model_version"),
            "model_sha256": model_hash,
            "scaler_sha256": manifest.get("scaler_sha256"),
            "dataset_sha256": manifest.get("dataset_sha256"),
            "split_manifest_sha256": manifest.get("split_manifest_sha256"),
            "preprocessing_version": manifest.get("preprocessing_version"),
            "input_shape": manifest.get("input_shape"),
            "output_shape": manifest.get("output_shape"),
            "horizons_ms": manifest.get("horizons_ms", [100, 200, 300, 400, 500]),
            "training_data": training_data,
            "simulation_mode": simulation_mode,
            "threshold_verified_on_hardware": False,
            "artifact_path": artifact_path,
            "load_error": predictor.load_error,
            "metrics": metrics,
            "promotion_status": manifest.get(
                "promotion_status",
                "SYNTHETIC_VALIDATED" if simulation_mode else "ACTIVE",
            ),
            "limitations": (
                [
                    "synthetic-only training and evaluation",
                    "threshold is a mathematical benchmark, not hardware calibration",
                ] if simulation_mode else []
            ),
        }

    def _source_info() -> dict[str, Any]:
        simulation_mode = settings.source_mode == "simulator"
        return {
            "source_mode": settings.source_mode,
            "source_label": "SYNTHETIC STREAM" if simulation_mode else "HARDWARE EXPERIMENTAL",
            "simulation_mode": simulation_mode,
            "threshold_verified_on_hardware": False,
        }

    def _live_record(request: Request) -> dict[str, Any] | None:
        store: Store = request.app.state.store
        row = store.latest_processed(settings.device_id, PROCESSOR_VERSION)
        if row is None:
            return None
        record = json.loads(row["result_json"])
        raw = store.get_raw(row["device_id"], row["boot_id"], row["sequence"])
        telemetry = json.loads(raw["payload_json"]) if raw else None
        edge = store.edge_prediction(row["device_id"], row["boot_id"], row["sequence"])
        edge_payload = json.loads(edge["payload_json"]) if edge else None
        model = _model_info(request)
        backend_distance = record.get("prediction", {}).get("distance_m")
        edge_distance = edge_payload.get("distance_m") if edge_payload else None
        parity_diff = None
        parity_status = "UNAVAILABLE"
        if isinstance(backend_distance, list) and isinstance(edge_distance, list) and len(backend_distance) == len(edge_distance):
            parity_diff = max(abs(float(a) - float(b)) for a, b in zip(backend_distance, edge_distance))
            parity_status = "MATCH" if parity_diff <= 0.0001 else "MISMATCH"
        return {
            "assessment": record,
            "telemetry": telemetry,
            "edge_prediction": edge_payload,
            "provenance": {
                "authority": "backend",
                "training_data": model["training_data"],
                "dataset_sha256": model["dataset_sha256"],
                "model_sha256": model["model_sha256"],
                "scaler_sha256": model["scaler_sha256"],
                "edge_available": edge_payload is not None,
                "edge_backend_max_abs_diff_m": parity_diff,
                "parity_status": parity_status,
            },
            "received_at": raw["received_at"] if raw else None,
        }

    @app.get("/live/latest")
    def live_latest(request: Request) -> dict[str, Any]:
        record = _live_record(request)
        model = _model_info(request)
        source = _source_info()
        return {
            "source": "LIVE",
            **source,
            "limitations": model["limitations"],
            "model": model,
            "latest": record,
        }

    @app.get("/live/history")
    def live_history(request: Request, seconds: int = Query(60, ge=1, le=3600), limit: int = Query(600, ge=1, le=5000)) -> dict[str, Any]:
        rows = request.app.state.store.recent_processed(settings.device_id, PROCESSOR_VERSION, limit)
        records = []
        for row in rows:
            record = json.loads(row["result_json"])
            raw = request.app.state.store.get_raw(row["device_id"], row["boot_id"], row["sequence"])
            records.append({"assessment": record, "telemetry": json.loads(raw["payload_json"]) if raw else None, "received_at": raw["received_at"] if raw else None})
        if records:
            newest = records[-1]["received_at"]
            try:
                from iot_backend.util import parse_iso_utc
                cutoff = parse_iso_utc(newest).timestamp() - seconds
                records = [r for r in records if not r["received_at"] or parse_iso_utc(r["received_at"]).timestamp() >= cutoff]
            except (TypeError, ValueError):
                pass
        model = _model_info(request)
        return {
            "source": "LIVE",
            **_source_info(),
            "seconds": seconds,
            "records": records,
            "model": model,
        }

    @app.get("/models/active")
    def models_active(request: Request) -> dict[str, Any]:
        return _model_info(request)

    @app.get("/models/metrics")
    def models_metrics(request: Request) -> dict[str, Any]:
        info = _model_info(request)
        return {"model_version": info["model_version"], "metrics": info["metrics"], "training_data": info["training_data"], "simulation_mode": info["simulation_mode"], "threshold_verified_on_hardware": False}

    @app.get("/system/health")
    def system_health(request: Request) -> dict[str, Any]:
        latest = _live_record(request)
        return {"status": "ok", "mqtt": request.app.state.listener is not None, "db": True, "ai": _model_info(request), "chain_bridge": {"configured": bool(settings.chain_api_url and settings.chain_ingest_token), "pending": len(request.app.state.store.due_chain_outbox()), "running": settings.chain_auto_submit}, **_source_info(), "latest_received_at": latest.get("received_at") if latest else None}

    @app.get("/live/stream")
    async def live_stream(request: Request) -> StreamingResponse:
        async def events():
            last_key = request.headers.get("last-event-id", "")
            last_seen = last_key
            logger.info("SSE_CLIENT_CONNECTED last_event_id=%s", last_key or "none")
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    snapshot = _live_record(request)
                    if snapshot:
                        assessment = snapshot["assessment"]
                        ref = assessment["sample_ref"]
                        event_id = f"{ref['device_id']}:{ref['boot_id']}:{ref['sequence']}"
                        if event_id != last_seen:
                            last_seen = event_id
                            for event_type, data in (("telemetry", snapshot["telemetry"]), ("assessment", assessment), ("runtime", snapshot)):
                                yield f"id: {event_id}\nevent: {event_type}\ndata: {json.dumps(data, ensure_ascii=True, separators=(',', ':'))}\n\n"
                    yield f"event: heartbeat\ndata: {json.dumps({'at': utc_now_iso()}, separators=(',', ':'))}\n\n"
                    await asyncio.sleep(1)
            finally:
                logger.info("SSE_CLIENT_DROPPED last_event_id=%s", last_seen or "none")
        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})

    # ---------------------------------------------------------------- sessions

    @app.post("/sessions/start")
    def start_session(body: SessionStartBody, request: Request) -> dict[str, Any]:
        """Bắt đầu session; session khác đang chạy → 409."""
        manager: SessionManager = request.app.state.manager
        try:
            info = manager.start_session(
                scenario=body.scenario,
                obj=body.object,
                zone_note=body.zone_note,
                session_id=body.session_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        response = info.to_dict()
        client = request.app.state.reward_client
        if client is not None and settings.reward_auto_start:
            try:
                response["reward_start"] = client.start_session(info.session_id)
            except RewardSettlementError as exc:
                # Keep the local session visible for diagnosis/manual retry;
                # no token movement is claimed when the signer API failed.
                response["reward_start"] = {
                    "success": False,
                    "status": "ERROR",
                    "error": str(exc),
                    "http_status": exc.status_code,
                }
        return response

    @app.post("/sessions/{session_id}/stop")
    def stop_session(session_id: str, request: Request) -> dict[str, Any]:
        """Đóng session theo id; không tìm thấy → 404, đã đóng → 409."""
        manager: SessionManager = request.app.state.manager
        try:
            info = manager.stop_session(session_id)
        except SessionAlreadyClosed as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        response = info.to_dict()
        client = request.app.state.reward_client
        if client is not None and settings.reward_auto_settle:
            response["reward_settlement"] = _settle_session(request, session_id, client)
        return response

    @app.get("/sessions")
    def list_sessions(request: Request) -> list[dict[str, Any]]:
        """Danh sách mọi session kèm thống kê cơ bản."""
        manager: SessionManager = request.app.state.manager
        return [info.to_dict() for info in manager.list_sessions()]

    def _session_outcome_data(request: Request, session_id: str) -> dict[str, Any]:
        # MQTT compute is asynchronous; catch_up makes the decision use all raw
        # rows already committed before reward policy evaluation.
        request.app.state.service.catch_up()
        manager: SessionManager = request.app.state.manager
        store: Store = request.app.state.store
        try:
            info = manager.get_session(session_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        session = info.to_dict()
        stats = store.session_stats(session_id, PROCESSOR_VERSION)
        return {
            "sessionId": session_id,
            "deviceId": session["device_id"],
            "session": session,
            "stats": stats,
            "result": evaluate_session(session, stats),
        }

    def _settle_session(request: Request, session_id: str, client: Any) -> dict[str, Any]:
        store: Store = request.app.state.store
        try:
            outcome = _session_outcome_data(request, session_id)
            session = outcome["session"]
            result = outcome["result"]
            previous = store.get_reward_settlement(session_id)
            if previous is not None and previous["status"] == "SETTLED":
                return json.loads(previous["response_json"])
            evidence = previous["evidence_hash"] if previous is not None else evidence_hash(session, outcome["stats"], result)
            store.save_reward_settlement(
                session_id, client.beneficiary, result["outcome"], evidence, "PENDING",
                {"status": "PENDING", "outcome": result}, utc_now_iso()
            )
            # Retry the on-chain stake start before settlement. This makes a
            # temporary API outage during START recoverable at STOP without
            # creating a second session or changing the IoT policy result.
            reward_start = client.start_session(session_id)
            settlement = client.settle(session_id, session, outcome["stats"], result, evidence=evidence)
            response = {
                "status": "SETTLED",
                "outcome": result,
                "reward_start": reward_start,
                "settlement": settlement,
            }
            store.save_reward_settlement(
                session_id, client.beneficiary, result["outcome"], evidence, "SETTLED", response, utc_now_iso()
            )
            return response
        except RewardSettlementError as exc:
            response = {"status": "ERROR", "error": str(exc), "http_status": exc.status_code}
            try:
                current = request.app.state.store.get_reward_settlement(session_id)
                if current is not None:
                    request.app.state.store.save_reward_settlement(
                        session_id, current["beneficiary"], current["outcome"], current["evidence_hash"],
                        "ERROR", response, utc_now_iso()
                    )
            except Exception:
                logger.exception("Không lưu được reward outbox state cho %s", session_id)
            return response

    @app.post("/sessions/{session_id}/settle")
    def settle_session(session_id: str, request: Request) -> dict[str, Any]:
        client = request.app.state.reward_client
        if client is None:
            raise HTTPException(status_code=503, detail="Reward bridge is not configured")
        return _settle_session(request, session_id, client)

    @app.get("/sessions/{session_id}")
    def get_session(session_id: str, request: Request) -> dict[str, Any]:
        """Chi tiết session + stats đầy đủ (sample/late/gap/violation/clear/unknown)."""
        store: Store = request.app.state.store
        manager: SessionManager = request.app.state.manager
        try:
            info = manager.get_session(session_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        data = info.to_dict()
        stats = store.session_stats(session_id, PROCESSOR_VERSION)
        for key, value in stats.items():
            data.setdefault(key, value)
        return data

    @app.get("/sessions/{session_id}/outcome")
    def session_outcome(session_id: str, request: Request) -> dict[str, Any]:
        """Return the deterministic session outcome used by reward settlement."""
        return _session_outcome_data(request, session_id)

    @app.get("/sessions/{session_id}/export")
    def export_session(
        session_id: str,
        request: Request,
        format: str = Query("jsonl", pattern="^(jsonl|csv)$"),
    ) -> StreamingResponse:
        """Streaming export raw samples của session dạng JSONL hoặc CSV."""
        store: Store = request.app.state.store
        if store.get_session(session_id) is None:
            raise HTTPException(
                status_code=404, detail=f"Không tìm thấy session {session_id}"
            )
        rows = store.session_raw_rows(session_id)
        if format == "csv":
            # Streaming CSV: sinh từ iterator của export helper qua buffer nhỏ.
            return StreamingResponse(
                _iter_csv(rows),
                media_type="text/csv",
                headers={
                    "Content-Disposition": f'attachment; filename="{session_id}.csv"'
                },
            )
        return StreamingResponse(
            _iter_ndjson(rows),
            media_type="application/x-ndjson",
            headers={
                "Content-Disposition": f'attachment; filename="{session_id}.jsonl"'
            },
        )

    @app.get("/events")
    def events(request: Request, session_id: str | None = None,
               processor_version: str = PROCESSOR_VERSION,
               limit: int = Query(1000, ge=1, le=100000)) -> list[dict]:
        return request.app.state.store.list_events(processor_version, session_id, limit)

    @app.get("/events/{event_id}")
    def event_package(event_id: str, request: Request) -> dict:
        package = request.app.state.store.event_package(event_id)
        if package is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy event")
        return package

    @app.get("/events/{event_id}/chain")
    def event_chain_status(event_id: str, request: Request) -> dict[str, Any]:
        if request.app.state.store.event_package(event_id) is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy event")
        row = request.app.state.store.get_chain_outbox(event_id)
        if row is None:
            return {"event_id": event_id, "status": "LOCAL_ONLY"}
        return {key: row[key] for key in row.keys()}

    @app.post("/events/{event_id}/chain/retry")
    def retry_event_chain(event_id: str, request: Request) -> dict[str, Any]:
        expected = settings.chain_ingest_token
        supplied = request.headers.get("X-HRC-Chain-Retry")
        if not expected or supplied != expected:
            raise HTTPException(status_code=403, detail="operator chain retry authorization required")
        row = request.app.state.store.get_chain_outbox(event_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy chain outbox row")
        request.app.state.store.update_chain_outbox(event_id, status="OUTBOX_PENDING", next_attempt_at=None, last_error=None)
        bridge = request.app.state.chain_bridge
        if settings.chain_auto_submit:
            bridge.start()
        return {"event_id": event_id, "status": "OUTBOX_PENDING"}

    # ------------------------------------------------------ assessments/samples

    @app.get("/assessments")
    def assessments(
        request: Request,
        session_id: str | None = Query(None),
        processor_version: str = Query(PROCESSOR_VERSION),
        limit: int = Query(DEFAULT_ASSESSMENT_LIMIT, ge=1, le=100000),
    ) -> list[dict[str, Any]]:
        """Danh sách assessment (result_json parsed) theo session hoặc toàn bộ."""
        store: Store = request.app.state.store
        if session_id is not None:
            if store.get_session(session_id) is None:
                raise HTTPException(
                    status_code=404, detail=f"Không tìm thấy session {session_id}"
                )
            rows = store.processed_for_session(session_id, processor_version, limit)
        else:
            rows = store.all_processed(processor_version, limit)
        return [json.loads(row["result_json"]) for row in rows]

    @app.get("/samples/{device_id}/{boot_id}/{sequence}")
    def sample_lookup(
        device_id: str,
        boot_id: str,
        sequence: int,
        request: Request,
        processor_version: str = Query(PROCESSOR_VERSION),
    ) -> dict[str, Any]:
        """Tra một assessment theo khóa (device_id, boot_id, sequence); 404 nếu không có."""
        store: Store = request.app.state.store
        row = store.get_processed(device_id, boot_id, sequence, processor_version)
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Không tìm thấy sample {device_id}/{boot_id}/{sequence} "
                    f"(processor_version={processor_version})"
                ),
            )
        return json.loads(row["result_json"])

    def _iter_ndjson(rows: list) -> Iterator[str]:
        """Sinh nội dung NDJSON: mỗi dòng payload_json nguyên vẹn kèm newline."""
        for line in iter_jsonl(rows):
            yield line + "\n"

    def _iter_csv(rows: list) -> Iterator[str]:
        """Sinh nội dung CSV dòng-tiến dùng cho StreamingResponse."""
        import csv as _csv
        import io

        buffer = io.StringIO()
        writer = _csv.writer(buffer, lineterminator="\n")
        writer.writerow(CSV_COLUMNS)
        yield buffer.getvalue()
        for row in rows:
            payload = json.loads(row["payload_json"])
            buffer.seek(0)
            buffer.truncate()
            writer = _csv.writer(buffer, lineterminator="\n")
            writer.writerow([payload.get(column) for column in CSV_COLUMNS])
            yield buffer.getvalue()

    return app


# Module level: tạo app thật khi chạy uvicorn. Test đặt HRC_APP_AUTOCREATE=0
# (trước import iot_backend.main) để tránh tạo app với settings môi trường thật.
if os.environ.get("HRC_APP_AUTOCREATE", "1") != "0":
    _env_file = Path(".env")
    if _env_file.exists():
        load_dotenv(_env_file)
    app = create_app(load_settings())
else:
    app = None  # type: ignore[assignment] — test tự create_app(settings)
