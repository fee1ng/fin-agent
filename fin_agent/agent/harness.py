"""Finance CLI adapter backed exclusively by the real Harness agent loop."""
import json
import os
from pathlib import Path
import uuid

from fin_agent.agent.harness_profile import SDK_VERSION, UPSTREAM_COMMIT, status_for_finish
from fin_agent.agent.harness_runtime import HarnessRuntime
from fin_agent.agent.redaction import Redactor
from fin_agent.agent.trace_store import TraceStore, file_lock, sync_directory, validate_session_id
from fin_agent.config import Config


class HarnessAgent:
    engine = "harness"

    def __init__(self, max_steps=None, max_time=None, *, trace_dir=None, session_id=None,
                 api_key=None, base_url=None, model=None):
        self.max_steps = max_steps if max_steps is not None else Config.AGENT_MAX_STEPS
        self.max_time = max_time if max_time is not None else Config.AGENT_MAX_TIME
        self.store = TraceStore(trace_dir or Path(Config.get_config_dir()) / "traces")
        self.session_id = session_id or "session-" + uuid.uuid4().hex
        validate_session_id(self.session_id)
        self.api_key = api_key if api_key is not None else Config.DEEPSEEK_API_KEY
        self.base_url = base_url or Config.DEEPSEEK_BASE_URL or "https://api.deepseek.com/v1"
        self.model = model or Config.DEEPSEEK_MODEL or "deepseek-chat"
        self.redactor = Redactor((self.api_key, Config.TUSHARE_TOKEN, Config.EMAIL_PASSWORD,
                                  Config.OPENAI_API_KEY))

    def _runtime(self, session_id):
        return HarnessRuntime(self.store.directory / "runtime" / session_id,
                              api_key=self.api_key, base_url=self.base_url, model=self.model)

    def stream_chat(self, user_input, *, input_id=None):
        session_id = self.session_id
        from fin_agent.agent.trace_recovery import recover
        recover(self.store.directory, session_id=session_id, redactor=self.redactor)
        input_id = input_id or "input-" + uuid.uuid4().hex
        text = self.redactor.text(user_input)
        with self.store.owned_accept(text, session_id=session_id, input_id=input_id,
            metadata={"engine": self.engine, "sdk_version": SDK_VERSION,
                      "upstream_commit": UPSTREAM_COMMIT, "model": self.model,
                      "data_source": Config.DATA_SOURCE}) as (receipt, accepted):
            try:
                yield from self._execute(receipt, accepted)
            except (GeneratorExit, KeyboardInterrupt):
                current = self.store.get_run(receipt['run_id'])
                if accepted and current['status'] in {'queued', 'running'}:
                    self.store.append('finished', run_id=receipt['run_id'], status='cancelled',
                                      finish_reason='consumer_cancelled',
                                      integrity_warning='native_outcome_requires_reconciliation')
                raise

    def _execute(self, receipt, accepted):
        session_id = receipt['harness_session_id']
        input_id, text = receipt['input_id'], receipt['input']
        run_id = receipt["run_id"]
        identity = {"run_id": run_id, "session_id": session_id, "input_id": input_id}
        yield {"type": "log", "content": f"run_id: {run_id}", "status": "queued", **identity}
        if not accepted:
            old = self.store.get_run(run_id)
            yield {"type": "answer" if old["status"] == "succeeded" else "log",
                   "content": old.get("answer", f"Existing run status: {old['status']}"),
                   "status": old["status"], **identity}
            return
        with self.store.session_lock(session_id):
            self.store.append("started", run_id=run_id, status="running")
            mapped_message = None
            turn = None
            active_turn = None
            settled = False

            def observe(notification):
                nonlocal mapped_message, turn, active_turn
                if notification.method != "session.event" or notification.payload.get("sessionId") != session_id:
                    return
                event = notification.payload.get("event", {})
                data = event.get("data", {})
                if event.get("type") == "turn/start":
                    active_turn = data["turn"]
                if event.get("type") == "agent/inbox/spliced" and mapped_message is None:
                    incoming = [m for m in data.get("inserted", []) if m.get("source", {}).get("kind") == "user"]
                    if len(incoming) != 1:
                        raise RuntimeError("Ambiguous native input receipt")
                    mapped_message = incoming[0]["id"]
                    self.store.append("native_input", run_id=run_id, native_message_id=mapped_message,
                                      native_seq=event["seq"])
                if event.get("type") == "user/message" and data.get("id") == mapped_message:
                    turn = active_turn
                    self.store.append("native_turn", run_id=run_id, turn=turn, native_seq=event["seq"])

            try:
                with self._runtime(session_id) as runtime:
                    native_root = runtime.directory / "native" / "sessions"
                    if any(native_root.glob(f"*/{session_id}/session.v4.jsonl")):
                        runtime.resume(session_id)
                    result = runtime.run(text, session_id=session_id, on_notification=observe)
                    if mapped_message is None or turn is None:
                        raise RuntimeError("Native input-to-turn association missing")
                    events = [e for e in result.events if e.get("data", {}).get("turn") == turn]
                    if not any(e["type"] == "turn/end" for e in events):
                        raise RuntimeError("Mapped native turn has no terminal fact")
                    status = status_for_finish(result.finish_reason)
                    answer = self.redactor.text(result.final_response)
                    self.store.append("finished", run_id=run_id, status=status, answer=answer,
                                      finish_reason=result.finish_reason, turn=turn,
                                      native_last_seq=max(e["seq"] for e in events),
                                      profile_hash=runtime.profile_hash)
                    settled = True
                    names = {}
                    for event in events:
                        data = event.get("data", {})
                        common = {**identity, "event_seq": event["seq"], "timestamp": event["time"],
                                  "step_id": f"{turn}:{data.get('step', '')}"}
                        if event["type"] == "tool/call":
                            names[data["callId"]] = data["name"]
                            yield {"type": "tool_call", "tool_name": data["name"],
                                   "args": self.redactor.text(data["arguments"]),
                                   "tool_call_id": data["callId"],
                                   "tool_execution_id": f"{session_id}:{event['seq']}", **common}
                        elif event["type"] == "tool/result":
                            message = data["message"]
                            content = "\n".join(b.get("text", "") for b in message.get("content", []))
                            yield {"type": "tool_result", "tool_name": names.get(message["toolCallId"], "tool"),
                                   "tool_call_id": message["toolCallId"], "result": self.redactor.text(content), **common}
                    yield {"type": "content", "content": answer, **identity}
                    yield {"type": "answer" if status == "succeeded" else "error", "content": answer,
                           "status": status, **identity}
            except Exception as exc:
                error = self.redactor.text(exc)
                if not settled:
                    self.store.append("finished", run_id=run_id, status="interrupted", error=error,
                                      integrity_warning='native_outcome_requires_reconciliation')
                yield {"type": "error", "content": f"{error} (run_id: {run_id})", **identity}

    def run(self, user_input, callback=None):
        # Reuse only the CLI renderer, never the LangGraph constructor or loop.
        from fin_agent.agent.core import FinAgent
        return FinAgent.run(self, user_input, callback)

    def clear_history(self):
        self.session_id = "session-" + uuid.uuid4().hex

    def save_session(self, filename="last_session.json"):
        destination = self.store.directory / "cli-session.json"
        with file_lock(self.store.directory / "cli-session.lock"):
            temporary = destination.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as target:
                json.dump({"version": 1, "session_id": self.session_id}, target)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, destination)
            sync_directory(self.store.directory)
        return "Harness session saved."

    def load_session(self, filename="last_session.json"):
        destination = self.store.directory / "cli-session.json"
        if not destination.exists():
            return "No saved Harness session found."
        with file_lock(self.store.directory / "cli-session.lock"):
            session_id = json.loads(destination.read_text(encoding="utf-8"))["session_id"]
        validate_session_id(session_id)
        self.session_id = session_id
        return "Harness session loaded."
