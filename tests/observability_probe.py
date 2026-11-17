"""Verify real DSH/MCP OTLP export against a local protobuf receiver.

Run in the observability image with opentelemetry-proto==1.40.0 installed.
Only local model fixtures are used; no cloud credentials are required.
"""

import http.server
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2

from fin_agent.agent import harness


class ModelHandler(http.server.BaseHTTPRequestHandler):
    """Serve two deterministic model calls, optionally causing tool failure."""

    tool_name = "get_current_time"
    calls = 0

    def log_message(self, format: str, *args: object) -> None:
        """Keep fixture HTTP traffic out of test output."""

    def do_POST(self) -> None:
        """Return a tool call followed by a final answer with known usage."""
        body = self.rfile.read(int(self.headers["Content-Length"]))
        request = json.loads(body)
        type(self).calls += 1
        completed = any(row["role"] == "tool" for row in request["messages"])
        tool = next(row["function"]["name"] for row in request["tools"]
                    if row["function"]["name"].endswith(self.tool_name))
        delta = {"content": "private-answer-sentinel"} if completed else {
            "tool_calls": [{
                "index": 0, "id": "fixture-tool", "type": "function",
                "function": {"name": tool, "arguments": "{}"},
            }],
        }
        chunks = [
            {"id": "fixture", "object": "chat.completion.chunk",
             "created": 1, "model": request["model"],
             "choices": [{"index": 0, "delta": delta,
                          "finish_reason": None}]},
            {"id": "fixture", "object": "chat.completion.chunk",
             "created": 1, "model": request["model"],
             "choices": [{"index": 0, "delta": {},
                          "finish_reason": "stop" if completed
                          else "tool_calls"}],
             "usage": {"prompt_tokens": 10, "completion_tokens": 2,
                       "total_tokens": 12,
                       "prompt_tokens_details": {"cached_tokens": 4}}},
        ]
        payload = ("".join(f"data: {json.dumps(row)}\n\n" for row in chunks)
                   + "data: [DONE]\n\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class CollectorHandler(http.server.BaseHTTPRequestHandler):
    """Decode real OTLP protobuf, including authenticated export headers."""

    traces: list[trace_service_pb2.ExportTraceServiceRequest] = []
    metrics: list[metrics_service_pb2.ExportMetricsServiceRequest] = []
    headers_seen: list[str | None] = []
    response_status = 200

    def log_message(self, format: str, *args: object) -> None:
        """Keep fixture HTTP traffic out of test output."""

    def do_POST(self) -> None:
        """Accept an OTLP export and retain its decoded message."""
        if self.headers.get("Transfer-Encoding") == "chunked":
            chunks = []
            while True:
                size = int(self.rfile.readline().split(b";", 1)[0], 16)
                if size == 0:
                    while self.rfile.readline() != b"\r\n":
                        pass
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)
            payload = b"".join(chunks)
        else:
            payload = self.rfile.read(int(self.headers["Content-Length"]))
        self.headers_seen.append(self.headers.get("x-arms-license-key"))
        if self.path == "/v1/traces":
            message = trace_service_pb2.ExportTraceServiceRequest()
            message.ParseFromString(payload)
            self.traces.append(message)
        elif self.path == "/v1/metrics":
            message = metrics_service_pb2.ExportMetricsServiceRequest()
            message.ParseFromString(payload)
            self.metrics.append(message)
        else:
            self.send_error(404)
            return
        self.send_response(self.response_status)
        self.send_header("Content-Type", "application/x-protobuf")
        self.send_header("Content-Length", "0")
        self.end_headers()


class ObservabilityProbe(unittest.TestCase):
    """Check observable execution rather than only successful plugin loading."""

    def setUp(self) -> None:
        CollectorHandler.traces = []
        CollectorHandler.metrics = []
        CollectorHandler.headers_seen = []
        CollectorHandler.response_status = 200
        ModelHandler.calls = 0
        self.model = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), ModelHandler,
        )
        self.collector = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), CollectorHandler,
        )
        for server in (self.model, self.collector):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

    def check_run(self, tool_name: str, failed: bool) -> None:
        """Check usage, privacy, ancestry and idempotent replay.

        Args:
            tool_name: Finance tool chosen by the model fixture.
            failed: Whether that tool is expected to report a business error.
        """
        ModelHandler.tool_name = tool_name
        environment = {
            "FIN_AGENT_OBSERVABILITY": "true",
            "OTEL_EXPORTER_OTLP_ENDPOINT": (
                f"http://127.0.0.1:{self.collector.server_port}"
            ),
            "OTEL_EXPORTER_OTLP_HEADERS": "x-arms-license-key=fixture-key",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "",
            "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": "",
            "OTEL_METRICS_EXPORTER": "otlp",
            "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "SPAN_ONLY",
        }
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, environment):
                agent = harness.HarnessAgent(
                    trace_dir=directory, api_key="fixture-model-key",
                    base_url=f"http://127.0.0.1:{self.model.server_port}/v1",
                )
                events = list(agent.stream_chat(
                    "private-input-sentinel", input_id="fixture-input",
                ))
                self.assertEqual(events[-1]["type"], "answer", events)
                run_id = events[0]["run_id"]
                before = ModelHandler.calls
                list(agent.stream_chat(
                    "private-input-sentinel", input_id="fixture-input",
                ))
                self.assertEqual(ModelHandler.calls, before)
        spans = []
        for request in CollectorHandler.traces:
            for resource in request.resource_spans:
                attributes = {item.key: item.value.string_value
                              for item in resource.resource.attributes}
                self.assertEqual(attributes["fin_agent.run_id"], run_id)
                for scope in resource.scope_spans:
                    spans.extend(scope.spans)
        self.assertTrue(spans, "Plugin loaded but exported no spans")
        self.assertEqual(len({span.trace_id for span in spans}), 1)
        self.assertEqual(len({span.span_id for span in spans}), len(spans))
        identifiers = {span.span_id for span in spans}
        models, tools = [], []
        for span in spans:
            self.assertGreaterEqual(span.end_time_unix_nano,
                                    span.start_time_unix_nano)
            if span.parent_span_id:
                self.assertIn(span.parent_span_id, identifiers)
            attributes = {item.key: item.value for item in span.attributes}
            operation = attributes.get("gen_ai.operation.name")
            if operation and operation.string_value == "chat":
                models.append(span)
                self.assertEqual(
                    attributes["gen_ai.usage.input_tokens"].int_value, 10,
                )
                self.assertEqual(
                    attributes["gen_ai.usage.output_tokens"].int_value, 2,
                )
                self.assertEqual(
                    attributes["gen_ai.usage.cache_read.input_tokens"]
                    .int_value, 4,
                )
                self.assertIn("langfuse.observation.completion_start_time",
                              attributes)
            if operation and operation.string_value == "execute_tool":
                tools.append(span)
        self.assertEqual(len(models), 2)
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0].status.code == 2, failed)
        self.assertTrue(CollectorHandler.metrics, "No metrics flushed on exit")
        self.assertEqual(set(CollectorHandler.headers_seen), {"fixture-key"})
        exported = str(CollectorHandler.traces) + str(CollectorHandler.metrics)
        for secret in ("private-input-sentinel", "private-answer-sentinel",
                       "fixture-model-key", "fixture-key"):
            self.assertNotIn(secret, exported)
        print(json.dumps({"run_id": run_id, "spans": len(spans),
                          "llm_calls": len(models), "tool_failed": failed}))

    def test_trace_success_usage_and_shutdown(self) -> None:
        """Export both model calls and a successful tool before SDK shutdown."""
        self.check_run("get_current_time", False)

    def test_trace_business_error(self) -> None:
        """Missing required tool arguments must produce a failed tool span."""
        self.check_run("get_daily_price", True)

    def test_export_failure_preserves_native_completion(self) -> None:
        """A failed remote export must not change a durably completed run."""
        CollectorHandler.response_status = 503
        ModelHandler.tool_name = "get_current_time"
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {
                "FIN_AGENT_OBSERVABILITY": "true",
                "OTEL_EXPORTER_OTLP_ENDPOINT": (
                    f"http://127.0.0.1:{self.collector.server_port}"
                ),
                "OTEL_METRICS_EXPORTER": "none",
            }):
                agent = harness.HarnessAgent(
                    trace_dir=directory, api_key="fixture-model-key",
                    base_url=f"http://127.0.0.1:{self.model.server_port}/v1",
                )
                events = list(agent.stream_chat("Get the current time."))
                self.assertEqual(events[-1]["type"], "answer", events)
                self.assertEqual(
                    agent.store.get_run(events[0]["run_id"])["status"],
                    "succeeded",
                )
                self.assertTrue(CollectorHandler.traces)


if __name__ == "__main__":
    unittest.main()
