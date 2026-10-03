"""SessionManager: quản lý lượt thu khóa bằng ack thiết bị (fallback DB).

Ranh giới session: lệnh ``session_start``/``session_stop`` qua MQTT control,
thiết bị trả ack ``{boot_id, sequence}`` (schema ack_v1). Không có control/broker
→ fallback từ DB (max sequence đã nhận / đã gán). Trong lúc chờ ACK,
``SessionManager`` giữ boundary gate; MQTT commit raw trước, worker compute sau
để mẫu đang bay không bị tính ngoài session.
"""

import json
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from backend.ingestion.mqtt_listener import MqttListener
from backend.ingestion.service import PROCESSOR_VERSION
from backend.ingestion.store import Store
from backend.safety import SafetyConfigVersion
from backend.util import utc_now_iso

__all__ = [
    "ControlTransport",
    "MqttControlTransport",
    "SessionAlreadyClosed",
    "SessionInfo",
    "SessionManager",
    "snapshot_configs",
]

CONTROL_ACK_PATTERN = "hrc/v1/devices/+/ack"


class SessionAlreadyClosed(ValueError):
    """Stop một session đã đóng — không gửi ``session_stop`` lần nữa xuống device.

    Là subclass của ``ValueError`` để CLI (bắt ValueError chung) hoạt động
    không đổi; API phân biệt 404 (không tồn tại) với 409 (đã đóng) qua lớp này.
    """


def snapshot_configs(configs: list[SafetyConfigVersion]) -> dict:
    """Chuyển danh sách cấu hình safety thành dict JSON-able để lưu ``config_json``."""
    return {
        "safety_versions": [
            {
                "config_version": c.config_version,
                "valid_from": c.valid_from,
                "params": {
                    "vh": c.params.vh_m_s,
                    "vr": c.params.vr_m_s,
                    "tr_s": c.params.tr_s,
                    "ts_s": c.params.ts_s,
                    "c_m": c.params.c_m,
                    "zd_m": c.params.zd_m,
                    "zr_m": c.params.zr_m,
                },
                "measurement_zone_m": {"min": c.zone_min_m, "max": c.zone_max_m},
            }
            for c in configs
        ]
    }


@dataclass(frozen=True)
class SessionInfo:
    """Thông tin một session (kèm thống kê) — mọi trường public, dễ serialize."""

    session_id: str
    device_id: str
    scenario: str | None
    object: str | None
    zone_note: str | None
    status: str
    started_at: str
    ended_at: str | None
    start_boot_id: str | None
    start_sequence: int | None
    stop_sequence: int | None
    boundary_source: str | None
    boot_change: bool
    sample_count: int
    late_count: int
    gap_count: int

    def to_dict(self) -> dict:
        """Dict đầy đủ các trường, dùng cho CLI JSON và API."""
        return {
            "session_id": self.session_id,
            "device_id": self.device_id,
            "scenario": self.scenario,
            "object": self.object,
            "zone_note": self.zone_note,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "start_boot_id": self.start_boot_id,
            "start_sequence": self.start_sequence,
            "stop_sequence": self.stop_sequence,
            "boundary_source": self.boundary_source,
            "boot_change": self.boot_change,
            "sample_count": self.sample_count,
            "late_count": self.late_count,
            "gap_count": self.gap_count,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row, stats: dict) -> "SessionInfo":
        """Tạo SessionInfo từ sqlite3.Row của bảng sessions + stats từ store."""
        return cls(
            session_id=row["session_id"],
            device_id=row["device_id"],
            scenario=row["scenario"],
            object=row["object"],
            zone_note=row["zone_note"],
            status=row["status"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            start_boot_id=row["start_boot_id"],
            start_sequence=row["start_sequence"],
            stop_sequence=row["stop_sequence"],
            boundary_source=row["boundary_source"],
            boot_change=bool(row["boot_change"]),
            sample_count=int(stats.get("sample_count", 0)),
            late_count=int(stats.get("late_count", 0)),
            gap_count=int(stats.get("gap_count", 0)),
        )


class ControlTransport(Protocol):
    """Giao diện gửi lệnh ranh giới tới thiết bị và chờ ack."""

    def session_boundary(
        self, device_id: str, command: str, session_id: str, timeout_s: float
    ) -> dict | None:
        """Trả về ``{'boot_id':..., 'sequence':...}`` từ ack khớp, hoặc None."""
        ...


class MqttControlTransport:
    """Control transport qua MQTT: publish control, chờ ack khớp từ device."""

    def __init__(self, listener: MqttListener):
        """Đăng ký handler wildcard ack vào listener; ack đẩy vào deque có lock."""
        self._listener = listener
        self._lock = threading.Lock()
        self._acks: deque[dict[str, Any]] = deque()
        listener.add_wildcard_handler(CONTROL_ACK_PATTERN, self._on_ack)

    def _on_ack(self, topic: str, payload_str: str) -> None:
        """Nhận ack từ thread paho: parse JSON, đẩy vào deque (lỗi bỏ qua)."""
        try:
            ack = json.loads(payload_str)
        except ValueError:
            return
        if isinstance(ack, dict):
            with self._lock:
                self._acks.append(ack)

    def _pop_matching(
        self, device_id: str, command: str, session_id: str
    ) -> dict | None:
        """Tìm và lấy ack khớp command+session_id+device_id+accepted=True."""
        with self._lock:
            for index, ack in enumerate(self._acks):
                if (
                    ack.get("schema_version") == "1.0"
                    and ack.get("command") == command
                    and ack.get("session_id") == session_id
                    and ack.get("device_id") == device_id
                    and ack.get("accepted") is True
                    and isinstance(ack.get("boot_id"), str)
                    and isinstance(ack.get("sequence"), int)
                ):
                    del self._acks[index]
                    return ack
        return None

    def session_boundary(
        self, device_id: str, command: str, session_id: str, timeout_s: float
    ) -> dict | None:
        """Publish lệnh control rồi poll deque chờ ack tới ``timeout_s``.

        Trả về ``{'boot_id':..., 'sequence':...}`` khi nhận ack khớp; None khi
        hết timeout (kể cả khi chưa kết nối broker — publish bị listener bỏ qua).
        """
        control = {
            "schema_version": "1.0",
            "command": command,
            "session_id": session_id,
            "issued_at": utc_now_iso(),
        }
        self._listener.publish_json(
            f"hrc/v1/devices/{device_id}/control", control, qos=1
        )
        deadline = time.monotonic() + timeout_s
        while True:
            ack = self._pop_matching(device_id, command, session_id)
            if ack is not None:
                return {"boot_id": ack["boot_id"], "sequence": ack["sequence"]}
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.05)


class SessionManager:
    """Quản lý vòng đời session: start/stop/list/get và gán mẫu cho service."""

    def __init__(
        self,
        store: Store,
        device_id: str,
        control: ControlTransport | None = None,
        ack_timeout_s: float = 2.0,
        safety_configs_snapshot: dict | None = None,
    ):
        """Lưu store, device mặc định, control transport tùy chọn và snapshot cấu hình."""
        self._store = store
        self._device_id = device_id
        self._control = control
        self._ack_timeout_s = ack_timeout_s
        self._safety_configs_snapshot = safety_configs_snapshot
        # START/STOP chờ ACK trong khi telemetry vẫn có thể tới.  Ingestion
        # worker sẽ tạm chờ condition này để mẫu bay không bị tính trước khi
        # session boundary được ghi vào DB.
        self._boundary_condition = threading.Condition()
        self._boundary_in_progress: str | None = None

    def set_control(self, control: ControlTransport | None) -> None:
        """Gán control transport sau khi tạo (dùng trong lifespan của FastAPI)."""
        self._control = control

    # ------------------------------------------------------------ start/stop

    def start_session(
        self,
        scenario: str | None = None,
        obj: str | None = None,
        zone_note: str | None = None,
        session_id: str | None = None,
    ) -> SessionInfo:
        """Bắt đầu một session running; chốt ranh giới start từ ack hoặc DB.

        Ack thiết bị → ``start_sequence = ack.sequence + 1`` (mẫu tại lệnh vẫn
        thuộc session trước, mẫu sau lệnh thuộc session mới). Không ack →
        fallback: boot + max sequence đã nhận của device trong DB; DB rỗng →
        ``start_boot_id/start_sequence = NULL`` (mọi mẫu sau đều gán được).
        """
        with self._boundary_condition:
            if self._boundary_in_progress is not None:
                raise ValueError("đang chốt ranh giới session, thử lại sau")
            existing = self._store.running_session(self._device_id)
            if existing is not None:
                raise ValueError(
                    f"Session {existing['session_id']} đang chạy, stop trước khi start"
                )
            if session_id is None:
                session_id = (
                    f"run_{datetime.now(UTC):%Y%m%d_%H%M%S}_{uuid4().hex[:4]}"
                )
            self._boundary_in_progress = f"start:{session_id}"
        started_at = utc_now_iso()

        try:
            ack: dict | None = None
            if self._control is not None:
                ack = self._control.session_boundary(
                    self._device_id, "session_start", session_id, self._ack_timeout_s
                )
            if ack is not None:
                start_boot_id: str | None = ack["boot_id"]
                start_sequence: int | None = int(ack["sequence"]) + 1
                boundary_source = "device_ack"
            else:
                latest = self._store.latest_raw(self._device_id)
                if latest is not None:
                    start_boot_id = latest["boot_id"]
                    start_sequence = int(latest["sequence"]) + 1
                else:
                    start_boot_id = None
                    start_sequence = None
                boundary_source = "db_fallback"

            config_json = (
                json.dumps(self._safety_configs_snapshot, sort_keys=True)
                if self._safety_configs_snapshot is not None
                else None
            )
            self._store.insert_session(
                session_id,
                self._device_id,
                scenario,
                obj,
                zone_note,
                started_at,
                config_json,
                start_boot_id,
                start_sequence,
                boundary_source,
            )
            return self.get_session(session_id)
        finally:
            with self._boundary_condition:
                if self._boundary_in_progress == f"start:{session_id}":
                    self._boundary_in_progress = None
                    self._boundary_condition.notify_all()

    def stop_session(self, session_id: str | None = None) -> SessionInfo:
        """Đóng session (mặc định session đang chạy); chốt ranh giới stop.

        Ack thiết bị → ``stop_sequence = ack.sequence``. Không ack → fallback:
        max sequence đã gán vào session (0 nếu chưa có mẫu nào).

        Guard double-stop: session đã ``closed`` → ``SessionAlreadyClosed``
        TRƯỚC khi gửi ``session_stop`` xuống device — ``stop_sequence`` /
        ``ended_at`` không bị ghi đè lần hai.
        """
        with self._boundary_condition:
            if self._boundary_in_progress is not None:
                raise ValueError("đang chốt ranh giới session, thử lại sau")
            if session_id is None:
                row = self._store.running_session(self._device_id)
                if row is None:
                    raise ValueError(
                        f"Không có session đang chạy cho device {self._device_id}"
                    )
                session_id = row["session_id"]
            else:
                row = self._store.get_session(session_id)
                if row is None:
                    raise ValueError(f"Không tìm thấy session {session_id}")

            if row["status"] == "closed":
                raise SessionAlreadyClosed(f"Session {row['session_id']} đã đóng")
            self._boundary_in_progress = f"stop:{session_id}"

        try:
            device_id = row["device_id"]
            ack: dict | None = None
            if self._control is not None:
                ack = self._control.session_boundary(
                    device_id, "session_stop", session_id, self._ack_timeout_s
                )
            if ack is not None:
                stop_sequence: int = int(ack["sequence"])
            else:
                stop_sequence = self._store.session_max_raw_sequence(session_id)

            ended_at = utc_now_iso()
            self._store.close_session(session_id, ended_at, stop_sequence)
            return self.get_session(session_id)
        finally:
            with self._boundary_condition:
                if self._boundary_in_progress == f"stop:{session_id}":
                    self._boundary_in_progress = None
                    self._boundary_condition.notify_all()

    # ------------------------------------------------------------ đọc dữ liệu

    def list_sessions(self) -> list[SessionInfo]:
        """Mọi session kèm thống kê, sắp theo started_at."""
        infos = []
        for row in self._store.list_sessions():
            stats = self._store.session_stats(row["session_id"], PROCESSOR_VERSION)
            infos.append(SessionInfo.from_row(row, stats))
        return infos

    def get_session(self, session_id: str) -> SessionInfo:
        """Thông tin một session kèm thống kê; ValueError nếu không tồn tại."""
        row = self._store.get_session(session_id)
        if row is None:
            raise ValueError(f"Không tìm thấy session {session_id}")
        return SessionInfo.from_row(
            row, self._store.session_stats(session_id, PROCESSOR_VERSION)
        )

    # ------------------------------------------------------------ gán mẫu

    def assign(self, device_id: str, boot_id: str, sequence: int) -> str | None:
        """Gán một mẫu vào session theo khoảng ranh giới (idempotent).

        Gọi bởi ``IngestionService`` mỗi lần compute một mẫu. Không gán được
        (ngoài khoảng, boot khác) → trả None; nếu session đang running có
        ``start_boot_id`` khác boot của mẫu → đánh dấu ``boot_change``.

        M3c m-3: session running có ``start_boot_id=NULL`` (bắt đầu trên DB
        rỗng) nhận mẫu đầu tiên → pin ``start_boot_id`` = boot của mẫu đó
        (cập nhật có điều kiện, chỉ lần đầu thắng); từ mẫu thứ hai của boot
        khác → không gán + ``set_boot_change`` như logic hiện tại.
        """
        # Không để telemetry đang xử lý chạy qua khoảng giữa ACK và INSERT
        # session. MqttListener xử lý telemetry trong worker riêng nên việc
        # chờ này không chặn thread paho nhận ACK.
        with self._boundary_condition:
            while self._boundary_in_progress is not None:
                self._boundary_condition.wait()

        session_id = self._store.session_for_sample(device_id, boot_id, sequence)
        if session_id is not None:
            # INSERT OR IGNORE — catch_up/replay gọi lại không nhân bản.
            self._store.assign_session_sample(session_id, device_id, boot_id, sequence)
            # Pin boot ở mẫu ĐẦU TIÊN gán được: session còn NULL (mới start
            # trên DB rỗng) → ghi boot của mẫu này một lần duy nhất. Duplicate
            # replay sau khi pin không đổi gì (WHERE start_boot_id IS NULL).
            row = self._store.get_session(session_id)
            if (
                row is not None
                and row["status"] == "running"
                and row["start_boot_id"] is None
            ):
                self._store.pin_session_boot(session_id, boot_id)
            return session_id
        running = self._store.running_session(device_id)
        if (
            running is not None
            and running["start_boot_id"] is not None
            and boot_id != running["start_boot_id"]
        ):
            self._store.set_boot_change(running["session_id"])
        return None
