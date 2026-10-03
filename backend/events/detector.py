"""Persistent 2-of-3 warning debounce and safety transition triggers.

State + event + processed sample are committed by the caller in one Store
transaction. No timers or invented raw samples: packages derive coverage from
source monotonic timestamps and expose missing/incomplete data explicitly.
"""
from __future__ import annotations

import json
import uuid

from backend.ingestion.store import Store


class EventDetector:
    def __init__(self, store: Store):
        self.store = store

    def observe(self, record: dict, payload: dict, processor_version: str) -> None:
        ref = record['sample_ref']
        key = json.dumps([ref['device_id'],ref['boot_id'],record['session_id'],processor_version],separators=(',',':'))
        state = self.store.event_state(key) or {}
        seq, mono = ref['sequence'], payload['monotonic_us']
        # Late arrival is retained in raw/assessment; it cannot rewind the
        # live detector or turn two old warnings into a new alert.
        if seq <= state.get('sequence',0):
            return
        connected = (seq == state.get('sequence',0)+1
            and abs((mono-state.get('monotonic_us',mono))/1e6 - .1) <= .03)
        if not connected:
            state = {}
        safety = record['safety']['status']
        prediction = record['prediction']
        warning = (safety == 'CLEAR' and prediction['status']=='OK'
            and prediction['early_warning'] is True and not prediction['stale'])
        token = [record['safety']['config_version'],prediction['model_version']]
        if state.get('token') != token:
            state['history'] = []
            state['warning_active'] = False
        history = state.get('history', [])
        history.append({'warning': warning, 'assessment': record, 'monotonic_us': mono})
        history = history[-3:]
        triggers = []
        if safety in {'VIOLATION','UNKNOWN'} and safety != state.get('safety'):
            triggers.append((safety,record,mono))
        warning_active = state.get('warning_active',False)
        if safety != 'CLEAR':
            history = []
            warning_active = False
        elif sum(item['warning'] for item in history) >= 2 and not warning_active:
            first = next(item for item in history if item['warning'])
            triggers.append(('PREDICTED_WARNING',first['assessment'],first['monotonic_us']))
            warning_active = True
        elif len(history)==3 and not any(item['warning'] for item in history):
            warning_active = False
        for kind, first_assessment, trigger_mono in triggers:
            first_ref = first_assessment['sample_ref']
            identity = json.dumps([key,kind,first_ref['sequence']],separators=(',',':'))
            event = {
                'schema_version':'1.0',
                'event_id':str(uuid.uuid5(uuid.NAMESPACE_URL,identity)),
                'event_type':kind, 'processor_version':processor_version,
                'mode':record['prediction']['mode'],
                'session_id':record['session_id'],
                'trigger_ref':first_ref, 'confirmed_ref':ref,
                'trigger_monotonic_us':trigger_mono,
                'assessment':first_assessment,
                'pre_ms':2000, 'post_ms':1000,
            }
            self.store.insert_event(event)
        self.store.save_event_state(key,{
            'sequence':seq,'monotonic_us':mono,'safety':safety,'token':token,
            'history':history,'warning_active':warning_active,
        })
