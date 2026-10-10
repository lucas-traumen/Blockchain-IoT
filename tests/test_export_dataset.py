from pathlib import Path

import pytest

from ai_model.prepare import load_jsonl_dataset, build_lstm_dataset
from ai_model.windows import WindowConfig
from iot_backend.ingestion.store import Store
from iot_backend.sessions import SessionManager
from scripts.export_dataset import export_dataset
from tests.test_event_packages import service, ingest
from tests.test_lstm_inference import CONFIG


def test_closed_sessions_export_to_lstm_pipeline(tmp_path):
    db=tmp_path/'db'
    with Store(db) as store:
        manager=SessionManager(store,'esp32_sim',control=None)
        s=service(store,manager=manager)
        for session in range(6):
            manager.start_session(session_id=f's{session}',scenario='approach',obj='flat')
            for seq in range(session*30+1,(session+1)*30+1):
                ingest(s,seq,900-seq)
            manager.stop_session(f's{session}')
    out=tmp_path/'dataset.jsonl'
    summary=export_dataset(db,out,'synthetic')
    assert summary['n_rows']==180
    records=load_jsonl_dataset(out)
    assert all(r['meta']['synthetic'] for r in records)
    ds=build_lstm_dataset(records,WindowConfig.from_dict(CONFIG),CONFIG)
    assert ds.test.n_windows>0
    assert all(m['target_type']=='flat' for m in ds.test.session_meta.values())
    assert not (ds.train.session_ids & ds.test.session_ids)


def test_running_session_export_rejected(tmp_path):
    db=tmp_path/'db'
    with Store(db) as store:
        SessionManager(store,'esp32_sim',control=None).start_session(session_id='s1')
    with pytest.raises(ValueError,match='chưa đóng'):
        export_dataset(db,tmp_path/'out','real')
