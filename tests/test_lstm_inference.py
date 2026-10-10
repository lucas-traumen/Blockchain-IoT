"""M13: shared train/runtime preprocessing, thresholds and fail-safe integration."""
import importlib.util
import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
import yaml
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from ai_model.features import window_to_lstm_features
from ai_model.preprocessing import LSTMScaler
from ai_model.windows import Window, WindowMeta
from iot_backend.inference.predictor import SafetyPredictor
from iot_backend.ingestion.service import IngestionService, PROCESSOR_VERSION
from iot_backend.ingestion.store import Store
from iot_backend.safety import load_safety_configs
from tests.test_inference import make_sample, NOW_ISO

CONFIG = yaml.safe_load(Path('configs/model.yaml').read_text())
MANIFEST = {
    'model_type': 'lstm', 'model_version': 'lstm-v1', 'preprocessing_version': 'pp-lstm-v1',
    'input_shape': [10, 2], 'output_shape': [5], 'features': ['distance_m', 'delta_distance_m'],
    'units': 'm', 'window_size': 10, 'delta_s': .1, 'horizons_ms': [100,200,300,400,500],
}


class Model:
    def __init__(self, output=None, fail=False):
        self.output = np.array([output or [.7, .6, .44001, .43999, .3]])
        self.fail = fail
        self.input = None

    def __call__(self, x, training=False):
        assert training is False
        self.input = np.array(x, copy=True)
        if self.fail:
            raise RuntimeError('inference failure')
        return self.output


def scaler():
    names = ['distance_m', 'delta_distance_m', 'target_distance_m']
    return LSTMScaler(dict(zip(names, [.5, -.01, 0])), dict(zip(names, [.2, .02, 1])), 10)


def predictor(model=None, manifest=None):
    return SafetyPredictor('unused', CONFIG,
        lambda _: (model or Model(), scaler(), manifest or MANIFEST), model_type='lstm')


def push(p, seq, **overrides):
    return p.predict(make_sample(seq, seq * 100_000, 900, **overrides), None, .44, 'offline')


def test_tensor_same_as_train_and_threshold_precision():
    model = Model()
    p = predictor(model)
    samples = []
    for seq in range(1,11):
        sample = make_sample(seq, seq*100_000, 900-seq*10)
        samples.append(sample)
        result = p.predict(sample, 's1', .44, 'offline')
        assert result['status'] == ('WARMUP' if seq < 10 else 'OK')
    window = Window([s['distance_mm']/1000 for s in samples], [],
        WindowMeta('esp32_sim','b1','s1',1,10,10,None))
    expected = scaler().transform_X(np.array([window_to_lstm_features(window, CONFIG)]))
    np.testing.assert_allclose(model.input, expected)
    assert result['predicted_violation_ms'] == 400
    assert result['distance_m'][2] == .44001
    assert result['early_warning'] is True
    assert result['preprocessing_version'] == 'pp-lstm-v1'


@pytest.mark.parametrize('overrides', [
    {'valid':False,'distance_mm':None,'error_code':'ECHO_TIMEOUT'},
    {'monotonic_us':1_600_000}, {'sequence':12},
])
def test_gap_jitter_error_reset(overrides):
    p = predictor()
    for seq in range(1,11):
        push(p, seq)
    sample = make_sample(11,1_100_000,900)
    sample.update(overrides)
    assert p.predict(sample,None,.44,'offline')['status'] == 'INVALID_WINDOW'
    following = make_sample(sample['sequence']+1,sample['monotonic_us']+100_000,900)
    assert p.predict(following,None,.44,'offline')['status'] == 'WARMUP'


def test_session_boot_reset_and_no_resume_old_buffer():
    p = predictor()
    for seq in range(1,11):
        push(p,seq)
    assert p.predict(make_sample(11,1_100_000,900),'new',.44,'offline')['status'] == 'WARMUP'
    assert push(p,12)['status'] == 'WARMUP'
    assert push(p,1,boot_id='newboot')['status'] == 'WARMUP'


@pytest.mark.parametrize('output', [[float('nan')]*5, [0]*4])
def test_bad_forecast_degrades(output):
    p = predictor(Model(output))
    for seq in range(1,11):
        r = push(p,seq)
    assert r['status'] == 'MODEL_UNAVAILABLE'
    assert r['predicted_violation_ms'] is None
    assert p.available is False


def test_runtime_exception_keeps_safety_alive(tmp_path):
    with Store(tmp_path/'db') as store:
        service = IngestionService(store, load_safety_configs('configs/safety.yaml'), predictor=predictor(Model(fail=True)))
        for seq in range(1,12):
            sample = make_sample(seq,seq*100_000,900 if seq <= 10 else 300)
            assert service.handle_message('hrc/v1/devices/esp32_sim/telemetry',json.dumps(sample))['action'] == 'stored'
        record=json.loads(store.get_processed('esp32_sim','b1',11,PROCESSOR_VERSION)['result_json'])
        assert record['prediction']['status']=='MODEL_UNAVAILABLE'
        assert record['safety']['status']=='VIOLATION'
        Draft202012Validator(json.loads(Path('schemas/assessment_v1.schema.json').read_text())).validate(record)


@pytest.mark.parametrize('sp,current',[(float('inf'),900),(.44,440),(.44,300)])
def test_warning_never_overrides_safety(sp,current):
    p=predictor()
    for seq in range(1,11):
        r=p.predict(make_sample(seq,seq*100_000,current),None,sp,'offline')
    assert r['early_warning'] is False
    if not np.isfinite(sp):
        assert r['predicted_violation_ms'] is None


def test_contract_mismatch_unavailable():
    p=predictor(manifest=dict(MANIFEST,horizons_ms=[500,400,300,200,100]))
    assert p.available is False
    assert 'horizons_ms' in p.load_error


def test_lstm_path_uses_no_dataframe(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules,'pandas',None)
    p=predictor()
    for seq in range(1,11):
        r=push(p,seq)
    assert r['status']=='OK'


@pytest.mark.skipif(importlib.util.find_spec('tensorflow') is None,reason='AI env required')
def test_real_artifact_runtime_and_api(tmp_path,monkeypatch):
    from ai_model.lstm_train import train_lstm, load_lstm_artifact
    from tests.test_lstm_train import _write_jsonl
    from iot_backend.main import create_app
    from iot_backend.settings import load_settings
    data=tmp_path/'d.jsonl'
    _write_jsonl(data)
    artifacts=tmp_path/'model'
    train_lstm(data,out_dir=artifacts,config_overrides={'max_epochs':1,'verbose':0})
    model,s,manifest=load_lstm_artifact(artifacts)
    p=SafetyPredictor(artifacts,CONFIG,model_type='lstm')
    for seq in range(1,11):
        r=push(p,seq)
    raw=np.array([[[.9,0 if i==0 else 0] for i in range(10)]])
    expected=s.inverse_y(model(s.transform_X(raw),training=False).numpy())[0]
    np.testing.assert_allclose(r['distance_m'],expected)
    settings=replace(load_settings(),db_path=str(tmp_path/'api.db'),mqtt_enabled=False,
        model_type='lstm',model_artifact_dir=str(artifacts))
    with TestClient(create_app(settings)) as client:
        assert client.get('/health').json()['model_available'] is True
        for seq in range(1,11):
            sample=make_sample(seq,seq*100_000,900)
            client.app.state.service.handle_message('hrc/v1/devices/esp32_sim/telemetry',json.dumps(sample))
        record=client.get('/samples/esp32_sim/b1/10').json()
        assert record['prediction']['status']=='OK'
        assert record['prediction']['preprocessing_version']=='pp-lstm-v1'
    import sys
    with monkeypatch.context() as patch:
        patch.setitem(sys.modules,'tensorflow',None)
        unavailable=SafetyPredictor(artifacts,CONFIG,model_type='lstm')
        assert unavailable.available is False
        assert 'TensorFlow' in unavailable.load_error



@pytest.mark.parametrize('pending', [False,True])
def test_restart_primes_lstm_buffer_from_processed_history(tmp_path,pending):
    with Store(tmp_path/'db') as store:
        s=IngestionService(store,load_safety_configs('configs/safety.yaml'),predictor=predictor())
        for seq in range(1,10):
            sample=make_sample(seq,seq*100_000,900)
            s.handle_message('hrc/v1/devices/esp32_sim/telemetry',json.dumps(sample))
        if pending:
            store.insert_raw(make_sample(10,1_000_000,900),NOW_ISO)
        restored=IngestionService(store,load_safety_configs('configs/safety.yaml'),predictor=predictor())
        assert restored.catch_up()==int(pending)
        if not pending:
            restored.handle_message('hrc/v1/devices/esp32_sim/telemetry',json.dumps(make_sample(10,1_000_000,900)))
        record=json.loads(store.get_processed('esp32_sim','b1',10,PROCESSOR_VERSION)['result_json'])
        assert record['prediction']['status']=='OK'
        assert record['prediction']['predicted_violation_ms']==400
