"""Reconcile abandoned receipts from committed native facts, without replay."""
from contextlib import ExitStack
import errno
import json
import hashlib

from fin_agent.agent.trace_store import TraceStore, file_lock, fold_runs
from fin_agent.agent.redaction import configured_redactor
from fin_agent.agent.harness_profile import status_for_finish


TERMINAL = {'succeeded', 'failed', 'cancelled', 'interrupted', 'rejected'}


def recover(directory, *, session_id=None, redactor=None):
    redactor = redactor or configured_redactor()
    store = TraceStore(directory)
    store.repair_tail()
    recovered, active = [], []
    for candidate in fold_runs(store.records()).values():
        if candidate['status'] in TERMINAL and not candidate.get('integrity_warning'):
            continue
        sid, rid = candidate['harness_session_id'], candidate['run_id']
        if session_id is not None and sid != session_id:
            continue
        with ExitStack() as ownership:
            try:
                ownership.enter_context(file_lock(store.lease_path(rid), blocking=False))
                # Never inspect and settle a session while another host owns it.
                session_path = store.directory / 'locks'
                ownership.enter_context(file_lock(session_path / (hashlib.sha256(sid.encode()).hexdigest() + '.lock'), blocking=False))
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                active.append(rid)
                continue
            run = store.get_run(rid)
            if run['status'] in TERMINAL and not run.get('integrity_warning'):
                continue
            native = store.native_events(sid)
            start = next((r['data']['native_seq'] for r in run['records'] if r['type'] == 'native_input'), None)
            # Missing host association must not turn another question into this one.
            events = [e for e in native if 'seq' in e and start is not None and e['seq'] >= start]
            next_inputs = [r['data']['native_seq'] for r in store.records()
                           if r['type'] == 'native_input' and r['data']['run_id'] != rid
                           and start is not None and r['data']['native_seq'] > start
                           and store.get_run(r['data']['run_id'])['harness_session_id'] == sid]
            if next_inputs:
                events = [e for e in events if e['seq'] < min(next_inputs)]
            ack_path = store.directory / 'runtime' / sid / 'acks' / (sid + '.json')
            ack = json.loads(ack_path.read_text())['sequence'] if ack_path.exists() else -1
            # Ignore any bytes newer than the acknowledged persistence watermark.
            events = [e for e in events if e['seq'] <= ack]
            calls = {e['data']['callId']: e for e in events if e['type'] == 'tool/call'}
            results = {e['data']['message']['toolCallId']: e for e in events if e['type'] == 'tool/result'}
            for call_id, event in calls.items():
                result = results.get(call_id)
                status = 'outcome_unknown' if result is None else (
                    'failed' if result['data']['message'].get('isError') else 'succeeded')
                store.append('tool_recovered', run_id=rid, tool_call_id=call_id,
                             tool_execution_id=f"{sid}:{event['seq']}", status=status,
                             native_seq=event['seq'])
            end = next((e for e in events if e['type'] == 'turn/end'), None)
            status, answer = ('cancelled' if run['status'] == 'cancelled' else 'interrupted'), ''
            details = {}
            if end is not None and start is not None:
                expected = list(range(start, end['seq'] + 1))
                through_end = [e for e in events if e['seq'] <= end['seq']]
                if [e['seq'] for e in through_end] == expected:
                    reason = end['data'].get('reason', {}).get('kind')
                    status = status_for_finish(reason)
                    for e in through_end:
                        if e['type'] == 'assistant/message':
                            data = e['data']
                            message = data.get('message', data)
                            answer = ''.join(b.get('text', '') for b in message.get('content', []) if b['type'] == 'text')
                    details = {'native_last_seq': end['seq'], 'finish_reason': reason,
                               'turn': end['data'].get('turn')}
            store.append('finished', run_id=rid, status=status, answer=redactor.text(answer),
                         recovered=True, integrity_warning=None,
                         recovery_reason='abandoned_host_lease', **details)
            recovered.append(rid)
    return {'recovered': recovered, 'active': active}
