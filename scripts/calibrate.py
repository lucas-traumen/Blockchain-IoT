"""Empirical HC-SR04 calibration report; never edits safety configuration.

CSV: reference_mm, measured_mm, valid, session_id, target_type, source,
conditions (free text). Source must explicitly be real or synthetic.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from statistics import fmean, stdev

from ai_model.preprocessing import sha256_file


CONTEXT_FIELDS = ('session_id','target_type','source','conditions')


def _metrics(errors_mm: list[float], multiplier: float) -> dict:
    bias=fmean(errors_mm)
    sigma=stdev(errors_mm) if len(errors_mm)>1 else None
    max_abs=max(abs(e) for e in errors_mm)
    candidate=None if sigma is None else max(abs(bias)+multiplier*sigma,max_abs)/1000
    return {'n':len(errors_mm),'bias_mm':bias,'mae_mm':fmean(abs(e) for e in errors_mm),
        'sigma_mm':sigma,'max_abs_error_mm':max_abs,'candidate_zd_m':candidate}


def calibration_report(rows: list[dict], sigma_multiplier: float = 3.0) -> dict:
    if not math.isfinite(sigma_multiplier) or sigma_multiplier < 0:
        raise ValueError('sigma_multiplier phải hữu hạn và >= 0')
    if not rows:
        raise ValueError('calibration input rỗng')
    errors=[]
    groups={}
    invalid=0
    sources=set()
    for index,row in enumerate(rows,1):
        if any(not str(row.get(key,'')).strip() for key in CONTEXT_FIELDS):
            raise ValueError(f'dòng {index}: thiếu session/target/source/conditions')
        source=row['source']
        if source not in {'real','synthetic'}:
            raise ValueError(f'dòng {index}: source phải là real hoặc synthetic')
        sources.add(source)
        valid=row.get('valid',True)
        if isinstance(valid,str):
            if valid.lower() not in {'true','false','1','0'}:
                raise ValueError(f'dòng {index}: valid phải true/false/1/0')
            valid=valid.lower() in {'true','1'}
        if not isinstance(valid,bool):
            raise ValueError(f'dòng {index}: valid phải boolean')
        if not valid:
            invalid+=1
            continue
        try:
            reference=float(row['reference_mm'])
            measured=float(row['measured_mm'])
        except (KeyError,ValueError,TypeError) as exc:
            raise ValueError(f'dòng {index}: thiếu/sai reference_mm hoặc measured_mm') from exc
        if not (math.isfinite(reference) and 0<reference<=4500 and math.isfinite(measured) and 0<=measured<=4500):
            raise ValueError(f'dòng {index}: giá trị đo không hữu hạn hoặc ngoài miền')
        if len(sources)>1:
            raise ValueError('không gộp dữ liệu real và synthetic trong một báo cáo')
        error=measured-reference
        errors.append(error)
        key=(reference,*(str(row[k]) for k in CONTEXT_FIELDS))
        groups.setdefault(key,[]).append(error)
    if len(errors)<2:
        raise ValueError('cần ít nhất 2 mẫu hợp lệ để tính sigma')
    if len(sources)>1:
        raise ValueError('không gộp dữ liệu real và synthetic trong một báo cáo')
    aggregate=_metrics(errors,sigma_multiplier)
    breakdown=[]
    for key,values in sorted(groups.items()):
        breakdown.append({'reference_mm':key[0],**dict(zip(CONTEXT_FIELDS,key[1:])),
            'metrics':_metrics(values,sigma_multiplier)})
    candidates=[g['metrics']['candidate_zd_m'] for g in breakdown
                if g['metrics']['candidate_zd_m'] is not None]
    recommendation=max([aggregate['candidate_zd_m'],*candidates])
    return {'schema_version':'1.0','source':next(iter(sources)),
        'synthetic':sources=={'synthetic'},'n_input':len(rows),'n_valid':len(errors),'n_invalid':invalid,
        'sigma_multiplier':sigma_multiplier,
        'formula':'max(abs(bias_mm) + k * sample_sigma_mm, max_abs_error_mm) / 1000',
        'aggregate':aggregate,'by_condition':breakdown,'recommended_zd_m':recommendation,
        'recommendation_scope':'empirical_observed_conditions_only',
        'safety_config_updated':False}


def main(argv: list[str] | None = None) -> int:
    parser=argparse.ArgumentParser(description='Báo cáo calibration HC-SR04 từ CSV')
    parser.add_argument('--input',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--sigma-multiplier',type=float,default=3)
    args=parser.parse_args(argv)
    with Path(args.input).open(newline='',encoding='utf-8') as handle:
        rows=list(csv.DictReader(handle))
    report=calibration_report(rows,args.sigma_multiplier)
    report['input_sha256']=sha256_file(args.input)
    Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    print(json.dumps({'output':args.output,'source':report['source'],
                      'recommended_zd_m':report['recommended_zd_m']}))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
