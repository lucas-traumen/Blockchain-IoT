"""Kiểm thử M5b: sinh dataset synthetic (scripts/synthetic_dataset.py) +
chuẩn bị/split theo session (ai_model/prepare.py).

Về xác thực schema: ``schemas/telemetry_v1.schema.json`` khai
``additionalProperties: false`` nên payload telemetry phải đứng MỘT MÌNH —
3 trường nhãn (synthetic/scenario/session_label) là nhãn mức JSONL, được tách
ra bằng ``load_jsonl_dataset`` trước khi validate. Validation dùng
``validate_telemetry`` của ``iot_backend/ingestion/validate.py`` — đúng
``Draft202012Validator`` + format checker "date-time" mà pipeline ingest thật
dùng. Dataset test sinh vào ``tmp_path`` (không tạo data thật trong repo).

Cách làm tròn split (largest remainder) được khóa bằng test 10 session →
7/2/1; chi tiết xem docstring ``ai_model/prepare.py``.
"""

import json
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from ai_model.prepare import (
    build_split_manifest,
    load_jsonl_dataset,
    split_by_session,
)
from ai_model.windows import WindowConfig, extract_windows
from iot_backend.ingestion.validate import validate_telemetry
from scripts.synthetic_dataset import (
    EDGE_GUARD,
    SCENARIOS,
    generate_sessions,
    main as generator_main,
    write_jsonl,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

N_SESSIONS = 5
N_SAMPLES = 40
SEED = 42


def _boot_id(idx: int) -> str:
    return f"sim_{SEED:08x}_{idx}"


def _group_by_session(lines: list[dict]) -> dict[str, list[dict]]:
    """Gom dòng JSONL phẳng theo ``session_label``, giữ thứ tự gốc."""
    grouped: dict[str, list[dict]] = {}
    for line in lines:
        grouped.setdefault(line["session_label"], []).append(line)
    return grouped


@pytest.fixture(scope="module")
def dataset_path(tmp_path_factory) -> Path:
    """Dataset nhỏ 5×40, seed 42 — sinh bằng đúng hàm generator mà CLI dùng."""
    lines = generate_sessions(N_SESSIONS, N_SAMPLES, SEED, scenario="all")
    path = tmp_path_factory.mktemp("dataset") / "sim_small.jsonl"
    write_jsonl(lines, path)
    return path


@pytest.fixture(scope="module")
def dataset_records(dataset_path) -> list[dict]:
    """Kết quả ``load_jsonl_dataset``: [{"row": ..., "meta": ...}, ...]."""
    return load_jsonl_dataset(dataset_path)


@pytest.fixture(scope="module")
def window_config() -> WindowConfig:
    """WindowConfig dựng từ configs/model.yaml THẬT (không copy số liệu)."""
    with (REPO_ROOT / "configs" / "model.yaml").open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    return WindowConfig.from_dict(config)


def test_payload_matches_schema_and_labels(dataset_records):
    """Mỗi dòng: payload 11 field thỏa schema sau khi tách nhãn; nhãn đủ."""
    assert len(dataset_records) == N_SESSIONS * N_SAMPLES
    for record in dataset_records:
        row, meta = record["row"], record["meta"]

        # Payload (đã tách nhãn) đúng schema telemetry_v1 như luồng ingest.
        ok, reason = validate_telemetry(row)
        assert ok, reason

        # Nhãn synthetic rõ ràng trên từng dòng.
        assert meta["synthetic"] is True
        assert meta["scenario"] in SCENARIOS
        assert meta["session_label"].startswith("sess_")

        # Nhãn được tách sạch — không sót lại trong payload.
        assert not (set(row) & {"synthetic", "scenario", "session_label"})


def test_session_structure(dataset_records):
    """Mỗi session: sequence 1..N, monotonic +100000µs, measured_at +0.1s,
    time_synced=true, boot_id duy nhất dạng sim_<seed>_<i>."""
    grouped = _group_by_session(
        [{**r["row"], **r["meta"]} for r in dataset_records]
    )
    assert len(grouped) == N_SESSIONS
    boots: set[str] = set()
    for label, lines in grouped.items():
        assert len(lines) == N_SAMPLES
        assert [l["sequence"] for l in lines] == list(range(1, N_SAMPLES + 1))

        monotonic = [l["monotonic_us"] for l in lines]
        assert all(b - a == 100_000 for a, b in zip(monotonic, monotonic[1:]))

        times = [datetime.fromisoformat(l["measured_at"]) for l in lines]
        assert all(
            (b - a).total_seconds() == 0.1 for a, b in zip(times, times[1:])
        )
        assert all(l["time_synced"] is True for l in lines)

        boot = lines[0]["boot_id"]
        assert boot.startswith("sim_")
        assert all(l["boot_id"] == boot for l in lines)
        boots.add(boot)
        assert lines[0]["device_id"] == "esp32_sim"

    # boot_id duy nhất mỗi session.
    assert len(boots) == N_SESSIONS


def test_windows_extractable_per_session(dataset_records, window_config):
    """Mỗi session đủ dài cho ít nhất 1 cửa sổ; không drop gap/jitter."""
    rows = [record["row"] for record in dataset_records]
    windows, stats = extract_windows(rows, window_config)

    assert stats.n_windows > 0
    # Row JSONL không có session_id → nhóm theo (None, device, boot); boot
    # duy nhất mỗi session nên đếm theo boot_id là đếm theo session.
    assert {w.meta.boot_id for w in windows} == {
        _boot_id(i) for i in range(N_SESSIONS)
    }
    # Nhịp 0.1s đều + sequence liên tiếp → chỉ có drop do mẫu lỗi (nếu có).
    assert stats.n_dropped_gap == 0
    assert stats.n_dropped_jitter == 0
    n_invalid = sum(1 for r in rows if not r["valid"])
    assert stats.n_dropped_error == n_invalid


def test_error_samples_not_at_edges():
    """Mẫu lỗi (ECHO_TIMEOUT) không nằm trong 5 mẫu đầu/cuối session."""
    # error_prob cao để chắc chắn có mẫu lỗi bất kể seed; seed cố định nên
    # kết quả deterministic.
    lines = generate_sessions(3, 60, SEED, scenario="mixed", error_prob=0.5)
    grouped = _group_by_session(lines)

    total_errors = 0
    for label, session_lines in grouped.items():
        n = len(session_lines)
        for i, line in enumerate(session_lines):
            if line["valid"]:
                continue
            total_errors += 1
            assert EDGE_GUARD <= i < n - EDGE_GUARD
            assert line["distance_mm"] is None
            assert line["echo_us"] is None
            assert line["error_code"] == "ECHO_TIMEOUT"
            # Nhịp thời gian vẫn tiến đều qua mẫu lỗi.
            assert line["sequence"] == i + 1
    assert total_errors > 0


def test_default_dataset_errors_respect_edges(dataset_records):
    """Mọi mẫu lỗi trong dataset mặc định (nếu có) cũng nằm ngoài biên."""
    grouped = _group_by_session(
        [{**r["row"], **r["meta"]} for r in dataset_records]
    )
    for lines in grouped.values():
        n = len(lines)
        for i, line in enumerate(lines):
            if not line["valid"]:
                assert EDGE_GUARD <= i < n - EDGE_GUARD


def test_approach_sessions_cross_below_sp_demo(dataset_records):
    """Kịch bản approach có ít nhất một crossing xuống ≤ 440mm (Sp demo)."""
    grouped = _group_by_session(
        [{**r["row"], **r["meta"]} for r in dataset_records]
    )
    approach_sessions = [
        lines
        for lines in grouped.values()
        if lines[0]["scenario"] == "approach"
    ]
    assert approach_sessions, "dataset all phải có session approach"
    mins = [
        min(l["distance_mm"] for l in lines if l["valid"])
        for lines in approach_sessions
    ]
    assert min(mins) <= 440


def test_split_by_session_deterministic():
    """10 session → 7/2/1 (largest remainder), tổng đủ, không giao, cùng
    seed chạy 2 lần ra cùng split."""
    keys = [{"session_label": f"sess_{i}"} for i in range(10)]
    first = split_by_session(keys, seed=SEED)
    second = split_by_session(keys, seed=SEED)
    assert first == second

    assert set(first) == {"train", "val", "test"}
    assert all(isinstance(v, set) for v in first.values())
    train, val, test = first["train"], first["val"], first["test"]
    assert not (train & val)
    assert not (train & test)
    assert not (val & test)
    assert len(train | val | test) == 10
    # Làm tròn largest remainder: 7.0/1.5/1.5 → 7/2/1 (hòa .5 ưu tiên val).
    assert (len(train), len(val), len(test)) == (7, 2, 1)


def test_split_works_on_generated_dataset(dataset_records):
    """Split nhận trực tiếp output load_jsonl_dataset (key = session_label)."""
    split = split_by_session(dataset_records, seed=SEED)
    labels = {r["meta"]["session_label"] for r in dataset_records}
    assert split["train"] | split["val"] | split["test"] == labels
    assert not (split["train"] & split["val"])


def test_split_rejects_bad_input():
    """Ratios sai / item thiếu session key / list rỗng → ValueError."""
    keys = [{"session_label": "sess_0"}]
    with pytest.raises(ValueError):
        split_by_session(keys, seed=1, ratios=(0.8, 0.1))  # thiếu ratio
    with pytest.raises(ValueError):
        split_by_session(keys, seed=1, ratios=(0.7, 0.2, 0.2))  # tổng ≠ 1
    with pytest.raises(ValueError):
        # Telemetry row thuần không có session_label/session_id.
        split_by_session([{"device_id": "x", "sequence": 1}], seed=1)
    with pytest.raises(ValueError):
        split_by_session([], seed=1)


def test_split_group_key_hook():
    """Hook group_key: session cùng buổi vào cùng split (nguyên nhóm)."""
    keys = [{"session_label": f"sess_{i}"} for i in range(10)]
    buoi_1 = {f"sess_{i}" for i in range(5)}
    buoi_2 = {f"sess_{i}" for i in range(5, 10)}
    split = split_by_session(
        keys,
        seed=SEED,
        group_key=lambda s: "buoi_1" if s in buoi_1 else "buoi_2",
    )
    all_keys = split["train"] | split["val"] | split["test"]
    assert all_keys == buoi_1 | buoi_2
    for members in (buoi_1, buoi_2):
        containing = [s for s in ("train", "val", "test") if split[s] & members]
        assert len(containing) == 1  # nguyên buổi nằm trọn trong một split

    # group_key nhận diện (mỗi session một nhóm) → đếm khớp trường hợp không
    # nhóm: 10 session → 7/2/1.
    by_identity = split_by_session(keys, seed=SEED, group_key=lambda s: s)
    assert (len(by_identity["train"]), len(by_identity["val"]), len(by_identity["test"])) == (7, 2, 1)


def test_build_split_manifest():
    """Manifest: seed, ratios, n_sessions, sessions sorted, created_at."""
    keys = [{"session_label": f"sess_{i}"} for i in range(10)]
    split = split_by_session(keys, seed=SEED)
    manifest = build_split_manifest(
        split, seed=SEED, created_at="2026-01-01T00:00:00.000Z"
    )
    assert manifest["seed"] == SEED
    assert manifest["ratios"] == [0.7, 0.15, 0.15]
    assert manifest["n_sessions"] == 10
    assert manifest["sessions"] == {
        name: sorted(ids) for name, ids in split.items()
    }
    assert manifest["created_at"] == "2026-01-01T00:00:00.000Z"
    # Sẵn sàng hash deterministically cho model manifest (M5c).
    json.dumps(manifest, sort_keys=True)


def test_cli_main_writes_dataset(tmp_path, capsys):
    """CLI wrapper: ghi file đủ dòng, in summary JSON, scenario noise không
    chạm ngưỡng Sp demo."""
    out = tmp_path / "cli_sim.jsonl"
    exit_code = generator_main(
        [
            "--out", str(out),
            "--sessions", "2",
            "--samples-per-session", "30",
            "--seed", "7",
            "--scenario", "noise",
        ]
    )
    assert exit_code == 0
    lines = [
        json.loads(raw) for raw in out.read_text(encoding="utf-8").splitlines()
    ]
    assert len(lines) == 60

    summary = json.loads(capsys.readouterr().out)
    assert summary["n_sessions"] == 2
    assert summary["n_samples"] == 60
    assert summary["seed"] == 7
    assert summary["scenario_counts"] == {"noise": 60}

    mins = [
        min(l["distance_mm"] for l in lines if l["valid"])
    ]
    assert min(mins) > 440  # noise không chạm ngưỡng Sp demo
