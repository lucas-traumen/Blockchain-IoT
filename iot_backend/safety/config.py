"""Nạp và tra cứu phiên bản cấu hình ngưỡng safety theo thời điểm."""

from dataclasses import dataclass
import math
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from iot_backend.safety.sp import SafetyParams

__all__ = ["SafetyConfigVersion", "load_safety_configs", "get_config_for_time"]

# Khóa params trong YAML → trường SafetyParams.
_REQUIRED_PARAM_KEYS = ("vh", "vr", "tr_s", "ts_s", "c_m", "zd_m", "zr_m")
# Các tham số vận tốc/thời gian phải > 0; các tham số dự phòng/khoảng cách chỉ cần >= 0.
_POSITIVE_PARAM_KEYS = ("vh", "vr", "tr_s", "ts_s")
_NONNEGATIVE_PARAM_KEYS = ("c_m", "zd_m", "zr_m")


@dataclass(frozen=True)
class SafetyConfigVersion:
    """Một phiên bản cấu hình ngưỡng, hiệu lực từ ``valid_from`` (ISO-8601 UTC)."""

    config_version: str
    valid_from: str
    params: SafetyParams
    zone_min_m: float
    zone_max_m: float
    mode: str = "ssm"


def _parse_iso_utc(value: Any, context: str) -> datetime:
    """Parse thời điểm ISO-8601 thành datetime UTC.

    Chuỗi thiếu múi giờ (ví dụ ``"2026-01-01"``) được hiểu là UTC.
    Sai định dạng → ``ValueError`` với thông điệp rõ.
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        # datetime.date (thường gặp khi YAML parse ngày không giờ, không ngoặc kép).
        dt = datetime(value.year, value.month, value.day)
    elif isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip())
        except ValueError as exc:
            raise ValueError(
                f"{context}: thời điểm {value!r} không đúng định dạng ISO-8601"
            ) from exc
    else:
        raise ValueError(f"{context}: thời điểm {value!r} phải là chuỗi ISO-8601")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_number(value: Any, label: str, minimum: float | None = None,
                  strictly_positive: bool = False) -> float:
    """Kiểm tra và ép một giá trị số; sai → ``ValueError``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} phải là số, nhận {value!r}")
    if not math.isfinite(value):
        raise ValueError(f"{label} phải hữu hạn, nhận {value!r}")
    if strictly_positive and value <= 0:
        raise ValueError(f"{label} phải > 0, nhận {value!r}")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} phải >= {minimum}, nhận {value!r}")
    return float(value)


def _parse_version(raw: Any, index: int, source: Path) -> SafetyConfigVersion:
    """Parse và validate một phần tử của ``versions``; sai → ``ValueError``."""
    label = f"Phiên bản thứ {index} trong {source}"
    if not isinstance(raw, dict):
        raise ValueError(
            f"{label}: mỗi phần tử của 'versions' phải là mapping, nhận {type(raw).__name__}"
        )

    config_version = raw.get("config_version")
    if not isinstance(config_version, str) or not config_version.strip():
        raise ValueError(f"{label}: thiếu 'config_version' (chuỗi không rỗng)")
    label = f"Phiên bản {config_version!r} trong {source}"

    if "valid_from" not in raw:
        raise ValueError(f"{label}: thiếu 'valid_from'")
    valid_from_raw = raw["valid_from"]
    if isinstance(valid_from_raw, str):
        valid_from = valid_from_raw.strip()
    elif isinstance(valid_from_raw, (datetime, date)):
        valid_from = valid_from_raw.isoformat()
    else:
        raise ValueError(
            f"{label}: 'valid_from' phải là chuỗi ISO-8601, nhận {valid_from_raw!r}"
        )
    # Chỉ kiểm tra định dạng; chuỗi gốc được giữ nguyên trong kết quả.
    _parse_iso_utc(valid_from, f"{label}: 'valid_from'")

    mode = raw.get("mode", "ssm")
    if mode not in {"ssm", "fixed_boundary"}:
        raise ValueError(f"{label}: mode phải là ssm hoặc fixed_boundary")
    if mode == "fixed_boundary":
        # Một cạnh vùng mô phỏng có ngưỡng hình học cố định. Các thành phần
        # động học bằng 0; không giả lập vận tốc/phanh để tạo ngưỡng mong muốn.
        threshold_m = _parse_number(raw.get("threshold_m"),
                                    f"{label}: threshold_m", strictly_positive=True)
        values = {key: 0.0 for key in _REQUIRED_PARAM_KEYS}
        values["c_m"] = threshold_m
    else:
        raw_params = raw.get("params")
        if not isinstance(raw_params, dict):
            raise ValueError(f"{label}: thiếu mapping 'params'")
        values: dict[str, float] = {}
        for key in _REQUIRED_PARAM_KEYS:
            if key not in raw_params:
                raise ValueError(f"{label}: params thiếu khóa '{key}'")
            values[key] = _parse_number(
                raw_params[key],
                f"{label}: params['{key}']",
                minimum=0.0 if key in _NONNEGATIVE_PARAM_KEYS else None,
                strictly_positive=key in _POSITIVE_PARAM_KEYS,
            )

    zone = raw.get("measurement_zone_m")
    if not isinstance(zone, dict) or "min" not in zone or "max" not in zone:
        raise ValueError(f"{label}: thiếu 'measurement_zone_m' với khóa 'min'/'max'")
    zone_min = _parse_number(zone["min"], f"{label}: measurement_zone_m['min']", minimum=0.0)
    zone_max = _parse_number(zone["max"], f"{label}: measurement_zone_m['max']")
    if zone_max <= zone_min:
        raise ValueError(
            f"{label}: measurement_zone_m yêu cầu max > min, nhận min={zone_min!r}, max={zone_max!r}"
        )

    params = SafetyParams(
        vh_m_s=values["vh"],
        vr_m_s=values["vr"],
        tr_s=values["tr_s"],
        ts_s=values["ts_s"],
        c_m=values["c_m"],
        zd_m=values["zd_m"],
        zr_m=values["zr_m"],
    )
    return SafetyConfigVersion(
        config_version=config_version,
        valid_from=valid_from,
        params=params,
        zone_min_m=zone_min,
        zone_max_m=zone_max,
        mode=mode,
    )


def load_safety_configs(path: str | Path) -> list[SafetyConfigVersion]:
    """Đọc file YAML cấu hình safety (khóa gốc ``versions``).

    Validate đủ trường, số dương (``> 0`` với tốc độ/thời gian, ``>= 0`` với
    c_m/zd_m/zr_m), zone hợp lệ và không trùng ``config_version``. Trả về danh
    sách sắp xếp theo ``valid_from`` tăng dần (so sánh datetime, không so chuỗi).
    Mọi lỗi đọc file/YAML/dữ liệu → ``ValueError`` với thông điệp rõ.
    """
    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"Không đọc được file cấu hình safety {file_path}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"File cấu hình safety {file_path} không phải YAML hợp lệ: {exc}") from exc

    if not isinstance(data, dict) or "versions" not in data:
        raise ValueError(f"File cấu hình safety {file_path} thiếu khóa gốc 'versions'")
    raw_versions = data["versions"]
    if not isinstance(raw_versions, list) or not raw_versions:
        raise ValueError(f"'versions' trong {file_path} phải là danh sách phiên bản không rỗng")

    configs: list[SafetyConfigVersion] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_versions):
        config = _parse_version(raw, index, file_path)
        if config.config_version in seen:
            raise ValueError(
                f"File cấu hình safety {file_path}: trùng config_version "
                f"{config.config_version!r} trong 'versions'"
            )
        seen.add(config.config_version)
        configs.append(config)

    configs.sort(key=lambda c: _parse_iso_utc(c.valid_from, f"config {c.config_version!r}"))
    return configs


def get_config_for_time(
    configs: list[SafetyConfigVersion], iso_ts: str
) -> SafetyConfigVersion:
    """Chọn phiên bản có ``valid_from <= iso_ts`` lớn nhất (mới nhất trong quá khứ).

    So sánh bằng datetime đã parse (không so chuỗi). ``iso_ts`` sai định dạng
    hoặc không có phiên bản nào hiệu lực → ``ValueError``.
    """
    moment = _parse_iso_utc(iso_ts, "get_config_for_time")
    best: SafetyConfigVersion | None = None
    best_from: datetime | None = None
    for config in configs:
        valid_from = _parse_iso_utc(config.valid_from, f"config {config.config_version!r}")
        if valid_from <= moment and (best_from is None or valid_from >= best_from):
            # '>= best_from': nếu hai phiên bản trùng valid_from, bản sau trong
            # danh sách (đã sort ổn định) thắng — chọn phiên bản khai báo sau.
            best, best_from = config, valid_from
    if best is None:
        raise ValueError(
            f"Không có phiên bản cấu hình safety nào có valid_from <= {iso_ts!r} "
            f"(tổng số phiên bản: {len(configs)})"
        )
    return best
