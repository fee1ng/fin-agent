"""Opt-in real-runtime compatibility probe, using a local scripted model server."""
import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fin_agent.agent.harness_runtime import HarnessRuntime


class ModelHandler(BaseHTTPRequestHandler):
    requests_seen = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests_seen.append(body)
        if self.path.endswith('/chat/completions'):
            has_result = any(m['role'] == 'tool' for m in body['messages'])
            if has_result:
                delta, finish = {'content': 'probe-complete'}, 'stop'
            else:
                tool = next(t['function']['name'] for t in body['tools']
                            if t['function']['name'].endswith('get_current_time'))
                delta = {'tool_calls': [{'index': 0, 'id': 'probe-tool-1',
                         'type': 'function', 'function': {'name': tool, 'arguments': '{}'}}]}
                finish = 'tool_calls'
            chunks = [{'id': 'probe', 'object': 'chat.completion.chunk', 'created': 1,
                       'model': body['model'], 'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]},
                      {'id': 'probe', 'object': 'chat.completion.chunk', 'created': 1,
                       'model': body['model'], 'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}],
                       'usage': {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12}}]
            self.send_stream((''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks)
                              + 'data: [DONE]\n\n').encode())
            return
        tools = [t["name"] for t in body.get("tools", [])]
        has_result = any(block.get("type") == "tool_result"
                         for m in body["messages"] for block in m.get("content", [])
                         if isinstance(block, dict))
        if not has_result:
            name = next(t for t in tools if t.endswith("get_current_time"))
            block = {"type": "tool_use", "id": "probe-tool-1", "name": name, "input": {}}
            delta = {"type": "input_json_delta", "partial_json": "{}"}
            finish = "tool_use"
        else:
            block = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": "probe-complete"}
            finish = "end_turn"
        chunks = [
            {"type": "message_start", "message": {"id": "probe", "type": "message",
             "role": "assistant", "model": body["model"], "content": [],
             "stop_reason": None, "usage": {"input_tokens": 10, "output_tokens": 0}}},
            {"type": "content_block_start", "index": 0, "content_block": block},
            {"type": "content_block_delta", "index": 0, "delta": delta},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": finish, "stop_sequence": None},
             "usage": {"output_tokens": 2}},
            {"type": "message_stop"},
        ]
        payload = "".join("event: " + c["type"] + "\ndata: " + json.dumps(c) + "\n\n"
                          for c in chunks).encode()
        self.send_stream(payload)

    def send_stream(self, payload):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with tempfile.TemporaryDirectory(prefix="fin-harness-probe-") as directory:
        root = Path(directory)
        try:
            with HarnessRuntime(root / "home", model="deepseek-chat", api_key="offline-probe", tools="get_current_time",
                                base_url=f"http://127.0.0.1:{server.server_port}/v1") as harness:
                result = harness.run("Get the current time.", session_id="probe-session")
                print("FINISH", result.finish_reason, "ANSWER", result.final_response, flush=True)
                print("EVENT_TYPES", sorted({e.get("type") for e in result.events}), flush=True)
                print("TOOL_EVENTS", json.dumps([e for e in result.events if e.get("type", "").startswith("tool/")]), flush=True)
                # Check native bytes before normal shutdown can flush anything.
                live_records = list((root / "home").rglob("session.v4.jsonl"))
                assert len(live_records) == 1
                persisted = live_records[0].read_text()
                assert 'probe-complete' in persisted and 'turn/end' in persisted
                assert len(ModelHandler.requests_seen) == 2
                names = [t['function']['name'] for t in ModelHandler.requests_seen[0]['tools']]
                assert names == ['mcp__finance__get_current_time'], names
                assert all('dsh_session_log' not in req and 'dsh_plugin_packages' not in req
                           for req in ModelHandler.requests_seen)
            records = list((root / "home").rglob("*.jsonl"))
            print("PERSISTED_LOGS", [(str(p.relative_to(root)), p.stat().st_size) for p in records])
            assert result.final_response == "probe-complete", result.finish_reason
            assert records, "No native persisted session records"
            assert any(e.get("type") == "tool/result" for e in result.events)
            with HarnessRuntime(root / "home", model="deepseek-chat", api_key="offline-probe",
                                base_url=f"http://127.0.0.1:{server.server_port}/v1") as restored:
                restored.resume("probe-session")
                assert len(ModelHandler.requests_seen) == 2, 'Resume unexpectedly requested the model'
            print("REAL_HARNESS_MCP_PROBE_OK")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
