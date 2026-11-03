"""Opt-in native persistence privacy gate. Exit 2 means deployment is blocked."""
import json
from pathlib import Path
import tempfile
import threading
from http.server import ThreadingHTTPServer

from fin_agent.agent.harness import HarnessAgent
from tests.harness_probe import ModelHandler


class SecretOutputHandler(ModelHandler):
    secret = 'sk-release-gate-0123456789abcdef'

    def send_stream(self, payload):
        super().send_stream(payload.replace(b'probe-complete', self.secret.encode()))


def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), SecretOutputHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as directory:
            agent = HarnessAgent(trace_dir=directory, api_key='offline-fixture',
                                 base_url=f'http://127.0.0.1:{server.server_port}/v1')
            output = list(agent.stream_chat('Get current time.'))
            if output[-1]['type'] != 'answer':
                raise RuntimeError('Real runtime did not complete the release fixture')
            secret = SecretOutputHandler.secret.encode()
            native_files = list(Path(directory).rglob('session.v4.jsonl'))
            leaked = sum(secret in path.read_bytes() for path in native_files)
            print(json.dumps({'native_secret_leaks': leaked,
                              'application_answer_redacted': SecretOutputHandler.secret not in output[-1]['content'],
                              'deployment_allowed': leaked == 0}))
            return 2 if leaked else 0
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    raise SystemExit(main())
