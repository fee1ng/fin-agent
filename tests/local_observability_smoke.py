"""Generate a real DSH trace using a local model, then verify it in Jaeger."""

import datetime
import http.server
import json
import tempfile
import threading
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from fin_agent.agent import harness
from tests import harness_probe


def read_json(path: str) -> dict[str, Any]:
    """Read a local Jaeger JSON API response with a bounded timeout."""
    endpoint = f"http://jaeger:16686{path}"
    with urllib.request.urlopen(endpoint, timeout=3) as reply:
        records = [json.loads(line) for line in reply if line.strip()]
        if len(records) == 1:
            return records[0]
        return {"result": {"resourceSpans": [
            resource for record in records
            for resource in record.get("result", {}).get("resourceSpans", [])
        ]}}


def await_trace(run_id: str) -> dict[str, Any]:
    """Wait for a single run to be searchable without rerunning the agent."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        now = datetime.datetime.now(datetime.timezone.utc)
        query = urllib.parse.urlencode({
            "query.service_name": "fin-agent-local",
            "query.start_time_min": (
                now - datetime.timedelta(minutes=5)
            ).isoformat(),
            "query.start_time_max": now.isoformat(),
            "query.attributes": json.dumps({"fin_agent.run_id": run_id}),
        })
        result = read_json(f"/api/v3/traces?{query}")
        spans = [span
                 for resource in result.get("result", {}).get(
                     "resourceSpans", [],
                 )
                 for scope in resource.get("scopeSpans", [])
                 for span in scope.get("spans", [])]
        if len(spans) >= 7:
            return {"spans": spans, "traceID": spans[0]["traceId"]}
        time.sleep(0.5)
    raise RuntimeError(
        "The run completed but its trace was not found in Jaeger",
    )


def verify_trace(trace: dict[str, Any]) -> None:
    """Check the persisted span tree, token usage and successful tool result."""
    spans = trace["spans"]
    identifiers = {span["spanId"] for span in spans}
    models, tools = [], []
    for span in spans:
        parent = span.get("parentSpanId")
        if parent and set(parent) != {"0"}:
            assert parent in identifiers, "Missing parent span"
        assert int(span["endTimeUnixNano"]) >= int(span["startTimeUnixNano"])
        tags = {tag["key"]: next(iter(tag["value"].values()))
                for tag in span["attributes"]}
        if tags.get("gen_ai.operation.name") == "chat":
            models.append(span)
            assert int(tags["gen_ai.usage.input_tokens"]) == 10
            assert int(tags["gen_ai.usage.output_tokens"]) == 2
        if tags.get("gen_ai.operation.name") == "execute_tool":
            tools.append(span)
            assert span.get("status", {}).get("code") not in (
                2, "STATUS_CODE_ERROR",
            )
    assert len(spans) == 7, f"Expected 7 spans, got {len(spans)}"
    assert len(models) == 2 and len(tools) == 1


def main() -> None:
    """Run an offline-model request and print its clickable local trace URL."""
    for attempt in range(30):
        try:
            read_json("/api/v3/services")
            break
        except (urllib.error.URLError, TimeoutError):
            if attempt == 29:
                raise
            time.sleep(1)
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), harness_probe.ModelHandler,
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as directory:
            agent = harness.HarnessAgent(
                trace_dir=directory, api_key="local-fixture",
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
            )
            events = list(agent.stream_chat("Get the current time."))
            assert events[-1]["type"] == "answer", events[-1]
            run_id = events[0]["run_id"]
            trace = await_trace(run_id)
            verify_trace(trace)
            print(json.dumps({
                "status": "passed", "run_id": run_id,
                "span_count": len(trace["spans"]),
                "trace_url": f"http://localhost:16686/trace/{trace['traceID']}",
            }))
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
