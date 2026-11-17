import os
import pathlib
import tempfile
import unittest
from unittest import mock

from fin_agent.agent import harness_profile
from fin_agent.agent import observability_config


class ObservabilityConfigTests(unittest.TestCase):
    """Verify opt-in configuration and credential isolation."""

    def test_disabled_needs_no_plugin_or_endpoint(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(observability_config.plugin_row())

    def test_enabled_rejects_invalid_endpoint(self) -> None:
        for endpoint in ("", "file:///tmp/out", "https://user:key@host",
                         "https://host?secret=value"):
            with self.subTest(endpoint=endpoint):
                with mock.patch.dict(os.environ, {
                    "FIN_AGENT_OBSERVABILITY": "true",
                    "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint,
                }, clear=True):
                    with self.assertRaises(ValueError):
                        observability_config.plugin_row()

    def test_profile_contains_identity_but_not_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = pathlib.Path(temporary)
            entry = directory / "index.js"
            entry.touch()
            with mock.patch.dict(os.environ, {
                "FIN_AGENT_OBSERVABILITY": "true",
                "FIN_AGENT_LOONGSUITE_ENTRY": str(entry),
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318",
                "OTEL_EXPORTER_OTLP_HEADERS": "authorization=secret-value",
            }, clear=True):
                profile, _ = harness_profile.write_profile(
                    directory, base_url="http://localhost/v1", model="test",
                    cwd=directory, run_id="run-observation-test",
                )
                contents = profile.read_text(encoding="utf-8")
                self.assertIn("run-observation-test", contents)
                self.assertIn('"captureContent": false', contents)
                self.assertNotIn("secret-value", contents)

    def test_missing_plugin_fails_before_runtime_start(self) -> None:
        with mock.patch.dict(os.environ, {
            "FIN_AGENT_OBSERVABILITY": "true",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318",
            "FIN_AGENT_LOONGSUITE_ENTRY": "/missing/plugin/index.js",
        }, clear=True):
            with self.assertRaises(FileNotFoundError):
                observability_config.plugin_row()
