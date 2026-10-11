import json

from scripts.prepare_synthetic_v2 import prepare_release
from scripts.synthetic_v2 import generate_release


CONFIG = "configs/simulation/square-bench-v2.yaml"


def test_prepared_views_keep_runtime_fields_separate_from_ground_truth(tmp_path):
    release = tmp_path / "release"
    prepared = tmp_path / "prepared"
    generate_release(CONFIG, release, seed=31, sessions_per_scenario=3, fault_sessions=1)
    result = prepare_release(release, prepared)

    assert result["training_rows"] > 0
    assert result["fault_rows"] > 0
    train_row = json.loads((prepared / "training_dataset.jsonl").read_text().splitlines()[0])
    fault_row = json.loads((prepared / "fault_dataset.jsonl").read_text().splitlines()[0])
    for row in (train_row, fault_row):
        assert row["synthetic"] is True
        assert "distance_true_m" not in row
        assert "target_d100_m" not in row
        assert "crossing_h500" not in row
        assert row["device_id"] == "sim-v2"
    assert train_row["target_type"] == "forecast"
    assert fault_row["target_type"] == "fault"


def test_prepared_split_excludes_fault_sessions(tmp_path):
    release = tmp_path / "release"
    prepared = tmp_path / "prepared"
    generate_release(CONFIG, release, seed=32, sessions_per_scenario=3, fault_sessions=1)
    prepare_release(release, prepared)

    manifest = json.loads((prepared / "training_split_manifest.json").read_text())
    sessions = set().union(*[set(values) for values in manifest["sessions"].values()])
    assert sessions
    assert not sessions.intersection(manifest["fault_sessions_excluded_from_training"])
