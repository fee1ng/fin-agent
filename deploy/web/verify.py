"""Opt-in real-provider acceptance against a separate local Compose project.

Run only after reading agnets.md. Creates three test accounts, two questions and
an ignored report. Credentials stay in .local and never appear in arguments.
"""
import argparse
import concurrent.futures
import json
import math
import os
from pathlib import Path
import secrets
import ssl
import subprocess
import time
import uuid
from datetime import datetime

import httpx


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def summary(values):
    return {'samples': len(values), 'p50_ms': percentile(values, .5), 'p95_ms': percentile(values, .95)}


def checked(response, status=200):
    if response.status_code != status:
        raise RuntimeError(f'Unexpected HTTP status {response.status_code} at {response.request.url.path}')
    return response.json() if response.content else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--docker', default=str(Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs/DockerDesktop/resources/bin/docker.exe'))
    parser.add_argument('--project', default='fin-agent-web-qa')
    parser.add_argument('--base-url', default='http://127.0.0.1:8180')
    parser.add_argument('--ca')
    parser.add_argument('--qa-overlay', action='store_true')
    parser.add_argument('--bootstrap-only', action='store_true')
    parser.add_argument('--controlled', action='store_true', help='Label an isolated scripted-provider benchmark honestly')
    args = parser.parse_args()
    compose = [args.docker, '--context', 'desktop-linux', 'compose', '-p', args.project, '-f', 'compose.web.yaml']
    if args.qa_overlay:
        compose += ['-f', 'deploy/web/compose.qa.yaml']
    credential_file = Path('.local') / (args.project + '.accounts.json')
    if credential_file.exists():
        accounts = json.loads(credential_file.read_text())
    else:
        accounts = [{'username': name, 'password': secrets.token_urlsafe(24), 'role': role}
                    for name, role in [('qa-admin', 'admin'), ('qa-alice', 'user'), ('qa-bob', 'user')]]
        credential_file.write_text(json.dumps(accounts), encoding='utf-8')
    bootstrap = """import json,sys
from sqlalchemy import select
from fin_agent.web.database import Database
from fin_agent.web.settings import Settings
from fin_agent.web.models import User
from fin_agent.web.auth import create_user, verify_password
db=Database(Settings.from_env().database)
try:
 for item in json.load(sys.stdin):
  with db.transaction() as session:
   existing=session.scalar(select(User).where(User.username==item['username']))
  if existing:
   if not verify_password(existing.password_hash,item['password']): raise RuntimeError('Acceptance account conflict')
  else: create_user(db,item['username'],item['password'],item['role'])
finally: db.close()
"""
    subprocess.run(compose + ['exec', '-T', 'api', 'python', '-c', bootstrap], input=json.dumps(accounts), text=True, check=True)
    if args.bootstrap_only:
        print('Three isolated acceptance accounts ready; credentials in ignored local file.')
        return
    context = ssl.create_default_context(cafile=args.ca) if args.ca else True
    report = {'origin': args.base_url, 'project': args.project,
              'provider': 'controlled scripted fixture' if args.controlled else 'actual service configuration', 'runs': []}

    def login(account):
        client = httpx.Client(base_url=args.base_url, headers={'Origin': args.base_url}, verify=context, timeout=330, trust_env=False)
        response = client.post('/api/v1/auth/login', json={k: account[k] for k in ('username', 'password')})
        identity = checked(response)
        if args.base_url.startswith('https:'):
            cookie = response.headers.get('set-cookie', '').lower()
            assert all(flag in cookie for flag in ('secure', 'httponly', 'samesite=strict'))
        client.headers['X-CSRF-Token'] = identity['csrf_token']
        return client

    clients = [login(account) for account in accounts]
    admin, alice, bob = clients
    try:
        sessions = [checked(client.post('/api/v1/sessions', json={'title': '真实模型部署验收'}), 201) for client in (alice, bob)]
        assert bob.get('/api/v1/sessions/' + sessions[0]['id'] + '/messages').status_code == 404
        assert alice.get('/api/v1/admin/runs').status_code == 403
        prompt = '请务必先调用 get_current_time 工具获取当前时间，然后用 Markdown 表格展示日期和时间。再用约150字解释查询金融数据时核对时间的意义。不要查询证券价格，不要提供投资建议。'
        runs = [checked(client.post('/api/v1/sessions/' + session['id'] + '/runs', json={'text': prompt},
                     headers={'Idempotency-Key': uuid.uuid4().hex}), 202) for client, session in zip((alice, bob), sessions)]
        assert bob.get('/api/v1/runs/' + runs[0]['id'] + '/events').status_code == 404

        def consume(client, run):
            arrival, replay_age, deltas, sequence, reconnects, terminal = [], [], 0, 0, 0, False
            first_delta_received_at = None
            setup_times = []
            started = time.monotonic()
            while not terminal:
                connecting_at = time.perf_counter()
                with client.stream('GET', '/api/v1/runs/' + run['id'] + '/events', headers={'Last-Event-ID': str(sequence)}) as response:
                    if response.status_code != 200:
                        raise RuntimeError('SSE request rejected')
                    connected_at = time.time()
                    setup_times.append((time.perf_counter() - connecting_at) * 1000)
                    for line in response.iter_lines():
                        if not line.startswith('data: '):
                            continue
                        event = json.loads(line[6:])
                        if 'seq' not in event:
                            continue
                        assert event['seq'] > sequence, 'Replay duplicated a committed event'
                        sequence = event['seq']
                        committed = datetime.fromisoformat(event['at']).timestamp()
                        (arrival if committed >= connected_at else replay_age).append(max(0, (time.time() - committed) * 1000))
                        if event['type'] == 'message.delta':
                            deltas += 1
                            if first_delta_received_at is None:
                                first_delta_received_at = time.time()
                            if reconnects == 0:
                                reconnects += 1
                                break  # Real transport closes; the Worker must keep running.
                        if event['type'] in ('run.completed', 'run.failed', 'run.interrupted'):
                            assert event['type'] == 'run.completed', 'Actual provider run did not complete'
                            terminal = True
                            break
                if time.monotonic() - started > 330:
                    raise RuntimeError('Acceptance stream deadline exceeded')
            detail = checked(admin.get('/api/v1/admin/runs/' + run['id']))
            assert detail['tool_calls'] >= 1 and detail['model_calls'] >= 2
            assert any(s['name'] == 'get_current_time' and s['status'] == 'completed' for s in detail['spans'])
            assert deltas > 1, 'Proxy did not expose incremental answer events'
            # Docker Desktop shares the host clock. Compare first arrival with
            # persisted completion, without delaying the stream for another TLS
            # connection and measuring that connection's setup instead.
            observed_running_delta = first_delta_received_at is not None and first_delta_received_at < detail['run']['finished_at']
            assert observed_running_delta, 'No answer delta arrived while the Worker was still generating'
            snapshot = checked(client.get('/api/v1/sessions/' + run['session_id'] + '/messages'))
            assert len(snapshot['runs']) == 1
            model_ms = sum(s['duration_ms'] or 0 for s in detail['spans'] if s['kind'] == 'model')
            tool_ms = sum(s['duration_ms'] or 0 for s in detail['spans'] if s['kind'] == 'tool')
            return {'id': run['id'], 'started_at': detail['run']['started_at'], 'finished_at': detail['run']['finished_at'],
                    'execution_ms': detail['run']['execution_ms'], 'queue_ms': detail['run']['queue_ms'],
                    'model_ms': model_ms, 'tool_ms': tool_ms,
                    'execution_minus_call_ms': detail['run']['execution_ms'] - model_ms - tool_ms,
                    'model_calls': detail['model_calls'], 'tool_calls': detail['tool_calls'], 'delta_events': deltas,
                    'usage': detail['usage'], 'transport_reconnections': reconnects,
                    'received_delta_while_running': observed_running_delta,
                    'subscription_setup': summary(setup_times),
                    'live_commit_to_http_arrival': summary(arrival), 'replay_event_age': summary(replay_age)}

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            # Each stream owns its user client. The shared admin client only GETs.
            futures = [pool.submit(consume, client, run) for client, run in zip((alice, bob), runs)]
            report['runs'] = [future.result() for future in futures]
        report['two_runs_overlap'] = max(r['started_at'] for r in report['runs']) < min(r['finished_at'] for r in report['runs'])
        assert report['two_runs_overlap'], 'Two-session real-provider concurrency was not observed'
        timings = []
        for _ in range(20):
            before = time.perf_counter()
            checked(alice.get('/api/v1/sessions/' + sessions[0]['id'] + '/messages'))
            timings.append((time.perf_counter() - before) * 1000)
        report['authenticated_snapshot_round_trip'] = summary(timings)
        report['measurement_limit'] = 'Loopback HTTP/TLS arrival timings; browser first paint is not measured. Replay age includes intentional transport disconnection. Two queries do not establish sustained capacity.'
        report['status'] = 'passed'
        path = Path('.local') / (args.project + '.acceptance.json')
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False))
    finally:
        for client in clients:
            client.close()


if __name__ == '__main__':
    main()
