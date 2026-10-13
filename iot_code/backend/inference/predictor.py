"""Runtime LSTM: buffer theo device/boot/session, shared preprocessing.

Model lỗi hoặc thiếu TensorFlow chỉ tắt prediction; safety vẫn tính độc lập.
"""
from __future__ import annotations

import logging
import math
import threading
from pathlib import Path
from typing import Any, Callable

import numpy as np
import yaml

from ai_model.features import window_to_lstm_features
from ai_model.windows import Window, WindowConfig, WindowMeta
from iot_backend.util import parse_iso_utc, utc_now_iso

__all__ = ["SafetyPredictor", "build_predictor"]
logger = logging.getLogger(__name__)


class SafetyPredictor:
    """Một model/instance, buffer độc lập theo khóa (device, boot, session)."""

    def __init__(
        self, model_dir: str | Path, config: dict,
        model_loader: Callable | None = None, *, model_type: str = "lstm",
    ) -> None:
        if model_type != "lstm":
            raise ValueError("model_type phải là lstm")
        self.model_type = model_type
        self.window_config = WindowConfig.from_dict(config)
        self.horizons_ms = list(self.window_config.horizons_ms)
        self.stale_after_s = float(config["stale_after_s"])
        self._config = config
        self._model: Any = None
        self._scaler: Any = None
        self._manifest: dict | None = None
        self.load_error: str | None = None
        self._buffers: dict[tuple[str, str, str | None], list[dict]] = {}
        self._active_keys: dict[str, tuple] = {}
        self._lock = threading.Lock()
        try:
            if model_loader is None:
                from ai_model.lstm_train import load_lstm_artifact
                model_loader = load_lstm_artifact
            self._model, self._scaler, self._manifest = model_loader(model_dir)
            self._validate_lstm_contract()
            self.available = True
        except Exception as exc:
            self.available = False
            self.load_error = str(exc)
            logger.warning("Model %s unavailable: %s", model_type, exc)

    def _validate_lstm_contract(self) -> None:
        from ai_model.lstm_train import validate_lstm_config
        validate_lstm_config(self._manifest, self._config)

    @staticmethod
    def _buffer_key(sample: dict, session_id: str | None) -> tuple:
        return sample["device_id"], sample["boot_id"], session_id

    @staticmethod
    def _make_entry(sample: dict) -> dict:
        return {
            "sequence": int(sample["sequence"]),
            "monotonic_us": int(sample["monotonic_us"]),
            "distance_m": float(sample["distance_mm"]) / 1000.0,
            "measured_at": sample.get("measured_at"),
        }

    def _push_sample_locked(self, sample: dict, session_id: str | None) -> bool:
        key = self._buffer_key(sample, session_id)
        # Mỗi (device, boot, session) có buffer riêng; session mới trong
        # cùng boot không được nối với cửa sổ của session cũ.
        previous = self._active_keys.get(key[0])
        if previous is not None and previous != key:
            # A session change within one boot must not resume the old
            # session window. A reboot has its own buffer; retain the old
            # boot buffer so delayed/out-of-order processing cannot destroy
            # an already valid window for that boot.
            if previous[1] == key[1]:
                self._buffers.pop(previous, None)
        self._active_keys[key[0]] = key
        if sample.get("valid") is not True or sample.get("distance_mm") is None:
            self._buffers[key] = []
            return True
        buffer = self._buffers.setdefault(key, [])
        if buffer:
            dt = (int(sample["monotonic_us"]) - buffer[-1]["monotonic_us"]) / 1e6
            if not (
                int(sample["sequence"]) == buffer[-1]["sequence"] + 1
                and 0 < dt <= self.window_config.max_gap_s
                and abs(dt - self.window_config.delta_s) <= self.window_config.max_time_jitter_s
            ):
                self._buffers[key] = [self._make_entry(sample)]
                return True
        buffer.append(self._make_entry(sample))
        del buffer[:-self.window_config.window_size]
        return False

    def push_sample(self, sample: dict, session_id: str | None) -> bool:
        with self._lock:
            return self._push_sample_locked(sample, session_id)

    def _compute_stale(self, sample: dict, mode: str, now_iso: str | None) -> bool:
        if mode == "offline" or sample.get("measured_at") is None:
            return False
        return (
            parse_iso_utc(now_iso or utc_now_iso()) - parse_iso_utc(sample["measured_at"])
        ).total_seconds() > self.stale_after_s

    def _prediction(self, status: str, stale: bool, mode: str, **fields) -> dict:
        result = {
            "status": status, "model_version": None, "horizons_ms": self.horizons_ms,
            "distance_m": None, "early_warning": None, "stale": stale,
            "window_ref": None, "mode": mode,
        }
        result.update(preprocessing_version=None, predicted_violation_ms=None)
        result.update(fields)
        return result

    def predict(
        self, sample: dict, session_id: str | None, sp_m: float,
        mode: str, now_iso: str | None = None,
    ) -> dict:
        if mode not in {"live", "offline"}:
            raise ValueError("mode phải là live hoặc offline")
        stale = self._compute_stale(sample, mode, now_iso)
        # Serialize model calls as well as buffer updates (MQTT/API threads).
        with self._lock:
            reset = self._push_sample_locked(sample, session_id)
            entries = self._buffers.get(self._buffer_key(sample, session_id), [])
            if not self.available:
                return self._prediction("MODEL_UNAVAILABLE", stale, mode)
            if reset:
                return self._prediction("INVALID_WINDOW", stale, mode)
            if len(entries) < self.window_config.window_size:
                return self._prediction("WARMUP", stale, mode)
            distances = [e["distance_m"] for e in entries]
            try:
                window = Window(distances, [], WindowMeta(
                    sample["device_id"], sample["boot_id"], session_id,
                    entries[0]["sequence"], entries[-1]["sequence"],
                    entries[-1]["sequence"], sample.get("measured_at"),
                ))
                raw = np.asarray([window_to_lstm_features(window, self._config)])
                scaled = self._scaler.transform_X(raw)
                if not np.isfinite(scaled).all():
                    raise ValueError("scaled X không hữu hạn")
                # Single small batch: calling model directly avoids a new
                # tf.data thread pool for every telemetry sample.
                forecast = self._model(scaled, training=False)
                if hasattr(forecast, "numpy"):
                    forecast = forecast.numpy()
                forecast = self._scaler.inverse_y(forecast)
                forecast = np.asarray(forecast, dtype=float)
                if forecast.shape != (1, len(self.horizons_ms)) or not np.isfinite(forecast).all():
                    raise ValueError("model output sai shape hoặc không hữu hạn")
            except Exception as exc:
                self.available = False
                self.load_error = str(exc)
                logger.warning("Prediction %s failed: %s", self.model_type, exc)
                return self._prediction("MODEL_UNAVAILABLE", stale, mode)
            # Compare/output full precision; no rounding before safety threshold.
            prediction_m = forecast[0].tolist()
            violation = next((h for h, d in zip(self.horizons_ms, prediction_m)
                              if math.isfinite(sp_m) and d <= sp_m), None)
            fields = {
                "model_version": self._manifest["model_version"],
                "distance_m": prediction_m,
                "early_warning": bool(distances[-1] > sp_m and violation is not None),
                "window_ref": {
                    "device_id": sample["device_id"], "boot_id": sample["boot_id"],
                    "sequence_first": entries[0]["sequence"],
                    "sequence_last": entries[-1]["sequence"],
                    "sample_count": len(entries),
                    "max_age_ms": (entries[-1]["monotonic_us"] - entries[0]["monotonic_us"]) / 1000,
                },
            }
            fields.update(preprocessing_version=self._manifest["preprocessing_version"],
                          predicted_violation_ms=violation)
            return self._prediction("OK", stale, mode, **fields)


def build_predictor(
    model_dir: str | Path, config_path: str | Path, model_type: str = "lstm",
) -> SafetyPredictor:
    """Factory dùng chung app/ingest/replay; config lỗi là lỗi operator."""
    with Path(config_path).open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    return SafetyPredictor(model_dir, config, model_type=model_type)
