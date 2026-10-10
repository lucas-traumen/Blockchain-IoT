"""Exercise scan/select/fallback/reconnect through the real Wi-Fi manager."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
NETWORKS = [
    ["Weak_AP", "mock-password"],
    ["Strong_AP", "strong-password"],
    ["Good_AP", "mock-password"],
    ["Open_AP", ""],
    ["12345678901234567890123456789012", "mock-password"],
    ["Invalid_AP", "short"],
]


@pytest.mark.parametrize("case", [
    "best", "fallback", "missing", "many_aps", "full_ssid", "open",
    "unsupported", "duplicate", "disconnect", "all_fail", "scan_failure",
    "blank", "invalid", "record_failure",
])
def test_known_wifi_list(tmp_path, case):
    compiler = shutil.which("cc")
    if compiler is None:
        pytest.skip("native C compiler unavailable")
    networks = NETWORKS
    if case == "duplicate":
        networks = [["Strong_AP", "wrong-password"], ["Strong_AP", "strong-password"]]
    elif case == "blank":
        networks = [["", ""], ["", ""]]
    elif case == "invalid":
        networks = [["Strong_AP", "short"], ["Weak_AP", "short"]]
    rows = ",".join("{" + ",".join(json.dumps(value) for value in row) + "}" for row in networks)
    config = tmp_path / "network_fixture.h"
    config.write_text(
        "#define HRC_WIFI_LIST_MODE 1\n"
        f"#define HRC_WIFI_NETWORKS {{{rows}}}\n"
        '#define HRC_MQTT_URI "mqtt://macro-broker.test:1885"\n'
    )
    executable = tmp_path / "network_harness"
    build = subprocess.run([
        compiler, "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-Wall", "-Wextra", "-Werror",
        f'-DHRC_NETWORK_CONFIG_HEADER="{config}"',
        "-I", str(ROOT / "tests/firmware/stubs"),
        "-I", str(ROOT / "firmware/esp32/main"),
        str(ROOT / "tests/firmware/network_harness.c"),
        str(ROOT / "firmware/esp32/main/wifi_mqtt.c"),
        str(ROOT / "firmware/esp32/main/network_setup.c"),
        "-o", str(executable),
    ], capture_output=True, text=True)
    assert build.returncode == 0, build.stderr
    result = subprocess.run([str(executable), "list_" + case], capture_output=True,
                            text=True, timeout=5)
    assert result.returncode == 0, result.stderr
