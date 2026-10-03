"""Cấu hình backend đọc từ biến môi trường (giá trị mặc định khớp .env.example)."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

__all__ = ["Settings", "load_settings"]


@dataclass(frozen=True)
class Settings:
    """Cấu hình runtime của backend (đường dẫn tính từ repo root)."""

    mqtt_host: str
    mqtt_port: int
    db_path: str
    device_id: str
    safety_config_path: str
    model_config_path: str
    model_artifact_dir: str
    # M3: bật/tắt MQTT (test chạy không broker) và timeout chờ ack thiết bị.
    mqtt_enabled: bool = True
    ack_timeout_s: float = 2.0
    model_type: str = "rf"


def _env_bool(name: str, default: bool) -> bool:
    """Đọc biến môi trường boolean: '0'/'false'/'no'/'off' → False, còn lại True."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def load_settings(env_file: str | None = None) -> Settings:
    """Đọc biến môi trường thành ``Settings``.

    Chỉ nạp ``env_file`` qua python-dotenv khi caller truyền đường dẫn
    (mặc định KHÔNG tự load ``.env`` — entrypoint quyết định). Giá trị mặc
    định khớp ``.env.example``: localhost/1883/data/hrc.db/esp32_01/
    configs/safety.yaml/configs/model.yaml/ai_model/artifacts.
    M3: ``HRC_MQTT_ENABLED`` (mặc định True) và ``HRC_ACK_TIMEOUT_S``
    (mặc định 2.0 giây).
    """
    if env_file is not None:
        load_dotenv(env_file)
    return Settings(
        mqtt_host=os.environ.get("MQTT_HOST", "localhost"),
        mqtt_port=int(os.environ.get("MQTT_PORT", "1883")),
        db_path=os.environ.get("HRC_DB_PATH", "data/hrc.db"),
        device_id=os.environ.get("DEVICE_ID", "esp32_01"),
        safety_config_path=os.environ.get("SAFETY_CONFIG", "configs/safety.yaml"),
        model_config_path=os.environ.get("MODEL_CONFIG", "configs/model.yaml"),
        model_artifact_dir=os.environ.get("MODEL_ARTIFACT_DIR", "ai_model/artifacts"),
        model_type=os.environ.get("MODEL_TYPE", "rf"),
        mqtt_enabled=_env_bool("HRC_MQTT_ENABLED", True),
        ack_timeout_s=float(os.environ.get("HRC_ACK_TIMEOUT_S", "2.0")),
    )
