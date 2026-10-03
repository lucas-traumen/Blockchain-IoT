"""Exercise the real serial setup workflow and fault policy with scripted I/O."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def setup_harness(tmp_path_factory):
    compiler = shutil.which("cc")
    if compiler is None:
        pytest.skip("native C compiler unavailable")
    executable = tmp_path_factory.mktemp("firmware") / "setup_harness"
    subprocess.run([
        compiler, "-std=c11", "-Wall", "-Wextra", "-Werror",
        "-I", str(ROOT / "iot_code/esp32/main"),
        str(ROOT / "tests/firmware/setup_harness.c"),
        str(ROOT / "iot_code/esp32/main/network_setup.c"),
        str(ROOT / "iot_code/esp32/main/diagnostics.c"),
        "-o", str(executable),
    ], check=True, capture_output=True, text=True)
    return executable


@pytest.mark.parametrize("case", [
    "join", "crlf", "open", "invalid_index", "overflow", "cancel",
    "hidden", "full_ssid", "invalid_broker", "broker", "unsaved", "faults",
])
def test_serial_setup(setup_harness, case):
    subprocess.run([str(setup_harness), case], check=True, capture_output=True,
                   text=True, timeout=5)
