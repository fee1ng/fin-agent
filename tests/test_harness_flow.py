"""Integration tests against the installed native Harness, MCP and disk backend."""
import importlib.util
import tempfile
import threading
import unittest
import json
from pathlib import Path
from http.server import ThreadingHTTPServer

from fin_agent.agent.harness import HarnessAgent
from fin_agent.agent.trace_query import TraceQuery
from fin_agent.agent.trace_recovery import recover
from tests.harness_probe import ModelHandler


@unittest.skipUnless(importlib.util.find_spec('deepseek_harness'), 'Harness SDK not installed')
class HarnessFlowTests(unittest.TestCase):
    def test_receipt_deduplication_and_restored_question_boundaries(self):
        ModelHandler.requests_seen = []
        server = ThreadingHTTPServer(('127.0.0.1', 0), ModelHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                options = dict(trace_dir=directory, api_key='fixture-credential',
                               base_url=f'http://127.0.0.1:{server.server_port}/v1')
                agent = HarnessAgent(**options)
                first = list(agent.stream_chat('Get the current time.', input_id='first'))
                self.assertEqual(first[-1]['type'], 'answer', first)
                names = {tool['function']['name'] for tool in ModelHandler.requests_seen[0]['tools']}
                self.assertTrue({'mcp__finance__get_current_time', 'mcp__finance__get_daily_price',
                                 'mcp__finance__get_technical_indicators', 'mcp__finance__get_portfolio_status'} <= names)
                self.assertFalse(any('bash' in name or name == 'read_file' for name in names))
                run_id = first[0]['run_id']
                self.assertEqual(agent.store.get_run(run_id)['status'], 'succeeded')
                # Crash window: native final checkpoint exists, host final append
                # is absent. Discard only the last host record to inject it.
                journal = agent.store.journal
                lines = journal.read_bytes().splitlines(keepends=True)
                self.assertEqual(json.loads(lines[-1])['type'], 'finished')
                journal.write_bytes(b''.join(lines[:-1]))
                agent.store.append('finished', run_id=run_id, status='interrupted',
                                   error='injected SDK wait failure',
                                   integrity_warning='native_outcome_requires_reconciliation')
                calls_before_recovery = len(ModelHandler.requests_seen)
                self.assertEqual(recover(directory)['recovered'], [run_id])
                self.assertEqual(agent.store.get_run(run_id)['status'], 'succeeded')
                self.assertEqual(agent.store.get_run(run_id)['answer'], 'probe-complete')
                self.assertEqual(len(ModelHandler.requests_seen), calls_before_recovery)
                calls = len(ModelHandler.requests_seen)
                duplicate = list(agent.stream_chat('Get the current time.', input_id='first'))
                self.assertEqual(duplicate[0]['run_id'], run_id)
                self.assertEqual(len(ModelHandler.requests_seen), calls)
                in_flight = agent.stream_chat('Summarize.', input_id='switching')
                accepted = next(in_flight)
                original_session = agent.session_id
                agent.clear_history()
                completed = list(in_flight)
                self.assertEqual(completed[-1]['type'], 'answer', completed)
                self.assertEqual(completed[-1]['session_id'], original_session)
                self.assertEqual(agent.store.get_run(accepted['run_id'])['harness_session_id'], original_session)
                agent.session_id = original_session
                agent.save_session()
                restored = HarnessAgent(**options)
                restored.load_session()
                second = list(restored.stream_chat('Summarize.', input_id='second'))
                self.assertEqual(second[-1]['type'], 'answer', second)
                self.assertNotEqual(second[0]['run_id'], run_id)
                one = agent.store.get_run(run_id)
                two = agent.store.get_run(second[0]['run_id'])
                self.assertNotEqual(one['turn'], two['turn'])
                native = agent.store.native_events(agent.session_id)
                self.assertTrue(native)
                server.shutdown()
                query = TraceQuery(directory)
                listing = query.list(limit=1)
                self.assertEqual(len(listing['runs']), 1)
                self.assertEqual(len(query.list(after=listing['next_cursor'])['runs']), 2)
                shown = query.show(run_id)
                self.assertEqual(shown['integrity_warnings'], [])
                self.assertTrue(any(e['type'] == 'tool/result' for e in shown['events']))
                output = Path(directory) / 'diagnostic.json'
                query.export(run_id, output)
                self.assertEqual(json.loads(output.read_text())['run']['run_id'], run_id)
                query.index.unlink()
                self.assertEqual(query.reindex()['run_count'], 3)
                native_file = next((Path(directory) / 'runtime' / original_session).rglob('session.v4.jsonl'))
                lines = native_file.read_text().splitlines()
                omitted_seq = next(e['seq'] for e in shown['events'] if e['type'] == 'tool/result')
                native_file.write_text('\n'.join(line for line in lines
                    if json.loads(line).get('seq') != omitted_seq) + '\n')
                self.assertIn('native_sequence_gap_or_duplicate', query.show(run_id)['integrity_warnings'])
                restored.clear_history()
                self.assertNotEqual(restored.session_id, agent.session_id)
                self.assertEqual(agent.store.get_run(run_id)['status'], 'succeeded')
        finally:
            server.shutdown()
            server.server_close()
