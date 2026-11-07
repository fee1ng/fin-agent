"""Offline, rebuildable SQLite projection over receipts and native session facts."""
import hashlib
import json
from pathlib import Path
import sqlite3
from contextlib import closing

from fin_agent.agent.trace_store import TraceStore, file_lock, fold_runs


class TraceQuery:
    def __init__(self, directory):
        self.store = TraceStore(directory)
        self.index = self.store.directory / 'index.sqlite3'

    def reindex(self):
        with file_lock(self.store.directory / 'index.lock'):
            receipts = self.store.records()
            run_ids = [r['data']['run_id'] for r in receipts if r['type'] == 'received']
            with closing(sqlite3.connect(self.index)) as db, db:
                db.execute('CREATE TABLE IF NOT EXISTS runs (seq INTEGER PRIMARY KEY, run_id TEXT UNIQUE, body TEXT)')
                db.execute('DELETE FROM runs')
                # Fold one consistent snapshot, not one journal read per run.
                runs = fold_runs(receipts)
                for run_id in run_ids:
                    run = runs[run_id]
                    db.execute('INSERT INTO runs VALUES (?,?,?)',
                               (run['receipt_seq'], run_id, json.dumps(run, ensure_ascii=False)))
            return {'run_count': len(run_ids), 'receipt_watermark': len(receipts) - 1}

    def list(self, *, after=-1, limit=30, session_id=None, status=None, tool=None,
             since=None, until=None):
        if not 1 <= limit <= 500:
            raise ValueError('limit must be between 1 and 500')
        watermark = self.reindex()
        selected = []
        with closing(sqlite3.connect(self.index)) as db, db:
            rows = db.execute('SELECT seq,body FROM runs WHERE seq>? ORDER BY seq', (after,))
            for sequence, body in rows:
                run = json.loads(body)
                if session_id and run['application_session_id'] != session_id:
                    continue
                if status and run['status'] != status:
                    continue
                if since and run['timestamp'] < since or until and run['timestamp'] > until:
                    continue
                if tool and not self._has_tool(run['run_id'], tool):
                    continue
                selected.append({k: v for k, v in run.items() if k not in {'records', 'input', 'answer'}})
                if len(selected) == limit:
                    break
        return {'runs': selected, 'next_cursor': selected[-1]['receipt_seq'] if selected else after, **watermark}

    def _has_tool(self, run_id, name):
        cursor = -1
        while True:
            page = self.show(run_id, after=cursor, limit=500)
            if any(e.get('type') == 'tool/call' and e.get('data', {}).get('name') == name
                   for e in page['events']):
                return True
            if not page['events']:
                return False
            cursor = page['next_cursor']

    def show(self, run_id, *, after=-1, limit=100):
        if not 1 <= limit <= 10000:
            raise ValueError('limit must be between 1 and 10000')
        run = self.store.get_run(run_id)
        native = self.store.native_events(run['harness_session_id'])
        start = next((r['data']['native_seq'] for r in run['records'] if r['type'] == 'native_input'), None)
        end = run.get('native_last_seq')
        # A crashed run has no terminal sequence. Its query must still stop at
        # the next admitted input instead of absorbing a later question.
        session_runs = {r['data']['run_id'] for r in self.store.records()
                        if r['type'] == 'received'
                        and r['data']['harness_session_id'] == run['harness_session_id']}
        next_inputs = [r['data']['native_seq'] for r in self.store.records()
                       if r['type'] == 'native_input' and r['data']['run_id'] in session_runs
                       and start is not None and r['data']['native_seq'] > start]
        upper_bound = min(next_inputs) - 1 if next_inputs else None
        facts = [event for event in native if 'seq' in event and start is not None
                 and event['seq'] >= start and (end is None or event['seq'] <= end)
                 and (upper_bound is None or event['seq'] <= upper_bound)]
        ack_path = self.store.directory / 'runtime' / run['harness_session_id'] / 'acks' / (run['harness_session_id'] + '.json')
        ack = json.loads(ack_path.read_text()) if ack_path.exists() else None
        warnings = []
        if start is not None and end is not None:
            sequences = [event['seq'] for event in facts]
            if sequences != list(range(start, end + 1)):
                warnings.append('native_sequence_gap_or_duplicate')
        if start is None:
            warnings.append('native_input_not_associated')
        if end is None:
            warnings.append('run_not_durably_settled')
        elif not ack or ack['sequence'] < end:
            warnings.append('native_checkpoint_not_confirmed')
        if end is not None and not any(e['seq'] == end for e in facts):
            warnings.append('native_terminal_fact_missing')
        page = [e for e in facts if e['seq'] > after][:limit]
        return {'run': run, 'events': page, 'next_cursor': page[-1]['seq'] if page else after,
                'total_events': len(facts), 'integrity_warnings': warnings, 'native_watermark': ack}

    def export(self, run_id, destination):
        first = self.show(run_id, limit=10000)
        events = list(first['events'])
        while len(events) < first['total_events']:
            page = self.show(run_id, after=events[-1]['seq'], limit=10000)
            if not page['events']:
                raise RuntimeError('Native log changed during export')
            events.extend(page['events'])
        package = {'format': 'fin-agent-trace', 'version': 1, **first, 'events': events,
                   'content_categories': ['user_input', 'model_output', 'financial_tool_data']}
        payload = json.dumps(package, ensure_ascii=False, indent=2).encode()
        target = Path(destination)
        # Export is explicit and must not overwrite an unrelated existing file.
        with target.open('xb') as output:
            output.write(payload)
        return {'path': str(target.resolve()), 'bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}


def run_trace_command(args):
    if args.trace_action == 'recover':
        from fin_agent.agent.trace_recovery import recover
        return recover(args.trace_dir)
    query = TraceQuery(args.trace_dir)
    if args.trace_action == 'list':
        return query.list(after=args.after, limit=args.limit, session_id=args.session,
                          status=args.status, tool=args.tool, since=args.since, until=args.until)
    if args.trace_action == 'show':
        return query.show(args.run_id, after=args.after, limit=args.limit)
    if args.trace_action == 'export':
        return query.export(args.run_id, args.output)
    return query.reindex()
