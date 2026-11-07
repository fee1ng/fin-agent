from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import select

from fin_agent.web.models import Run, User
from fin_agent.web.runs import Runs
from tests.test_web_api import WebApiFixture


def at(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


class AdminTests(WebApiFixture):
    def setUp(self):
        super().setUp()
        self.login()
        self.session = self.create_session()
        self.runs = Runs(self.database, self.settings)
        with self.database.transaction() as db:
            self.alice = db.scalar(select(User).where(User.username == 'alice')).id
        self.run = self.runs.submit(self.session['id'], self.alice, '分析平安银行', 'admin-fixture-0001')
        self.runs.claim('fixture')
        with self.database.transaction() as db:
            run = db.get(Run, self.run['id'])
            run.received_at, run.started_at, run.first_content_at = 1000, 1002, 1004
        events = [
            {'type': 'model.started', 'span_id': 'model-1', 'model': 'test-model', 'at': at(1002)},
            {'type': 'model.first_chunk', 'span_id': 'model-1', 'duration_ms': 200, 'kind': 'tool_call'},
            {'type': 'model.finished', 'span_id': 'model-1', 'at': at(1002.5), 'status': 'completed', 'duration_ms': 500,
             'usage': {'input': 10, 'output': 0, 'total': 10}},
            {'type': 'tool.started', 'span_id': 'tool-1', 'tool_call_id': 'call-1', 'parent_id': 'model-1',
             'name': 'get_daily_price', 'at': at(1002.5), 'arguments': {'ts_code': '000001.SZ', 'api_key': 'never-expose-this'}},
            {'type': 'tool.finished', 'span_id': 'tool-1', 'at': at(1003.5), 'name': 'get_daily_price',
             'status': 'failed', 'duration_ms': 1000, 'summary': 'Error: upstream unavailable'},
            {'type': 'tool.started', 'span_id': 'tool-2', 'tool_call_id': 'call-2', 'parent_id': 'model-1',
             'name': 'get_daily_price', 'at': at(1003.5), 'arguments': {'ts_code': '000001.SZ'}},
            {'type': 'tool.finished', 'span_id': 'tool-2', 'at': at(1003.6), 'name': 'get_daily_price',
             'status': 'completed', 'duration_ms': 100, 'summary': 'fixture-summary'},
            {'type': 'model.started', 'span_id': 'model-2', 'model': 'test-model', 'at': at(1003.6)},
            {'type': 'model.finished', 'span_id': 'model-2', 'at': at(1005), 'status': 'completed', 'duration_ms': 1400, 'usage': None},
            {'type': 'execution.completed', 'message_id': 'answer', 'text': '数据源部分失败，请稍后重试', 'history': []},
        ]
        for event in events:
            self.runs.record(self.run['id'], 'fixture', event)
        with self.database.transaction() as db:
            db.get(Run, self.run['id']).finished_at = 1005

    def test_regular_user_cannot_access_any_administrator_endpoint(self):
        for path in ('/admin/users', '/admin/runs', '/admin/runs/' + self.run['id']):
            response = self.client.get('/api/v1' + path)
            self.assertEqual(response.status_code, 403)
            self.assertNotIn('fixture-summary', response.text)

    def test_list_filters_counts_and_pagination_use_persisted_calls(self):
        self.login(username='admin')
        response = self.client.get('/api/v1/admin/runs', params={'username': 'ALICE', 'status': 'completed',
            'tool': 'get_daily_price', 'session_id': self.session['id'], 'since': at(999), 'until': at(1006), 'limit': 1})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result['total'], 1)
        row = result['runs'][0]
        self.assertEqual((row['tool_calls'], row['failed_tools'], row['slowest_tool_ms']), (2, 1, 1000))
        self.assertEqual(row['queue_ms'], 2000)
        self.assertEqual(row['execution_ms'], 3000)
        self.assertEqual(row['first_content_ms'], 4000)
        self.assertEqual(self.client.get('/api/v1/admin/runs?tool=unknown').json()['total'], 0)
        self.assertEqual(self.client.get('/api/v1/admin/runs?offset=1').json()['runs'], [])
        self.assertNotIn('never-expose-this', response.text)

    def test_detail_preserves_distinct_calls_unknown_usage_and_redacts_old_records(self):
        self.login(username='admin')
        result = self.client.get('/api/v1/admin/runs/' + self.run['id'])
        self.assertEqual(result.status_code, 200)
        detail = result.json()
        self.assertNotIn('never-expose-this', result.text)
        self.assertEqual(detail['usage'], {'input': None, 'output': None, 'total': None})
        self.assertEqual(len(detail['spans']), 4)
        tools = [s for s in detail['spans'] if s['kind'] == 'tool']
        self.assertEqual([s['tool_call_id'] for s in tools], ['call-1', 'call-2'])
        self.assertEqual([s['duration_ms'] for s in tools], [1000, 100])
        first = detail['spans'][0]
        self.assertEqual(first['first_chunk_ms'], 200)
        self.assertEqual(first['usage']['output'], 0)
        self.assertIsNone(detail['browser_first_render_ms'])
        self.assertEqual(detail['run']['status'], 'completed')  # Failed tool is not a failed whole answer.

    def test_filter_validation_and_role_revocation_apply_to_actual_admin_routes(self):
        self.login(username='admin')
        for params in ({'status': 'invented'}, {'limit': 101}, {'offset': -1}, {'since': '2026-11-05T12:00:00'},
                       {'since': at(1005), 'until': at(1000)}):
            self.assertEqual(self.client.get('/api/v1/admin/runs', params=params).status_code, 422)
        users = self.client.get('/api/v1/admin/users')
        self.assertNotIn('password_hash', users.text)
        self.assertEqual(len(users.json()['users']), 3)
        with self.database.transaction() as db:
            db.scalar(select(User).where(User.username == 'admin')).role = 'user'
        self.assertEqual(self.client.get('/api/v1/admin/runs').status_code, 403)
