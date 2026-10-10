"""Validate topic và payload telemetry theo JSON Schema Draft 2020-12."""

import json
import re
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from iot_backend.util import parse_iso_utc

__all__ = ["TELEMETRY_TOPIC_RE", "parse_topic", "validate_telemetry"]

# Path schemas/ tính từ repo root: iot_backend/ingestion/validate.py → parents[2].
SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"
_TELEMETRY_SCHEMA_PATH = SCHEMAS_DIR / "telemetry_v1.schema.json"

TELEMETRY_TOPIC_RE = re.compile(r"^hrc/v1/devices/([^/]+)/telemetry$")

# jsonschema chỉ kiểm tra format khi có checker; checker "date-time" sẵn có
# yêu cầu rfc3339-validator (không có trong venv, không thêm dependency mới)
# nên đăng ký kiểm tra thật bằng parse_iso_utc.
FORMAT_CHECKER = FormatChecker()


@FORMAT_CHECKER.checks("date-time", raises=ValueError)
def _check_date_time(value: Any) -> bool:
    """Chuỗi phải parse được ISO-8601 (tz-naive coi là UTC); sai → ValueError.

    Giá trị không phải chuỗi được bỏ qua — keyword ``type`` của schema lo phần đó.
    """
    if not isinstance(value, str):
        return True
    parse_iso_utc(value)
    return True


with _TELEMETRY_SCHEMA_PATH.open(encoding="utf-8") as _schema_file:
    _TELEMETRY_SCHEMA: dict[str, Any] = json.load(_schema_file)
Draft202012Validator.check_schema(_TELEMETRY_SCHEMA)
_validator = Draft202012Validator(_TELEMETRY_SCHEMA, format_checker=FORMAT_CHECKER)


def parse_topic(topic: str) -> str | None:
    """Trả về device_id từ topic ``hrc/v1/devices/{device_id}/telemetry``, else None."""
    match = TELEMETRY_TOPIC_RE.fullmatch(topic)
    return match.group(1) if match else None


def validate_telemetry(payload: dict) -> tuple[bool, str | None]:
    """Validate payload telemetry_v1; trả (True, None) hoặc (False, reason ngắn).

    reason gồm JSON path và thông điệp của lỗi đầu tiên (sắp xếp ổn định theo
    path + message để kết quả xác định). Payload JSON hợp lệ nhưng không phải
    object cũng bị từ chối ở đây ("is not of type 'object'").
    """
    errors = sorted(
        _validator.iter_errors(payload),
        key=lambda err: (str(err.json_path), err.message),
    )
    if not errors:
        return True, None
    first = errors[0]
    return False, f"{first.json_path}: {first.message}"
