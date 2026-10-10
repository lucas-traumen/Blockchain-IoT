"""Baseline persistence + constant-velocity để đối chiếu cùng split với LSTM.

Đầu vào là dãy khoảng cách theo thứ tự cũ → mới, đơn vị mét. Dự báo giữ
giá trị thô để evaluator phản ánh đúng sai số, không clamp hoặc làm tròn.
"""

from __future__ import annotations

from typing import Sequence

__all__ = ["N_OUTPUTS", "predict_persistence", "predict_constant_velocity"]

# Số output của model v1 = số horizon trong configs/model.yaml
# (horizons_ms: [100, 200, 300, 400, 500]). Baseline trả đúng một dự báo
# cho mỗi horizon, cùng thứ tự horizons_ms.
N_OUTPUTS = 5


def predict_persistence(
    features: Sequence[float], n_horizons: int = N_OUTPUTS
) -> list[float]:
    """Persistence: dự báo MỌI horizon bằng d(t) — phần tử CUỐI của cửa sổ.

    Giả định "khoảng cách giữ nguyên": d̂(t+h) = d(t) với mọi h, nên giá trị
    lặp giống hệt nhau cho từng horizon. Mặc định trả ``N_OUTPUTS`` giá trị
    (5 horizon của model v1, khớp ``configs/model.yaml``); truyền
    ``n_horizons`` khác nếu cấu hình đổi số horizon.

    ``features`` rỗng hoặc ``n_horizons < 1`` → ValueError.
    """
    feats = list(features)
    if not feats:
        raise ValueError("features rỗng — không có d(t) để lặp")
    if n_horizons < 1:
        raise ValueError(f"n_horizons phải >= 1, nhận {n_horizons}")
    return [float(feats[-1])] * n_horizons


def predict_constant_velocity(
    features: Sequence[float],
    delta_s: float,
    horizons_ms: Sequence[int],
) -> list[float]:
    """Constant-velocity: ngoại suy tuyến tính d̂(t+h) = d(t) + v·h (mét).

    Cách ước lượng v — hai phương án thiết kế, ĐÃ CHỌN phương án 2:

    1. Hiệu hai mẫu cuối chia Δ: ``v = (d(t) − d(t−Δ))/Δ``. Chỉ dùng 1 cặp
       mẫu nên phương sai lớn — một mẫu nhiễu đo làm v sai lệch nặng.
    2. **Trung bình hiệu các cặp liền kề (ĐƯỢC CHỌN):**
       ``v = (1/((N−1)·Δ)) · Σ_{i=1..N−1} (d_i − d_{i−1})``. Tổng telescoping
       rút gọn thành hiệu hai ĐẦU cửa sổ: ``v = (d(t) − d0)/((N−1)·Δ)`` —
       dùng toàn bộ cửa sổ nên bền với nhiễu hơn phương án 1, chi phí O(1).

    Ngoại suy ``d̂(t+h) = d(t) + v·h`` với ``h = horizon_ms/1000`` (giây),
    trả đúng một dự báo mỗi horizon theo thứ tự ``horizons_ms``. KHÔNG
    clamp: dự báo có thể âm hoặc vượt miền đo — evaluate phải thấy lỗi thật.

    Cửa sổ < 2 mẫu (không suy ra được v) hoặc ``delta_s <= 0`` → ValueError.
    """
    feats = [float(value) for value in features]
    if len(feats) < 2:
        raise ValueError(
            f"cần >= 2 mẫu trong cửa sổ để ước lượng vận tốc, nhận {len(feats)}"
        )
    if delta_s <= 0:
        raise ValueError(f"delta_s phải > 0, nhận {delta_s}")
    velocity = (feats[-1] - feats[0]) / ((len(feats) - 1) * float(delta_s))
    return [feats[-1] + velocity * (horizon_ms / 1000.0) for horizon_ms in horizons_ms]
