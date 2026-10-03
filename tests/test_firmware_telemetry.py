"""Console distance logs and sampling continue when MQTT admission fails."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("case,interval", [("valid", 1000), ("invalid", 1000),
                                          ("too_far", 1000), ("disabled", 0)])
def test_distance_logging(tmp_path, case, interval):
    compiler = shutil.which("cc")
    if compiler is None:
        pytest.skip("native C compiler unavailable")
    executable = tmp_path / "telemetry"
    build = subprocess.run([
        compiler, "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-Wall", "-Wextra", "-Werror",
        f"-DCONFIG_HRC_DISTANCE_LOG_PERIOD_MS={interval}",
        "-I", str(ROOT / "tests/firmware/stubs"),
        "-I", str(ROOT / "iot_code/esp32/main"),
        str(ROOT / "tests/firmware/telemetry_harness.c"),
        str(ROOT / "iot_code/esp32/main/telemetry.c"),
        str(ROOT / "iot_code/esp32/main/diagnostics.c"),
        "-o", str(executable),
    ], capture_output=True, text=True)
    assert build.returncode == 0, build.stderr
    result = subprocess.run([str(executable), case], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
