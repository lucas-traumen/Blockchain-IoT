import json
import math
from pathlib import Path

import pytest

from scripts.calibrate import calibration_report, main


def row(measured,**kwargs):
    return dict(reference_mm=500,measured_mm=measured,valid=True,session_id='cal1',
        target_type='flat',source='synthetic',conditions='fixture only',**kwargs)


def test_bias_mae_sample_sigma_and_conservative_proposal():
    r=calibration_report([row(501),row(502),row(503)])
    assert r['aggregate']['bias_mm']==2
    assert r['aggregate']['mae_mm']==2
    assert r['aggregate']['sigma_mm']==1
    assert r['recommended_zd_m']==.005
    assert r['synthetic'] is True
    assert r['safety_config_updated'] is False


def test_negative_bias_absolute_errors_and_condition_breakdown():
    rows=[row(490),row(492),dict(row(504),session_id='cal2'),dict(row(508),session_id='cal2')]
    r=calibration_report(rows)
    assert r['aggregate']['bias_mm']==-1.5
    assert r['aggregate']['mae_mm']==7.5
    assert len(r['by_condition'])==2
    assert r['recommended_zd_m']>=max(c['metrics']['candidate_zd_m'] for c in r['by_condition'])


def test_invalid_readings_counted_and_singleton_sigma_null():
    rows=[row(501),row(503),dict(row(None),valid=False),dict(row(505),reference_mm=1000)]
    r=calibration_report(rows)
    assert (r['n_valid'],r['n_invalid'])==(3,1)
    assert r['by_condition'][1]['metrics']['sigma_mm'] is None
    json.dumps(r,allow_nan=False)


@pytest.mark.parametrize('rows', [[],[row(500)],[row(float('nan')),row(500)],
    [dict(row(501),source='real'),row(503)],[dict(row(501),conditions=''),row(503)]])
def test_bad_or_unlabelled_data_rejected(rows):
    with pytest.raises(ValueError):
        calibration_report(rows)


def test_cli_does_not_edit_safety_config(tmp_path,capsys):
    before=Path('configs/safety.yaml').read_bytes()
    csv=tmp_path/'cal.csv'
    csv.write_text('reference_mm,measured_mm,valid,session_id,target_type,source,conditions\n'
        '500,501,true,s1,flat,synthetic,fixture\n500,503,true,s1,flat,synthetic,fixture\n')
    out=tmp_path/'report.json'
    assert main(['--input',str(csv),'--output',str(out)])==0
    report=json.loads(out.read_text())
    assert len(report['input_sha256'])==64
    assert json.loads(capsys.readouterr().out)['source']=='synthetic'
    assert Path('configs/safety.yaml').read_bytes()==before
