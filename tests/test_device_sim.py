import json
from pathlib import Path

from jsonschema import Draft202012Validator

from scripts.device_sim import DeviceSimulator, TELEMETRY_FIELDS, main


def test_dry_run_labelled_payloads_and_schema(capsys):
    assert main(['--dry-run','--samples','20','--seed','7'])==0
    rows=[json.loads(line) for line in capsys.readouterr().out.splitlines()]
    validator=Draft202012Validator(json.loads(Path('schemas/telemetry_v1.schema.json').read_text()))
    assert len(rows)==20
    for seq,row in enumerate(rows,1):
        assert row['synthetic'] is True
        assert row['sequence']==seq
        assert row['session_label']==row['boot_id']
        validator.validate({k:row[k] for k in TELEMETRY_FIELDS})


def test_control_ack_matches_latest_measurement_and_rejects_bad_command():
    simulator=DeviceSimulator(samples=20)
    command={'schema_version':'1.0','command':'session_start','session_id':'s1',
        'issued_at':'2026-10-02T00:00:00.000Z'}
    assert simulator.control_ack(command) is None
    for _ in range(5):
        payload=simulator.next_payload()
    ack=simulator.control_ack(command)
    validator=Draft202012Validator(json.loads(Path('schemas/ack_v1.schema.json').read_text()))
    validator.validate(ack)
    assert ack['sequence']==payload['sequence']==5
    assert ack['boot_id']==payload['boot_id']
    assert simulator.control_ack(dict(command,command='bad')) is None
    assert simulator.control_ack(dict(command,extra=True)) is None


def test_recording_keeps_source_labels(tmp_path,capsys):
    path=tmp_path/'record.jsonl'
    main(['--dry-run','--samples','20','--record-jsonl',str(path)])
    assert path.read_text()==capsys.readouterr().out
    assert all(json.loads(line)['synthetic'] for line in path.read_text().splitlines())
