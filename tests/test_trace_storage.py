import io
import tempfile
import unittest
from unittest.mock import patch

from fin_agent.agent.redaction import Redactor
from fin_agent.agent.trace_store import TraceStore, TraceStorageError
from fin_agent.agent.tool_bridge import SafeLog, is_business_error
from fin_agent.agent.trace_query import TraceQuery


class TraceStorageTests(unittest.TestCase):
    def test_tool_filter_scans_all_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            query = TraceQuery(directory)
            pages = [{'events': [{'seq': 500, 'type': 'unknown'}], 'next_cursor': 500},
                     {'events': [{'seq': 501, 'type': 'tool/call', 'data': {'name': 'late-tool'}}],
                      'next_cursor': 501}]
            with patch.object(query, 'show', side_effect=pages):
                self.assertTrue(query._has_tool('run-test', 'late-tool'))

    def test_custom_safe_session_ids_are_queryable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TraceStore(directory)
            for session in ('custom', 'session-a_b'):
                self.assertEqual(store.native_events(session), [])
            with self.assertRaises(ValueError):
                store.native_events('../escape')

    def test_failed_fsync_blocks_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TraceStore(directory)
            with patch('fin_agent.agent.trace_store.os.fsync', side_effect=OSError('disk full')):
                with self.assertRaises(TraceStorageError):
                    store.accept('question', session_id='session-test', input_id='id', metadata={})

    def test_partial_journal_is_never_silently_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TraceStore(directory)
            store.journal.write_bytes(b'{"partial":')
            with self.assertRaises(TraceStorageError):
                store.accept('question', session_id='session-test', input_id='id', metadata={})

    def test_redacts_structured_fields_known_values_and_split_log_writes(self):
        redactor = Redactor(['fixture-secret'])
        cleaned = redactor.clean({'password': 'hidden', 'message': 'fixture-secret'})
        self.assertEqual(cleaned, {'password': '[REDACTED]', 'message': '[REDACTED]'})
        target = io.StringIO()
        safe = SafeLog(target, redactor)
        safe.write('fixture-')
        safe.flush()
        self.assertEqual(target.getvalue(), '')
        safe.write('secret\n')
        self.assertEqual(target.getvalue(), '[REDACTED]\n')

    def test_business_failure_is_not_a_successful_protocol_result(self):
        for result in ({'ok': False}, {'error': 'failure'}, 'Error executing tool: broken',
                       '{"success": false}', '错误：参数无效'):
            self.assertTrue(is_business_error(result), result)
        self.assertFalse(is_business_error('行情查询成功'))
