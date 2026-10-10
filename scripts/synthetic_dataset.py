"""Sinh dataset SYNTHETIC telemetry_v1 dạng JSONL, ghi nhãn rõ ràng.

CẢNH BÁO — DỮ LIỆU TỔNG HỢP (SYNTHETIC), KHÔNG PHẢI ĐO THẬT: toàn bộ mẫu do
file này sinh ra là mô phỏng chuyển động vật/robot quanh robot gắn HC-SR04.
Mỗi dòng JSONL mang nhãn ``"synthetic": true`` + ``"scenario"`` +
``"session_label"`` theo quy tắc PLAN.md mục 2 ("dữ liệu synthetic phải ghi
nhãn synthetic rõ ràng") để không bao giờ lẫn với dữ liệu đo thật.

Mỗi session mô phỏng một lần chạy độc lập:

- ``device_id`` cố định ``esp32_sim``; ``boot_id`` duy nhất mỗi session
  (dạng ``sim_<seed-hex8>_<i>``); ``sequence`` 1..N; ``monotonic_us`` tăng
  đúng 100_000 µs mỗi mẫu (Δ = 0.1 s khớp ``configs/model.yaml``);
  ``measured_at`` ISO-8601 UTC tăng 0.1 s với ``time_synced=true``.
- Kịch bản khoảng cách lý tưởng (trước nhiễu), Sp demo = 0.44 m theo
  ``configs/safety.yaml`` (440 mm):
  - ``approach``: giữ ~1.0 m → giảm dần với vận tốc thay đổi (2 mốc đổi
    tốc) → chạm/crossing xuống đáy 120..420 mm (luôn dưới Sp) → hồi phục
    về 500..800 mm.
  - ``recede``: giữ ngắn tại ~0.2 m → tăng dần vận tốc thay đổi → ~1.0 m,
    giữ đến cuối.
  - ``mixed``: xen kẽ 4..6 pha approach/recede (độ sâu pha approach
    150..700 mm — có lúc dưới, có lúc trên Sp).
  - ``noise``: dao động sin quanh nền ngẫu nhiên 0.5..0.9 m, biên độ
    10..30 mm — min ≥ ~455 mm nên không bao giờ chạm ngưỡng 440 mm.
- Nhiễu đo Gaussian σ ∈ [2, 5] mm mỗi session (``numpy.random.default_rng
  (seed)``), clamp 0..4500 mm, làm tròn nguyên. ``echo_us`` xấp xỉ xung
  echo hai chiều (~5.83 µs/mm) cộng trừ ±50 µs.
- Xác suất 2% mẫu lỗi ``ECHO_TIMEOUT`` (``valid=false``, ``distance_mm``,
  ``echo_us`` null), KHÔNG chèn vào 5 mẫu đầu/cuối session để không cắt
  mạch cửa sổ ngay biên; sequence/monotonic/measured_at vẫn tiến đều.

CLI (chạy từ repo root):

    .venv/bin/python -m scripts.synthetic_dataset --out /tmp/opencode/sim.jsonl \
        --sessions 30 --samples-per-session 200 --seed 42 --scenario all

Kết quả sinh ra dùng cho train qua ``ai_model/prepare.py:load_jsonl_dataset``
(tách nhãn metadata khỏi payload). Cách nạp vào DB qua
``scripts/ingest_jsonl.py`` xem khối ghi chú cuối file.
"""

from __future__ import annotations

import argparse
import json
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

__all__ = [
    "DEVICE_ID",
    "SCENARIOS",
    "METADATA_KEYS",
    "ERROR_PROB",
    "EDGE_GUARD",
    "generate_sessions",
    "write_jsonl",
    "main",
]

DEVICE_ID = "esp32_sim"
SCENARIOS = ("approach", "recede", "mixed", "noise")
# Nhãn mức JSONL — KHÔNG thuộc payload schema (additionalProperties=false).
METADATA_KEYS = ("synthetic", "scenario", "session_label")

ERROR_PROB = 0.02  # xác suất mẫu lỗi ECHO_TIMEOUT mỗi mẫu
ERROR_CODE = "ECHO_TIMEOUT"
EDGE_GUARD = 5  # cấm chèn mẫu lỗi vào 5 mẫu đầu/cuối session
US_PER_SAMPLE = 100_000  # 0.1 s theo configs/model.yaml
SP_DEMO_MM = 440  # Sp demo theo configs/safety.yaml (0.44 m)
ECHO_US_PER_MM = 5.83  # xấp xỉ xung echo HC-SR04 hai chiều (µs/mm)
_TIME_BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
_SESSION_GAP_S = 60.0  # nghỉ giữa hai session mô phỏng


def _iso_utc(dt: datetime) -> str:
    """ISO-8601 UTC millisecond, hậu tố Z (khớp iot_backend.util.utc_now_iso)."""
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _keypoint_curve(n: int, keypoints: list[tuple[float, float]]) -> list[float]:
    """Nội suy tuyến tính qua các mốc ``(fraction, value_mm)`` trên n mẫu.

    ``fraction`` ∈ [0, 1] ánh xạ chỉ số 0..n-1; mốc phải tăng dần về fraction.
    """
    xs = [frac * (n - 1) for frac, _ in keypoints]
    ys = [value for _, value in keypoints]
    curve: list[float] = []
    for i in range(n):
        x = float(i)
        if x <= xs[0]:
            curve.append(ys[0])
            continue
        if x >= xs[-1]:
            curve.append(ys[-1])
            continue
        j = bisect_right(xs, x) - 1
        t = (x - xs[j]) / (xs[j + 1] - xs[j])
        curve.append(ys[j] + t * (ys[j + 1] - ys[j]))
    return curve


def _approach_curve(n: int, rng: np.random.Generator) -> list[float]:
    """Giữ ~1.0 m → giảm với 2 mốc đổi tốc → đáy 120..420 mm → hồi phục."""
    d_start = rng.uniform(950.0, 1050.0)
    d_bottom = rng.uniform(120.0, SP_DEMO_MM - 20.0)  # luôn dưới Sp demo
    d_rebound = rng.uniform(500.0, 800.0)
    hold_end = rng.uniform(0.10, 0.20)
    dip_end = hold_end + rng.uniform(0.50, 0.65)  # điểm chạm đáy
    bottom_end = min(dip_end + rng.uniform(0.03, 0.08), 0.93)
    # Hai mốc trung gian để vận tốc đổi vài lần trong đoạn giảm.
    mid1 = hold_end + (dip_end - hold_end) * rng.uniform(0.25, 0.45)
    val1 = d_start - (d_start - d_bottom) * rng.uniform(0.15, 0.45)
    mid2 = mid1 + (dip_end - mid1) * rng.uniform(0.45, 0.70)
    val2 = d_bottom + (d_start - d_bottom) * rng.uniform(0.10, 0.35)
    return _keypoint_curve(
        n,
        [
            (0.0, d_start),
            (hold_end, d_start),
            (mid1, val1),
            (mid2, val2),
            (dip_end, d_bottom),
            (bottom_end, d_bottom),
            (1.0, d_rebound),
        ],
    )


def _recede_curve(n: int, rng: np.random.Generator) -> list[float]:
    """~0.2 m → tăng với vận tốc thay đổi → ~1.0 m, giữ đến cuối."""
    d_start = rng.uniform(180.0, 260.0)
    d_end = rng.uniform(950.0, 1050.0)
    hold_end = rng.uniform(0.08, 0.18)
    rise_end = hold_end + rng.uniform(0.55, 0.70)
    mid1 = hold_end + (rise_end - hold_end) * rng.uniform(0.30, 0.50)
    val1 = d_start + (d_end - d_start) * rng.uniform(0.15, 0.45)
    mid2 = mid1 + (rise_end - mid1) * rng.uniform(0.40, 0.65)
    val2 = d_end - (d_end - d_start) * rng.uniform(0.10, 0.30)
    return _keypoint_curve(
        n,
        [
            (0.0, d_start),
            (hold_end, d_start),
            (mid1, val1),
            (mid2, val2),
            (rise_end, d_end),
            (1.0, d_end),
        ],
    )


def _mixed_curve(n: int, rng: np.random.Generator) -> list[float]:
    """Xen kẽ 4..6 pha approach/recede, chạy liên tục giữa các đích."""
    n_phases = int(rng.integers(4, 7))
    value = rng.uniform(500.0, 1000.0)
    keypoints = [(0.0, value)]
    for k in range(n_phases):
        if k % 2 == 0:  # pha approach: đích có thể dưới hoặc trên Sp
            value = rng.uniform(150.0, 700.0)
        else:  # pha recede
            value = rng.uniform(700.0, 1000.0)
        keypoints.append(((k + 1) / n_phases, value))
    return _keypoint_curve(n, keypoints)


def _noise_curve(n: int, rng: np.random.Generator) -> list[float]:
    """Dao động quanh nền 0.5..0.9 m; min ≥ ~455 mm — không chạm 440 mm."""
    base = rng.uniform(500.0, 900.0)
    amp = rng.uniform(10.0, 30.0)
    period = max(rng.uniform(0.3, 0.6) * n, 4.0)
    phase = rng.uniform(0.0, 2.0 * np.pi)
    t = np.arange(n, dtype=float)
    return list(base + amp * np.sin(2.0 * np.pi * t / period + phase))


_PROFILES = {
    "approach": _approach_curve,
    "recede": _recede_curve,
    "mixed": _mixed_curve,
    "noise": _noise_curve,
}


def _generate_session(
    idx: int,
    scenario: str,
    n: int,
    seed: int,
    rng: np.random.Generator,
    error_prob: float,
) -> list[dict]:
    """Sinh n dòng JSONL của một session (payload 11 field + 3 nhãn metadata).

    Thứ tự gọi rng cố định (profile → σ → nhiễu → mask lỗi → mono_start →
    echo) để kết quả deterministic theo seed.
    """
    ideal = _PROFILES[scenario](n, rng)
    sigma_mm = float(rng.uniform(2.0, 5.0))
    noise = rng.normal(0.0, sigma_mm, size=n)
    distances = np.clip(np.rint(np.asarray(ideal) + noise), 0, 4500).astype(int)

    error_mask = rng.random(n) < error_prob
    error_mask[:EDGE_GUARD] = False
    error_mask[max(0, n - EDGE_GUARD):] = False

    boot_id = f"sim_{seed:08x}_{idx}"
    mono_start = int(rng.integers(1_000_000, 10_000_000))
    session_start = _TIME_BASE + timedelta(
        seconds=idx * (n * (US_PER_SAMPLE / 1_000_000) + _SESSION_GAP_S)
    )

    lines: list[dict] = []
    for i in range(n):
        measured_at = _iso_utc(session_start + timedelta(seconds=0.1 * i))
        if error_mask[i]:
            payload = {
                "schema_version": "1.0",
                "device_id": DEVICE_ID,
                "boot_id": boot_id,
                "sequence": i + 1,
                "monotonic_us": mono_start + i * US_PER_SAMPLE,
                "measured_at": measured_at,
                "time_synced": True,
                "distance_mm": None,
                "echo_us": None,
                "valid": False,
                "error_code": ERROR_CODE,
            }
        else:
            mm = int(distances[i])
            echo_us = max(1, int(round(mm * ECHO_US_PER_MM)) + int(rng.integers(-50, 51)))
            payload = {
                "schema_version": "1.0",
                "device_id": DEVICE_ID,
                "boot_id": boot_id,
                "sequence": i + 1,
                "monotonic_us": mono_start + i * US_PER_SAMPLE,
                "measured_at": measured_at,
                "time_synced": True,
                "distance_mm": mm,
                "echo_us": echo_us,
                "valid": True,
                "error_code": None,
            }
        payload["synthetic"] = True
        payload["scenario"] = scenario
        payload["session_label"] = f"sess_{idx}"
        lines.append(payload)
    return lines


def generate_sessions(
    n_sessions: int,
    samples_per_session: int,
    seed: int,
    scenario: str = "all",
    error_prob: float = ERROR_PROB,
) -> list[dict]:
    """Sinh toàn bộ dòng JSONL (SYNTHETIC — xem cảnh báo module docstring).

    Trả về list phẳng theo thứ tự: đủ ``samples_per_session`` mẫu của
    ``sess_0``, rồi ``sess_1``, ... ``scenario="all"`` → session i nhận
    ``SCENARIOS[i % 4]`` (phân bố đều); giá trị khác chọn một kịch bản cho
    mọi session. ``error_prob`` mặc định 0.02; test có thể nâng lên để chắc
    chắn có mẫu lỗi mà không đổi seed.
    """
    if n_sessions < 1:
        raise ValueError(f"n_sessions phải >= 1, nhận {n_sessions}")
    if samples_per_session < 1:
        raise ValueError(f"samples_per_session phải >= 1, nhận {samples_per_session}")
    if seed < 0:
        raise ValueError(f"seed phải >= 0, nhận {seed}")
    if scenario == "all":
        session_scenarios = [SCENARIOS[i % len(SCENARIOS)] for i in range(n_sessions)]
    elif scenario in SCENARIOS:
        session_scenarios = [scenario] * n_sessions
    else:
        raise ValueError(
            f"scenario phải là 'all' hoặc một trong {SCENARIOS}, nhận {scenario!r}"
        )

    rng = np.random.default_rng(seed)
    lines: list[dict] = []
    for idx, scen in enumerate(session_scenarios):
        lines.extend(
            _generate_session(
                idx, scen, samples_per_session, seed, rng, error_prob
            )
        )
    return lines


def write_jsonl(lines: list[dict], out_path: str | Path) -> int:
    """Ghi list dòng ra file JSONL (mỗi dòng một JSON object); trả số dòng."""
    path = Path(out_path)
    if path.parent != Path("."):
        path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")
    return len(lines)


def _summary(lines: list[dict], seed: int) -> dict:
    """Summary stdout: số session, số mẫu, số mẫu lỗi, phân bố scenario, seed."""
    n_error = 0
    scenario_counts: dict[str, int] = {}
    for line in lines:
        if not line["valid"]:
            n_error += 1
        scenario_counts[line["scenario"]] = scenario_counts.get(line["scenario"], 0) + 1
    return {
        "n_sessions": len({line["session_label"] for line in lines}),
        "n_samples": len(lines),
        "n_error_samples": n_error,
        "scenario_counts": dict(sorted(scenario_counts.items())),
        "seed": seed,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI sinh dataset synthetic; in summary JSON ra stdout."""
    parser = argparse.ArgumentParser(
        description=(
            "Sinh dataset SYNTHETIC telemetry_v1 dạng JSONL (dữ liệu tổng hợp, "
            "KHÔNG phải đo thật). Mỗi dòng gồm payload 11 field đúng "
            "schemas/telemetry_v1.schema.json CỘNG 3 trường nhãn metadata: "
            '"synthetic": true, "scenario", "session_label".'
        )
    )
    parser.add_argument(
        "--out",
        required=True,
        help="Đường dẫn file JSONL đầu ra (bắt buộc)",
    )
    parser.add_argument(
        "--sessions",
        type=int,
        default=30,
        help="Số session mô phỏng (mặc định: 30)",
    )
    parser.add_argument(
        "--samples-per-session",
        type=int,
        default=200,
        help="Số mẫu mỗi session, nhịp 0.1 s (mặc định: 200)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Seed RNG — cùng seed cho ra dataset giống hệt nhau (mặc định: 42)",
    )
    parser.add_argument(
        "--scenario",
        choices=["all", *SCENARIOS],
        default="all",
        help=(
            "Kịch bản vận động: approach (tiến gần, có crossing ≤ Sp demo "
            "440 mm), recede (lùi xa), mixed (xen kẽ), noise (nhiễu quanh "
            "0.5–0.9 m, không chạm ngưỡng). Mặc định: all = xen kẽ cả 4 "
            "theo thứ tự session"
        ),
    )
    args = parser.parse_args(argv)

    lines = generate_sessions(
        args.sessions, args.samples_per_session, args.seed, args.scenario
    )
    write_jsonl(lines, args.out)
    print(json.dumps(_summary(lines, args.seed), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# ---------------------------------------------------------------------------
# Ghi chú: nạp dataset này vào DB qua scripts/ingest_jsonl.py
#
# schemas/telemetry_v1.schema.json khai ``additionalProperties: false`` nên
# pipeline ingest CHỈ nhận đúng 11 field payload — 3 trường nhãn metadata
# (synthetic/scenario/session_label) phải được TÁCH RA trước khi nạp. Ví dụ
# smoke (chạy từ repo root):
#
#   .venv/bin/python -m scripts.synthetic_dataset --out /tmp/opencode/sim.jsonl
#   .venv/bin/python - <<'PY'
#   import json
#   META = ("synthetic", "scenario", "session_label")
#   with open("/tmp/opencode/sim.jsonl", encoding="utf-8") as fin, \
#        open("/tmp/opencode/sim_ingest.jsonl", "w", encoding="utf-8") as fout:
#       for raw in fin:
#           row = json.loads(raw)
#           fout.write(json.dumps({k: v for k, v in row.items() if k not in META}) + "\n")
#   PY
#   .venv/bin/python -m scripts.ingest_jsonl --input /tmp/opencode/sim_ingest.jsonl --db /tmp/opencode/sim.db
#
# Phần tách nhãn này cũng chính là việc
# ``ai_model/prepare.py:load_jsonl_dataset`` làm khi chuẩn bị dữ liệu train.
# ---------------------------------------------------------------------------
