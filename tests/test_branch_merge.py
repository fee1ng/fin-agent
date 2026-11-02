"""Offline integration checks for the LangGraph / market-data merge."""
import json
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
from langchain_core.messages import AIMessage, ToolMessage

from fin_agent.agent.core import FinAgent
from fin_agent.backtest.engine import BacktestEngine
from fin_agent.config import Config
from fin_agent.tools import eastmoney_tools, technical_indicators, tushare_tools


class ScriptedLLM:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def bind_tools(self, tools):
        self.schemas = tools
        return self

    def invoke(self, messages):
        self.calls.append(list(messages))
        return next(self.responses)


def tool_request():
    return AIMessage(content="", tool_calls=[
        {"id": "time-1", "name": "get_current_time", "args": {}}
    ])


class MergeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        config_dir = patch.object(Config, "get_config_dir", return_value=self.temp.name)
        config_dir.start()
        self.addCleanup(config_dir.stop)

    def agent(self, responses, **kwargs):
        llm = ScriptedLLM(responses)
        with patch.object(FinAgent, "_create_llm", return_value=llm):
            agent = FinAgent(**kwargs)
        return agent, llm

    def test_graph_executes_tools_and_persists_session(self):
        agent, llm = self.agent([tool_request(), AIMessage(content="done")])
        events = list(agent.stream_chat("time?"))
        self.assertFalse([event for event in events if event["type"] == "error"])
        self.assertEqual(events[-1], {"type": "answer", "content": "done"})
        self.assertEqual(sum(e["type"] == "tool_result" for e in events), 1)
        self.assertIsInstance(llm.calls[1][-1], ToolMessage)
        self.assertEqual([m["role"] for m in agent.history],
                         ["system", "user", "assistant", "tool", "assistant"])
        agent.save_session()
        expected = agent.history.copy()
        agent.clear_history()
        self.assertEqual(agent.load_session(), "Session loaded.")
        self.assertEqual(agent.history, expected)

    def test_execution_limit_completes_pending_tool_protocol(self):
        agent, llm = self.agent([tool_request(), AIMessage(content="summary")], max_steps=1)
        with patch("fin_agent.agent.core.execute_tool_call") as execute:
            events = list(agent.stream_chat("time?"))
        execute.assert_not_called()
        self.assertEqual(events[-1]["content"], "summary")
        self.assertIsInstance(llm.calls[-1][-2], ToolMessage)
        self.assertEqual(llm.calls[-1][-2].tool_call_id, "time-1")
        self.assertEqual(agent.history[-2]["role"], "tool")
        self.assertEqual(agent.history[-2]["tool_call_id"], "time-1")

    def test_real_langchain_adapter_accepts_combined_tool_schemas(self):
        with patch.object(Config, "validate"), \
             patch.object(Config, "LLM_PROVIDER", "local"), \
             patch.object(Config, "OPENAI_API_KEY", "offline-check"), \
             patch.object(Config, "OPENAI_BASE_URL", "http://127.0.0.1:9/v1"):
            agent = FinAgent()
            bound = agent.llm.bind_tools(tushare_tools.TOOLS_SCHEMA)
        self.assertEqual(len(bound.kwargs["tools"]), len(tushare_tools.TOOLS_SCHEMA))

    def test_long_tool_loop_uses_configured_limit(self):
        agent, llm = self.agent([tool_request() for _ in range(15)] +
                                [AIMessage(content="summary")], max_steps=15)
        events = list(agent.stream_chat("repeat"))
        self.assertFalse([e for e in events if e["type"] == "error"])
        self.assertEqual(len(llm.calls), 16)
        self.assertEqual(events[-1]["content"], "summary")

    def test_tool_exception_is_reported_to_model(self):
        agent, llm = self.agent([tool_request(), AIMessage(content="handled")])
        with patch("fin_agent.agent.core.execute_tool_call", side_effect=ValueError("bad data")):
            events = list(agent.stream_chat("time?"))
        self.assertIn("bad data", llm.calls[-1][-1].content)
        self.assertEqual(events[-1]["content"], "handled")

    def test_registered_tools_include_both_branches(self):
        names = [tool["function"]["name"] for tool in tushare_tools.TOOLS_SCHEMA]
        self.assertEqual(len(names), len(set(names)))
        for name in ("get_hist_data_em", "get_stock_news_em", "get_stock_info_cjzc_em",
                     "query_notified_news", "create_news_subscription", "run_backtest",
                     "add_portfolio_position", "get_daily_price"):
            self.assertIn(name, names)
        with patch.object(eastmoney_tools, "get_realtime_quote_em", return_value="quote"):
            self.assertEqual(tushare_tools.execute_tool_call(
                "get_realtime_quote_em", '{"ts_code":"000001.SZ"}'), "quote")
        with patch("fin_agent.tools.news.get_stock_info_cjzc_em", return_value="headlines"):
            self.assertEqual(tushare_tools.execute_tool_call(
                "get_stock_info_cjzc_em", {}), "headlines")

    def test_akshare_historical_data_preserves_unadjusted_option(self):
        frame = pd.DataFrame([{"日期": "20260101", "开盘": 10, "收盘": 11}])
        with patch("akshare.stock_zh_a_hist", return_value=frame) as fetch:
            result = json.loads(eastmoney_tools.get_hist_data_em(
                "000001.SZ", "20260101", "20260131", adjust=""))
        self.assertEqual(fetch.call_args.kwargs["adjust"], "")
        self.assertEqual(result["data"][0]["close"], 11)

    def test_backtest_uses_configured_provider_and_sorts_dates(self):
        frame = pd.DataFrame({"trade_date": ["20260102", "20260101"], "close": [11, 10]})
        with patch("fin_agent.backtest.engine.get_provider") as provider:
            provider.return_value.get_daily_price.return_value = frame
            result = BacktestEngine()._fetch_data("000001.SZ", "20260101", "20260131")
        provider.return_value.get_daily_price.assert_called_once()
        self.assertEqual(result["trade_date"].tolist(), ["20260101", "20260102"])

    def test_technical_indicators_use_configured_provider(self):
        frame = pd.DataFrame({"trade_date": pd.date_range("2025-01-01", periods=100).strftime("%Y%m%d"),
                              "close": range(100, 200), "high": range(101, 201), "low": range(99, 199)})
        with patch.object(technical_indicators, "get_provider") as provider:
            provider.return_value.get_daily_price.return_value = frame
            result = technical_indicators.get_technical_indicators("000001.SZ", end_date="20250410")
        self.assertIsInstance(json.loads(result), list)
        provider.return_value.get_daily_price.assert_called_once()


if __name__ == "__main__":
    unittest.main()
