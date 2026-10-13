"""Synthetic MQTT telemetry/control/ack simulator; dry-run JSONL is labelled."""
from __future__ import annotations

import argparse
import json
import logging
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from uuid import uuid4

import paho.mqtt.client as mqtt
from jsonschema import Draft202012Validator

from scripts.synthetic_dataset import generate_sessions, SCENARIOS

ROOT=Path(__file__).resolve().parents[1]
_CONTROL=Draft202012Validator(json.loads((ROOT/'schemas/control_v1.schema.json').read_text()))
TELEMETRY_FIELDS=('schema_version','device_id','boot_id','sequence','monotonic_us',
    'measured_at','time_synced','distance_mm','echo_us','valid','error_code')


class DeviceSimulator:
    """Control ack and telemetry emission share the same sequence lock."""
    def __init__(self, samples=100, seed=42, scenario='approach', device_id='esp32_sim'):
        if not device_id or '/' in device_id or '+' in device_id or '#' in device_id:
            raise ValueError('device_id phải là một cấp topic MQTT')
        self.profiles=generate_sessions(1,samples,seed,scenario,error_prob=.02)
        self.device_id=device_id
        self.boot_id='synthetic_'+uuid4().hex
        self.sequence=0
        self.monotonic_us=0
        self.started_at=datetime.now(timezone.utc)
        self._lock=threading.Lock()

    def next_payload(self,monotonic_us: int | None = None) -> dict:
        with self._lock:
            if self.sequence>=len(self.profiles):
                raise StopIteration
            row=self.profiles[self.sequence]
            self.sequence+=1
            self.monotonic_us=self.sequence*100_000 if monotonic_us is None else monotonic_us
            payload={k:row[k] for k in TELEMETRY_FIELDS}
            payload.update(device_id=self.device_id,boot_id=self.boot_id,sequence=self.sequence,
                monotonic_us=self.monotonic_us,
                measured_at=(self.started_at+timedelta(microseconds=self.monotonic_us)).isoformat(timespec='milliseconds').replace('+00:00','Z'))
            return payload

    def control_ack(self,command: dict) -> dict | None:
        if not _CONTROL.is_valid(command):
            return None
        with self._lock:
            if self.sequence==0:
                return None
            return {'schema_version':'1.0','command':command['command'],
                'session_id':command['session_id'],'device_id':self.device_id,
                'boot_id':self.boot_id,'sequence':self.sequence,
                'monotonic_us':self.monotonic_us,'time_synced':True,'accepted':True}

    def labelled(self,payload: dict,scenario: str) -> dict:
        return {**payload,'synthetic':True,'scenario':scenario,'session_label':self.boot_id}


def main(argv: list[str] | None = None) -> int:
    parser=argparse.ArgumentParser(description='Synthetic device simulator (không phải cảm biến thật)')
    parser.add_argument('--host',default='localhost')
    parser.add_argument('--port',type=int,default=1883)
    parser.add_argument('--device-id',default='esp32_sim')
    parser.add_argument('--samples',type=int,default=100)
    parser.add_argument('--seed',type=int,default=42)
    parser.add_argument('--scenario',choices=SCENARIOS,default='approach')
    parser.add_argument('--dry-run',action='store_true',help='JSONL có nhãn, không kết nối MQTT')
    parser.add_argument('--record-jsonl',help='Lưu bản synthetic có nhãn để truy vết')
    args=parser.parse_args(argv)
    simulator=DeviceSimulator(args.samples,args.seed,args.scenario,args.device_id)
    recording=Path(args.record_jsonl).open('w',encoding='utf-8') if args.record_jsonl else None
    client=None
    try:
        if not args.dry_run:
            connected=threading.Event()
            client=mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,client_id=simulator.boot_id)
            def on_connect(client,userdata,flags,reason_code,properties):
                if reason_code==0:
                    client.subscribe(f'hrc/v1/devices/{args.device_id}/control',qos=1)
                    connected.set()
            def on_message(client,userdata,message):
                try:
                    ack=simulator.control_ack(json.loads(message.payload))
                except (ValueError,TypeError):
                    return
                if ack:
                    client.publish(f'hrc/v1/devices/{args.device_id}/ack',json.dumps(ack),qos=1,retain=False)
            client.on_connect=on_connect
            client.on_message=on_message
            client.reconnect_delay_set(1,10)
            client.connect(args.host,args.port,keepalive=30)
            client.loop_start()
            if not connected.wait(5):
                raise RuntimeError('MQTT connection timeout')
            logging.warning('SYNTHETIC telemetry: device=%s boot=%s',args.device_id,simulator.boot_id)
        origin=time.monotonic()
        for index in range(args.samples):
            if client is not None:
                delay=origin+index*.1-time.monotonic()
                if delay>0:
                    time.sleep(delay)
                payload=simulator.next_payload(max(0,int((time.monotonic()-origin)*1e6)))
                info=client.publish(f'hrc/v1/devices/{args.device_id}/telemetry',json.dumps(payload),qos=1,retain=False)
                info.wait_for_publish(timeout=5)
                if not info.is_published():
                    raise RuntimeError('MQTT publish timeout')
            else:
                payload=simulator.next_payload()
            labelled=json.dumps(simulator.labelled(payload,args.scenario),allow_nan=False)
            if recording:
                recording.write(labelled+'\n')
            if args.dry_run:
                print(labelled)
    finally:
        if client is not None:
            client.disconnect()
            client.loop_stop()
        if recording:
            recording.close()
    return 0


if __name__=='__main__':
    raise SystemExit(main())
