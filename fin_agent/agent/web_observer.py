"""Observe actual model/tool boundaries, not delayed graph update messages."""
import json
import time
import uuid
from datetime import datetime, timezone

from langchain_core.callbacks import BaseCallbackHandler

from fin_agent.agent.tool_bridge import is_business_error


def timestamped(emit):
    def send(event):
        emit({'at': datetime.now(timezone.utc).isoformat(), **event})
    return send


class ModelObserver(BaseCallbackHandler):
    # Missing tracing must fail the run rather than silently report a complete trace.
    raise_error = True

    def __init__(self, emit, clean=lambda value: value):
        self.emit, self.clean = timestamped(emit), clean
        self.active = {}
        self.last_model_id = None

    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, **kwargs):
        key = str(run_id)
        self.last_model_id = key
        self.active[key] = {'start': time.monotonic(), 'first': None}
        self.emit({'type': 'model.started', 'span_id': key,
                   'parent_id': str(parent_run_id) if parent_run_id else None,
                   'model': self.clean(kwargs.get('invocation_params', {}).get('model_name')
                                       or kwargs.get('invocation_params', {}).get('model'))})

    def on_llm_new_token(self, token, *, run_id, **kwargs):
        state = self.active.get(str(run_id))
        chunk = kwargs.get('chunk')
        message = getattr(chunk, 'message', None)
        tool_chunks = getattr(message, 'tool_call_chunks', None)
        if not token and not tool_chunks:
            return
        if state is not None and state['first'] is None:
            state['first'] = time.monotonic() - state['start']
            self.emit({'type': 'model.first_chunk', 'span_id': str(run_id),
                       'kind': 'tool_call' if tool_chunks else 'text',
                       'duration_ms': state['first'] * 1000})

    def _finish(self, run_id, status, **fields):
        state = self.active.pop(str(run_id), None)
        if state is not None:
            self.emit({'type': 'model.finished', 'span_id': str(run_id),
                       'status': status,
                       'duration_ms': (time.monotonic() - state['start']) * 1000,
                       **self.clean(fields)})

    def on_llm_end(self, response, *, run_id, **kwargs):
        usage = (response.llm_output or {}).get('token_usage')
        if usage is None and response.generations:
            message = getattr(response.generations[0][0], 'message', None)
            usage = getattr(message, 'usage_metadata', None)
        if isinstance(usage, dict):
            usage = {name: next((usage[key] for key in candidates
                                if isinstance(usage.get(key), int) and not isinstance(usage[key], bool)
                                and usage[key] >= 0), None)
                     for name, candidates in {
                         'input': ('input_tokens', 'prompt_tokens'),
                         'output': ('output_tokens', 'completion_tokens'),
                         'total': ('total_tokens',),
                     }.items()}
        self._finish(run_id, 'completed', usage=usage)

    def on_llm_error(self, error, *, run_id, **kwargs):
        self._finish(run_id, 'failed', error=str(error))


class ObservedTools:
    def __init__(self, execute, emit, clean=lambda value: value, model_observer=None):
        self.execute, self.emit, self.clean = execute, timestamped(emit), clean
        self.model_observer = model_observer

    def execute_call(self, call):
        return self(call['name'], json.dumps(call['args']), tool_call_id=call['id'])

    def __call__(self, name, arguments, *, tool_call_id=None):
        span_id = uuid.uuid4().hex
        self.emit({'type': 'tool.started', 'span_id': span_id, 'name': name,
                   'tool_call_id': tool_call_id,
                   'parent_id': self.model_observer.last_model_id if self.model_observer else None,
                   'arguments': self.clean(json.loads(arguments))})
        start = time.monotonic()
        status, error, result = 'completed', None, None
        try:
            result = self.execute(name, arguments)
            if is_business_error(result):
                status = 'failed'
            return result
        except Exception as exc:
            status, error = 'failed', self.clean(str(exc))
            raise
        finally:
            self.emit({'type': 'tool.finished', 'span_id': span_id, 'name': name,
                       'status': status, 'error': error,
                       'duration_ms': (time.monotonic() - start) * 1000,
                       'summary': str(self.clean(result))[:2000] if result is not None else None})


def create_web_agent(emit):
    from fin_agent.agent.core import FinAgent
    from fin_agent.agent.redaction import configured_redactor
    from fin_agent.agent.web_policy import ReadOnlyTools, WEB_SYSTEM_PROMPT
    from fin_agent.tools.tushare_tools import TOOLS_SCHEMA, execute_tool_call

    clean = configured_redactor().clean
    policy = ReadOnlyTools(TOOLS_SCHEMA, execute_tool_call)
    observer = ModelObserver(emit, clean)
    return FinAgent(tool_schemas=policy.schemas,
                    tool_executor=ObservedTools(policy, emit, clean, observer),
                    system_prompt=WEB_SYSTEM_PROMPT,
                    observer=observer)
