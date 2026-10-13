"""Runtime inference (M6a): buffer cửa sổ + dự báo LSTM cho iot_backend.

Re-export API công khai — xem ``iot_backend.inference.predictor`` cho chi tiết
spec nguồn (PLAN.md mục 4) và contract đầu ra (khối ``prediction`` của
``schemas/assessment_v1.schema.json``).
"""

from iot_backend.inference.predictor import SafetyPredictor

__all__ = ["SafetyPredictor"]
