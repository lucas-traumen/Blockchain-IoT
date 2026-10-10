"""Cấu hình iot_backend đọc từ biến môi trường (giá trị mặc định khớp .env.example)."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

__all__ = ["Settings", "load_settings"]


@dataclass(frozen=True)
class Settings:
    """Cấu hình runtime của iot_backend (đường dẫn tính từ repo root)."""

    mqtt_host: str
    mqtt_port: int
    db_path: str
    device_id: str
    safety_config_path: str
    model_config_path: str
    model_artifact_dir: str
    # M3: bật/tắt MQTT (test chạy không broker) và timeout chờ ack thiết bị.
    mqtt_enabled: bool = True
    ack_timeout_s: float = 8.0
    model_type: str = "lstm"
    # Optional bridge to the trusted signer API.  No private key in IoT/backend.
    reward_api_url: str = ""
    reward_ingest_token: str = ""
    reward_beneficiary: str = ""
    reward_auto_start: bool = False
    reward_auto_settle: bool = False
    reward_timeout_s: float = 10.0
    # Optional safety-event bridge. Reward bridge and safety-event bridge are
    # deliberately independent outboxes and may be enabled separately.
    chain_api_url: str = ""
    chain_ingest_token: str = ""
    chain_auto_submit: bool = False
    chain_max_attempts: int = 8
    # V2 confirmation is runtime-configurable so legacy replay/test consumers
    # can keep safety-v1 semantics while the compose demo uses 2-of-3.
    violation_confirmation_enabled: bool = False
    # The source label is an operator choice.  It must not be inferred from
    # the model artifact name or from a legacy device id such as esp32_01.
    source_mode: str = "esp32"
    cors_origins: tuple[str, ...] = (
        "http://localhost:8080",
        "http://127.0.0.1:8080",
    )

    def __post_init__(self) -> None:
        if self.model_type != "lstm":
            raise ValueError("model_type phải là lstm")
        if self.source_mode not in {"simulator", "esp32"}:
            raise ValueError("source_mode phải là simulator hoặc esp32")


def _env_bool(name: str, default: bool) -> bool:
    """Đọc biến môi trường boolean: '0'/'false'/'no'/'off' → False, còn lại True."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_origins(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """Parse a comma-separated explicit CORS allowlist; never return '*'."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    values = tuple(item.strip().rstrip("/") for item in raw.split(",") if item.strip())
    if not values or "*" in values:
        raise ValueError(f"{name} must contain explicit origins, not '*'")
    return values


def load_settings(env_file: str | None = None) -> Settings:
    """Đọc biến môi trường thành ``Settings``.

    Chỉ nạp ``env_file`` qua python-dotenv khi caller truyền đường dẫn
    (mặc định KHÔNG tự load ``.env`` — entrypoint quyết định). Giá trị mặc
    định khớp ``.env.example``: localhost/1883/data/hrc.db/esp32_01/
    configs/safety.yaml/configs/model.yaml và artifact benchmark square40.
    ``HRC_SOURCE_MODE`` là nhãn nguồn do operator chọn.
    M3: ``HRC_MQTT_ENABLED`` (mặc định True) và ``HRC_ACK_TIMEOUT_S``
    (mặc định 8.0 giây, để chịu được ACK muộn trong lúc MQTT reconnect).
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
        model_artifact_dir=os.environ.get(
            "MODEL_ARTIFACT_DIR", "ai_model/artifacts/lstm-v1"
        ),
        model_type=os.environ.get("MODEL_TYPE", "lstm"),
        mqtt_enabled=_env_bool("HRC_MQTT_ENABLED", True),
        ack_timeout_s=float(os.environ.get("HRC_ACK_TIMEOUT_S", "8.0")),
        reward_api_url=os.environ.get("HRC_REWARD_API_URL", ""),
        reward_ingest_token=os.environ.get("HRC_REWARD_INGEST_TOKEN", ""),
        reward_beneficiary=os.environ.get("HRC_REWARD_BENEFICIARY", ""),
        reward_auto_start=_env_bool("HRC_REWARD_AUTO_START", False),
        reward_auto_settle=_env_bool("HRC_REWARD_AUTO_SETTLE", False),
        reward_timeout_s=float(os.environ.get("HRC_REWARD_TIMEOUT_S", "10.0")),
        chain_api_url=os.environ.get("HRC_CHAIN_API_URL", ""),
        chain_ingest_token=os.environ.get("HRC_CHAIN_INGEST_TOKEN", ""),
        chain_auto_submit=_env_bool("HRC_CHAIN_AUTO_SUBMIT", False),
        chain_max_attempts=int(os.environ.get("HRC_CHAIN_MAX_ATTEMPTS", "8")),
        violation_confirmation_enabled=_env_bool("HRC_VIOLATION_CONFIRMATION", False),
        source_mode=os.environ.get("HRC_SOURCE_MODE", "esp32").strip().lower(),
        cors_origins=_env_origins(
            "HRC_CORS_ORIGINS",
            ("http://localhost:8080", "http://127.0.0.1:8080"),
        ),
    )
