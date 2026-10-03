"""M14: debounce, first evidence, raw scope, replay and durable recovery."""
import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.ingestion.service import IngestionService, PROCESSOR_VERSION
from backend.ingestion.store import Store
from backend.safety import load_safety_configs
from backend.sessions import SessionManager
from backend.settings import load_settings
from tests.test_inference import make_sample


class Predictor:
    def __init__(self, warnings=()):
        self.warnings=set(warnings)

    def predict(self,payload,session_id,sp_m,mode):
        return {'status':'OK','model_version':'test-v1','horizons_ms':[100,200,300,400,500],
            'distance_m':[.3]*5,'early_warning':payload['sequence'] in self.warnings,
            'stale':False,'window_ref':{'device_id':payload['device_id'],'boot_id':payload['boot_id'],
            'sequence_first':max(1,payload['sequence']-9),'sequence_last':payload['sequence'],
            'sample_count':10,'max_age_ms':900},'mode':mode}

    def push_sample(self,*args):
        pass


def service(store,warnings=(),manager=None):
    return IngestionService(store,load_safety_configs('configs/safety.yaml'),
        session_assigner=manager,predictor=Predictor(warnings))


def ingest(s,seq,distance=900,**overrides):
    sample=make_sample(seq,seq*100_000,distance,**overrides)
    return s.handle_message('hrc/v1/devices/esp32_sim/telemetry',json.dumps(sample))


def test_two_of_three_first_assessment_and_full_raw_package(tmp_path):
    with Store(tmp_path/'db') as store:
        s=service(store,[21,23,24,25])
        for seq in range(1,33):
            ingest(s,seq)
        events=store.list_events(PROCESSOR_VERSION)
        assert len(events)==1
        e=events[0]
        assert e['event_type']=='PREDICTED_WARNING'
        assert e['trigger_ref']['sequence']==21
        assert e['confirmed_ref']['sequence']==23
        assert e['assessment']['sample_ref']['sequence']==21
        package=store.event_package(e['event_id'])
        assert package['package_status']=='ready'
        assert package['coverage']['complete'] is True
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource
        schemas=[json.loads(p.read_text()) for p in Path('schemas').glob('*.schema.json')]
        registry=Registry().with_resources((s['$id'],Resource.from_contents(s)) for s in schemas)
        schema=next(s for s in schemas if s.get('title')=='TV3/TV4 event package v1')
        Draft202012Validator(schema,registry=registry).validate(package)
        assert [r['sequence'] for r in package['raw_samples']]==list(range(1,32))
        for seq in range(1,33):
            assert ingest(s,seq)['action']=='duplicate'
        assert store.list_events(PROCESSOR_VERSION)==events


def test_isolated_warning_no_event_rearm_after_three_clear(tmp_path):
    with Store(tmp_path/'db') as store:
        s=service(store,[1,5,6,10,11])
        for seq in range(1,13):
            ingest(s,seq)
        events=store.list_events(PROCESSOR_VERSION)
        assert [e['trigger_ref']['sequence'] for e in events]==[5,10]
        first=store.event_package(events[0]['event_id'])
        assert first['coverage']['pre_complete'] is False
        assert first['package_status']=='collecting'


def test_safety_priority_and_transition_only(tmp_path):
    with Store(tmp_path/'db') as store:
        s=service(store,range(1,20))
        for seq in range(1,4):
            ingest(s,seq,300)
        for seq in range(4,7):
            ingest(s,seq,None,valid=False,error_code='ECHO_TIMEOUT')
        events=store.list_events(PROCESSOR_VERSION)
        assert [e['event_type'] for e in events]==['VIOLATION','UNKNOWN']
        assert [e['trigger_ref']['sequence'] for e in events]==[1,4]


def test_gap_and_late_warning_cannot_confirm(tmp_path):
    with Store(tmp_path/'db') as store:
        s=service(store,[1,3,4,5])
        ingest(s,1)
        ingest(s,3) # gap clears first warning
        assert store.list_events(PROCESSOR_VERSION)==[]
        ingest(s,2) # late arrival cannot rewind detector
        assert store.list_events(PROCESSOR_VERSION)==[]
        ingest(s,4)
        assert len(store.list_events(PROCESSOR_VERSION))==1


def test_event_state_survives_restart_and_replay_is_idempotent(tmp_path):
    path=tmp_path/'db'
    with Store(path) as store:
        s=service(store,[21,22])
        for seq in range(1,22):
            ingest(s,seq)
        assert store.list_events(PROCESSOR_VERSION)==[]
    with Store(path) as store:
        s=service(store,[21,22])
        ingest(s,22)
        assert len(store.list_events(PROCESSOR_VERSION))==1
        offline=service(store,[21,22])
        assert offline.process_raw_rows(store.all_raw_rows(),'replay-events','offline')==22
        events=store.list_events('replay-events')
        assert len(events)==1
        assert events[0]['mode']=='offline'
        assert offline.process_raw_rows(store.all_raw_rows(),'replay-events','offline')==0
        assert store.list_events('replay-events')==events


def test_packages_do_not_cross_session_or_boot(tmp_path):
    with Store(tmp_path/'db') as store:
        manager=SessionManager(store,'esp32_sim',control=None)
        s=service(store,manager=manager)
        for seq in range(1,21):
            ingest(s,seq)
        manager.start_session(session_id='s1')
        ingest(s,21,300)
        manager.stop_session('s1')
        for seq in range(22,33):
            ingest(s,seq)
        ingest(s,1,boot_id='b2')
        event=store.list_events(PROCESSOR_VERSION,'s1')[0]
        package=store.event_package(event['event_id'])
        assert [r['sequence'] for r in package['raw_samples']]==[21]
        assert package['coverage']['complete'] is False


def test_partial_post_and_sequence_gap_explicit(tmp_path):
    with Store(tmp_path/'db') as store:
        s=service(store)
        for seq in list(range(1,25))+list(range(26,32)):
            ingest(s,seq,300 if seq==21 else 900)
        e=next(e for e in store.list_events(PROCESSOR_VERSION) if e['event_type']=='VIOLATION')
        package=store.event_package(e['event_id'])
        assert package['package_status']=='ready'
        assert package['coverage']['sequence_gap_count']==1
        assert package['coverage']['complete'] is False
        assert 25 not in [r['sequence'] for r in package['raw_samples']]


def test_event_and_processed_atomic_rollback(tmp_path,monkeypatch):
    with Store(tmp_path/'db') as store:
        s=service(store)
        def broken(*args):
            raise RuntimeError('event write failed')
        monkeypatch.setattr(s._events,'observe',broken)
        with pytest.raises(RuntimeError,match='event write failed'):
            ingest(s,1,300)
        assert store.get_processed('esp32_sim','b1',1,PROCESSOR_VERSION) is None
        assert len(store.fetch_raw('esp32_sim','b1'))==1
        monkeypatch.undo()
        assert s.catch_up()==1
        assert len(store.list_events(PROCESSOR_VERSION))==1


def test_event_api(tmp_path):
    from backend.main import create_app
    settings=replace(load_settings(),mqtt_enabled=False,db_path=str(tmp_path/'db'))
    with TestClient(create_app(settings)) as client:
        ingest(client.app.state.service,1,300)
        events=client.get('/events').json()
        assert len(events)==1
        p=client.get('/events/'+events[0]['event_id']).json()
        assert p['coverage']['post_complete'] is False
        assert client.get('/events/missing').status_code==404


def test_raw_window_brackets_endpoints_with_real_sampling_jitter(tmp_path):
    with Store(tmp_path/'db') as store:
        s=service(store)
        for seq in range(1,33):
            ingest(s,seq,300 if seq==21 else 900,monotonic_us=seq*100003)
        e=next(e for e in store.list_events(PROCESSOR_VERSION) if e['event_type']=='VIOLATION')
        package=store.event_package(e['event_id'])
        assert package['coverage']['complete'] is True
        assert package['coverage']['pre_available_ms']>=2000
        assert package['coverage']['post_available_ms']>=1000
        assert package['raw_samples'][-1]['sequence']==31
