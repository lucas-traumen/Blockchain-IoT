"""Check real console adapter receives bytes from UART and native USB ports."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("uart,usb,source", [
    (1, 0, "uart"), (1, 1, "uart"), (1, 1, "usb"), (0, 1, "usb"),
    (1, 0, "failure"), (0, 1, "failure"),
])
def test_console_port(tmp_path, uart, usb, source):
    compiler = shutil.which("cc")
    if compiler is None:
        pytest.skip("native C compiler unavailable")
    executable = tmp_path / "console"
    build = subprocess.run([
        compiler, "-std=c11", "-Wall", "-Wextra", "-Werror",
        "-DCONFIG_HRC_SERIAL_SETUP=1", f"-DCONFIG_ESP_CONSOLE_UART={uart}",
        f"-DCONFIG_ESP_CONSOLE_USB_SERIAL_JTAG={int(usb and not uart)}",
        f"-DCONFIG_ESP_CONSOLE_USB_SERIAL_JTAG_ENABLED={usb}",
        "-DCONFIG_ESP_CONSOLE_UART_NUM=0", "-DCONFIG_ESP_CONSOLE_UART_BAUDRATE=115200",
        "-I", str(ROOT / "tests/firmware/stubs"),
        "-I", str(ROOT / "firmware/esp32/main"),
        str(ROOT / "tests/firmware/console_harness.c"),
        str(ROOT / "firmware/esp32/main/network_console.c"),
        "-o", str(executable),
    ], capture_output=True, text=True)
    assert build.returncode == 0, build.stderr
    result = subprocess.run([str(executable), source], capture_output=True,
                            text=True, timeout=5)
    assert result.returncode == 0, result.stderr
