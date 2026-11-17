"""Configure opt-in LoongSuite export without persisting credentials."""

import os
import pathlib
from typing import Any
import urllib.parse


def plugin_row(run_id: str | None = None) -> dict[str, Any] | None:
    """Return a secret-free DSH plugin row when tracing is enabled.

    Args:
        run_id: Application request identity attached to exported resources.

    Returns:
        A profile insertion row, or None when collection is disabled.

    Raises:
        ValueError: Enabled tracing has no valid HTTP export endpoint.
        FileNotFoundError: The pinned plugin is absent from the image.
    """
    enabled = os.environ.get("FIN_AGENT_OBSERVABILITY", "false").lower()
    if enabled not in {"true", "false"}:
        raise ValueError("FIN_AGENT_OBSERVABILITY must be true or false")
    if enabled == "false":
        return None
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or (
        os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    )
    parsed = urllib.parse.urlsplit(endpoint or "")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Tracing requires an HTTP(S) OTLP endpoint")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Put OTLP credentials in headers, not the URL")
    entry = pathlib.Path(os.environ.get(
        "FIN_AGENT_LOONGSUITE_ENTRY",
        "/opt/fin-agent-observability/node_modules/"
        "@loongsuite/dsh-plugin/dist/index.js",
    ))
    if not entry.is_file():
        raise FileNotFoundError(
            "Build the LoongSuite observability image first",
        )
    attributes = {"fin_agent.engine": "harness"}
    if run_id is not None:
        attributes["fin_agent.run_id"] = run_id
    return {
        "id": "finance-observability",
        "name": str(pathlib.Path(__file__).with_name(
            "harness_observability.mjs",
        )),
        "config": {
            "captureContent": False,
            "resourceAttributes": attributes,
            "exportTimeoutMs": 3000,
            "traceExportIntervalMs": 1000,
        },
    }
