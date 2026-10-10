"""Native parity and fail-safe tests for the generated ESP32 LSTM core."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "ai_model/artifacts/lstm-v1/model.keras"
SCALER = ROOT / "ai_model/artifacts/lstm-v1/scaler.json"
SOURCE = ROOT / "firmware/esp32/main/lstm_inference.c"
HEADER = ROOT / "firmware/esp32/main/lstm_inference.h"
HARNESS = ROOT / "tests/firmware/lstm_harness.c"
GENERATOR = ROOT / "scripts/generate_esp32_lstm.py"

# Generated independently from model.keras with the Keras float32 tensor
# contract (input [0.180, ..., 0.315] m, delta[0]=0). Keep this vector in the
# test so the native check does not call the implementation under test.
REFERENCE_FORECAST_M = [
    0.213127851,
    0.218651801,
    0.216729075,
    0.221079051,
    0.221869230,
]

REFERENCE_SLIDING_FORECAST_M = [
    0.221045554,
    0.226572514,
    0.223114431,
    0.227835357,
    0.227717817,
]


@pytest.fixture(scope="module")
def native_binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    compiler = shutil.which("cc")
    if compiler is None:
        pytest.skip("native C compiler unavailable")
    output = tmp_path_factory.mktemp("firmware-lstm") / "lstm_harness"
    build = subprocess.run(
        [
            compiler,
            "-std=c11",
            "-D_POSIX_C_SOURCE=200809L",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-pedantic",
            "-I",
            str(ROOT / "tests/firmware/stubs"),
            "-I",
            str(ROOT / "firmware/esp32/main"),
            str(HARNESS),
            str(SOURCE),
            "-lm",
            "-o",
            str(output),
        ],
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    return output


def run_case(binary: Path, case: str) -> str:
    result = subprocess.run(
        [str(binary), case], capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_generated_source_contains_artifact_hash() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    assert "model.keras sha256=94d017fa94ca25ee22bacba740ac37102bd1537c82b009a61817cb62506b2b52" in source
    assert HEADER.exists()


def test_generated_source_is_reproducible(tmp_path: Path) -> None:
    output = tmp_path / "lstm_inference.c"
    output.write_text(SOURCE.read_text(encoding="utf-8"), encoding="utf-8")
    generated = subprocess.run(
        [
            shutil.which("python3") or "python3",
            str(GENERATOR),
            "--artifact",
            str(ARTIFACT),
            "--scaler",
            str(SCALER),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        env={**__import__("os").environ, "PYTHONPATH": "/tmp/hrc-h5py"},
    )
    if generated.returncode != 0 and "No module named 'h5py'" in generated.stderr:
        pytest.skip("h5py is required only on the model-generation machine")
    assert generated.returncode == 0, generated.stderr
    assert output.read_text(encoding="utf-8") == SOURCE.read_text(encoding="utf-8")


def test_native_matches_artifact_reference(native_binary: Path) -> None:
    output = run_case(native_binary, "vector")
    line = next(line for line in output.splitlines() if line.startswith("forecast="))
    values = [float(value) for value in line.removeprefix("forecast=").split(",")]
    assert len(values) == len(REFERENCE_FORECAST_M)
    for actual, expected in zip(values, REFERENCE_FORECAST_M):
        assert actual == pytest.approx(expected, abs=5e-5)


def test_native_sliding_window_matches_artifact_reference(native_binary: Path) -> None:
    output = run_case(native_binary, "sliding")
    line = next(line for line in output.splitlines() if line.startswith("forecast="))
    values = [float(value) for value in line.removeprefix("forecast=").split(",")]
    assert len(values) == len(REFERENCE_SLIDING_FORECAST_M)
    for actual, expected in zip(values, REFERENCE_SLIDING_FORECAST_M):
        assert actual == pytest.approx(expected, abs=5e-5)


@pytest.mark.parametrize("case", ["warmup", "invalid", "gap", "threshold"])
def test_native_window_and_threshold_cases(native_binary: Path, case: str) -> None:
    run_case(native_binary, case)
