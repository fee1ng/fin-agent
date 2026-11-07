import tempfile
import time
import unittest
import io
from contextlib import ExitStack
from contextlib import redirect_stdout
from pathlib import Path
from typing import Annotated
from unittest.mock import patch

from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import select

from fin_agent.web.api import COOKIE, create_app
from fin_agent.web.auth import create_user, set_disabled
from fin_agent.web.database import Database
from fin_agent.web.models import AuthSession, User
from fin_agent.web.settings import Settings

PASSWORD = 'controlled-test-password'
ORIGIN = 'https://fin-agent.test'


class WebApiFixture(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.settings = Settings(Path(directory) / 'web.sqlite3', ORIGIN)
        self.database = Database(self.settings.database)
        self.stack.callback(self.database.close)
        self.database.migrate()
        self.database.migrate()  # Upgrade is repeatable; no duplicate tables/data loss.
        create_user(self.database, 'alice', PASSWORD)
        create_user(self.database, 'bravo', PASSWORD)
        create_user(self.database, 'admin', PASSWORD, 'admin')
        self.app = create_app(self.settings)

        @self.app.get('/api/test-admin')
        def test_admin(user: Annotated[User, Depends(self.app.state.admin_user)]):
            return {'role': user.role}

        self.client = self.new_client()

    def new_client(self):
        return self.stack.enter_context(TestClient(self.app, base_url=ORIGIN, headers={'Origin': ORIGIN}))

    def login(self, client=None, username='alice'):
        client = client or self.client
        response = client.post('/api/v1/auth/login', json={'username': username, 'password': PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        client.headers['X-CSRF-Token'] = response.json()['csrf_token']
        return response

    def create_session(self, client=None, title='市场研究'):
        client = client or self.client
        response = client.post('/api/v1/sessions', json={'title': title})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()


class WebApiTests(WebApiFixture):
    def test_login_cookie_hash_and_logout_revoke_session(self):
        response = self.login()
        cookie = response.headers['set-cookie']
        self.assertIn('HttpOnly', cookie)
        self.assertIn('Secure', cookie)
        self.assertIn('SameSite=strict', cookie)
        self.assertIn('Path=/api', cookie)
        raw = self.client.cookies.get(COOKIE)
        with self.database.transaction() as db:
            auth = db.scalar(select(AuthSession))
            self.assertNotEqual(auth.token_hash, raw)
            self.assertNotIn(raw, db.scalar(select(User.password_hash)))
        self.assertEqual(self.client.get('/api/v1/auth/me').json()['username'], 'alice')
        self.assertEqual(self.client.post('/api/v1/auth/logout').status_code, 204)
        self.client.cookies.set(COOKIE, raw, domain='fin-agent.test', path='/api')
        self.assertEqual(self.client.get('/api/v1/auth/me').status_code, 401)

    def test_two_users_cannot_read_rename_or_delete_each_others_session(self):
        self.login()
        session = self.create_session()
        other = self.new_client()
        self.login(other, 'bravo')
        self.assertEqual(other.get('/api/v1/sessions').json()['sessions'], [])
        url = '/api/v1/sessions/' + session['id']
        self.assertEqual(other.get(url).status_code, 404)
        self.assertEqual(other.patch(url, json={'title': 'attack'}).status_code, 404)
        self.assertEqual(other.delete(url).status_code, 404)
        self.assertEqual(self.client.get(url).json()['title'], '市场研究')

    def test_create_rename_delete_and_pagination(self):
        self.login()
        first = self.create_session(title='  初始标题  ')
        self.create_session(title='另一个主题')
        self.assertEqual(first['title'], '初始标题')
        self.assertEqual(len(self.client.get('/api/v1/sessions?limit=1').json()['sessions']), 1)
        url = '/api/v1/sessions/' + first['id']
        self.assertEqual(self.client.patch(url, json={'title': '新标题'}).json()['title'], '新标题')
        self.assertEqual(self.client.delete(url).status_code, 204)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.get('/api/v1/sessions?offset=-1').status_code, 422)
        self.assertEqual(self.client.post('/api/v1/sessions', json={'title': '  '}).status_code, 422)

    def test_mutations_require_origin_and_csrf_but_login_needs_no_previous_token(self):
        self.assertEqual(self.client.post('/api/v1/auth/login', headers={'Origin': 'https://attacker.test'},
                                         json={'username': 'alice', 'password': PASSWORD}).status_code, 403)
        self.login()
        self.assertEqual(self.client.post('/api/v1/sessions', headers={'X-CSRF-Token': ''}, json={}).status_code, 403)
        self.assertEqual(self.client.post('/api/v1/sessions', headers={'Origin': 'https://attacker.test'}, json={}).status_code, 403)
        self.assertEqual(self.client.post('/api/v1/sessions', json={}).status_code, 201)

    def test_disabled_account_and_expired_session_cannot_authenticate(self):
        self.login()
        set_disabled(self.database, 'alice', True)
        self.assertEqual(self.client.get('/api/v1/auth/me').status_code, 401)
        self.assertEqual(self.client.post('/api/v1/auth/login', json={'username': 'alice', 'password': PASSWORD}).status_code, 401)
        set_disabled(self.database, 'alice', False)
        self.login()
        with self.database.transaction() as db:
            auth = db.scalar(select(AuthSession))
            auth.expires_at = time.time() - 1
        self.assertEqual(self.client.get('/api/v1/auth/me').status_code, 401)

    def test_role_is_loaded_from_database_and_enforced(self):
        self.login()
        self.assertEqual(self.client.get('/api/test-admin').status_code, 403)
        self.login(username='admin')
        self.assertEqual(self.client.get('/api/test-admin').status_code, 200)
        with self.database.transaction() as db:
            db.scalar(select(User).where(User.username == 'admin')).role = 'user'
        self.assertEqual(self.client.get('/api/test-admin').status_code, 403)

    def test_bad_password_failures_persist_and_are_rate_limited(self):
        for _ in range(5):
            response = self.client.post('/api/v1/auth/login', json={'username': 'alice', 'password': 'wrong'})
            self.assertEqual(response.status_code, 401)
        response = self.client.post('/api/v1/auth/login', json={'username': 'alice', 'password': PASSWORD})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers['retry-after'], '900')

    def test_login_rotation_invalidates_previous_cookie(self):
        self.login()
        previous = self.client.cookies.get(COOKIE)
        self.login()
        other = self.new_client()
        other.cookies.set(COOKIE, previous, domain='fin-agent.test', path='/api')
        self.assertEqual(other.get('/api/v1/auth/me').status_code, 401)

    def test_startup_requires_migration_and_configuration_rejects_unsafe_dev_mode(self):
        with self.assertRaisesRegex(RuntimeError, 'migration'):
            create_app(Settings(self.settings.database.parent / 'unmigrated.db', ORIGIN))
        with self.assertRaises(ValueError):
            Settings(self.settings.database, 'http://192.168.1.10', False)
        self.assertEqual(self.client.get('/api/v1/health').headers['cache-control'], 'no-store')

    def test_admin_cli_creates_and_disables_account_without_printing_password(self):
        from fin_agent.web.__main__ import main
        output = io.StringIO()
        with patch.dict('os.environ', {'FIN_WEB_DATABASE': str(self.settings.database),
                                      'FIN_WEB_ORIGIN': ORIGIN, 'FIN_WEB_DEV_HTTP': '0'}), \
             redirect_stdout(output):
            with patch('sys.argv', ['fin-agent-web', 'create-user', 'charlie']), \
                 patch('getpass.getpass', side_effect=[PASSWORD, PASSWORD]):
                main()
            with patch('sys.argv', ['fin-agent-web', 'disable-user', 'charlie']):
                main()
        self.assertNotIn(PASSWORD, output.getvalue())
        with self.database.transaction() as db:
            account = db.scalar(select(User).where(User.username == 'charlie'))
            self.assertTrue(account.disabled)


if __name__ == '__main__':
    unittest.main()
