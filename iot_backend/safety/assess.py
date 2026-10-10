"""Đánh giá an toàn điểm cho một mẫu đo khoảng cách (thuần hàm, đơn vị SI)."""

from dataclasses import dataclass

from iot_backend.safety.sp import SafetyParams

__all__ = ["SafetyAssessment", "assess_sample", "STATUS_CLEAR", "STATUS_VIOLATION", "STATUS_UNKNOWN"]

STATUS_CLEAR = "CLEAR"
STATUS_VIOLATION = "VIOLATION"
STATUS_UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class SafetyAssessment:
    """Kết quả đánh giá an toàn cho một mẫu — khớp khối ``safety`` của assessment_v1."""

    status: str  # 'CLEAR' | 'VIOLATION' | 'UNKNOWN'
    sensor_valid: bool
    assessment_valid: bool
    distance_m: float | None
    sp_m: float | None
    margin_m: float | None
    config_version: str | None
    config_time_source: str | None
    components: dict[str, float] | None
    config_fit_warnings: list[str]


def assess_sample(
    distance_mm: int | None,
    sensor_valid: bool,
    params: SafetyParams | None,
    zone_min_m: float | None = None,
    zone_max_m: float | None = None,
    config_version: str | None = None,
    config_time_source: str | None = None,
) -> SafetyAssessment:
    """Đánh giá một mẫu đo: ``margin_m = distance_m - Sp``.

    - ``margin_m > 0`` → ``CLEAR``; ``margin_m <= 0`` → ``VIOLATION`` (bên chính
      xác bằng Sp là VIOLATION); mẫu lỗi/thiếu dữ liệu → ``UNKNOWN``, không bao
      giờ "nâng" UNKNOWN thành CLEAR.
    - ``sensor_valid=False``, ``distance_mm=None`` hoặc ``params=None`` → UNKNOWN
      với ``sp_m/margin_m/components=None``. Khi cảm biến không tin cậy, khoảng
      cách không được ghi nhận; khi chỉ thiếu tham số cấu hình (cảm biến tốt),
      khoảng cách đo được vẫn được ghi nhận nhưng ``assessment_valid=False``.
    - Cảnh báo cấu hình (không chặn, không giảm Sp): ``Sp > zone_max`` → Sp ngoài
      vùng đo đã kiểm chứng; ``Sp <= zone_min`` → không còn vùng quan sát trước
      ngưỡng. Chỉ tính khi có tham số và có biên zone tương ứng.
    - Hàm thuần: không đọc file, không đọc đồng hồ. ``config_version`` và
      ``config_time_source`` được truyền thẳng vào kết quả.
    """
    if (not sensor_valid) or distance_mm is None or params is None:
        distance_m: float | None = None
        if sensor_valid and distance_mm is not None:
            # Cảm biến tin cậy nhưng thiếu tham số cấu hình: ghi nhận khoảng cách, không đánh giá.
            distance_m = distance_mm / 1000.0
        return SafetyAssessment(
            status=STATUS_UNKNOWN,
            sensor_valid=sensor_valid,
            assessment_valid=False,
            distance_m=distance_m,
            sp_m=None,
            margin_m=None,
            config_version=config_version,
            config_time_source=config_time_source,
            components=None,
            config_fit_warnings=[],
        )

    distance_m = distance_mm / 1000.0
    sp_m = params.sp_m
    margin_m = distance_m - sp_m
    status = STATUS_CLEAR if margin_m > 0 else STATUS_VIOLATION

    config_fit_warnings: list[str] = []
    if zone_max_m is not None and sp_m > zone_max_m:
        config_fit_warnings.append(
            f"Sp_m {sp_m:.3f} exceeds validated measurement zone max {zone_max_m:.2f}m"
        )
    if zone_min_m is not None and sp_m <= zone_min_m:
        config_fit_warnings.append(
            f"Sp_m {sp_m:.3f} leaves no observable region before threshold within zone min {zone_min_m:.2f}"
        )

    return SafetyAssessment(
        status=status,
        sensor_valid=True,
        assessment_valid=True,
        distance_m=distance_m,
        sp_m=sp_m,
        margin_m=margin_m,
        config_version=config_version,
        config_time_source=config_time_source,
        components=params.components(),
        config_fit_warnings=config_fit_warnings,
    )
