"""Exercise real offline restore in the isolated local HTTPS acceptance project."""
import json
from pathlib import Path
import ssl
import subprocess
import time
import uuid

import httpx


def main():
    docker = str(Path.home() / 'AppData/Local/Programs/DockerDesktop/resources/bin/docker.exe')
    compose = [docker, '--context', 'desktop-linux', 'compose', '-f', 'compose.web.yaml']
    origin = 'https://localhost:8443'
    context = ssl.create_default_context(cafile='.local/web-tls/server.crt')
    account = json.loads(Path('.local/fin-agent-web.accounts.json').read_text())[1]
    report = {}

    def command(arguments, *, expected=0):
        result = subprocess.run(compose + arguments, text=True, capture_output=True)
        if (result.returncode == 0) != (expected == 0):
            raise RuntimeError('Unexpected Compose result for ' + arguments[0] + ': ' + result.stderr[-500:])
        return result.stdout

    def login(client):
        response = client.post('/api/v1/auth/login', json={key: account[key] for key in ('username', 'password')})
        response.raise_for_status()
        client.headers['X-CSRF-Token'] = response.json()['csrf_token']

    with httpx.Client(base_url=origin, headers={'Origin': origin}, verify=context, trust_env=False, timeout=15) as client:
        login(client)
        response = client.post('/api/v1/sessions', json={'title': '删除重放验收'})
        response.raise_for_status()
        deleted = response.json()['id']
        backup = '/data/backups/acceptance-' + uuid.uuid4().hex + '.sqlite3'
        report['backup'] = json.loads(command(['exec', '-T', 'api', 'python', '-m', 'fin_agent.web', 'backup', backup]))
        assert client.delete('/api/v1/sessions/' + deleted).status_code == 204
        restore = ['run', '--rm', '--no-deps', 'api', 'restore', backup]
        command(restore, expected=1)
        report['worker_online_restore_refused'] = True
        command(['stop', 'worker'])
        try:
            refused = subprocess.run(compose + restore, capture_output=True, text=True)
            assert refused.returncode != 0 and 'Stop all Web API' in refused.stderr
            report['api_other_container_restore_refused'] = True
            command(['stop', 'web', 'api'])
            report['restore'] = json.loads(command(restore))
        finally:
            command(['up', '-d'])
        deadline = time.monotonic() + 30
        while True:
            try:
                response = client.get('/api/v1/health')
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError('HTTPS did not recover after restore')
            time.sleep(.2)
        assert client.get('/api/v1/auth/me').status_code == 401
        report['old_login_revoked'] = True
        client.cookies.clear()
        login(client)
        assert client.get('/api/v1/sessions/' + deleted + '/messages').status_code == 404
        report['deleted_session_not_resurrected'] = True
        report['status'] = 'passed'
        Path('.local/web-container-restore.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report))


if __name__ == '__main__':
    main()
