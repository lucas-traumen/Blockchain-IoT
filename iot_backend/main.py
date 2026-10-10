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
from contextlib import asynccontextmanager
from collections.abc import Iterator
from pathlib import Path
from typing import Any, AsyncIterator

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from iot_backend.inference.predictor import SafetyPredictor, build_predictor
from iot_backend.ingestion.mqtt_listener import MqttListener
from iot_backend.ingestion.service import PROCESSOR_VERSION, IngestionService
from iot_backend.ingestion.store import Store
from iot_backend.safety import load_safety_configs
from iot_backend.rewards import RewardSettlementError, build_reward_client, evaluate_session, evidence_hash
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
            store, configs, session_assigner=manager, predictor=predictor
        )

        listener: MqttListener | None = None
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
            store.close()

    app = FastAPI(title="HRC Safety Log", version="1.0.0", lifespan=lifespan)

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
        }

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
