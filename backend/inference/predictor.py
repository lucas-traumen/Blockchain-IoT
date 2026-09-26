"""Runtime inference (M6a): buffer cửa sổ trượt + dự báo RF cho backend.

Nguồn spec:
- PLAN.md mục 4, đoạn "Inference runtime": buffer per
  ``(device_id, boot_id, session_id)``; dưới ``window_size`` mẫu →
  ``WARMUP``; mẫu lỗi/gap → ``INVALID_WINDOW``; thiếu model →
  ``MODEL_UNAVAILABLE``; đủ ``window_size`` → dự báo 5 horizon; early
  warning chỉ khi hiện tại còn trên ngưỡng — hiện tại đã ≤ Sp là
  VIOLATION (việc của khối safety), không phải cảnh báo sớm.
- Contract ĐẦU RA: khối ``prediction`` của
  ``schemas/assessment_v1.schema.json`` — đúng 8 field ``status``,
  ``model_version``, ``horizons_ms``, ``distance_m``, ``early_warning``,
  ``stale``, ``window_ref``, ``mode`` (``additionalProperties: false``);
  ``distance_m``/``early_warning``/``window_ref`` null khi ``status`` !=
  ``OK``; ``distance_m`` đúng 5 phần tử ứng ``horizons_ms``.
- Quy ước feature (contract dùng chung train + runtime,
  ``ai_model/features.py``): ``d0`` là khoảng cách CŨ NHẤT của cửa sổ
  (mẫu ``t-(N-1)Δ``), ``d(N-1)`` là MỚI NHẤT (mẫu vừa nhận, mẫu ``t``);
  đơn vị mét. Thứ tự phải khớp ``feature_names`` của manifest artifact —
  ``load_model`` đã chặn nạp khi lệch, ``predict`` truyền DataFrame với
  ``columns=FEATURE_NAMES`` để sklearn kiểm lần nữa.

Quy tắc run hợp lệ dùng lại đúng định nghĩa của ``ai_model/windows.py``
(``sequence`` +1, ``dt <= max_gap_s``, ``abs(dt - delta_s) <=
max_time_jitter_s``, dt tính từ ``monotonic_us``): ``push_sample`` tự so
sánh các điều kiện này thay vì dò điểm cắt qua ``extract_windows`` — lý
do chọn cách này xem docstring ``push_sample``.

Thread-safety: mqtt thread và API có thể gọi đồng thời — mọi truy cập
buffer bọc ``threading.Lock``; ``predict`` push + chụp snapshot buffer
trong cùng một lần giữ khóa để kết quả phản ánh đúng mẫu vừa push.

Đơn vị nội bộ SI (m, s); ``distance_m = distance_mm / 1000`` chuyển tại
biên (PLAN.md mục 2). Import thuần túy — không mở DB, không network.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from ai_model.features import FEATURE_NAMES
from ai_model.train import load_model
from ai_model.windows import WindowConfig
from backend.util import parse_iso_utc, utc_now_iso

__all__ = ["SafetyPredictor"]

# Hằng số đơn vị µs → giây (giống ai_model.windows.US_PER_S).
_US_PER_S = 1_000_000


class SafetyPredictor:
    """Buffer cửa sổ trượt per (device, boot, session) + dự báo RF 5 horizon.

    Một instance dùng cho cả tiến trình backend: model nạp một lần lúc
    khởi tạo, buffer tích lũy theo key ``(device_id, boot_id,
    session_id|None)`` (``session_id=None`` là một nhóm riêng — cùng quy
    ước nhóm của ``ai_model/windows.py``).
    """

    def __init__(
        self,
        model_dir: str | Path,
        config: dict,
        model_loader: Callable[[str | Path], tuple[Any, dict]] = load_model,
    ) -> None:
        """Nạp model từ ``model_dir`` và chuẩn bị cấu hình từ ``config``.

        ``config``: dict đã nạp từ ``configs/model.yaml``. ``model_loader``:
        callable trả ``(model, manifest)`` — mặc định ``ai_model.train.load_model``.

        Lỗi nạp artifact (thiếu file, manifest hỏng, joblib lỗi, ...) →
        ``self.available = False``, KHÔNG raise: runtime phải sống còn khi
        thiếu model (PLAN mục 4) và ``predict`` trả ``MODEL_UNAVAILABLE``.
        Buffer vẫn hoạt động bình thường.

        Thiếu key cấu hình bắt buộc (``window_size``, ``delta_s``,
        ``horizons_ms``, ``max_time_jitter_s``, ``max_gap_s``,
        ``stale_after_s``) là lỗi cấu hình operator → raise
        (``KeyError``/``ValueError`` từ ``WindowConfig.from_dict``), không nuốt.
        """
        self._model: Any = None
        self._manifest: dict | None = None
        try:
            model, manifest = model_loader(model_dir)
        except Exception:
            # Cố ý bắt rộng: mọi dạng hỏng của artifact đều cùng hệ quả —
            # unavailable. Không nuốt KeyboardInterrupt/SystemExit (không
            # thuộc Exception).
            self.available = False
        else:
            self.available = True
            self._model = model
            self._manifest = manifest

        self.window_config = WindowConfig.from_dict(config)
        self.stale_after_s = float(config["stale_after_s"])
        # Luôn lấy từ config (qua WindowConfig — cùng nguồn, đã chuẩn int).
        self.horizons_ms: list[int] = list(self.window_config.horizons_ms)

        # Buffer nội: key → run hiện tại, list mẫu
        # {sequence, monotonic_us, distance_m, measured_at}; chỉ chứa mẫu
        # valid, tối đa window_size mẫu cuối của run.
        self._buffers: dict[
            tuple[str, str, str | None], list[dict[str, Any]]
        ] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Buffer nội bộ
    # ------------------------------------------------------------------

    @staticmethod
    def _buffer_key(sample: dict, session_id: str | None) -> tuple[str, str, str | None]:
        """Key buffer: ``(device_id, boot_id, session_id|None)``."""
        return (sample["device_id"], sample["boot_id"], session_id)

    @staticmethod
    def _make_entry(sample: dict) -> dict[str, Any]:
        """Mẫu lưu buffer — chuyển SI tại biên (``distance_mm`` → mét)."""
        return {
            "sequence": int(sample["sequence"]),
            "monotonic_us": int(sample["monotonic_us"]),
            "distance_m": float(sample["distance_mm"]) / 1000.0,
            "measured_at": sample.get("measured_at"),
        }

    def _push_sample_locked(self, sample: dict, session_id: str | None) -> bool:
        """Như ``push_sample`` nhưng GIẢ ĐỊNH ``self._lock`` đang giữ.

        Dùng riêng cho ``predict`` để push + snapshot buffer nguyên tử.
        """
        key = self._buffer_key(sample, session_id)

        # Mẫu không dùng được → cắt run, buffer rỗng; mẫu lỗi KHÔNG vào run
        # (giống windows.py: mẫu invalid không thuộc run nào).
        if sample.get("valid") is not True or sample.get("distance_mm") is None:
            self._buffers[key] = []
            return True

        buffer_ = self._buffers.setdefault(key, [])
        if buffer_:
            last = buffer_[-1]
            dt_s = (int(sample["monotonic_us"]) - int(last["monotonic_us"])) / _US_PER_S
            connected = (
                int(sample["sequence"]) == int(last["sequence"]) + 1
                and dt_s <= self.window_config.max_gap_s
                and abs(dt_s - self.window_config.delta_s)
                <= self.window_config.max_time_jitter_s
            )
            if not connected:
                # Đứt run (sequence không +1, gap, hoặc lệch nhịp quá
                # jitter): reset rồi mở run mới từ mẫu này.
                self._buffers[key] = [self._make_entry(sample)]
                return True

        buffer_.append(self._make_entry(sample))
        # Giữ buffer bounded: chỉ cần mẫu cuối (kiểm nối) và window_size
        # mẫu cuối (features); phần cũ hơn bỏ để chặn tăng trưởng vô hạn
        # khi backend chạy dài (MQTT ~10 Hz).
        if len(buffer_) > self.window_config.window_size:
            del buffer_[: len(buffer_) - self.window_config.window_size]
        return False

    def push_sample(self, sample: dict, session_id: str | None) -> bool:
        """Thêm một mẫu telemetry ĐÃ VALIDATE vào buffer; trả cờ reset.

        Key: ``(sample["device_id"], sample["boot_id"], session_id)``.

        Quy tắc (khớp định nghĩa run trong ``ai_model/windows.py``):

        - ``valid`` không phải True hoặc ``distance_mm`` None → reset
          buffer của key (mẫu lỗi không vào run) → trả True;
        - buffer rỗng → mẫu mở run mới → trả False (không phải reset do
          mẫu này gây ra);
        - nối được vào run (``sequence == cuối + 1``, ``dt <= max_gap_s``,
          ``abs(dt − delta_s) <= max_time_jitter_s`` với ``dt`` từ
          ``monotonic_us``) → append → trả False;
        - đứt (sequence/gap/jitter) → reset buffer, mẫu thành đầu run mới
          → trả True.

        Cách kiểm nối — đã chọn: so trực tiếp các điều kiện trên thay vì
        gọi ``extract_windows(buffer + [mẫu])`` rồi đếm cửa sổ. Hai cách
        cùng áp một bộ quy tắc cắt nên tương đương về ngữ nghĩa, nhưng
        ``extract_windows`` chỉ tạo được cửa sổ khi run dài ít nhất
        ``window_size + offset(horizon xa nhất)`` mẫu — trước ngưỡng đó
        số cửa sổ là 0 cả trước lẫn sau append, tức không phát hiện được
        điểm cắt trong suốt giai đoạn WARMUP (chính là lúc buffer cần
        biết mình còn liền mạch hay không). So điều kiện trực tiếp áp
        đúng cùng quy tắc ở mọi độ dài run.

        Buffer chỉ giữ ``window_size`` mẫu cuối của run (đủ cho kiểm nối
        và features). Thread-safe qua ``self._lock``.
        """
        with self._lock:
            return self._push_sample_locked(sample, session_id)

    # ------------------------------------------------------------------
    # Dự báo
    # ------------------------------------------------------------------

    def _compute_stale(self, sample: dict, mode: str, now_iso: str | None) -> bool:
        """Cờ ``stale`` cho mẫu vừa nhận.

        - ``mode == "offline"`` → False (replay tính theo thời gian nguồn,
          không so với giờ backend — PLAN mục 4).
        - ``measured_at`` None → False: không xác định được tuổi mẫu,
          không bịa thay thế (chưa sync thì backend không đoán giờ đo).
        - Còn lại (mode live): ``now_iso`` (None → ``utc_now_iso()``) −
          ``measured_at`` của mẫu hiện tại > ``stale_after_s`` → True.
          Mode khác "offline" xử lý như live.

        ``now_iso``/``measured_at`` sai định dạng ISO-8601 → ``ValueError``
        từ ``parse_iso_utc`` (lỗi caller/payload, không nuốt).
        """
        if mode == "offline":
            return False
        measured_at = sample.get("measured_at")
        if measured_at is None:
            return False
        reference = now_iso if now_iso is not None else utc_now_iso()
        age_s = (parse_iso_utc(reference) - parse_iso_utc(measured_at)).total_seconds()
        return age_s > self.stale_after_s

    @staticmethod
    def _prediction(
        status: str,
        model_version: str | None,
        horizons_ms: list[int],
        distance_m: list[float] | None,
        early_warning: bool | None,
        stale: bool,
        window_ref: dict | None,
        mode: str,
    ) -> dict:
        """Dựng dict khối ``prediction`` đúng field schema assessment_v1."""
        return {
            "status": status,
            "model_version": model_version,
            "horizons_ms": horizons_ms,
            "distance_m": distance_m,
            "early_warning": early_warning,
            "stale": stale,
            "window_ref": window_ref,
            "mode": mode,
        }

    def predict(
        self,
        sample: dict,
        session_id: str | None,
        sp_m: float,
        mode: str,
        now_iso: str | None = None,
    ) -> dict:
        """Push mẫu rồi trả khối ``prediction`` của assessment_v1 (dict).

        Thứ tự ưu tiên trạng thái (đánh giá theo mẫu VỪA push):

        1. ``MODEL_UNAVAILABLE`` — model không nạp được lúc khởi tạo.
        2. ``INVALID_WINDOW`` — mẫu vừa push gây reset (invalid, hoặc
           valid nhưng đứt run). Ưu tiên TRƯỚC ``WARMUP``: mẫu gây reset
           luôn đưa buffer về đúng 1 mẫu, nếu kiểm ``WARMUP`` trước thì
           trạng thái này không bao giờ đạt được.
        3. ``WARMUP`` — buffer < ``window_size``.
        4. ``OK`` — đủ ``window_size``.

        Với status != ``OK``: ``model_version``/``distance_m``/
        ``early_warning``/``window_ref`` đều null.

        Khi ``OK``:

        - features = ``window_size`` mẫu cuối của buffer theo thứ tự d0
          (cũ nhất) → d(N-1) (mới nhất = mẫu hiện tại); dự báo qua
          ``pandas.DataFrame([features], columns=FEATURE_NAMES)``;
          ``distance_m`` = 5 float round(3) theo ``horizons_ms``;
          ``model_version`` từ manifest artifact.
        - ``early_warning``: ``features[-1] > sp_m`` (hiện tại còn trên
          ngưỡng; ``features[-1]`` tức ``features[9]`` khi N=10) VÀ
          min(dự báo) ``<= sp_m`` — so trên giá trị đã round(3) để khớp
          giá trị công bố. Hiện tại đã ≤ ``sp_m`` → False: đó là
          VIOLATION, việc của khối safety.
        - ``window_ref``: ``{device_id, boot_id, sequence_first,
          sequence_last, sample_count: window_size, max_age_ms:
          (window_size−1)*delta_s*1000}`` của cửa sổ đầu vào.

        ``stale``/``horizons_ms``/``mode``: xem ``_compute_stale``;
        ``horizons_ms`` luôn từ config, ``mode`` echo tham số.

        ``sample`` là payload telemetry_v1 đã validate (device_id,
        boot_id, sequence, monotonic_us bắt buộc có). Thread-safe: push +
        snapshot buffer trong cùng một lần giữ khóa; model predict chạy
        ngoài khóa trên snapshot đã chụp.
        """
        with self._lock:
            reset = self._push_sample_locked(sample, session_id)
            buffer_snapshot = list(
                self._buffers.get(self._buffer_key(sample, session_id), [])
            )

        stale = self._compute_stale(sample, mode, now_iso)
        horizons_ms = list(self.window_config.horizons_ms)

        if not self.available:
            return self._prediction(
                "MODEL_UNAVAILABLE", None, horizons_ms, None, None, stale, None, mode
            )
        if reset:
            return self._prediction(
                "INVALID_WINDOW", None, horizons_ms, None, None, stale, None, mode
            )
        if len(buffer_snapshot) < self.window_config.window_size:
            return self._prediction(
                "WARMUP", None, horizons_ms, None, None, stale, None, mode
            )

        # OK: cửa sổ = window_size mẫu cuối của buffer, d0 cũ nhất → d(N-1)
        # mới nhất (mẫu vừa push) — đúng quy ước feature contract.
        window_entries = buffer_snapshot[-self.window_config.window_size :]
        features = [entry["distance_m"] for entry in window_entries]
        frame = pd.DataFrame([features], columns=FEATURE_NAMES)
        forecast = self._model.predict(frame)[0]
        distance_m = [round(float(value), 3) for value in forecast]

        early_warning = bool(
            features[-1] > float(sp_m) and min(distance_m) <= float(sp_m)
        )
        window_ref = {
            "device_id": sample["device_id"],
            "boot_id": sample["boot_id"],
            "sequence_first": int(window_entries[0]["sequence"]),
            "sequence_last": int(window_entries[-1]["sequence"]),
            "sample_count": self.window_config.window_size,
            "max_age_ms": (self.window_config.window_size - 1)
            * self.window_config.delta_s
            * 1000,
        }
        model_version = str(self._manifest["model_version"])
        return self._prediction(
            "OK",
            model_version,
            horizons_ms,
            distance_m,
            early_warning,
            stale,
            window_ref,
            mode,
        )
