"""Drive the real firmware network manager against simulated SDK event/NVS I/O."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def network_harness(tmp_path_factory):
    compiler = shutil.which("cc")
    if compiler is None:
        pytest.skip("native C compiler unavailable")
    directory = tmp_path_factory.mktemp("firmware_network")
    executable = directory / "network_harness"
    config = directory / "network_fixture.h"
    config.write_text("#define HRC_WIFI_LIST_MODE 0\n")
    result = subprocess.run([
        compiler, "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-Wall", "-Wextra", "-Werror",
        f'-DHRC_NETWORK_CONFIG_HEADER="{config}"',
        "-I", str(ROOT / "tests/firmware/stubs"),
        "-I", str(ROOT / "iot_code/esp32/main"),
        str(ROOT / "tests/firmware/network_harness.c"),
        str(ROOT / "iot_code/esp32/main/wifi_mqtt.c"),
        str(ROOT / "iot_code/esp32/main/network_setup.c"),
        "-o", str(executable),
    ], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return executable


@pytest.mark.parametrize("case", [
    "blank", "join", "failed_join", "full_ssid", "saved_boot", "scan_retry",
    "backoff", "broker_deferred", "broker_active", "outbox", "mqtt_nonblocking",
    "mqtt_headroom", "mqtt_subscribe_retry", "mqtt_suback_gate", "mqtt_offline",
    "mqtt_invalid", "mqtt_puback_watchdog", "mqtt_puback_progress",
    "mqtt_suback_rejected", "mqtt_suback_timeout", "mqtt_sdk_retry",
    "mqtt_ack_priority", "mqtt_payload_copy",
])
def test_network_manager(network_harness, case):
    result = subprocess.run([str(network_harness), case], capture_output=True,
                            text=True, timeout=5)
    assert result.returncode == 0, result.stderr
