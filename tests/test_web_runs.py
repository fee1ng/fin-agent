import json
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from sqlalchemy import select
from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk

from fin_agent.agent.core import FinAgent
from fin_agent.agent.redaction import Redactor
from fin_agent.agent.web_observer import ModelObserver, ObservedTools
from fin_agent.agent.web_policy import ReadOnlyTools, WEB_SYSTEM_PROMPT
from fin_agent.web.filtering import TextFilter, visible
from fin_agent.web.models import Conversation, Run, Span
from fin_agent.web.worker import Worker
from tests.test_web_agent import ScriptedModel
from tests.test_web_api import WebApiFixture


def controlled_factory(emit):
    schemas = [{'type': 'function', 'function': {'name': 'get_current_time',
                'parameters': {'type': 'object', 'properties': {}}}}]
    def tool(name, args):
        time.sleep(.03)
        return {'time': '2026-11-04'}
    observer = ModelObserver(emit)
    policy = ReadOnlyTools(schemas, tool)
    with patch.object(FinAgent, '_create_llm', return_value=ScriptedModel()):
        return FinAgent(tool_schemas=schemas, system_prompt=WEB_SYSTEM_PROMPT,
                        tool_executor=ObservedTools(policy, emit, model_observer=observer), observer=observer)


class SlowModel(ScriptedModel):
    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        if not any(m.type == 'tool' for m in messages):
            yield from super()._stream(messages, stop, run_manager, **kwargs)
        else:
            for _ in range(12):
                yield ChatGenerationChunk(message=AIMessageChunk(content='市场数据分析结果。'))
                time.sleep(.08)


def streaming_factory(emit):
    agent = controlled_factory(emit)
    agent.llm = SlowModel()
    return agent


class FilterTests(unittest.TestCase):
    def test_fragmented_thoughts_nested_tags_and_secrets_never_leave_filter(self):
        text = '公开说明。<think>私密<think>嵌套</think>仍私密</think> API_KEY=unknown-sensitive-value ' + 'prefix fixture-secret suffix。' * 20
        filtering = TextFilter(Redactor(['fixture-secret']))
        result = ''.join(filtering.feed(char) for char in text) + filtering.feed('', final=True)
        self.assertNotIn('私密', result)
        self.assertNotIn('fixture-secret', result)
        self.assertNotIn('unknown-sensitive-value', result)
        self.assertIn('公开说明', result)
        self.assertIn('suffix', result)
        self.assertEqual(visible('safe<think>unclosed', Redactor()), 'safe')


class WebRunTests(WebApiFixture):
    def setUp(self):
        super().setUp()
        self.login()
        self.session = self.create_session()

    def submit(self, key='submission-0001', text='当前时间', session_id=None):
        return self.client.post('/api/v1/sessions/' + (session_id or self.session['id']) + '/runs',
            json={'text': text}, headers={'Idempotency-Key': key})

    def test_submissions_are_idempotent_and_session_serialized_under_concurrency(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.submit(), range(2)))
        self.assertEqual([r.status_code for r in responses], [202, 202])
        self.assertEqual(responses[0].json()['id'], responses[1].json()['id'])
        self.assertEqual(self.submit(key='different-key').status_code, 409)
        self.assertEqual(self.submit(text='changed question').status_code, 409)
        self.assertEqual(self.client.delete('/api/v1/sessions/' + self.session['id']).status_code, 409)
        snapshot = self.client.get('/api/v1/sessions/' + self.session['id'] + '/messages').json()
        self.assertEqual(len(snapshot['messages']), 1)
        self.assertEqual(len(snapshot['runs']), 1)

    def test_real_spawned_agent_persists_answer_context_and_public_sse(self):
        run = self.submit().json()
        with Worker(self.settings, factory=controlled_factory) as worker:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                worker.tick()
                current = self.client.get('/api/v1/runs/' + run['id']).json()
                if current['status'] not in ('queued', 'running'):
                    break
                time.sleep(.03)
            self.assertEqual(current['status'], 'completed', current)
        snapshot = self.client.get('/api/v1/sessions/' + self.session['id'] + '/messages').json()
        answer = [m for m in snapshot['messages'] if m['role'] == 'assistant' and m['phase'] == 'final']
        self.assertEqual(len(answer), 1)
        self.assertEqual(answer[0]['content'], '查询完成')
        events = self.client.get('/api/v1/runs/' + run['id'] + '/events')
        self.assertEqual(events.status_code, 200)
        self.assertIn('event: message.delta', events.text)
        self.assertIn('event: run.completed', events.text)
        self.assertNotIn('arguments', events.text)
        self.assertNotIn('model.started', events.text)
        with self.database.transaction() as db:
            self.assertTrue(db.get(Conversation, self.session['id']).history)
            spans = db.scalars(select(Span).where(Span.run_id == run['id'])).all()
            self.assertEqual(len(spans), 4)  # two model requests and two individual tools
            self.assertTrue(all(s.finished for s in spans))
        count = len(snapshot['messages'])
        # Replay of the original key after completion still cannot create new work.
        self.assertEqual(self.submit().json()['id'], run['id'])
        self.assertEqual(len(self.client.get('/api/v1/sessions/' + self.session['id'] + '/messages').json()['messages']), count)
        with Worker(self.settings, factory=controlled_factory) as worker:
            next_run = self.submit(key='next-turn-0001', text='继续分析').json()
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                worker.tick()
                status = self.client.get('/api/v1/runs/' + next_run['id']).json()['status']
                if status not in ('queued', 'running'):
                    break
                time.sleep(.03)
            self.assertEqual(status, 'completed')
        with self.database.transaction() as db:
            history = db.get(Conversation, self.session['id']).history
        self.assertEqual([m['content'] for m in history if m['role'] == 'user'], ['当前时间', '继续分析'])

    def test_snapshot_and_stream_access_are_checked_against_owner(self):
        run = self.submit().json()
        other = self.new_client()
        self.login(other, 'bravo')
        for path in ('/runs/' + run['id'], '/runs/' + run['id'] + '/events',
                     '/sessions/' + self.session['id'] + '/messages'):
            self.assertEqual(other.get('/api/v1' + path).status_code, 404)

    def test_two_sessions_run_in_parallel_and_partial_answers_exist_before_completion(self):
        first = self.submit().json()
        second_session = self.create_session()
        second = self.submit(key='submission-0002', text='另一个主题', session_id=second_session['id']).json()
        third_session = self.create_session()
        self.assertEqual(self.submit(key='submission-0003', session_id=third_session['id']).status_code, 429)
        partial_seen = False
        with Worker(self.settings, factory=streaming_factory) as worker:
            worker.tick()
            self.assertEqual(len(worker.children), 2)
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                worker.tick()
                snapshot = self.client.get('/api/v1/sessions/' + self.session['id'] + '/messages').json()
                if snapshot['runs'][0]['status'] == 'running' and any(m['role'] == 'assistant' and m['content'] for m in snapshot['messages']):
                    partial_seen = True
                states = [self.client.get('/api/v1/runs/' + r['id']).json()['status'] for r in (first, second)]
                if all(s == 'completed' for s in states):
                    break
                time.sleep(.03)
            self.assertEqual(states, ['completed', 'completed'])
        self.assertTrue(partial_seen, 'A complete-only reply cannot satisfy streaming acceptance')
        with self.database.transaction() as db:
            first_history = db.get(Conversation, self.session['id']).history
            second_history = db.get(Conversation, second_session['id']).history
        self.assertNotIn('另一个主题', json.dumps(first_history, ensure_ascii=False))
        self.assertIn('另一个主题', json.dumps(second_history, ensure_ascii=False))

    def test_terminal_failure_does_not_commit_context_or_allow_late_writes(self):
        from fin_agent.web.runs import Runs
        runs = Runs(self.database, self.settings)
        run = self.submit().json()
        runs.claim('owner')
        runs.record(run['id'], 'owner', {'type': 'message.delta', 'message_id': 'partial', 'text': '部分回答'})
        runs.record(run['id'], 'owner', {'type': 'execution.failed'})
        self.assertFalse(runs.record(run['id'], 'owner', {'type': 'message.delta', 'message_id': 'partial', 'text': '迟到'}))
        with self.database.transaction() as db:
            self.assertIsNone(db.get(Conversation, self.session['id']))
        snapshot = self.client.get('/api/v1/sessions/' + self.session['id'] + '/messages').json()
        self.assertEqual(snapshot['messages'][-1]['status'], 'failed')

    def test_storage_failure_rolls_back_answer_context_and_terminal_state_together(self):
        from fin_agent.web.runs import Runs
        from sqlalchemy.exc import IntegrityError
        runs = Runs(self.database, self.settings)
        run = self.submit().json()
        runs.claim('owner')
        with self.database.engine.begin() as connection:
            connection.exec_driver_sql("CREATE TRIGGER fail_terminal BEFORE INSERT ON run_events WHEN NEW.type='run.completed' BEGIN SELECT RAISE(ABORT,'injected storage failure'); END")
        with self.assertRaises(IntegrityError):
            runs.record(run['id'], 'owner', {'type': 'execution.completed', 'message_id': 'answer',
                                            'text': '不可伪造成功', 'history': [{'role': 'assistant', 'content': '不可伪造成功'}]})
        snapshot = self.client.get('/api/v1/sessions/' + self.session['id'] + '/messages').json()
        self.assertEqual(snapshot['runs'][0]['status'], 'running')
        self.assertEqual(len(snapshot['messages']), 1)
        with self.database.transaction() as db:
            self.assertIsNone(db.get(Conversation, self.session['id']))

    def test_queue_and_user_capacity_are_enforced_without_creating_extra_messages(self):
        from dataclasses import replace
        from fastapi import HTTPException
        from fin_agent.web.runs import Runs
        runs = Runs(self.database, replace(self.settings, queue_limit=1))
        runs.submit(self.session['id'], self.login().json()['id'], '问题', 'capacity-0001')
        second = self.create_session()
        with self.assertRaises(HTTPException) as rejected:
            runs.submit(second['id'], self.login().json()['id'], '另一个问题', 'capacity-0002')
        self.assertEqual(rejected.exception.status_code, 429)
        self.assertEqual(self.client.get('/api/v1/sessions/' + second['id'] + '/messages').json()['messages'], [])

    def test_failure_details_are_retained_only_for_diagnostics(self):
        from fin_agent.web.runs import Runs
        runs = Runs(self.database, self.settings)
        run = self.submit().json()
        runs.claim('owner')
        runs.record(run['id'], 'owner', {'type': 'execution.failed', 'error': {
            'type': 'TimeoutError', 'message': 'provider connection timed out'}})
        public = self.client.get('/api/v1/runs/' + run['id'] + '/events').text
        self.assertNotIn('provider connection timed out', public)
        with self.database.transaction() as db:
            span = db.scalar(select(Span).where(Span.run_id == run['id'], Span.kind == 'execution'))
            self.assertEqual(span.finished['error']['type'], 'TimeoutError')


if __name__ == '__main__':
    unittest.main()
