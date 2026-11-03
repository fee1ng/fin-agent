"""Own one real Harness process and require a durable native final checkpoint."""
import json
from pathlib import Path
import threading
import time

from fin_agent.agent.harness_profile import PROVIDER, SDK_VERSION, write_profile
from fin_agent.agent.trace_store import validate_session_id


class HarnessStorageError(RuntimeError):
    pass


class HarnessRuntime:
    def __init__(self, directory, *, api_key, base_url, model, cwd=None, ack_timeout=15, tools="*"):
        from importlib.metadata import version
        from deepseek_harness import DeepSeekHarness

        for package in ("deepseek-harness-sdk", "deepseek-harness-runtime-bin"):
            if version(package) != SDK_VERSION:
                raise RuntimeError(f"{package} must match pinned version {SDK_VERSION}")
        self.directory = Path(directory).resolve()
        cwd = Path(cwd or Path.cwd()).resolve()
        profile, self.profile_hash = write_profile(
            self.directory, base_url=base_url, model=model, cwd=cwd, tools=tools)
        self.ack_timeout = ack_timeout
        self._lock = threading.Lock()
        self.sdk = DeepSeekHarness(
            dsh_home=str(self.directory / "native"), cwd=str(cwd),
            profile="sdk-minimal", patches=(str(profile),), provider=PROVIDER,
            model=model, max_tokens=4096, api_key=api_key,
            initialize_timeout_seconds=45, request_timeout_seconds=30,
            shutdown_timeout_seconds=15,
        )

    @staticmethod
    def _validate_session_id(session_id):
        validate_session_id(session_id)

    def wait_for_checkpoint(self, session_id, sequence):
        self._validate_session_id(session_id)
        deadline = time.monotonic() + self.ack_timeout
        ack_file = self.directory / "acks" / f"{session_id}.json"
        while time.monotonic() < deadline:
            try:
                ack = json.loads(ack_file.read_text(encoding="utf-8"))
            except FileNotFoundError:
                pass
            else:
                if ack["session_id"] == session_id and ack["sequence"] >= sequence:
                    return ack
            time.sleep(.02)
        raise HarnessStorageError("storage_error: final native checkpoint was not acknowledged")

    def run(self, text, *, session_id, on_notification=None):
        self._validate_session_id(session_id)
        with self._lock:
            result = self.sdk.run(text, session_id=session_id, on_notification=on_notification)
            terminal = [event for event in result.events if event.get("type") == "turn/end"]
            if not terminal:
                raise HarnessStorageError("No native terminal event; execution outcome is unresolved")
            self.wait_for_checkpoint(session_id, max(event["seq"] for event in terminal))
            return result

    def close(self):
        with self._lock:
            self.sdk.close()

    def resume(self, session_id):
        self._validate_session_id(session_id)
        with self._lock:
            return self.sdk.resume_session(session_id)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
