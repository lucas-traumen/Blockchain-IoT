"""Verify simulation provenance, split isolation, and event support."""
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from ai_model.prepare import METADATA_KEYS, load_jsonl_dataset, validate_split
from scripts.prepare_real_dataset import prepare
from scripts.synthetic_square_dataset import STRATA, generate_bundle


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    root = tmp_path_factory.mktemp("square40")
    first, second = root / "a", root / "b"
    summary = generate_bundle(first, sessions_per_stratum=10, samples_per_session=200, seed=41)
    generate_bundle(second, sessions_per_stratum=10, samples_per_session=200, seed=41)
    return first, second, summary


def test_reproducible_bytes_and_explicit_synthetic_source(bundle):
    first, second, summary = bundle
    assert summary["synthetic"] is True
    assert summary["rows"] == 20_000
    for path in first.iterdir():
        assert path.read_bytes() == (second / path.name).read_bytes()
    records = load_jsonl_dataset(first / "dataset.jsonl")
    assert all(r["meta"]["synthetic"] is True for r in records)
    assert all(r["row"]["boot_id"].startswith("synthetic_") for r in records)
    assert all("ideal_distance_mm" not in r["row"] for r in records)


def test_split_has_positive_and_negative_sessions_without_overlap(bundle):
    first, _, summary = bundle
    split = json.loads((first / "split_manifest.json").read_text())["sessions"]
    assert validate_split(split, summary["session_parameters"]) == {"train": 70, "val": 20, "test": 10}
    for name, ids in split.items():
        support = summary["coverage"][name]
        assert support["positive_sessions"] > 0
        assert support["no_crossing_sessions"] > 0
        assert support["ideal_crossings"] > 0
        assert {(summary["session_parameters"][sid]["scenario"], summary["session_parameters"][sid]["speed_level"]) for sid in ids} == set(STRATA)
        for sid in ids:
            params = summary["session_parameters"][sid]
            if params["scenario"] in ("stop", "stationary", "recede"):
                assert params["ideal_crossings"] == 0


def test_payload_and_fault_rows_conform_to_schema(bundle):
    first, _, _ = bundle
    schema = json.loads((Path(__file__).parents[1] / "schemas/telemetry_v1.schema.json").read_text())
    validator = Draft202012Validator(schema)
    codes = set()
    for filename in ("dataset.jsonl", "sensor-fault-dataset.jsonl"):
        for line in (first / filename).read_text().splitlines():
            row = json.loads(line)
            validator.validate({k: v for k, v in row.items() if k not in METADATA_KEYS})
            assert row["synthetic"] is True
            if row["valid"] is False:
                assert row["distance_mm"] is None
                codes.add(row["error_code"])
    assert codes == {"ECHO_TIMEOUT", "ECHO_STUCK_LOW", "ECHO_STUCK_HIGH", "TRIG_FAIL"}


def test_real_merger_rejects_generated_telemetry(bundle, tmp_path):
    first, _, _ = bundle
    with pytest.raises(ValueError, match="không phải real"):
        prepare(first, tmp_path / "real.jsonl")
    assert not (tmp_path / "real.jsonl").exists()


def test_existing_bundle_is_never_overwritten(bundle):
    first, _, _ = bundle
    with pytest.raises(FileExistsError):
        generate_bundle(first)


@pytest.mark.parametrize("kwargs", [{"sessions_per_stratum": 9}, {"samples_per_session": 199}, {"seed": -1}])
def test_invalid_parameters_fail_before_writing(tmp_path, kwargs):
    out = tmp_path / "invalid"
    with pytest.raises(ValueError):
        generate_bundle(out, **kwargs)
    assert not out.exists()
