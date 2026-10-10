"""Service ingestion: message MQTT → validate → raw → tính safety → processed.

``IngestionService`` là cổng vào duy nhất cho telemetry (live qua MQTT và
catch_up khi restart), đảm bảo hai đường tính ra kết quả giống hệt nhau qua
private method ``_compute_and_store``.
"""

import json
import logging
import math
import sqlite3
import threading
from typing import TYPE_CHECKING, Any, Protocol

from iot_backend.ingestion.store import Store
from iot_backend.ingestion.validate import parse_topic, validate_telemetry
from iot_backend.safety import SafetyConfigVersion, assess_sample, get_config_for_time
from iot_backend.util import utc_now_iso

if TYPE_CHECKING:
    from iot_backend.inference.predictor import SafetyPredictor

__all__ = ["PROCESSOR_VERSION", "IngestionService", "SessionAssigner"]

logger = logging.getLogger(__name__)

PROCESSOR_VERSION = "safety-v1"


class SessionAssigner(Protocol):
    """Giao diện gán mẫu vào session (được ``SessionManager`` cài đặt)."""

    def assign(self, device_id: str, boot_id: str, sequence: int) -> str | None:
        """Trả về session_id gán được cho mẫu, hoặc None nếu không gán."""
        ...


class IngestionService:
    """Nhận payload telemetry đã validate, lưu raw và tính kết quả safety."""

    def __init__(
        self,
        store: Store,
        safety_configs: list[SafetyConfigVersion],
        session_assigner: SessionAssigner | None = None,
        predictor: "SafetyPredictor | None" = None,
    ):
        """Lưu store, danh sách phiên bản cấu hình safety (đã load sẵn), hook
        gán session tùy chọn (M3) — mọi object có ``.assign(...)`` — và
        predictor tùy chọn (M6b): ``predictor=None`` giữ hành vi cũ
        (prediction luôn MODEL_UNAVAILABLE); có predictor → mỗi mẫu được push
        vào buffer cửa sổ trượt và dự báo 5 horizon khi đủ window.
        """
        self._store = store
        self._safety_configs = safety_configs
        self._session_assigner = session_assigner
        self._predictor = predictor
        from iot_backend.events import EventDetector
        self._events = EventDetector(store)
        self._compute_lock = threading.RLock()

    def handle_message(
        self,
        topic: str,
        payload_bytes: bytes | str,
        received_at: str | None = None,
    ) -> dict:
        """Lưu raw và tính safety/AI đồng bộ cho caller không phải MQTT."""
        raw_row, response = self._persist_message(topic, payload_bytes, received_at)
        if raw_row is None:
            return response
        summary = self._compute_and_store(raw_row)
        return {
            **response,
            "action": "stored",
            "safety_status": summary["safety_status"],
            "assessment_valid": summary["assessment_valid"],
        }

    def accept_message(
        self,
        topic: str,
        payload_bytes: bytes | str,
        received_at: str | None = None,
    ) -> dict:
        """Validate + commit raw, chưa chạy compute nặng.

        MQTT gọi method này ngay trong callback để broker chỉ nhận ACK sau khi
        raw đã bền vững trong SQLite. Phần safety/AI/event được worker xử lý
        sau; nếu process dừng giữa hai bước, ``catch_up`` sẽ tính lại row còn
        thiếu processed assessment.
        """
        raw_row, response = self._persist_message(topic, payload_bytes, received_at)
        if raw_row is None:
            return response
        return response

    def process_sample(self, sample_ref: dict[str, Any]) -> dict:
        """Tính một raw sample đã commit, dùng bởi MQTT worker."""
        raw_row = self._store.get_raw(
            sample_ref["device_id"], sample_ref["boot_id"], int(sample_ref["sequence"])
        )
        if raw_row is None:
            raise KeyError(f"Không tìm thấy raw sample {sample_ref}")
        summary = self._compute_and_store(raw_row)
        return {
            "sample_ref": summary["sample_ref"],
            "safety_status": summary["safety_status"],
            "assessment_valid": summary["assessment_valid"],
        }

    def _persist_message(
        self,
        topic: str,
        payload_bytes: bytes | str,
        received_at: str | None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """Parse/validate/chèn raw và trả row cho bước compute tiếp theo."""
        if received_at is None:
            received_at = utc_now_iso()
        if isinstance(payload_bytes, (bytes, bytearray)):
            # errors="replace" để luôn ghi được payload thô khi UTF-8 hỏng.
            text = bytes(payload_bytes).decode("utf-8", errors="replace")
        else:
            text = str(payload_bytes)

        # Bước 1: parse JSON.
        try:
            payload: Any = json.loads(text)
        except ValueError:
            self._store.mark_rejected(topic, "invalid_json", text, received_at)
            return None, {"action": "rejected", "reason": "invalid_json", "received_at": received_at}

        # Bước 2: topic và khớp device_id.
        topic_device = parse_topic(topic)
        if topic_device is None:
            self._store.mark_rejected(topic, "bad_topic", text, received_at)
            return None, {"action": "rejected", "reason": "bad_topic", "received_at": received_at}
        if isinstance(payload, dict) and payload.get("device_id") != topic_device:
            self._store.mark_rejected(topic, "device_mismatch", text, received_at)
            return None, {"action": "rejected", "reason": "device_mismatch", "received_at": received_at}

        # Bước 3: schema telemetry_v1.
        schema_ok, reason = validate_telemetry(payload)
        if not schema_ok:
            self._store.mark_rejected(topic, reason, text, received_at)
            return None, {"action": "rejected", "reason": reason, "received_at": received_at}

        # Bước 4: lưu raw (chống trùng, phát hiện mẫu muộn).
        raw_result = self._store.insert_raw(payload, received_at)
        sample_ref = {
            "device_id": payload["device_id"],
            "boot_id": payload["boot_id"],
            "sequence": payload["sequence"],
        }
        if not raw_result.inserted:
            # QoS1 redelivery sau khi raw đã commit nhưng worker chưa kịp
            # compute vẫn cần được đưa lại vào worker; row processed đã có thì
            # giữ semantics duplicate cũ.
            existing = self._store.get_processed(
                payload["device_id"], payload["boot_id"], payload["sequence"], PROCESSOR_VERSION
            )
            if existing is not None:
                logger.debug("Trùng mẫu %s (QoS1 redelivery)", sample_ref)
                return None, {
                    "action": "duplicate",
                    "reason": raw_result.reason,
                    "sample_ref": sample_ref,
                    "received_at": received_at,
                }
            raw_row = self._store.get_raw(
                payload["device_id"], payload["boot_id"], payload["sequence"]
            )
            if raw_row is None:
                raise RuntimeError(f"raw duplicate không đọc lại được {sample_ref}")
            return {
                "device_id": raw_row["device_id"],
                "boot_id": raw_row["boot_id"],
                "sequence": raw_row["sequence"],
            }, {
                "action": "accepted",
                "sample_ref": sample_ref,
                "is_late": raw_result.is_late,
                "received_at": received_at,
            }

        raw_row: dict[str, Any] = {
            "payload_json": json.dumps(payload, sort_keys=True),
            "received_at": received_at,
        }
        return raw_row, {
            "action": "accepted",
            "sample_ref": sample_ref,
            "is_late": raw_result.is_late,
            "received_at": received_at,
        }

    def catch_up(self) -> int:
        """Tính lại mọi raw chưa có processed cho PROCESSOR_VERSION (restart recovery).

        ``received_at`` lấy từ cột raw (không đè thời điểm mới); trả về số row
        processed đã insert mới.
        """
        count = 0
        pending = self._store.pending_raw(PROCESSOR_VERSION)
        pending_keys = {(r["device_id"],r["boot_id"],r["sequence"]) for r in pending}
        for row in self._store.all_raw_rows():
            key = (row["device_id"],row["boot_id"],row["sequence"])
            if key not in pending_keys:
                if self._predictor is not None:
                    payload = json.loads(row["payload_json"])
                    existing = self._store.get_processed(*key, PROCESSOR_VERSION)
                    session_id = json.loads(existing["result_json"])["session_id"]
                    self._predictor.push_sample(payload, session_id)
                continue
            summary = self._compute_and_store(row)
            if summary["processed_inserted"]:
                count += 1
        return count

    def process_raw_rows(
        self, rows: list[sqlite3.Row], processor_version: str, mode: str
    ) -> int:
        """Tính lại một danh sách raw row với processor_version/mode tùy chọn.

        Dùng cho replay offline. Trả về số processed row insert MỚI
        (INSERT OR IGNORE — chạy lại với cùng processor_version → 0).
        """
        return sum(
            1
            for summary in self.compute_raw_rows(rows, processor_version, mode)
            if summary["processed_inserted"]
        )

    def compute_raw_rows(
        self, rows: list[sqlite3.Row], processor_version: str, mode: str
    ) -> list[dict]:
        """Như ``process_raw_rows`` nhưng trả về summary từng raw row đã tính
        (sample_ref, safety_status, assessment_valid, processed_inserted) —
        dùng cho replay tổng hợp by_status từ record vừa tính.
        """
        return [
            self._compute_and_store(
                row, processor_version=processor_version, mode=mode
            )
            for row in rows
        ]

    def _compute_and_store(self, raw_row, processor_version=PROCESSOR_VERSION, mode="live") -> dict:
        with self._compute_lock:
            return self._compute_and_store_locked(raw_row, processor_version, mode)

    def _compute_and_store_locked(
        self,
        raw_row: "sqlite3.Row | dict[str, Any]",
        processor_version: str = PROCESSOR_VERSION,
        mode: str = "live",
    ) -> dict:
        """Tính safety và lưu processed_samples cho một raw row.

        Dùng chung cho handle_message (live), catch_up (restart) và
        process_raw_rows (replay offline) — đảm bảo kết quả safety giống hệt
        nhau cho cùng mẫu, chỉ khác processor_version/mode/computed_at.
        raw_row cần truy cập được ``raw_row["payload_json"]`` và
        ``raw_row["received_at"]``. Trả về dict tóm tắt gồm sample_ref,
        safety_status, assessment_valid và cờ processed_inserted.
        """
        payload: dict[str, Any] = json.loads(raw_row["payload_json"])
        received_at: str = raw_row["received_at"]

        # Thời điểm tra config: measured_at khi đã sync, fallback received_at.
        if payload.get("time_synced") and payload.get("measured_at"):
            config_time_source = "measured_at"
            config_time: str = payload["measured_at"]
        else:
            config_time_source = "received_at"
            config_time = received_at

        try:
            cfg = get_config_for_time(self._safety_configs, config_time)
        except ValueError:
            # Chưa có phiên bản hiệu lực tại thời điểm mẫu → đánh giá UNKNOWN.
            cfg = None

        if cfg is not None:
            sa = assess_sample(
                payload["distance_mm"],
                payload["valid"],
                cfg.params,
                zone_min_m=cfg.zone_min_m,
                zone_max_m=cfg.zone_max_m,
                config_version=cfg.config_version,
                config_time_source=config_time_source,
            )
        else:
            sa = assess_sample(
                payload["distance_mm"],
                payload["valid"],
                None,
                config_version=None,
                config_time_source=config_time_source,
            )

        # Gán mẫu vào session (M3) nếu có hook; không thì None.
        session_id: str | None = None
        if self._session_assigner is not None:
            session_id = self._session_assigner.assign(
                payload["device_id"], payload["boot_id"], int(payload["sequence"])
            )

        if self._predictor is not None:
            # M6b: dự báo thật qua SafetyPredictor. ``sp_m`` None (UNKNOWN —
            # sensor lỗi hoặc chưa có config hiệu lực) → math.inf: phép so
            # ``features[-1] > inf`` luôn False nên ``early_warning`` False,
            # không bịa ngưỡng thay thế; dự báo khoảng cách vẫn chạy nếu
            # cửa sổ đủ. Predictor nhận payload telemetry 11 field gốc.
            prediction = self._predictor.predict(
                payload,
                session_id,
                sp_m=(sa.sp_m if sa.sp_m is not None else math.inf),
                mode=mode,
            )
        else:
            prediction = {
                "status": "MODEL_UNAVAILABLE",
                "model_version": None,
                "horizons_ms": [100, 200, 300, 400, 500],
                "distance_m": None,
                "early_warning": None,
                "stale": False,
                "window_ref": None,
                "mode": mode,
            }
        record: dict[str, Any] = {
            "schema_version": "1.0",
            "sample_ref": {
                "device_id": payload["device_id"],
                "boot_id": payload["boot_id"],
                "sequence": payload["sequence"],
            },
            "session_id": session_id,
            "measured_at": payload["measured_at"],
            "computed_at": utc_now_iso(),
            "received_at": received_at,
            "distance_m": sa.distance_m,
            "safety": {
                "status": sa.status,
                "sensor_valid": sa.sensor_valid,
                "assessment_valid": sa.assessment_valid,
                "sp_m": sa.sp_m,
                "margin_m": sa.margin_m,
                "config_version": sa.config_version,
                "config_time_source": sa.config_time_source,
                "components": sa.components,
                "config_fit_warnings": sa.config_fit_warnings,
            },
            "prediction": prediction,
        }
        with self._store.transaction():
            inserted = self._store.insert_processed(
                device_id=payload["device_id"],
                boot_id=payload["boot_id"],
                sequence=payload["sequence"],
                processor_version=processor_version,
                computed_at=record["computed_at"],
                mode=mode,
                result_json=json.dumps(record, sort_keys=True, allow_nan=False),
            )
            if inserted:
                self._events.observe(record, payload, processor_version)

        return {
            "sample_ref": record["sample_ref"],
            "safety_status": sa.status,
            "assessment_valid": sa.assessment_valid,
            "processed_inserted": inserted,
        }
