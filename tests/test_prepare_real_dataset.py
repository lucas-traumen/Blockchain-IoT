"""Check measurement preservation and contamination guards of the real merger."""
import hashlib
import json

import pytest

from scripts.prepare_real_dataset import prepare


def sample(sequence=1, **changes):
    return {
        "schema_version": "1.0", "device_id": "esp32_01", "boot_id": "real_boot",
        "sequence": sequence, "monotonic_us": sequence * 100000,
        "measured_at": None, "time_synced": False, "distance_mm": 123,
        "echo_us": 717, "valid": True, "error_code": None,
        "synthetic": False, "session_label": "s1", "scenario": "recede",
        "target_type": "person", "speed_level": "mdeium", **changes,
    }


def write_dataset(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_preserves_measurements_invalid_and_source_files(tmp_path):
    source = tmp_path / "input" / "one" / "dataset.jsonl"
    rows = [sample(), sample(2, valid=False, distance_mm=None, echo_us=None, error_code="ECHO_STUCK_LOW")]
    write_dataset(source, rows)
    original_bytes = source.read_bytes()
    out = tmp_path / "prepared.jsonl"
    summary = prepare(tmp_path / "input", out)
    result = [json.loads(x) for x in out.read_text().splitlines()]
    assert result == [{**r, "speed_level": "medium"} for r in rows]
    assert summary["invalid_rows"] == 1
    assert summary["sessions_detail"]["s1"]["speed_level"] == "medium"
    assert summary["dataset_sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()
    assert summary["normalized_mdeium_to_medium"] == 2
    assert source.read_bytes() == original_bytes


@pytest.mark.parametrize("changes", [{"synthetic": True}, {"boot_id": "sim_boot"}, {"boot_id": "synthetic_boot"}])
def test_rejects_synthetic_before_writing_output(tmp_path, changes):
    write_dataset(tmp_path / "input" / "one" / "dataset.jsonl", [sample(**changes)])
    out = tmp_path / "prepared.jsonl"
    with pytest.raises(ValueError, match="không phải real"):
        prepare(tmp_path / "input", out)
    assert not out.exists()


def test_duplicate_samples_cannot_cross_sessions(tmp_path):
    write_dataset(tmp_path / "input" / "one" / "dataset.jsonl", [sample()])
    write_dataset(tmp_path / "input" / "two" / "dataset.jsonl", [sample(session_label="s2")])
    with pytest.raises(ValueError, match="Trùng sample"):
        prepare(tmp_path / "input", tmp_path / "prepared.jsonl")


def test_duplicate_measurement_timestamp_cannot_cross_sessions(tmp_path):
    measured_at = "2026-10-09T00:00:00.000Z"
    first = sample(measured_at=measured_at, time_synced=True)
    second = sample(2, measured_at=measured_at, time_synced=True, session_label="s2")
    write_dataset(tmp_path / "input" / "one" / "dataset.jsonl", [first])
    write_dataset(tmp_path / "input" / "two" / "dataset.jsonl", [second])
    with pytest.raises(ValueError, match="Trùng phép đo"):
        prepare(tmp_path / "input", tmp_path / "prepared.jsonl")
