import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from fin_agent.agent.trace_recovery import recover
from fin_agent.agent.trace_store import TraceStore
from fin_agent.agent.harness import HarnessAgent
from fin_agent.agent.redaction import Redactor
from fin_agent.agent.trace_query import TraceQuery


class HarnessRecoveryTests(unittest.TestCase):
    def test_native_cancel_status_and_recovered_answer_redaction(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TraceStore(directory)
            receipt, _ = store.accept('question', session_id='session-cancel', input_id='cancel', metadata={})
            rid = receipt['run_id']
            store.append('started', run_id=rid, status='running')
            store.append('native_input', run_id=rid, native_seq=0, native_message_id='input')
            secret = 'fake-smtp-password-for-recovery'
            native_dir = Path(directory) / 'runtime/session-cancel/native/sessions/cwd/session-cancel'
            native_dir.mkdir(parents=True)
            events = [{'type': 'agent/inbox/spliced', 'seq': 0, 'data': {}},
                      {'type': 'assistant/message', 'seq': 1,
                       'data': {'content': [{'type': 'text', 'text': secret}]}},
                      {'type': 'turn/end', 'seq': 2,
                       'data': {'turn': 1, 'reason': {'kind': 'aborted', 'reason': {'kind': 'user'}}}}]
            (native_dir / 'session.v4.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events))
            ack_dir = Path(directory) / 'runtime/session-cancel/acks'
            ack_dir.mkdir()
            (ack_dir / 'session-cancel.json').write_text(json.dumps({'sequence': 2}))
            recover(directory, redactor=Redactor([secret]))
            self.assertEqual(store.get_run(rid)['status'], 'cancelled')
            self.assertEqual(store.get_run(rid)['answer'], '[REDACTED]')
            self.assertNotIn(secret, store.journal.read_text())
            query = TraceQuery(directory)
            query.reindex()
            self.assertNotIn(secret.encode(), query.index.read_bytes())

    def test_closing_queued_generator_cancels_without_starting_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = HarnessAgent(trace_dir=directory, api_key='fixture')
            stream = agent.stream_chat('question', input_id='close')
            receipt = next(stream)
            self.assertEqual(recover(directory)['active'], [receipt['run_id']])
            stream.close()
            self.assertEqual(agent.store.get_run(receipt['run_id'])['status'], 'cancelled')
            recover(directory)
            self.assertEqual(agent.store.get_run(receipt['run_id'])['status'], 'cancelled')
            self.assertFalse((Path(directory) / 'runtime').exists())

    def test_torn_tail_preserves_committed_receipt_and_archives_original_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TraceStore(directory)
            receipt, _ = store.accept('question', session_id='session-torn', input_id='torn', metadata={})
            original = store.journal.read_bytes() + b'{"seq":1,"partial"'
            store.journal.write_bytes(original)
            result = recover(directory)
            self.assertEqual(result['recovered'], [receipt['run_id']])
            self.assertEqual(store.get_run(receipt['run_id'])['status'], 'interrupted')
            archive = next(Path(directory).glob('torn-receipt-*.bin'))
            self.assertEqual(archive.read_bytes(), original)

    def test_live_owner_is_skipped_and_killed_owner_is_recovered_without_replay(self):
        worker = """
import sys,time
from fin_agent.agent.trace_store import TraceStore
s=TraceStore(sys.argv[1])
with s.owned_accept('question',session_id='session-killed',input_id='fixed',metadata={}) as (r,a):
 print(r['run_id'],flush=True)
 time.sleep(60)
"""
        with tempfile.TemporaryDirectory() as directory:
            proc = subprocess.Popen([sys.executable, '-c', worker, directory],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                run_id = proc.stdout.readline().strip()
                self.assertTrue(run_id.startswith('run-'))
                self.assertEqual(recover(directory), {'recovered': [], 'active': [run_id]})
                proc.kill()
                proc.wait(timeout=10)
                self.assertEqual(recover(directory)['recovered'], [run_id])
                run = TraceStore(directory).get_run(run_id)
                self.assertEqual(run['status'], 'interrupted')
                self.assertEqual(recover(directory)['recovered'], [])
                self.assertFalse((Path(directory) / 'runtime').exists())
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=10)
                proc.stdout.close()
                proc.stderr.close()

    def test_unsettled_tool_is_unknown_and_never_replayed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TraceStore(directory)
            receipt, _ = store.accept('write', session_id='session-write', input_id='write', metadata={})
            rid = receipt['run_id']
            store.append('started', run_id=rid, status='running')
            store.append('native_input', run_id=rid, native_seq=0, native_message_id='native-input')
            native_dir = Path(directory) / 'runtime/session-write/native/sessions/cwd/session-write'
            native_dir.mkdir(parents=True)
            events = [{'type': 'agent/inbox/spliced', 'seq': 0, 'data': {}},
                      {'type': 'tool/call', 'seq': 1,
                       'data': {'callId': 'write-call', 'name': 'write-portfolio', 'arguments': '{}'}}]
            (native_dir / 'session.v4.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events))
            ack_dir = Path(directory) / 'runtime/session-write/acks'
            ack_dir.mkdir()
            (ack_dir / 'session-write.json').write_text(json.dumps({'sequence': 1}))
            recover(directory)
            run = store.get_run(rid)
            tool = next(r for r in run['records'] if r['type'] == 'tool_recovered')
            self.assertEqual(tool['data']['status'], 'outcome_unknown')
            self.assertEqual(run['status'], 'interrupted')
            self.assertEqual((native_dir / 'session.v4.jsonl').read_text(), ''.join(json.dumps(e)+'\n' for e in events))
