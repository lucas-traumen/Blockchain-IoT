"""MQTT listener nhận telemetry và đưa vào ``IngestionService``.

paho-mqtt 2.x, CallbackAPIVersion.VERSION2, QoS 1, tự reconnect. Chạy độc lập:
``.venv/bin/python -m backend.ingestion.mqtt_listener``.
M3: subscribe thêm topic ack, hỗ trợ đăng ký handler theo topic chính xác hoặc
pattern wildcard (phân đoạn ``+``) và ``publish_json`` cho lệnh control.
"""

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

from backend.ingestion.service import IngestionService
from backend.safety import load_safety_configs
from backend.settings import Settings, load_settings
from backend.ingestion.store import Store

__all__ = [
    "ACK_SUBSCRIBE_TOPIC",
    "TELEMETRY_SUBSCRIBE_TOPIC",
    "MqttListener",
    "main",
]

logger = logging.getLogger(__name__)

TELEMETRY_SUBSCRIBE_TOPIC = "hrc/v1/devices/+/telemetry"
ACK_SUBSCRIBE_TOPIC = "hrc/v1/devices/+/ack"

TELEMETRY_TOPIC_RE = re.compile(r"^hrc/v1/devices/([^/]+)/telemetry$")


def _wildcard_pattern_to_re(pattern: str) -> re.Pattern[str]:
    """Chuyển pattern MQTT (phân đoạn '+' khớp một cấp không chứa '/') thành regex."""
    parts = (re.escape(part) if part != "+" else r"[^/]+" for part in pattern.split("/"))
    return re.compile("^" + "/".join(parts) + "$")


class MqttListener:
    """Listener MQTT: subscribe telemetry + ack, dispatch message tới service/handler."""

    def __init__(self, service: IngestionService, host: str, port: int):
        """Lưu service và thông tin broker; chưa kết nối cho tới ``start()``."""
        self._service = service
        self._host = host
        self._port = port
        self._client: mqtt.Client | None = None
        # Handler topic chính xác (dict) và theo pattern wildcard (danh sách).
        self._topic_handlers: dict[str, Callable[[str, str], None]] = {}
        self._wildcard_handlers: list[tuple[re.Pattern[str], Callable[[str, str], None]]] = []
        # Không xử lý telemetry trực tiếp trên paho network thread: START/STOP
        # có thể chờ ACK trong lúc một mẫu đang bay tới. Worker tuần tự giữ
        # thứ tự telemetry nhưng để callback ACK tiếp tục chạy.
        self._telemetry_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="hrc-telemetry"
        )
        self._telemetry_executor_stopped = False

    def add_topic_handler(self, topic: str, handler: Callable[[str, str], None]) -> None:
        """Đăng ký handler cho MỘT topic chính xác (không wildcard).

        handler(topic, payload_str) được gọi khi message đến đúng topic đó
        (không khớp telemetry — telemetry luôn đi vào service).
        """
        self._topic_handlers[topic] = handler

    def add_wildcard_handler(self, pattern: str, handler: Callable[[str, str], None]) -> None:
        """Đăng ký handler theo pattern wildcard; '+' khớp đúng một phân đoạn."""
        self._wildcard_handlers.append((_wildcard_pattern_to_re(pattern), handler))

    def start(self) -> None:
        """Kết nối broker và chạy network loop trong thread nền."""
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"hrc-backend-{uuid4().hex[:8]}",
        )
        client.reconnect_delay_set(1, 60)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.connect(self._host, self._port, keepalive=60)
        client.loop_start()
        self._client = client
        logger.info("MQTT listener đang kết nối tới %s:%d", self._host, self._port)

    def stop(self) -> None:
        """Dừng network loop và ngắt kết nối (an toàn khi chưa start)."""
        if self._client is not None:
            self._client.loop_stop()
            self._client.disconnect()
            self._client = None
        # Chờ telemetry đã nhận xử lý xong để không mất hàng đợi khi app
        # đóng. ACK vẫn được callback trực tiếp trên paho thread.
        if not self._telemetry_executor_stopped:
            self._telemetry_executor.shutdown(wait=True)
            self._telemetry_executor_stopped = True
        logger.info("MQTT listener đã dừng.")

    def publish(self, topic: str, payload: str, qos: int = 1) -> None:
        """Publish một message (dành cho M3 gửi control); log nếu thất bại."""
        client = self._client
        if client is None:
            logger.warning("publish bị bỏ qua (chưa start): %s", topic)
            return
        try:
            info = client.publish(topic, payload, qos=qos)
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                logger.warning(
                    "publish %s thất bại: %s", topic, mqtt.error_string(info.rc)
                )
        except Exception:  # noqa: BLE001 — publish lỗi không được làm sập listener
            logger.exception("publish %s raise ngoại lệ", topic)

    def publish_json(self, topic: str, obj: Any, qos: int = 1) -> None:
        """Publish một object Python dạng JSON (helper cho control command)."""
        self.publish(topic, json.dumps(obj), qos=qos)

    # -------------------------------------------------------------- callbacks

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        """Subscribe wildcard telemetry + ack khi kết nối thành công."""
        logger.info("MQTT connected (reason_code=%s)", reason_code)
        client.subscribe(TELEMETRY_SUBSCRIBE_TOPIC, qos=1)
        client.subscribe(ACK_SUBSCRIBE_TOPIC, qos=1)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties) -> None:
        """Log khi mất kết nối — paho tự reconnect (1..60s)."""
        logger.warning("MQTT disconnected (reason_code=%s)", reason_code)

    def _process_telemetry(self, sample_ref: dict[str, Any]) -> None:
        """Tính safety/AI ngoài paho network thread sau khi raw đã commit."""
        try:
            summary = self._service.process_sample(sample_ref)
            logger.debug("%s → %s", sample_ref, summary)
        except Exception:  # noqa: BLE001 — log đầy đủ, không làm sập worker
            logger.exception("Lỗi compute telemetry %s", sample_ref)

    def _on_message(self, client, userdata, message) -> None:
        """Dispatch message: telemetry → service, còn lại → handler đã đăng ký.

        Lỗi không được làm sập thread paho.
        """
        text = bytes(message.payload).decode("utf-8", errors="replace")
        try:
            if TELEMETRY_TOPIC_RE.match(message.topic):
                accepted = self._service.accept_message(message.topic, message.payload)
                if accepted.get("action") == "accepted":
                    self._telemetry_executor.submit(
                        self._process_telemetry, accepted["sample_ref"]
                    )
                else:
                    logger.debug("%s → %s", message.topic, accepted)
                return
            handler = self._topic_handlers.get(message.topic)
            if handler is None:
                for pattern, wildcard_handler in self._wildcard_handlers:
                    if pattern.match(message.topic):
                        handler = wildcard_handler
                        break
            if handler is not None:
                handler(message.topic, text)
            else:
                logger.debug("Bỏ qua message topic không đăng ký: %s", message.topic)
        except Exception:  # noqa: BLE001 — log đầy đủ, không raise ra thread paho
            logger.exception("Lỗi xử lý message từ topic %s", message.topic)


def main() -> None:
    """Chạy ingestion listener: load .env (nếu có), mở Store + service + MQTT."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    env_file = Path(".env")
    if env_file.exists():
        load_dotenv(env_file)
    settings: Settings = load_settings()
    store = Store(settings.db_path)
    configs = load_safety_configs(settings.safety_config_path)
    from backend.inference.predictor import build_predictor
    from backend.sessions.manager import SessionManager
    service = IngestionService(
        store, configs, session_assigner=SessionManager(store, settings.device_id, control=None),
        predictor=build_predictor(settings.model_artifact_dir, settings.model_config_path, settings.model_type),
    )
    # M3c m-5: restart recovery — tính nốt raw pending của lần chạy trước
    # (mẫu đã lưu raw nhưng chưa kịp compute) trước khi vào vòng lặp chờ.
    recovered = service.catch_up()
    if recovered:
        logger.info("catch_up: đã tính %d raw pending khi khởi động", recovered)
    listener = MqttListener(service, settings.mqtt_host, settings.mqtt_port)
    listener.start()
    logger.info(
        "HRC ingestion đang chạy: mqtt://%s:%d, db=%s",
        settings.mqtt_host,
        settings.mqtt_port,
        settings.db_path,
    )
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Nhận KeyboardInterrupt, đang dừng ingestion.")
    finally:
        listener.stop()
        store.close()


if __name__ == "__main__":
    main()
