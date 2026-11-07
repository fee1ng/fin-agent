import multiprocessing
import time
import socket
import os
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch

import psutil
from sqlalchemy import delete, select

from fin_agent.web.api import create_app
from fin_agent.web.models import Conversation, Message, Run, RunEvent, Span, WorkerState
from fin_agent.web.processes import terminate_identity
from fin_agent.web.runs import Runs
from fin_agent.web.worker import Worker
from tests.test_web_api import WebApiFixture
from tests.test_web_api import PASSWORD
from tests.test_web_runs import controlled_factory, streaming_factory


def hang_tool(name, arguments):
    time.sleep(60)
    return 'not reached during fault tests'


def hanging_factory(emit):
    from fin_agent.agent.web_policy import ReadOnlyTools
    agent = controlled_factory(emit)
    agent.tool_executor.execute = ReadOnlyTools(agent.tool_schemas, hang_tool)
    return agent


def supervisor(settings):
    Worker(settings, factory=hanging_factory).serve()


def serve_api(settings, port):
    import uvicorn
    # Each restarted Windows process must own a fresh kernel socket; sharing an
    # IOCP-associated listening handle across restarts is not supported.
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', port))
        listener.listen(16)
        uvicorn.Server(uvicorn.Config(create_app(settings), log_config=None,
                                     access_log=False, log_level='error')).run(sockets=[listener])


def admission_probe_factory(emit):
    Path(os.environ['FIN_TEST_ADMISSION_MARKER']).write_text('executed', encoding='utf-8')
    return controlled_factory(emit)


class RecoveryTests(WebApiFixture):
    def setUp(self):
        super().setUp()
        self.login()
        self.session = self.create_session()
        self.runs = Runs(self.database, self.settings)

    def submit(self, key='recovery-0001', session_id=None):
        response = self.client.post('/api/v1/sessions/' + (session_id or self.session['id']) + '/runs',
            json={'text': '查询时间'}, headers={'Idempotency-Key': key})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def retry(self, run_id, key='retry-0001'):
        return self.client.post('/api/v1/runs/' + run_id + '/retry', headers={'Idempotency-Key': key})

    def wait(self, condition, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = condition()
            if result:
                return result
            time.sleep(.04)
        self.fail('Timed out waiting for verified process state')

    def test_killed_supervisor_recovers_running_and_queued_work_and_fences_old_events(self):
        first = self.submit()
        second_session = self.create_session()
        second = self.submit('recovery-0002', second_session['id'])
        process = multiprocessing.get_context('spawn').Process(target=supervisor,
            args=(replace(self.settings, concurrency=1),))
        process.start()
        child_identity = None
        try:
            def admitted():
                with self.database.transaction() as db:
                    run = db.get(Run, first['id'])
                    tool = db.scalar(select(Span).where(Span.run_id == run.id, Span.kind == 'tool'))
                    return (run.child_pid, run.child_created_at, run.owner) if run.child_pid and tool else None
            child_identity = self.wait(admitted)
            pid, created, old_owner = child_identity
            psutil.Process(process.pid).kill()
            process.join(timeout=5)
            self.assertFalse(process.is_alive())
            with Worker(self.settings, factory=controlled_factory) as recovered:
                recovered.tick()
                self.assertEqual(len(recovered.children), 0, 'Restart must not replay old inputs')
                self.assertFalse(self.runs.record(first['id'], old_owner,
                    {'type': 'message.delta', 'message_id': 'late', 'text': 'late result'}))
                for run_id in (first['id'], second['id']):
                    self.assertEqual(self.client.get('/api/v1/runs/' + run_id).json()['status'], 'interrupted')
                with self.database.transaction() as db:
                    self.assertIsNone(db.get(Conversation, self.session['id']))
                    tool = db.scalar(select(Span).where(Span.run_id == first['id'], Span.kind == 'tool'))
                    self.assertEqual(tool.finished['status'], 'interrupted')
            # On Linux an orphan can briefly remain a non-executing zombie
            # until PID 1 reaps it. It is no longer a live execution process.
            if psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE:
                self.assertNotAlmostEqual(psutil.Process(pid).create_time(), created, delta=.001)
        finally:
            if process.is_alive():
                psutil.Process(process.pid).kill()
                process.join(timeout=5)
            process.close()
            if child_identity:
                terminate_identity(child_identity[0], child_identity[1])

    def test_timeout_stops_subprocess_before_capacity_is_released(self):
        run = self.submit()
        settings = replace(self.settings, run_timeout=.4)
        with Worker(settings, factory=hanging_factory) as worker:
            worker.tick()
            pid = next(iter(worker.children.values())).process.pid
            created = psutil.Process(pid).create_time()
            def settled():
                worker.tick()
                return self.client.get('/api/v1/runs/' + run['id']).json()['status'] == 'failed'
            self.wait(settled)
            self.assertEqual(worker.children, {})
            self.assertFalse(psutil.pid_exists(pid) and psutil.Process(pid).create_time() == created)
            self.assertIn('timeout', self.client.get('/api/v1/runs/' + run['id'] + '/events').text)

    def test_explicit_retry_reuses_question_but_creates_one_new_run(self):
        original = self.submit()
        self.runs.claim('manual-owner')
        self.runs.record(original['id'], 'manual-owner', {'type': 'execution.failed'})
        with Worker(self.settings, factory=controlled_factory) as worker:
            retried = self.retry(original['id']).json()
            self.assertNotEqual(retried['id'], original['id'])
            self.assertEqual(retried['retry_of'], original['id'])
            self.assertEqual(self.retry(original['id']).json()['id'], retried['id'])
            self.assertEqual(self.retry(original['id'], 'another-retry-key').status_code, 409)
            def completed():
                worker.tick()
                return self.client.get('/api/v1/runs/' + retried['id']).json()['status'] == 'completed'
            self.wait(completed)
        snapshot = self.client.get('/api/v1/sessions/' + self.session['id'] + '/messages').json()
        self.assertEqual(len([m for m in snapshot['messages'] if m['role'] == 'user']), 1)
        with self.database.transaction() as db:
            history = db.get(Conversation, self.session['id']).history
        self.assertEqual(len([m for m in history if m['role'] == 'user']), 1)
        self.assertEqual(self.retry(retried['id']).status_code, 409)

    def test_pid_reuse_guard_never_terminates_unrelated_process(self):
        process = multiprocessing.get_context('spawn').Process(target=time.sleep, args=(30,))
        process.start()
        created = psutil.Process(process.pid).create_time()
        try:
            terminate_identity(process.pid, created - .0001)
            self.assertTrue(process.is_alive())
        finally:
            terminate_identity(process.pid, created)
            process.join(timeout=5)
            process.close()

    def test_only_one_worker_can_own_database(self):
        with Worker(self.settings):
            with self.assertRaises(OSError):
                with Worker(self.settings):
                    self.fail('Second executor acquired the ownership lock')

    def test_event_cursor_precedence_and_expired_replay_fall_back_to_snapshot(self):
        run = self.submit()
        self.runs.claim('manual-owner')
        self.runs.record(run['id'], 'manual-owner', {'type': 'message.delta', 'message_id': 'm', 'text': '部分正文'})
        self.runs.record(run['id'], 'manual-owner', {'type': 'execution.failed'})
        response = self.client.get('/api/v1/runs/' + run['id'] + '/events?after=0', headers={'Last-Event-ID': '2'})
        self.assertNotIn('event: run.queued', response.text)
        self.assertIn('id: 3', response.text)
        with self.database.transaction() as db:
            db.execute(delete(RunEvent).where(RunEvent.run_id == run['id']))
        response = self.client.get('/api/v1/runs/' + run['id'] + '/events?after=0')
        self.assertIn('event: stream.reset', response.text)
        snapshot = self.client.get('/api/v1/sessions/' + self.session['id'] + '/messages').json()
        self.assertEqual(snapshot['messages'][-1]['content'], '部分正文')
        self.assertEqual(len(snapshot['runs']), 1)

    def test_api_recreated_while_worker_continues_without_subscription(self):
        run = self.submit()
        with Worker(self.settings, factory=streaming_factory) as worker:
            worker.tick()
            # A distinct API app/connection pool is created and shut down while
            # the worker is live. There is no browser/SSE subscription at all.
            from fastapi.testclient import TestClient
            with TestClient(create_app(self.settings)) as replacement:
                self.assertEqual(replacement.get('/api/v1/health').status_code, 200)
            def completed():
                worker.tick()
                return self.client.get('/api/v1/runs/' + run['id']).json()['status'] == 'completed'
            self.wait(completed)
        snapshot = self.client.get('/api/v1/sessions/' + self.session['id'] + '/messages').json()
        self.assertTrue(any(m['role'] == 'assistant' and m['status'] == 'completed' for m in snapshot['messages']))

    def test_unregistered_child_never_executes_a_tool_when_admission_write_fails(self):
        marker = self.settings.database.parent / 'admission-marker'
        with patch.dict(os.environ, {'FIN_TEST_ADMISSION_MARKER': str(marker)}):
            with Worker(self.settings, factory=admission_probe_factory) as worker:
                run = self.submit()
                with patch.object(worker.runs, 'register_process', side_effect=OSError('injected write failure')):
                    with self.assertRaises(OSError):
                        worker.tick()
                self.assertFalse(marker.exists())
                self.assertEqual(worker.children, {})
                self.assertEqual(self.client.get('/api/v1/runs/' + run['id']).json()['status'], 'failed')

    def test_database_failure_stops_all_children_and_restart_settles_old_runs(self):
        from sqlalchemy.exc import IntegrityError
        first = self.submit()
        second_session = self.create_session()
        second = self.submit('failure-0002', second_session['id'])
        identities = []
        try:
            with self.assertRaises(IntegrityError):
                with Worker(self.settings, factory=hanging_factory) as worker:
                    worker.tick()
                    identities = [(child.process.pid, psutil.Process(child.process.pid).create_time())
                                  for child in worker.children.values()]
                    self.assertEqual(len(identities), 2)
                    with self.database.engine.begin() as connection:
                        connection.exec_driver_sql("CREATE TRIGGER fail_run_writes BEFORE UPDATE ON runs BEGIN SELECT RAISE(ABORT,'injected storage failure'); END")
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        worker.tick()
                        time.sleep(.04)
                    self.fail('The injected storage error was not observed')
        finally:
            with self.database.engine.begin() as connection:
                connection.exec_driver_sql('DROP TRIGGER IF EXISTS fail_run_writes')
            survivors = []
            for pid, created in identities:
                if psutil.pid_exists(pid) and psutil.Process(pid).create_time() == created:
                    survivors.append(pid)
                    terminate_identity(pid, created)
            self.assertFalse(survivors, 'Worker cleanup left execution subprocesses alive')
        with Worker(self.settings, factory=controlled_factory) as restarted:
            restarted.tick()
            self.assertEqual(restarted.children, {})
        for run in (first, second):
            self.assertEqual(self.client.get('/api/v1/runs/' + run['id']).json()['status'], 'interrupted')

    def test_slow_network_reader_and_actual_api_process_restart_do_not_stop_execution(self):
        import httpx
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
        listener.close()
        base = f'http://127.0.0.1:{port}'
        settings = replace(self.settings, origin=base, secure_cookie=False)
        context = multiprocessing.get_context('spawn')
        processes = []

        def launch():
            process = context.Process(target=serve_api, args=(settings, port))
            process.start()
            processes.append(process)
            def ready():
                if not process.is_alive():
                    self.fail('API process exited before readiness')
                try:
                    return httpx.get(base + '/api/v1/health', timeout=.3).status_code == 200
                except httpx.HTTPError:
                    return False
            self.wait(ready)
            return process

        try:
            api_process = launch()
            with httpx.Client(base_url=base, headers={'Origin': base}, timeout=5) as browser:
                login = browser.post('/api/v1/auth/login', json={'username': 'alice', 'password': PASSWORD})
                self.assertEqual(login.status_code, 200)
                browser.headers['X-CSRF-Token'] = login.json()['csrf_token']
                session = browser.post('/api/v1/sessions', json={}).json()
                with Worker(settings, factory=streaming_factory) as worker:
                    run = browser.post('/api/v1/sessions/' + session['id'] + '/runs',
                        json={'text': '查询市场'}, headers={'Idempotency-Key': 'network-restart-0001'}).json()
                    worker.tick()
                    with browser.stream('GET', '/api/v1/runs/' + run['id'] + '/events') as stream:
                        self.assertEqual(stream.status_code, 200)
                        self.assertIn('event:', next(line for line in stream.iter_lines() if line.startswith('event:')))
                        # Hold a real HTTP stream open and stop reading; SQLite-backed
                        # execution must make progress independently of this reader.
                        def partial():
                            worker.tick()
                            snapshot = self.runs.snapshot(session['id'], login.json()['id'])
                            return any(m['role'] == 'assistant' and m['content'] for m in snapshot['messages'])
                        self.wait(partial)
                        api_process.kill()
                        api_process.join(timeout=5)
                        self.assertFalse(api_process.is_alive())
                        def completed():
                            worker.tick()
                            return self.runs.get(run['id'], login.json()['id'])['status'] == 'completed'
                        self.wait(completed)
                cookies = browser.cookies
            launch()
            with httpx.Client(base_url=base, cookies=cookies, headers={'Origin': base}, timeout=5) as reopened:
                response = reopened.get('/api/v1/sessions/' + session['id'] + '/messages')
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()['runs'][0]['status'], 'completed')
                self.assertEqual(len(response.json()['runs']), 1)
                replay = reopened.get('/api/v1/runs/' + run['id'] + '/events', headers={'Last-Event-ID': '1'})
                self.assertIn('run.completed', replay.text)
                self.assertNotIn('event: run.queued', replay.text)
        finally:
            for process in processes:
                if process.is_alive():
                    process.kill()
                process.join(timeout=5)
                process.close()
            listener.close()
