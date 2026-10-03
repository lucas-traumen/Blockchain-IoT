"""Closed SQLite sessions -> labelled JSONL usable by RF/LSTM trainers."""
import argparse
import json
from pathlib import Path

from backend.ingestion.store import Store


def export_dataset(db_path: str | Path, out_path: str | Path, source: str) -> dict:
    if source not in {'real','synthetic'}:
        raise ValueError('source phải là real hoặc synthetic')
    if not Path(db_path).is_file():
        raise FileNotFoundError(db_path)
    lines=[]
    sessions=[]
    with Store(db_path) as store:
        for session in store.list_sessions():
            if session['status']!='closed':
                raise ValueError(f"session {session['session_id']} chưa đóng")
            rows=store.session_raw_rows(session['session_id'])
            if not rows:
                continue
            sessions.append(session['session_id'])
            for row in rows:
                payload=json.loads(row['payload_json'])
                if source=='real' and payload['boot_id'].startswith(('synthetic_','sim_')):
                    raise ValueError('không gắn source=real cho boot simulator/synthetic')
                labelled={**payload,'session_label':session['session_id'],
                    'synthetic':source=='synthetic','scenario':session['scenario'],
                    'target_type':session['object']}
                lines.append(json.dumps(labelled,ensure_ascii=False,allow_nan=False))
    if not lines:
        raise ValueError('không có sample được gán vào session đã đóng')
    Path(out_path).write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return {'output':str(out_path),'source':source,'n_rows':len(lines),'sessions':sessions}


def main(argv: list[str] | None = None) -> int:
    parser=argparse.ArgumentParser(description='Export dataset từ các session đã đóng')
    parser.add_argument('--db',required=True)
    parser.add_argument('--out',required=True)
    parser.add_argument('--source',choices=('real','synthetic'),required=True)
    args=parser.parse_args(argv)
    print(json.dumps(export_dataset(args.db,args.out,args.source),ensure_ascii=False))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
