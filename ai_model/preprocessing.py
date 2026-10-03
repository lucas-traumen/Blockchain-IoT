"""Preprocessing LSTM (M10): scaler train-only + hash/manifest helpers.

Module này là nguồn duy nhất cho các hàm preprocessing thuần của pipeline
LSTM (pp-lstm-v1). Ràng buộc (PLAN mục 4 + PROJECT_MEMORY):

- KHÔNG import sklearn, TensorFlow, pandas hay pickle — artifact scaler là
  JSON số thuần (không pickle), module dùng được ở cả train (M11) và runtime
  (M13) mà không kéo dependency nặng. Chỉ dùng stdlib + numpy.
- Scaler fit CHỈ trên X/y của split train (``fitted_on: "train_only"``);
  validation/test không được chạm khi fit — chống leakage.
- Đơn vị mét (SI). Scaler transform feature ``[distance_m, delta_distance_m]``
  theo từng kênh của tensor ``(N, window_size, 2)`` và target ``y (N, 5)``
  (khoảng cách tương lai, mét). ``inverse_y`` đưa dự báo về mét trước khi so
  với ngưỡng Sp — không round trước threshold.

Policy std = 0: fit raise ``ValueError`` (dữ liệu train hằng số ở một kênh
gần như chắc chắn là bug dữ liệu; suy diễn std thay thế dễ che lỗi). Số liệu
không hữu hạn (NaN/±inf) hoặc tập rỗng cũng bị từ chối rõ ràng — không ép
giá trị, không fallback âm thầm (cùng triết lý với ai_model/features.py M9).

Hash: SHA-256 thuần hashlib. JSON canonical dùng ``sort_keys=True`` +
separator gọn nên hash tái lập được; timestamp chỉ vào manifest khi caller
truyền ``created_at`` tường minh — mặc định manifest không có created_at để
hash deterministic.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np

__all__ = [
    "PP_LSTM_VERSION",
    "SCALER_FEATURE_NAMES",
    "SCALER_TARGET_NAME",
    "FITTED_ON",
    "LSTMScaler",
    "sha256_bytes",
    "sha256_file",
    "canonical_json",
    "sha256_json",
    "dataset_sha256",
    "split_manifest_sha256",
    "scaler_json_sha256",
    "write_split_manifest",
    "write_scaler_json",
]

# Khớp configs/model.yaml: lstm.preprocessing_version (pp-lstm-v1).
PP_LSTM_VERSION = "pp-lstm-v1"
# Tên feature mỗi timestep của tensor X — khớp ai_model.features.
# LSTM_FEATURE_NAMES.
SCALER_FEATURE_NAMES = ["distance_m", "delta_distance_m"]
# Target là khoảng cách tương lai (mét) theo horizons_ms.
SCALER_TARGET_NAME = "target_distance_m"
FITTED_ON = "train_only"


# ------------------------------------------------------------------- hash


def sha256_bytes(data: bytes) -> str:
    """SHA-256 hex (chữ thường) của bytes — dùng cho model file/dataset."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    """SHA-256 hex của file (đọc theo khối, không nạp cả file vào RAM)."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(obj) -> str:
    """Chuỗi JSON canonical: sort_keys, separator gọn, ensure_ascii=False,
    allow_nan=False.

    Cùng dict → cùng chuỗi → cùng hash, bất kể thứ tự chèn key. Float được
    ``json.dumps`` serialize theo repr shortest — deterministic trên cùng
    Python. ``allow_nan=False``: NaN/inf lọt vào manifest → ValueError ngay
    (fail fast), không sinh JSON không hợp lệ để hash giá trị rác.
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def sha256_json(obj) -> str:
    """SHA-256 của ``canonical_json(obj)`` (UTF-8) — hash manifest tái lập."""
    return sha256_bytes(canonical_json(obj).encode("utf-8"))


def dataset_sha256(path: str | Path) -> str:
    """SHA-256 của file dataset (ví dụ JSONL dùng train) — giá trị cho
    ``model_manifest_v1.dataset_sha256``. Hash file thật, không bịa."""
    return sha256_file(path)


def split_manifest_sha256(manifest: dict) -> str:
    """SHA-256 canonical JSON của split manifest — giá trị cho
    ``model_manifest_v1.split_manifest_sha256``. Manifest muốn hash tái lập
    thì không chứa created_at sinh tự động (xem canonical_json)."""
    return sha256_json(manifest)


def scaler_json_sha256(scaler_dict: dict) -> str:
    """SHA-256 canonical JSON của scaler (dạng dict từ ``LSTMScaler.to_dict``)
    — dành cho model manifest nếu M11 quyết định ghi hash scaler."""
    return sha256_json(scaler_dict)


def write_split_manifest(manifest: dict, path: str | Path) -> str:
    """Ghi split manifest ra file dạng canonical JSON (không thêm ký tự khác)
    và trả về SHA-256 của nội dung đã ghi — hash file khớp
    ``split_manifest_sha256(manifest)``. Ghi đè nếu file tồn tại."""
    payload = canonical_json(manifest).encode("utf-8")
    Path(path).write_bytes(payload)
    return sha256_bytes(payload)


def write_scaler_json(scaler_dict: dict, path: str | Path) -> str:
    """Ghi scaler dict ra file canonical JSON; trả SHA-256 nội dung đã ghi."""
    payload = canonical_json(scaler_dict).encode("utf-8")
    Path(path).write_bytes(payload)
    return sha256_bytes(payload)


# ------------------------------------------------------------------ scaler


class LSTMScaler:
    """Scaler kênh-đơn giản (z-score) cho X ``(N, W, 2)`` và y ``(N, H)``.

    - ``X``: cột 0 = distance_m, cột 1 = delta_distance_m (đơn vị mét) —
      khớp ``SCALER_FEATURE_NAMES`` và tensor của ``windows_to_lstm_array``.
    - ``y``: 5 khoảng cách tương lai (mét) — cùng bộ tham số
      ``target_distance_m`` cho mọi horizon (một mean/std duy nhất; metric
      báo theo từng horizon ở M12 không cần scaler riêng).

    Fit chỉ gọi với dữ liệu train. ``transform_*`` không mutate input,
    deterministic, và từ chối giá trị không hữu hạn. ``inverse_y`` dùng cho
    dự báo về mét trước metric/runtime threshold.
    """

    def __init__(self, means: dict[str, float], stds: dict[str, float], n_samples_fit: int):
        # Thứ tự key cố định (features rồi target) để dict JSON deterministic.
        expected = [*SCALER_FEATURE_NAMES, SCALER_TARGET_NAME]
        missing = {k for k in expected if k not in means or k not in stds}
        if missing:
            raise ValueError(f"scaler thiếu tham số: {sorted(missing)}")
        extra = (set(means) | set(stds)) - set(expected)
        if extra:
            raise ValueError(f"scaler có tham số lạ: {sorted(extra)}")
        self.means = {k: float(means[k]) for k in expected}
        self.stds = {k: float(stds[k]) for k in expected}
        for k in expected:
            if not (math.isfinite(self.means[k]) and math.isfinite(self.stds[k])):
                raise ValueError(f"tham số scaler không hữu hạn tại {k}")
            if self.stds[k] <= 0:
                raise ValueError(f"std của {k} phải > 0, nhận {self.stds[k]}")
        self.n_samples_fit = int(n_samples_fit)

    # ------------------------------------------------------------------ fit

    @classmethod
    def fit(cls, X, y) -> "LSTMScaler":
        """Fit mean/std từ X train ``(N, W, 2)`` và y train ``(N, H)``.

        Rỗng, sai shape hoặc chứa NaN/±inf → ``ValueError``. std = 0 →
        ``ValueError`` (policy ghi ở module docstring — không thay 1.0 âm
        thầm).
        """
        x = np.asarray(X, dtype="float64")
        y_arr = np.asarray(y, dtype="float64")
        if x.size == 0 or y_arr.size == 0:
            raise ValueError("không thể fit scaler trên dữ liệu rỗng")
        if x.ndim != 3 or x.shape[2] != 2:
            raise ValueError(f"X phải shape (N, W, 2), nhận {x.shape}")
        if y_arr.ndim != 2:
            raise ValueError(f"y phải shape (N, H), nhận {y_arr.shape}")
        if len(x) != len(y_arr):
            raise ValueError(
                f"X và y lệch số mẫu: {len(x)} vs {len(y_arr)}"
            )
        if not (np.isfinite(x).all() and np.isfinite(y_arr).all()):
            raise ValueError("X/y chứa giá trị không hữu hạn — từ chối fit")

        dist = x[:, :, 0]
        delta = x[:, :, 1]
        means = {
            "distance_m": float(dist.mean()),
            "delta_distance_m": float(delta.mean()),
            SCALER_TARGET_NAME: float(y_arr.mean()),
        }
        stds = {
            "distance_m": float(dist.std()),
            "delta_distance_m": float(delta.std()),
            SCALER_TARGET_NAME: float(y_arr.std()),
        }
        for name, value in stds.items():
            if value == 0.0:
                raise ValueError(
                    f"std của {name} bằng 0 trên tập train — dữ liệu hằng "
                    "số, từ chối fit (policy pp-lstm-v1: không thay std ẩn "
                    "danh, kiểm tra lại dữ liệu)"
                )
        return cls(means=means, stds=stds, n_samples_fit=len(x))

    # ------------------------------------------------------------ transform

    def transform_X(self, X) -> np.ndarray:
        """Chuẩn hóa X ``(N, W, 2)`` theo từng kênh; trả bản mới, không mutate."""
        x = np.asarray(X, dtype="float64")
        if x.ndim != 3 or x.shape[2] != 2:
            raise ValueError(f"X phải shape (N, W, 2), nhận {x.shape}")
        if x.size and not np.isfinite(x).all():
            raise ValueError("X chứa giá trị không hữu hạn — từ chối transform")
        out = np.empty_like(x)
        out[:, :, 0] = (x[:, :, 0] - self.means["distance_m"]) / self.stds[
            "distance_m"
        ]
        out[:, :, 1] = (x[:, :, 1] - self.means["delta_distance_m"]) / self.stds[
            "delta_distance_m"
        ]
        return out

    def transform_y(self, y) -> np.ndarray:
        """Chuẩn hóa y ``(N, H)`` (mét) bằng tham số target; không mutate."""
        y_arr = np.asarray(y, dtype="float64")
        if y_arr.ndim != 2:
            raise ValueError(f"y phải shape (N, H), nhận {y_arr.shape}")
        if y_arr.size and not np.isfinite(y_arr).all():
            raise ValueError("y chứa giá trị không hữu hạn — từ chối transform")
        return (y_arr - self.means[SCALER_TARGET_NAME]) / self.stds[
            SCALER_TARGET_NAME
        ]

    def inverse_y(self, y) -> np.ndarray:
        """Đưa y/dự báo đã chuẩn hóa về mét (dùng cho metric + runtime)."""
        y_arr = np.asarray(y, dtype="float64")
        if y_arr.size and not np.isfinite(y_arr).all():
            raise ValueError("y chứa giá trị không hữu hạn — từ chối inverse")
        return y_arr * self.stds[SCALER_TARGET_NAME] + self.means[
            SCALER_TARGET_NAME
        ]

    # ------------------------------------------------------------ JSON I/O

    def to_dict(self) -> dict:
        """Dict số thuần để ghi ``scaler.json`` (không pickle, không numpy)."""
        return {
            "fitted_on": FITTED_ON,
            "preprocessing_version": PP_LSTM_VERSION,
            "features": list(SCALER_FEATURE_NAMES),
            "target": SCALER_TARGET_NAME,
            "means": dict(self.means),
            "stds": dict(self.stds),
            "n_samples_fit": self.n_samples_fit,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LSTMScaler":
        """Dựng scaler từ dict JSON; thiếu/sai version hoặc std <= 0 → lỗi."""
        if not isinstance(data, dict):
            raise ValueError("scaler dict phải là JSON object")
        if data.get("preprocessing_version") != PP_LSTM_VERSION:
            raise ValueError(
                f"preprocessing_version phải là {PP_LSTM_VERSION}, nhận "
                f"{data.get('preprocessing_version')!r}"
            )
        if data.get("fitted_on") != FITTED_ON:
            raise ValueError(
                f"fitted_on phải là {FITTED_ON!r}, nhận {data.get('fitted_on')!r}"
            )
        if list(data.get("features") or []) != SCALER_FEATURE_NAMES:
            raise ValueError(
                f"features phải là {SCALER_FEATURE_NAMES}, nhận "
                f"{data.get('features')!r}"
            )
        # Bắt buộc đủ 3 khối số liệu — thiếu → ValueError rõ ràng, không
        # default (n_samples_fit=0 ẩn danh) hay KeyError khi đọc sâu.
        missing = [
            key
            for key in ("means", "stds", "n_samples_fit")
            if key not in data
        ]
        if missing:
            raise ValueError(f"scaler dict thiếu trường: {missing}")
        return cls(
            means=data["means"],
            stds=data["stds"],
            n_samples_fit=int(data["n_samples_fit"]),
        )

    def save(self, path: str | Path) -> str:
        """Ghi ``scaler.json`` (canonical JSON); trả SHA-256 nội dung."""
        return write_scaler_json(self.to_dict(), path)

    @classmethod
    def load(cls, path: str | Path) -> "LSTMScaler":
        """Đọc scaler.json; JSON hỏng → json.JSONDecodeError; sai contract →
        ValueError (không fallback giá trị mặc định)."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data)
