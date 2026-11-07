"""Behaviour tests through the existing Agent stream with controlled dependencies."""
import json
import unittest
from unittest.mock import patch

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult, LLMResult

from fin_agent.agent.core import FinAgent
from fin_agent.agent.web_observer import ModelObserver, ObservedTools
from fin_agent.agent.web_policy import ReadOnlyTools, WEB_SYSTEM_PROMPT
from fin_agent.agent.redaction import Redactor


class ScriptedModel(BaseChatModel):
    tool_name: str = 'get_current_time'

    @property
    def _llm_type(self):
        return 'controlled-test'

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self._reply(messages))])

    def _reply(self, messages):
        if not any(m.type == 'tool' for m in messages):
            return AIMessage(content='', tool_calls=[
                {'id': 'call-1', 'name': self.tool_name, 'args': {}},
                {'id': 'call-2', 'name': self.tool_name, 'args': {}},
            ])
        return AIMessage(content='查询完成')

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        reply = self._reply(messages)
        if reply.tool_calls:
            yield ChatGenerationChunk(message=AIMessageChunk(
                content='', tool_call_chunks=[
                    {'id': c['id'], 'name': c['name'], 'args': '{}', 'index': i}
                    for i, c in enumerate(reply.tool_calls)]))
        else:
            for text in ['查询', '完成']:
                yield ChatGenerationChunk(message=AIMessageChunk(content=text))


class WebAgentTests(unittest.TestCase):
    def make_agent(self, execute, name='get_current_time'):
        events = []
        policy = ReadOnlyTools([{'type': 'function', 'function': {
            'name': 'get_current_time', 'parameters': {'type': 'object', 'properties': {}}}}], execute)
        observer = ModelObserver(events.append)
        with patch.object(FinAgent, '_create_llm', return_value=ScriptedModel(tool_name=name)):
            agent = FinAgent(tool_schemas=policy.schemas,
                             tool_executor=ObservedTools(policy, events.append, model_observer=observer),
                             system_prompt=WEB_SYSTEM_PROMPT, observer=observer)
        return agent, events

    def test_stream_records_each_real_call_before_result_and_links_model(self):
        executed = []
        def execute(name, arguments):
            executed.append(name)
            # Observer must announce this particular execution before invoking it.
            self.assertEqual(len([e for e in trace if e['type'] == 'tool.started']), len(executed))
            return json.dumps({'time': '2026-11-04'})
        agent, trace = self.make_agent(execute)
        stream = list(agent.stream_chat('当前时间'))
        self.assertEqual(executed, ['get_current_time', 'get_current_time'])
        self.assertEqual(stream[-1]['type'], 'answer')
        self.assertIn('完成', stream[-1]['content'])
        starts = [e for e in trace if e['type'] == 'tool.started']
        self.assertEqual({e['tool_call_id'] for e in starts}, {'call-1', 'call-2'})
        self.assertEqual(len({e['span_id'] for e in starts}), 2)
        self.assertTrue(all(e['parent_id'] for e in starts))
        self.assertEqual(len([e for e in trace if e['type'] == 'model.finished']), 2)
        self.assertTrue(all(e['duration_ms'] >= 0 for e in trace if e['type'] == 'tool.finished'))

    def test_forged_write_call_never_reaches_executor(self):
        called = []
        agent, trace = self.make_agent(lambda *args: called.append(args), 'update_user_profile')
        list(agent.stream_chat('保存我的偏好'))
        self.assertFalse(called)
        self.assertTrue(all(e['status'] == 'failed' for e in trace if e['type'] == 'tool.finished'))

    def test_business_error_is_failed_span_without_exception(self):
        agent, trace = self.make_agent(lambda *args: {'ok': False, 'error': 'unavailable'})
        list(agent.stream_chat('查询'))
        self.assertEqual([e['status'] for e in trace if e['type'] == 'tool.finished'], ['failed', 'failed'])

    def test_web_prompt_never_reads_global_profile(self):
        with patch('fin_agent.agent.core.get_profile_manager', side_effect=AssertionError('private profile accessed')):
            agent, _ = self.make_agent(lambda *args: 'ok')
            list(agent.stream_chat('查询'))

    def test_policy_hides_private_and_write_tools(self):
        names = ['get_current_time', 'get_user_profile', 'get_portfolio_status', 'reset_core_config', 'add_price_alert']
        policy = ReadOnlyTools([{'function': {'name': name}} for name in names], lambda *args: 'ok')
        self.assertEqual(policy.names, {'get_current_time'})
        with self.assertRaises(PermissionError):
            policy('reset_core_config', '{}')

    def test_tool_exception_trace_is_redacted_and_exception_preserved(self):
        events = []
        secret = 'test-private-value'
        def fail(*args):
            raise RuntimeError('provider rejected ' + secret)
        tools = ObservedTools(fail, events.append, Redactor([secret]).clean)
        with self.assertRaises(RuntimeError):
            tools('get_current_time', json.dumps({'api_key': secret}))
        self.assertNotIn(secret, json.dumps(events))
        self.assertEqual(events[-1]['status'], 'failed')

    def test_model_failure_and_unknown_usage_are_recorded(self):
        events = []
        observer = ModelObserver(events.append)
        observer.on_chat_model_start({}, [], run_id='first')
        observer.on_llm_new_token('', run_id='first')
        observer.on_llm_error(RuntimeError('timeout'), run_id='first')
        observer.on_chat_model_start({}, [], run_id='second')
        observer.on_llm_end(LLMResult(generations=[[ChatGeneration(message=AIMessage(content='ok'))]]), run_id='second')
        finished = [e for e in events if e['type'] == 'model.finished']
        self.assertEqual([e['status'] for e in finished], ['failed', 'completed'])
        self.assertIsNone(finished[-1]['usage'])


if __name__ == '__main__':
    unittest.main()
