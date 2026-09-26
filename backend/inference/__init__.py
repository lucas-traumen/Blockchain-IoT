"""Runtime inference (M6a): buffer cửa sổ + dự báo RF cho backend.

Re-export API công khai — xem ``backend.inference.predictor`` cho chi tiết
spec nguồn (PLAN.md mục 4) và contract đầu ra (khối ``prediction`` của
``schemas/assessment_v1.schema.json``).
"""

from backend.inference.predictor import SafetyPredictor

__all__ = ["SafetyPredictor"]
