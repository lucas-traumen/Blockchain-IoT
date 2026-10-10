"""Collection boundaries, label provenance, interruption, and HTTP integration."""

import io
import json
import os
import urllib.error
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker

os.environ.setdefault("HRC_APP_AUTOCREATE", "0")

from ai_model.prepare import build_lstm_dataset, load_jsonl_dataset
from ai_model.windows import WindowConfig
from scripts.collect_dataset import (
    ApiError, CollectionError, CollectionRunner, HttpSessionApi,
    SessionSpec, _interactive_loop, load_plan, main, validate_collected_rows,
)

ROOT = Path(__file__).resolve().parents[1]


def payload(sequence, **overrides):
    return {
        "schema_version": "1.0", "device_id": "esp32_01", "boot_id": "boot-1",
        "sequence": sequence, "monotonic_us": sequence * 100_000,
        "measured_at": None, "time_synced": False, "distance_mm": 600,
        "echo_us": 3500, "valid": True, "error_code": None, **overrides,
    }


def session_info(status="closed", session_id="run_1"):
    return {
        "session_id": session_id, "device_id": "esp32_01", "scenario": "approach",
        "object": "person", "zone_note": None, "status": status,
        "start_boot_id": "boot-1", "start_sequence": 11,
        "stop_sequence": 14 if status == "closed" else None,
        "boundary_source": "device_ack", "boot_change": False,
    }


class FakeClock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeApi:
    base_url = "http://fake-iot_backend"

    def __init__(self):
        self.calls = []
        self.rows = [payload(seq) for seq in range(11, 15)]
        self.existing = []
        self.start_info = session_info("running")
        self.stop_info = session_info()

    def health(self):
        return {"status": "ok", "mqtt": True}

    def list_sessions(self):
        return self.existing

    def start_session(self, request):
        self.calls.append(("start", dict(request)))
        return {**self.start_info, "session_id": request["session_id"]}

    def stop_session(self, session_id):
        self.calls.append(("stop", session_id))
        return {**self.stop_info, "session_id": session_id}

    def get_session(self, session_id):
        return {**self.stop_info, "session_id": session_id}

    def export_session(self, session_id):
        return list(self.rows)


def make_runner(tmp_path, api=None, **overrides):
    clock = FakeClock()
    params = dict(
        poll_s=0.1, settle_timeout_s=1, sleep_fn=clock.sleep,
        clock=clock, output_fn=lambda message: None,
    )
    params.update(overrides)
    return CollectionRunner(api or FakeApi(), tmp_path / "out", "esp32_01", **params)


def spec(**overrides):
    return SessionSpec(**{
        "scenario": "approach", "target_type": "person", "speed_level": "slow",
        "duration_s": 0.1, "session_id": "run_1", **overrides,
    })


def collect(runner, **overrides):
    return runner.collect_one(spec(**overrides), prompt=False, non_interactive=True)


def test_labels_preserve_wire_payload_and_loader_contract(tmp_path):
    api = FakeApi()
    result = collect(make_runner(tmp_path, api))
    assert result.quality_ok and result.rows == 4
    assert [name for name, _ in api.calls] == ["start", "stop"]
    request = api.calls[0][1]
    assert request == {"session_id": "run_1", "scenario": "approach", "object": "person", "zone_note": None}
    records = load_jsonl_dataset(tmp_path / "out/dataset.jsonl")
    assert [r["row"] for r in records] == api.rows
    assert all(r["meta"] == {
        "session_label": "run_1", "synthetic": False, "scenario": "approach",
        "target_type": "person", "speed_level": "slow",
    } for r in records)
    manifest = json.loads((tmp_path / "out/manifest.json").read_text())
    assert manifest["attempts"][0]["state"] == "exported"
    assert manifest["quality_pass"] == 1
    assert manifest["sessions"][0]["boundary"]["stop_boundary_source"] == "not_exposed_by_api"


def test_waits_for_middle_sample_even_when_stop_sequence_arrived(tmp_path):
    class DelayedApi(FakeApi):
        def __init__(self):
            super().__init__()
            self.exports = 0

        def export_session(self, session_id):
            self.exports += 1
            return self.rows if self.exports >= 3 else [payload(11), payload(13), payload(14)]

    api = DelayedApi()
    result = collect(make_runner(tmp_path, api))
    assert api.exports == 3
    assert result.quality_ok and result.rows == 4


def test_timeout_keeps_missing_samples_in_manifest(tmp_path):
    api = FakeApi()
    api.rows = [payload(11), payload(13)]
    result = collect(make_runner(tmp_path, api))
    assert result.rows == 2 and not result.quality_ok
    assert result.quality["missing_sequences"] == 2
    assert result.quality["timing_error_count"] == 1


@pytest.mark.parametrize("mutation,issue", [
    ({"boot_change": True}, "boot_change"),
    ({"boundary_source": "db_fallback"}, "db_fallback"),
])
def test_quality_includes_boundary_flags(mutation, issue):
    _, _, issues = validate_collected_rows(
        [payload(seq) for seq in range(11, 15)], {**session_info(), **mutation}, spec(),
    )
    assert any(issue in message for message in issues)


def test_timing_uses_config_and_invalid_samples_are_preserved():
    rows = [payload(11), payload(12, monotonic_us=1_240_000), payload(13), payload(14,
        valid=False, distance_mm=None, echo_us=None, error_code="ECHO_TIMEOUT")]
    labelled, quality, issues = validate_collected_rows(rows, session_info(), spec())
    assert len(labelled) == 4 and labelled[-1]["valid"] is False
    assert quality["timing_error_count"] == 2
    assert quality["invalid_count"] == 1
    assert issues


@pytest.mark.parametrize("rows,match", [
    ([{"sequence": 11}], "schema"),
    ([payload(11, boot_id="synthetic_fixture")], "simulator"),
    ([payload(11, boot_id="sim_fixture")], "simulator"),
    ([payload(10)], "ngoài boundary"),
    ([payload(11), payload(11)], "trùng sample key"),
    ([payload(11, device_id="another")], "device/boot"),
])
def test_rejects_wrong_source_before_label_assignment(rows, match):
    with pytest.raises(CollectionError, match=match):
        validate_collected_rows(rows, session_info(), spec())


def test_empty_session_is_never_quality_pass():
    _, quality, issues = validate_collected_rows([], {**session_info(), "stop_sequence": 10}, spec())
    assert quality["rows"] == 0
    assert issues == ["session không có mẫu"]


def test_interruption_sends_single_stop_and_exports_partial_run(tmp_path):
    prompts = []

    def interrupt_on_stop(prompt):
        prompts.append(prompt)
        if len(prompts) == 2:
            raise KeyboardInterrupt
        return ""

    api = FakeApi()
    runner = make_runner(tmp_path, api, input_fn=interrupt_on_stop)
    with pytest.raises(KeyboardInterrupt):
        runner.collect_one(spec(duration_s=None))
    assert [name for name, _ in api.calls] == ["start", "stop"]
    assert runner.results[0].quality_ok is False
    manifest = json.loads(runner.manifest_path.read_text())
    assert manifest["attempts"][0]["state"] == "interrupted"
    assert runner.combined_path.exists()


@pytest.mark.parametrize("stage", ["start", "stop"])
def test_boundary_timeout_is_journalled_without_retry(tmp_path, stage):
    class TimeoutApi(FakeApi):
        def start_session(self, request):
            result = super().start_session(request)
            journal = json.loads((tmp_path / "out/manifest.json").read_text())
            assert journal["attempts"][0]["spec"]["speed_level"] == "slow"
            assert journal["attempts"][0]["state"] == "start_requested"
            if stage == "start":
                raise ApiError("POST", "/sessions/start", "timed out")
            return result

        def stop_session(self, session_id):
            super().stop_session(session_id)
            raise ApiError("POST", "/sessions/run_1/stop", "timed out")

    api = TimeoutApi()
    runner = make_runner(tmp_path, api)
    with pytest.raises(ApiError, match="timed out"):
        collect(runner)
    assert [name for name, _ in api.calls] == (["start"] if stage == "start" else ["start", "stop"])
    manifest = json.loads(runner.manifest_path.read_text())
    assert manifest["attempts"][0]["state"] == "needs_reconciliation"
    assert not runner.combined_path.exists()


def test_start_fallback_aborts_before_physical_collection(tmp_path):
    api = FakeApi()
    api.start_info["boundary_source"] = "db_fallback"
    runner = make_runner(tmp_path, api)
    with pytest.raises(CollectionError, match="chưa nhận ACK"):
        collect(runner)
    assert [name for name, _ in api.calls] == ["start", "stop"]
    assert not runner.combined_path.exists()


def test_interactive_continues_after_confirmed_closed_failed_run(tmp_path, monkeypatch):
    class FirstStartFallback(FakeApi):
        def start_session(self, request):
            result = super().start_session(request)
            if len(self.calls) == 1:
                result["boundary_source"] = "db_fallback"
            return result

    api = FirstStartFallback()
    messages = []
    runner = make_runner(tmp_path, api, input_fn=lambda prompt: "", output_fn=messages.append)
    choices = iter([spec(session_id="failed_1"), spec(session_id="ok_2"), None])
    monkeypatch.setattr("scripts.collect_dataset.prompt_spec", lambda: next(choices))
    assert _interactive_loop(runner) == 0
    assert [(name, value["session_id"] if name == "start" else value)
            for name, value in api.calls] == [
                ("start", "failed_1"), ("stop", "failed_1"),
                ("start", "ok_2"), ("stop", "ok_2"),
            ]
    manifest = json.loads(runner.manifest_path.read_text())
    assert [attempt["state"] for attempt in manifest["attempts"]] == ["failed", "exported"]
    assert [result.session_id for result in runner.results] == ["ok_2"]
    assert manifest["quality_pass"] == 1


@pytest.mark.parametrize("stage", ["start", "cleanup_stop"])
def test_interactive_stops_on_ambiguous_boundary(tmp_path, monkeypatch, stage):
    class TimeoutApi(FakeApi):
        def start_session(self, request):
            result = super().start_session(request)
            if stage == "start":
                raise ApiError("POST", "/sessions/start", "timed out")
            result["boundary_source"] = "db_fallback"
            return result

        def stop_session(self, session_id):
            super().stop_session(session_id)
            raise ApiError("POST", f"/sessions/{session_id}/stop", "timed out")

    api = TimeoutApi()
    runner = make_runner(tmp_path, api, input_fn=lambda prompt: "")
    choices = iter([spec(session_id="failed_1"), spec(session_id="must_not_start")])
    monkeypatch.setattr("scripts.collect_dataset.prompt_spec", lambda: next(choices))
    assert _interactive_loop(runner) == 1
    assert len([call for call in api.calls if call[0] == "start"]) == 1
    manifest = json.loads(runner.manifest_path.read_text())
    assert manifest["attempts"][0]["state"] == "needs_reconciliation"
    assert not runner.results


def test_wrong_backend_device_closes_only_new_session(tmp_path):
    api = FakeApi()
    api.start_info["device_id"] = "other_device"
    with pytest.raises(CollectionError, match="device_id iot_backend"):
        collect(make_runner(tmp_path, api))
    assert api.calls[-1] == ("stop", "run_1")


def test_existing_session_blocks_new_request(tmp_path):
    api = FakeApi()
    api.existing = [session_info("running", "someone_else")]
    with pytest.raises(CollectionError, match="đang chạy"):
        collect(make_runner(tmp_path, api))
    assert not api.calls


def test_existing_output_is_preserved(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    sentinel = out / "manifest.json"
    sentinel.write_text('{"existing":true}')
    with pytest.raises(CollectionError, match="đã có dữ liệu"):
        make_runner(tmp_path)
    assert sentinel.read_text() == '{"existing":true}'


def test_bad_schema_keeps_unlabelled_raw_export(tmp_path):
    api = FakeApi()
    api.rows = [payload(11, distance_mm="broken"), *api.rows[1:]]
    runner = make_runner(tmp_path, api)
    with pytest.raises(CollectionError, match="schema"):
        collect(runner)
    assert (tmp_path / "out/run_1.raw.jsonl").exists()
    assert not runner.combined_path.exists()
    assert [name for name, _ in api.calls] == ["start", "stop"]


def test_load_plan_expands_repeats_and_object_overrides_default(tmp_path):
    path = tmp_path / "plan.yaml"
    path.write_text(yaml.safe_dump({"defaults": {"target_type": "flat_board", "duration_s": 2}, "sessions": [
        {"scenario": "approach", "speed_level": "slow", "repetitions": 2, "session_id": "repeat"},
        {"scenario": "stationary", "object": "person"},
    ]}))
    runs = load_plan(path)
    assert [r.session_id for r in runs] == ["repeat_001", "repeat_002", None]
    assert [r.target_type for r in runs] == ["flat_board", "flat_board", "person"]
    assert runs[0].duration_s == 2


@pytest.mark.parametrize("kwargs", [{"duration_s": float("nan")}, {"duration_s": float("inf")}, {"session_id": '"inject"'}, {"session_id": "a" * 64}])
def test_invalid_specs_rejected_before_start(tmp_path, kwargs):
    api = FakeApi()
    with pytest.raises(CollectionError):
        collect(make_runner(tmp_path, api), **kwargs)
    assert not api.calls


def test_noninteractive_requires_duration_before_creating_artifacts(tmp_path, capsys):
    out = tmp_path / "invalid-output"
    assert main(["--scenario", "approach", "--target-type", "person", "--non-interactive", "--output-dir", str(out)]) == 1
    assert "duration-s" in capsys.readouterr().err
    assert not out.exists()


def test_http_error_preserves_response_and_does_not_retry():
    calls = []

    def opener(request, timeout):
        calls.append(request.get_method())
        raise urllib.error.HTTPError(request.full_url, 409, "Conflict", {}, io.BytesIO(b'{"detail":"session is running"}'))

    api = HttpSessionApi("http://iot_backend", opener=opener)
    with pytest.raises(ApiError, match="session is running") as failure:
        api.start_session({"scenario": "approach"})
    assert failure.value.status == 409 and calls == ["POST"]


def test_http_runner_real_api_mqtt_control_ack_and_delayed_export(tmp_path):
    """Exercise actual HTTP endpoints and MqttControlTransport, without hardware."""
    from fastapi.testclient import TestClient
    from iot_backend.main import create_app
    from iot_backend.sessions.manager import MqttControlTransport
    from iot_backend.settings import Settings

    settings = Settings(
        mqtt_host="unused", mqtt_port=1883, db_path=str(tmp_path / "iot_backend.db"),
        device_id="esp32_01", safety_config_path=str(ROOT / "configs/safety.yaml"),
        model_config_path=str(ROOT / "configs/model.yaml"),
        model_artifact_dir=str(tmp_path / "absent-artifacts"), mqtt_enabled=False,
    )
    app = create_app(settings)
    clock = FakeClock()
    control_events = []
    started = False
    stopped = False
    emitted_tail = False
    control_schema = Draft202012Validator(json.loads((ROOT / "schemas/control_v1.schema.json").read_text()), format_checker=FormatChecker())

    class DeviceTransport:
        def add_wildcard_handler(self, pattern, callback):
            self.ack_callback = callback

        def publish_json(self, topic, command, qos):
            nonlocal started, stopped
            control_schema.validate(command)
            assert topic == "hrc/v1/devices/esp32_01/control" and qos == 1
            control_events.append(command)
            sequence = 10 if command["command"] == "session_start" else 14
            started = started or command["command"] == "session_start"
            stopped = stopped or command["command"] == "session_stop"
            self.ack_callback("hrc/v1/devices/esp32_01/ack", json.dumps({
                "schema_version": "1.0", "command": command["command"],
                "session_id": command["session_id"], "device_id": "esp32_01",
                "boot_id": "boot-1", "sequence": sequence, "monotonic_us": sequence * 100_000,
                "time_synced": False, "accepted": True,
            }))

    with TestClient(app) as client:
        # Mark health as enabled and inject a device that acknowledges actual
        # MqttControlTransport commands. Lifespan uses no real MQTT listener.
        app.state.listener = DeviceTransport()
        app.state.manager.set_control(MqttControlTransport(app.state.listener))

        def emit(sequence):
            app.state.service.handle_message("hrc/v1/devices/esp32_01/telemetry", json.dumps(payload(sequence)))

        def sleep(seconds):
            nonlocal emitted_tail
            clock.sleep(seconds)
            if started and not stopped:
                for seq in range(11, 14):
                    emit(seq)
            elif stopped and not emitted_tail:
                emit(14)  # Arrives after STOP ACK and after the first export.
                emitted_tail = True

        class Response(io.BytesIO):
            def __init__(self, response):
                super().__init__(response.content)
                self.status = response.status_code

        def opener(request, timeout):
            return Response(client.request(request.get_method(), request.full_url, content=request.data, headers=dict(request.header_items())))

        api = HttpSessionApi("http://testserver", opener=opener)
        runner = make_runner(tmp_path, api, clock=clock, sleep_fn=sleep)
        result = collect(runner)
        assert result.rows == 4 and result.quality_ok
        assert [c["command"] for c in control_events] == ["session_start", "session_stop"]
        assert all(c["session_id"] == "run_1" for c in control_events)
        metadata = client.get("/sessions/run_1").json()
        assert metadata["status"] == "closed" and metadata["sample_count"] == 4
        assert metadata["start_sequence"] == 11 and metadata["stop_sequence"] == 14


def test_collected_labels_load_into_session_split_without_leakage(tmp_path):
    api = FakeApi()
    runner = make_runner(tmp_path, api)
    for index in range(6):
        start = 1 + index * 30
        stop = start + 29
        api.start_info.update(start_sequence=start)
        api.stop_info.update(start_sequence=start, stop_sequence=stop)
        api.rows = [payload(seq) for seq in range(start, stop + 1)]
        collect(runner, session_id=f"split_{index}")
    config = yaml.safe_load((ROOT / "configs/model.yaml").read_text())
    ds = build_lstm_dataset(load_jsonl_dataset(runner.combined_path), WindowConfig.from_dict(config), config)
    assert all(split.n_windows > 0 for split in ds.splits.values())
    assert not (ds.train.session_ids & ds.val.session_ids)
    assert not (ds.train.session_ids & ds.test.session_ids)
    assert not (ds.val.session_ids & ds.test.session_ids)
    for split in ds.splits.values():
        assert all((w.meta.sequence_first - 1) // 30 == (w.meta.sequence_target_last - 1) // 30 for w in split.windows)
        assert all(meta["speed_level"] == "slow" for meta in split.session_meta.values())
