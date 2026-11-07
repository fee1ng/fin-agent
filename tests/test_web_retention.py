import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from fin_agent.web.api import create_app
from fin_agent.web.auth import create_user
from fin_agent.web.backups import backup_file, checksum, prune_backups, restore_file
from fin_agent.web.database import Database
from fin_agent.web.deletions import DeletionJournal, flush_deletions
from fin_agent.web.maintenance import cleanup
from fin_agent.web.models import AuthSession, ChatSession, Conversation, DeletedSession, Message, Run, RunEvent, Span, identifier
from fin_agent.web.runs import Runs
from fin_agent.web.settings import Settings
from fin_agent.web.worker import Worker
from tests.test_web_api import PASSWORD, ORIGIN


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(Path(self.temp.name) / 'web.sqlite3', ORIGIN)
        self.db = Database(self.settings.database)
        self.addCleanup(self.db.close)
        self.db.migrate()
        self.owner = create_user(self.db, 'administrator', PASSWORD, 'admin')
        self.runs = Runs(self.db, self.settings)

    def seed(self, *, age=0, running=False):
        with self.db.transaction() as db:
            session = ChatSession(user_id=self.owner, title='保留策略测试')
            db.add(session)
            db.flush()
            session_id = session.id
        run = self.runs.submit(session_id, self.owner, '查询行情', identifier())
        self.runs.claim('fixture')
        stamp = time.time() - age * 86400
        span = identifier()
        self.runs.record(run['id'], 'fixture', {'type': 'model.started', 'span_id': span, 'at': '2026-11-05T01:00:00+00:00'})
        self.runs.record(run['id'], 'fixture', {'type': 'message.delta', 'message_id': identifier(), 'text': '部分结果'})
        if not running:
            self.runs.record(run['id'], 'fixture', {'type': 'model.finished', 'span_id': span, 'status': 'completed', 'duration_ms': 10})
            self.runs.record(run['id'], 'fixture', {'type': 'execution.completed', 'message_id': identifier(),
                'text': '完整结果', 'history': [{'role': 'user', 'content': '查询行情'}, {'role': 'assistant', 'content': '完整结果'}]})
        with self.db.transaction() as db:
            row = db.get(Run, run['id'])
            row.received_at, row.started_at = stamp - 3, stamp - 2
            if not running:
                row.finished_at = stamp
        return session_id, run['id']

    def login(self, client):
        response = client.post('/api/v1/auth/login', json={'username': 'administrator', 'password': PASSWORD})
        self.assertEqual(response.status_code, 200)
        client.headers['X-CSRF-Token'] = response.json()['csrf_token']

    def client(self, **options):
        return TestClient(create_app(self.settings), base_url=ORIGIN, headers={'Origin': ORIGIN}, **options)

    def test_trace_and_event_ttl_are_independent_and_preserve_context_and_active_runs(self):
        old_session, old = self.seed(age=31)
        _, recent = self.seed(age=2)
        _, active = self.seed(age=40, running=True)
        result = cleanup(self.db, self.settings)
        self.assertEqual(result, {'trace_runs_purged': 1, 'event_runs_purged': 2})
        with self.db.transaction() as db:
            self.assertIsNone(db.scalar(select(Span).where(Span.run_id == old)))
            self.assertIsNotNone(db.scalar(select(Span).where(Span.run_id == recent)))
            self.assertIsNotNone(db.scalar(select(RunEvent).where(RunEvent.run_id == active)))
            self.assertIsNone(db.get(Run, active).trace_purged_at)
            self.assertTrue(db.get(Conversation, old_session).history)
            self.assertEqual(db.get(Run, old).trace_summary['model_calls'], 1)
        with self.client() as client:
            self.login(client)
            detail = client.get('/api/v1/admin/runs/' + old).json()
            self.assertEqual(detail['trace_state'], 'expired')
            self.assertEqual(detail['model_calls'], 1)
            self.assertIn('stream.reset', client.get('/api/v1/runs/' + old + '/events').text)
            self.assertTrue(client.get('/api/v1/sessions/' + old_session + '/messages').json()['messages'])

    def test_online_backup_delete_and_offline_restore_do_not_resurrect_conversation_or_login(self):
        deleted, run_id = self.seed()
        kept, _ = self.seed()
        backup = self.settings.database.parent / 'backup.sqlite3'
        with self.client() as client:
            self.login(client)
            backup_file(self.db, backup)  # Consistent backup while the API is online.
            old_cookie = client.cookies
            self.assertEqual(client.delete('/api/v1/sessions/' + deleted).status_code, 204)
        with self.db.transaction() as db:
            self.assertIsNone(db.get(Run, run_id))
            self.assertIsNone(db.get(Conversation, deleted))
            for model in (Message, RunEvent, Span):
                self.assertIsNone(db.scalar(select(model).where(model.run_id == run_id)))
        self.assertEqual(DeletionJournal(self.db.path).read()[0]['session_id'], deleted)
        restored = restore_file(self.db, backup)
        self.assertTrue(Path(restored['rollback_backup']).exists())
        with self.db.transaction() as db:
            self.assertIsNone(db.get(ChatSession, deleted))
            self.assertIsNotNone(db.get(ChatSession, kept))
            self.assertEqual(db.scalar(select(func.count()).select_from(AuthSession)), 0)
        with self.client() as client:
            client.cookies.update(old_cookie)
            self.assertEqual(client.get('/api/v1/auth/me').status_code, 401)

    def test_restore_requires_api_and_worker_to_be_offline_and_rejects_modified_backup(self):
        self.seed()
        backup = self.settings.database.parent / 'backup.sqlite3'
        backup_file(self.db, backup)
        with self.client():
            # A different container cannot resolve the API PID. The shared
            # filesystem lifetime lock must still reject an online restore.
            with patch('fin_agent.web.services.psutil.Process', side_effect=AssertionError('PID namespace unavailable')):
                with self.assertRaisesRegex(RuntimeError, 'Stop all Web API'):
                    restore_file(self.db, backup)
            with self.assertRaisesRegex(RuntimeError, 'Stop all Web API'):
                restore_file(self.db, backup)
        with Worker(self.settings):
            with self.assertRaises(OSError):
                restore_file(self.db, backup)
        with backup.open('ab') as file:
            file.write(b'tampered')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            restore_file(self.db, backup)

    def test_restore_after_database_loss_marks_unfinished_backup_runs_interrupted(self):
        session, run = self.seed(running=True)
        backup = self.settings.database.parent / 'backup.sqlite3'
        backup_file(self.db, backup)
        self.db.close()
        self.settings.database.unlink()
        result = restore_file(self.db, backup)
        self.assertIsNone(result['rollback_backup'])
        with self.db.transaction() as db:
            self.assertEqual(db.get(Run, run).status, 'interrupted')
            self.assertIsNotNone(db.get(ChatSession, session))

    def test_backup_pruning_never_removes_unrelated_or_modified_files(self):
        self.seed()
        directory = self.settings.database.parent / 'backups'
        target = directory / 'old.sqlite3'
        backup_file(self.db, target)
        metadata = Path(str(target) + '.json')
        manifest = json.loads(metadata.read_text())
        manifest['created_at'] = time.time() - 8 * 86400
        metadata.write_text(json.dumps(manifest))
        unrelated = directory / 'unrelated.sqlite3'
        unrelated.write_text('user file')
        (directory / 'unrelated.sqlite3.json').write_text('[]')
        self.assertEqual(prune_backups(self.db, directory, 7), 1)
        self.assertFalse(target.exists())
        self.assertEqual(unrelated.read_text(), 'user file')
        with self.assertRaises(FileExistsError):
            backup_file(self.db, unrelated)

    def test_failed_journal_delivery_cannot_acknowledge_delete_and_pending_outbox_is_replayed(self):
        session, _ = self.seed()
        backup = self.settings.database.parent / 'backup.sqlite3'
        backup_file(self.db, backup)
        with self.client(raise_server_exceptions=False) as client:
            self.login(client)
            with patch.object(DeletionJournal, 'deliver', side_effect=OSError('journal unavailable')):
                self.assertEqual(client.delete('/api/v1/sessions/' + session).status_code, 500)
        with self.db.transaction() as db:
            self.assertFalse(db.get(DeletedSession, session).delivered)
        flush_deletions(self.db)
        restore_file(self.db, backup)
        with self.db.transaction() as db:
            self.assertIsNone(db.get(ChatSession, session))
