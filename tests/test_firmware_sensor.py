"""Exercise real HC-SR04 classification/recovery with simulated GPIO/RMT edges."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def sensor_harness(tmp_path_factory):
    compiler = shutil.which("cc")
    if compiler is None:
        pytest.skip("native C compiler unavailable")
    executable = tmp_path_factory.mktemp("firmware_sensor") / "sensor_harness"
    result = subprocess.run([
        compiler, "-std=c11", "-Wall", "-Wextra", "-Werror",
        "-I", str(ROOT / "tests/firmware/stubs"),
        "-I", str(ROOT / "firmware/esp32/main"),
        str(ROOT / "tests/firmware/sensor_harness.c"),
        str(ROOT / "firmware/esp32/main/hcsr04.c"),
        str(ROOT / "firmware/esp32/main/diagnostics.c"),
        "-o", str(executable),
    ], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return executable


@pytest.mark.parametrize("case", [
    "recovery", "high_timeout", "stuck_high", "too_long", "reset_failure", "trig_recovery",
])
def test_sensor_recovery(sensor_harness, case):
    result = subprocess.run([str(sensor_harness), case], capture_output=True,
                            text=True, timeout=5)
    assert result.returncode == 0, result.stderr
