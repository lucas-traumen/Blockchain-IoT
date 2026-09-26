"""FastAPI app HRC Safety Log: health, sessions, export, assessments, sample lookup.

Chạy thật (nạp env từ shell hoặc file .env)::

    .venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8000

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
import yaml

from backend.inference.predictor import SafetyPredictor
from backend.ingestion.mqtt_listener import MqttListener
from backend.ingestion.service import PROCESSOR_VERSION, IngestionService
from backend.ingestion.store import Store
from backend.safety import load_safety_configs
from backend.sessions import (
    MqttControlTransport,
    SessionManager,
    iter_jsonl,
)
from backend.sessions.export import CSV_COLUMNS
from backend.sessions.manager import SessionAlreadyClosed, snapshot_configs
from backend.settings import Settings, load_settings

__all__ = ["create_app", "app"]

logger = logging.getLogger(__name__)

DEFAULT_ASSESSMENT_LIMIT = 1000


class SessionStartBody(BaseModel):
    """Body POST /sessions/start — mọi trường tùy chọn."""

    scenario: str | None = None
    object: str | None = None
    zone_note: str | None = None
    session_id: str | None = None


def _build_predictor(settings: Settings) -> SafetyPredictor | None:
    """Tạo ``SafetyPredictor`` khi thư mục artifact đủ model.joblib +
    manifest.json (M6b); thiếu → None (log info).

    Model config nạp MỘT lần từ ``settings.model_config_path`` rồi truyền cho
    predictor (predictor chỉ đọc window/horizon từ dict này). Predictor tự
    nuốt lỗi nạp artifact (``available=False`` → prediction MODEL_UNAVAILABLE,
    PLAN mục 4) nên thiếu/hỏng model không làm app chết. Thứ tự if kiểm file
    TRƯỚC khi gọi SafetyPredictor chỉ để log rõ ràng; ngay cả khi file có
    mà joblib hỏng, predictor vẫn degrade an toàn như trên.
    """
    artifact_dir = Path(settings.model_artifact_dir)
    if not (
        (artifact_dir / "model.joblib").is_file()
        and (artifact_dir / "manifest.json").is_file()
    ):
        logger.info(
            "Không có model artifact trong %s — predictor tắt "
            "(prediction MODEL_UNAVAILABLE)",
            artifact_dir,
        )
        return None
    with Path(settings.model_config_path).open(encoding="utf-8") as handle:
        model_config = yaml.safe_load(handle)
    return SafetyPredictor(artifact_dir, model_config)


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
            listener.start()

        try:
            recovered = service.catch_up()
            if recovered:
                logger.info("catch_up: đã tính %d raw pending khi khởi động", recovered)
            app.state.settings = settings
            app.state.store = store
            app.state.service = service
            app.state.manager = manager
            app.state.listener = listener
            app.state.predictor = predictor
            logger.info(
                "HRC backend sẵn sàng: db=%s, mqtt=%s",
                settings.db_path,
                listener is not None,
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
        return info.to_dict()

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
        return info.to_dict()

    @app.get("/sessions")
    def list_sessions(request: Request) -> list[dict[str, Any]]:
        """Danh sách mọi session kèm thống kê cơ bản."""
        manager: SessionManager = request.app.state.manager
        return [info.to_dict() for info in manager.list_sessions()]

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
# (trước import backend.main) để tránh tạo app với settings môi trường thật.
if os.environ.get("HRC_APP_AUTOCREATE", "1") != "0":
    _env_file = Path(".env")
    if _env_file.exists():
        load_dotenv(_env_file)
    app = create_app(load_settings())
else:
    app = None  # type: ignore[assignment] — test tự create_app(settings)
