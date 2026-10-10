import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
import yaml

from scripts.synthetic_v2 import generate_release


CONFIG = "configs/simulation/square-bench-v2.yaml"


def test_dry_run_uses_v2_contract():
    result = generate_release(CONFIG, "/tmp/unused", seed=7, dry_run=True)
    assert result == {"dry_run": True, "forecast_sessions": 10, "fault_sessions": 5, "seed": 7}


def test_release_is_reproducible_and_separates_ground_truth(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    a = generate_release(CONFIG, first, seed=17, sessions_per_scenario=3, fault_sessions=1)
    b = generate_release(CONFIG, second, seed=17, sessions_per_scenario=3, fault_sessions=1)
    assert a["dataset_sha256"] == b["dataset_sha256"]
    assert (first / "dataset.jsonl").read_bytes() == (second / "dataset.jsonl").read_bytes()
    assert (first / "telemetry.jsonl").read_bytes() == (second / "telemetry.jsonl").read_bytes()
    dataset_row = json.loads((first / "dataset.jsonl").read_text().splitlines()[0])
    telemetry_row = json.loads((first / "telemetry.jsonl").read_text().splitlines()[0])
    assert "distance_true_m" in dataset_row
    assert "distance_true_m" not in telemetry_row
    assert "target_d100_m" not in telemetry_row
    assert dataset_row["simulation_mode"] is True


def test_split_is_session_disjoint_and_schema_valid(tmp_path):
    out = tmp_path / "release"
    generate_release(CONFIG, out, seed=19, sessions_per_scenario=4, fault_sessions=1)
    split = json.loads((out / "split_manifest.json").read_text())
    sets = [set(split["session_splits"][name]) for name in ("train", "val", "test")]
    assert not (sets[0] & sets[1] or sets[0] & sets[2] or sets[1] & sets[2])
    schema = json.loads(Path("schemas/synthetic_v2.schema.json").read_text())
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    rows = [json.loads(line) for line in (out / "dataset.jsonl").read_text().splitlines()]
    for row in rows[:50] + rows[-50:]:
        assert list(validator.iter_errors(row)) == []


def test_invalid_profile_is_rejected(tmp_path):
    config = yaml.safe_load(Path(CONFIG).read_text())
    config["threshold_m"] = 0.44
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump(config), encoding="utf-8")
    with pytest.raises(ValueError, match="threshold_m"):
        generate_release(bad, tmp_path / "out", dry_run=True)
