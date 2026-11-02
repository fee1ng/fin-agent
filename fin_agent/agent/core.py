import json
import os
import sys
import time
from colorama import Fore, Style
from fin_agent.config import Config
from fin_agent.tools.tushare_tools import TOOLS_SCHEMA, execute_tool_call
from fin_agent.tools.profile_tools import get_profile_manager
from fin_agent.utils import FinMarkdown, debug_print
from rich.console import Console
from rich.live import Live

# LangGraph / LangChain
from langgraph.graph import StateGraph, MessagesState, END, START
from langchain_core.messages import (
    SystemMessage, HumanMessage, AIMessage, ToolMessage, AIMessageChunk,
)
from langchain_openai import ChatOpenAI


class AgentState(MessagesState):
    step_count: int
    start_time: float


FORCED_STOP_MESSAGE = (
    "已达到最大步数/时间限制，请根据已收集到的信息直接给出最终答案，"
    "不要再调用任何工具。"
)


class FinAgent:
    def __init__(self, max_steps: int = None, max_time: float = None):
        self.max_steps = max_steps if max_steps is not None else Config.AGENT_MAX_STEPS
        self.max_time  = max_time  if max_time  is not None else Config.AGENT_MAX_TIME
        self.llm = self._create_llm()
        self.graph = self._build_graph()
        self.history = []
        self._init_history()

    # ------------------------------------------------------------------ #
    # LLM construction
    # ------------------------------------------------------------------ #

    def _create_llm(self) -> ChatOpenAI:
        Config.validate()
        provider = Config.LLM_PROVIDER

        if provider == "deepseek":
            return ChatOpenAI(
                api_key=Config.DEEPSEEK_API_KEY,
                base_url=Config.DEEPSEEK_BASE_URL,
                model=Config.DEEPSEEK_MODEL,
                streaming=True,
            )
        elif provider in ("openai", "local", "openrouter", "kimi", "glm", "qwen", "siliconflow"):
            extra = {}
            if provider == "openrouter":
                extra["default_headers"] = {
                    "HTTP-Referer": "https://github.com/fin-agent/fin-agent",
                    "X-Title": "Fin-Agent CLI",
                }
            return ChatOpenAI(
                api_key=Config.OPENAI_API_KEY or "none",
                base_url=Config.OPENAI_BASE_URL,
                model=Config.OPENAI_MODEL,
                streaming=True,
                **extra,
            )
        else:
            raise ValueError(f"Unsupported LLM provider: {provider}")

    # ------------------------------------------------------------------ #
    # LangGraph graph construction
    # ------------------------------------------------------------------ #

    def _build_graph(self):
        """
        Build and compile a LangGraph ReAct StateGraph.

        Graph topology:
            START → agent ──(no tool_calls)──────────────→ END
                         ──(tool_calls, within limits)───→ tools → agent
                         ──(tool_calls, limit exceeded)──→ wrap_up → END
        """
        def agent_node(state: AgentState) -> dict:
            response = self.llm.bind_tools(TOOLS_SCHEMA).invoke(state["messages"])
            return {"messages": [response], "step_count": state.get("step_count", 0) + 1}

        def tools_node(state: AgentState) -> dict:
            last = state["messages"][-1]
            results = []
            for tc in last.tool_calls:
                try:
                    result = execute_tool_call(tc["name"], json.dumps(tc["args"]))
                    if tc["name"] == "reset_core_config":
                        self.llm = self._create_llm()
                except Exception as exc:
                    result = f"Error executing tool: {exc}"
                results.append(ToolMessage(
                    content=str(result),
                    tool_call_id=tc["id"],
                    name=tc["name"],
                ))
            return {"messages": results}

        def wrap_up_node(state: AgentState) -> dict:
            # Complete pending calls before asking the model for a final answer.
            skipped = [ToolMessage(content="Skipped: execution limit reached.",
                                   tool_call_id=tc["id"], name=tc["name"])
                       for tc in state["messages"][-1].tool_calls]
            messages = list(state["messages"]) + skipped + [HumanMessage(content=FORCED_STOP_MESSAGE)]
            response = self.llm.invoke(messages)
            return {"messages": skipped + [response]}

        def should_continue(state: AgentState) -> str:
            last = state["messages"][-1]
            if not getattr(last, "tool_calls", None):
                return END
            elapsed = time.time() - state.get("start_time", float("inf"))
            step_count = state.get("step_count", 0)
            if step_count >= self.max_steps or elapsed >= self.max_time:
                return "wrap_up"
            return "tools"

        builder = StateGraph(AgentState)
        builder.add_node("agent", agent_node)
        builder.add_node("tools", tools_node)
        builder.add_node("wrap_up", wrap_up_node)
        builder.add_edge(START, "agent")
        builder.add_conditional_edges("agent", should_continue)
        builder.add_edge("tools", "agent")
        builder.add_edge("wrap_up", END)
        return builder.compile()

    # ------------------------------------------------------------------ #
    # System prompt
    # ------------------------------------------------------------------ #

    # 无 Tushare Token 时不可用的工具清单，用于提示模型不要反复尝试
    TUSHARE_ONLY_TOOLS = (
        "screen_stocks, get_long_tail_stocks, get_moneyflow, get_limit_list, get_top_list, "
        "get_forecast, get_concept_detail, get_hsgt_top10, get_hk_stock_basic, get_hk_daily_price, "
        "get_hk_realtime_price, get_us_stock_basic, get_us_daily_price, get_us_realtime_price, "
        "get_etf_basic, get_etf_daily_price, get_cb_basic, get_cb_daily_price, "
        "get_futures_basic, get_futures_daily_price, get_macro_gdp, get_macro_cpi, get_macro_m2, "
        "get_macro_interest_rate, get_global_index_comparison"
    )

    def _get_datasource_note(self):
        source = Config.DATA_SOURCE or "akshare"
        if Config.TUSHARE_TOKEN:
            return (
                f"### DATA SOURCE ###\\n"
                f"Current market data source: {source}. All listed tools are available.\\n\\n"
            )
        return (
            f"### DATA SOURCE ###\\n"
            f"Current market data source: {source}. The user has NOT configured a Tushare token, "
            f"so the following tools are UNAVAILABLE and will return a setup hint instead of data. "
            f"Do NOT call them; if the user asks for such data, tell them to configure a Tushare "
            f"token in the settings page.\\n"
            f"Unavailable tools: {self.TUSHARE_ONLY_TOOLS}\\n\\n"
        )

    def _get_system_content(self):
        # Get user profile summary
        user_profile_summary = get_profile_manager().get_profile_summary()
        datasource_note = self._get_datasource_note()

        return (
            "You are a financial assistant powered by an LLM and live market data tools.\\n"
            "Use get_hist_data_em and get_stock_news_em together for A-share trend/news analysis, then analyze their combined results. "
            "Use get_realtime_quote_em for spot prices, search_stock_em for symbol lookup, "
            "get_market_indices_em/get_market_movers_em for market overviews, and "
            "get_stock_news_main_cx/get_stock_info_cjzc_em for market headlines. "
            "These public East Money/AKShare tools need no Tushare token. "
            "Core daily-price, realtime-price, technical and backtest tools use the configured data provider (AKShare by default).\\n"
            "You can help users analyze stocks, check prices, and provide recommendations.\\n\\n"
            f"{datasource_note}"
            "### CRITICAL PROTOCOL ###\\n"
            "1. **CHECK TIME FIRST**: For any request involving market data, 'today', 'latest', or time-sensitive info, you MUST first call 'get_current_time' to establish the correct date. This is mandatory. Call it silently: do NOT tell the user you are checking/confirming the time, and do not narrate this step in the reply.\\n"
            "2. **USE TOOLS**: All market data MUST be obtained via the provided market data tools. Do not use internal knowledge.\\n\\n"
            "### TOOL USAGE ###\\n"
            "For 'latest' or 'current' price queries, use 'get_realtime_price'. "
            "For trends and analysis, use 'get_daily_price' to get historical context (optional adj: qfq/前复权, hfq/后复权, omit for 不复权). "
            "For valuation (PE, PB) or market cap, use 'get_daily_basic'. "
            "For financial performance (Revenue, Profit), use 'get_income_statement'. "
            "For market index (Shanghai Composite, etc.), use 'get_index_daily'. "
            "For Hong Kong stocks (e.g., '00700.HK', Tencent), use 'get_hk_stock_basic' for basic info, 'get_hk_daily_price' for historical data, and 'get_hk_realtime_price' for latest price. "
            "For US stocks (e.g., 'AAPL.O', Apple), use 'get_us_stock_basic' for basic info, 'get_us_daily_price' for historical data, and 'get_us_realtime_price' for latest price. "
            "For ETF (Exchange Traded Fund), use 'get_etf_basic' and 'get_etf_daily_price'. "
            "For convertible bonds (可转债), use 'get_cb_basic' and 'get_cb_daily_price'. "
            "For futures contracts, use 'get_futures_basic' and 'get_futures_daily_price'. "
            "For macroeconomic data, use 'get_macro_gdp' (GDP), 'get_macro_cpi' (CPI), 'get_macro_m2' (M2 money supply), 'get_macro_interest_rate' (interest rates). "
            "For global market index comparison, use 'get_global_index_comparison'. "
            "For technical analysis (MACD, RSI, KDJ, BOLL), use 'get_technical_indicators'. "
            "To check for technical patterns (Golden Cross, Overbought, etc.), use 'get_technical_patterns'. "
            "For funds flow, limit up/down, or concepts, use the corresponding tools. "
            "For portfolio queries (e.g. 'my portfolio', '我的持仓', 'holdings'), ALWAYS use 'get_portfolio_status'. "
            "For portfolio management (add/remove position), use 'add_portfolio_position' or 'remove_portfolio_position'. "
            "For news questions, call 'query_notified_news' before answering so the answer is based on locally recorded news rather than internal knowledge. "
            "When the user asks to subscribe to a sector, topic, or portfolio/holdings news, call 'create_news_subscription' instead of merely describing how to subscribe. "
            "A portfolio news subscription dynamically follows positions across ALL portfolios; never pass or invent a symbols list for it. "
            "Use 'list_news_subscriptions', 'update_news_subscription', 'pause_news_subscription', 'enable_news_subscription', and 'delete_news_subscription' to manage subscriptions, and 'refresh_news' only when an immediate background refresh is requested. "
            "News reminders are delivered through desktop notifications. The monitor stores source-provided titles and summaries; never claim that a background LLM generates or will generate news summaries. "
            "For create/update/pause/enable/delete/refresh operations, do not announce success before the tool returns. Afterward, report the exact tool outcome: an accepted refresh means background work was requested, not that fetching or notification delivery has completed. "
            "When setting price alerts with percentages (e.g. 'alert if rises 5%'), you MUST first fetch the current price (or relevant base price), calculate the target absolute price, and then set the alert with that absolute value. "
            "For configuration (email, tushare token, llm settings), use 'reset_email_config' or 'reset_core_config'. These tools are INTERACTIVE, so simply call them when requested; do NOT ask the user for details in the chat."
            "When analyzing, EXPLICITLY mention the date of the data you are using. "
            "Calculate percentage changes and describe the trend (e.g., upward, downward, volatile) based on the data. "
            "When you have enough information, answer the user's question directly.\\n\\n"
            "### OUTPUT FORMATTING ###\\n"
            "**CRITICAL**: If you need to present a list of items (stocks, companies, data points, etc.) and the count is 3 or more, you MUST format it as a Markdown table or a structured list. "
            "Do NOT present 3+ items as plain text paragraphs. Use tables for structured data (e.g., stock lists with columns like code, name, price) or numbered/bulleted lists for simple items. "
            "For example, if listing 3+ stocks, use a table format with columns. If listing 3+ simple items, use a numbered or bulleted list. "
            "Do NOT use ASCII tree diagrams (├── └── │) or monospace code blocks to show price ladders, support/resistance levels, or MA layouts — use Markdown tables with columns like level, price, and role instead; the client will render tables and charts visually.\\n\\n"
            "### QUICK REPLY CHOICES (client UI, high priority) ###\\n"
            "The desktop client ALWAYS shows quick-reply buttons, including a fixed 功能总览 button that sends a preset user message asking you to explain capabilities and end with FIN_AGENT_CHOICES_JSON; users expect quick replies on every substantive turn. "
            "Therefore on EVERY assistant message that is more than a trivial one-line acknowledgment (e.g. any analysis, tables, tool results, lists, or recommendations), you MUST append the FIN_AGENT_CHOICES_JSON line at the ABSOLUTE END, after all visible Markdown. "
            "The JSON array must contain 2-8 objects tailored to THIS reply: mix concrete next steps (e.g. pick a stock/strategy) with generic follow-ups (expand rationale, compare to index, verify data date, risk checklist) as needed. "
            "Format: one line only: FIN_AGENT_CHOICES_JSON, one space, then one compact JSON array (no line breaks inside the JSON). "
            "Each object: optional string label (short button text), required string send (full user message sent on click). "
            "Example: FIN_AGENT_CHOICES_JSON [{\"label\":\"MACD\",\"send\":\"用 MACD 策略回测 600519.SH 近一年\"},{\"label\":\"均线\",\"send\":\"用双均线策略回测 600519.SH 近一年\"}] "
            "Only omit FIN_AGENT_CHOICES_JSON for ultra-short replies with no follow-up (e.g. a single '好的'). Never put the marker in the middle of the reply. "
            "If the user message is the preset '功能总览' capability request (asks you to list what you can do and to end with FIN_AGENT_CHOICES_JSON), answer comprehensively in Chinese, then append FIN_AGENT_CHOICES_JSON with 6-8 diverse, concrete example user queries covering major features.\\n\\n"
            "### USER CONTEXT & MEMORY ###\\n"
            "You have access to a long-term memory of the user's investment preferences. "
            "Use the 'update_user_profile' tool to SAVE new preferences when the user explicitly states them or when you infer them (e.g., 'I prefer low risk', 'I only buy tech stocks'). "
            "The current user profile is:\\n"
            f"{user_profile_summary}\\n\\n"
            "Tailor your responses and recommendations based on this profile. "
            "If the user asks for recommendations without specifying criteria, refer to their profile (e.g. 'Based on your preference for low risk...')."
        )

    def _init_history(self):
        self.history = [{"role": "system", "content": self._get_system_content()}]

    def _history_to_lc(self) -> list:
        """Convert self.history (list[dict]) → list[LangChain BaseMessage]."""
        messages = []
        for msg in self.history:
            role = msg.get("role")
            content = msg.get("content") or ""
            if role == "system":
                messages.append(SystemMessage(content=content))
            elif role == "user":
                messages.append(HumanMessage(content=content))
            elif role == "assistant":
                raw_tcs = msg.get("tool_calls")
                if raw_tcs:
                    lc_tcs = []
                    for tc in raw_tcs:
                        fn = tc.get("function", {})
                        try:
                            args = json.loads(fn.get("arguments", "{}"))
                        except json.JSONDecodeError:
                            args = {}
                        lc_tcs.append({
                            "id": tc.get("id", ""),
                            "name": fn.get("name", ""),
                            "args": args,
                            "type": "tool_call",
                        })
                    messages.append(AIMessage(content=content, tool_calls=lc_tcs))
                else:
                    messages.append(AIMessage(content=content))
            elif role == "tool":
                messages.append(ToolMessage(
                    content=content,
                    tool_call_id=msg.get("tool_call_id", ""),
                    name=msg.get("name", ""),
                ))
        return messages

    def _ai_msg_to_dict(self, msg: AIMessage) -> dict:
        d = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc["args"]),
                    },
                }
                for tc in msg.tool_calls
            ]
        return d

    def _tool_msg_to_dict(self, msg: ToolMessage) -> dict:
        return {
            "role": "tool",
            "tool_call_id": msg.tool_call_id,
            "content": msg.content,
            "name": msg.name or "",
        }

    # CLI 使用的固定会话 id，与多标签会话共用同一套存储
    CLI_SESSION_ID = "cli-last-session"

    def save_session(self, filename="last_session.json"):
        """保存当前上下文。保留 filename 参数以兼容 CLI 调用。"""
        from fin_agent import session_store
        session_store.save_llm_history(self.CLI_SESSION_ID, self.history)
        return f"Session saved to {session_store.sessions_dir()}"

    def load_session(self, filename="last_session.json"):
        """载入 CLI 会话上下文。保留 filename 参数以兼容 CLI 调用。"""
        from fin_agent import session_store
        try:
            body = session_store.get_session(self.CLI_SESSION_ID)
        except KeyError:
            return "No saved session found."
        history = body.get("llm_history") or []
        if not history:
            return "No saved session found."
        self.history = history
        return "Session loaded."

    def clear_history(self):
        self._init_history()

    # ------------------------------------------------------------------ #
    # <think> tag buffer processor  (preserved from original)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _drain_buffer(buffer: str, thinking_state: bool):
        """
        Parse <think>...</think> tags from streaming buffer.
        Returns (events, remaining_buffer, new_thinking_state).
        events is a list of ("content"|"thinking"|"log", text) tuples.
        """
        events = []
        while True:
            if not thinking_state:
                tag = "<think>"
                if tag in buffer:
                    pre, buffer = buffer.split(tag, 1)
                    if pre:
                        events.append(("content", pre))
                    events.append(("log", "Thinking..."))
                    thinking_state = True
                    continue
                if "<" not in buffer:
                    if buffer:
                        events.append(("content", buffer))
                        buffer = ""
                    break
                idx = buffer.find("<")
                if idx > 0:
                    events.append(("content", buffer[:idx]))
                    buffer = buffer[idx:]
                if tag.startswith(buffer):
                    break
                if len(buffer) >= len(tag):
                    events.append(("content", "<"))
                    buffer = buffer[1:]
                    continue
                if not tag.startswith(buffer):
                    events.append(("content", "<"))
                    buffer = buffer[1:]
                    continue
                break
            else:
                tag = "</think>"
                if tag in buffer:
                    pre, buffer = buffer.split(tag, 1)
                    if pre:
                        events.append(("thinking", pre))
                    thinking_state = False
                    if buffer.startswith("\n"):
                        buffer = buffer[1:]
                    elif buffer.startswith("\r\n"):
                        buffer = buffer[2:]
                    continue
                if "<" not in buffer:
                    if buffer:
                        events.append(("thinking", buffer))
                        buffer = ""
                    break
                idx = buffer.find("<")
                if idx > 0:
                    events.append(("thinking", buffer[:idx]))
                    buffer = buffer[idx:]
                if tag.startswith(buffer):
                    break
                if len(buffer) >= len(tag):
                    events.append(("thinking", "<"))
                    buffer = buffer[1:]
                    continue
                if not tag.startswith(buffer):
                    events.append(("thinking", "<"))
                    buffer = buffer[1:]
                    continue
                break
        return events, buffer, thinking_state

    # ------------------------------------------------------------------ #
    # stream_chat  (same public event protocol as before)
    # ------------------------------------------------------------------ #

    def stream_chat(self, user_input):
        """
        Generator that yields event dicts for the chat interaction.

        Event types (unchanged from original interface):
          content        – LLM text chunk
          thinking       – chain-of-thought chunk (inside <think> tags)
          tool_call_chunk– streaming fragment of a tool call being constructed
          tool_call      – complete tool call about to be executed
          tool_result    – tool execution result
          log            – informational message (e.g. "Thinking...")
          answer         – final answer text (signals completion)
          error          – error message

        Implementation:
          Uses LangGraph stream_mode=["messages","updates"] which yields:
            "messages" events → real-time LLM chunks (AIMessageChunk / ToolMessage)
            "updates"  events → complete node output after each node finishes

          The "updates" stream provides complete AIMessage objects, which are
          used to:
            1. emit tool_call events with full argument JSON
            2. persist new messages to self.history
        """
        debug_print(f"stream_chat: {user_input[:50]}", file=sys.stderr)

        if not self.llm:
            yield {"type": "error", "content": "LLM not initialized. Please check configuration."}
            return

        # Refresh system prompt to pick up latest user profile
        if self.history and self.history[0].get("role") == "system":
            self.history[0]["content"] = self._get_system_content()
        else:
            self.history.insert(0, {"role": "system", "content": self._get_system_content()})

        # Append user turn
        self.history.append({"role": "user", "content": user_input})

        # Convert history to LangChain message objects
        lc_messages = self._history_to_lc()

        # Per-invocation streaming state
        buffer = ""
        thinking_state = False
        final_content = ""  # accumulates current agent-round text for "answer" event
        _wrap_up_logged = False

        try:
            for event_type, data in self.graph.stream(
                {
                    "messages": lc_messages,
                    "step_count": 0,
                    "start_time": time.time(),
                },
                stream_mode=["messages", "updates"],
                config={"recursion_limit": max(25, self.max_steps * 2 + 3)},
            ):

                # ── "messages" → real-time streaming chunks ──────────────
                if event_type == "messages":
                    chunk, meta = data
                    node = meta.get("langgraph_node", "")

                    # Agent / wrap_up node: AIMessageChunk from LLM
                    if node in ("agent", "wrap_up") and isinstance(chunk, AIMessageChunk):
                        # Log once when wrap_up starts
                        if node == "wrap_up" and not _wrap_up_logged:
                            _wrap_up_logged = True
                            yield {"type": "log", "content": "已达到执行限制，正在生成最终答案..."}

                        # Text content — run through <think> parser
                        if chunk.content:
                            buffer += chunk.content
                            final_content += chunk.content
                            evs, buffer, thinking_state = self._drain_buffer(buffer, thinking_state)
                            for ev_type, ev_text in evs:
                                if ev_type == "log":
                                    yield {"type": "log", "content": ev_text}
                                else:
                                    yield {"type": ev_type, "content": ev_text}

                        # Tool-call chunks — real-time display while LLM assembles the call
                        # (wrap_up uses bare LLM without tools, so tool_call_chunks only from agent)
                        if chunk.tool_call_chunks:
                            # Flush any pending text before tool calls appear
                            if buffer:
                                yield {
                                    "type": "thinking" if thinking_state else "content",
                                    "content": buffer,
                                }
                                buffer = ""
                            for tc in chunk.tool_call_chunks:
                                yield {
                                    "type": "tool_call_chunk",
                                    "index": tc.get("index", 0),
                                    "id": tc.get("id"),
                                    "name": tc.get("name"),
                                    "arguments": tc.get("args", ""),
                                }

                # ── "updates" → complete node output (after node finishes) ─
                elif event_type == "updates":
                    for node_name, node_out in data.items():
                        msgs = node_out.get("messages", [])

                        if node_name == "agent":
                            for msg in msgs:
                                if not isinstance(msg, AIMessage):
                                    continue
                                # Emit one tool_call event per tool call (complete args)
                                for tc in (msg.tool_calls or []):
                                    yield {
                                        "type": "tool_call",
                                        "tool_name": tc["name"],
                                        "args": json.dumps(tc["args"]),
                                    }
                                # Persist to history
                                self.history.append(self._ai_msg_to_dict(msg))
                                if not msg.tool_calls:
                                    final_content = msg.content
                                # If this round had tool calls, reset for next round
                                if msg.tool_calls:
                                    final_content = ""
                                    buffer = ""
                                    thinking_state = False

                        elif node_name == "tools":
                            for msg in msgs:
                                if isinstance(msg, ToolMessage):
                                    self.history.append(self._tool_msg_to_dict(msg))
                                    yield {"type": "tool_result", "tool_name": msg.name or "tool", "result": msg.content}

                        elif node_name == "wrap_up":
                            for msg in msgs:
                                if isinstance(msg, AIMessage):
                                    self.history.append(self._ai_msg_to_dict(msg))
                                    final_content = msg.content
                                elif isinstance(msg, ToolMessage):
                                    self.history.append(self._tool_msg_to_dict(msg))

            # Flush any remaining buffer content
            if buffer:
                yield {
                    "type": "thinking" if thinking_state else "content",
                    "content": buffer,
                }

            # Signal completion with final answer text
            yield {"type": "answer", "content": final_content}

        except KeyboardInterrupt:
            yield {"type": "error", "content": "Interrupted by user"}
        except Exception as exc:
            import traceback
            debug_print(traceback.format_exc(), file=sys.stderr)
            yield {"type": "error", "content": f"Error: {exc}"}

    # ------------------------------------------------------------------ #
    # run()  — CLI mode (unchanged public interface)
    # ------------------------------------------------------------------ #

    def run(self, user_input, callback=None):
        """
        Run the agent with user input (CLI / backward-compat entry point).
        Consumes stream_chat() and renders output via rich Live.
        """
        print(f"{Fore.CYAN}Agent: {Style.RESET_ALL}")

        live_md = None
        md_buffer = ""

        def stop_md():
            nonlocal live_md, md_buffer
            if live_md:
                live_md.stop()
                live_md = None
                md_buffer = ""

        def update_md(text):
            nonlocal live_md, md_buffer
            md_buffer += text
            if live_md is None:
                live_md = Live(
                    FinMarkdown(md_buffer),
                    auto_refresh=True,
                    refresh_per_second=4,
                    vertical_overflow="visible",
                )
                live_md.start()
            else:
                live_md.update(FinMarkdown(md_buffer))

        final_answer = ""

        try:
            for event in self.stream_chat(user_input):
                event_type = event["type"]

                if event_type == "content":
                    update_md(event["content"])
                    if callback:
                        callback("content", event["content"])

                elif event_type == "thinking":
                    stop_md()
                    print(f"{Style.DIM}{Fore.YELLOW}{event['content']}", end="", flush=True)
                    if callback:
                        callback("thinking", event["content"])

                elif event_type == "tool_call":
                    stop_md()
                    print(Style.RESET_ALL, end="", flush=True)
                    name = event["tool_name"]
                    args = event["args"]
                    print(f"\n{Fore.CYAN}Calling Tool: {name} with args: {args}{Style.RESET_ALL}")
                    if callback:
                        callback("tool_call", {"name": name, "args": args})

                elif event_type == "tool_result":
                    result = event["result"]
                    display_result = result[:200] + "..." if len(result) > 200 else result
                    print(f"{Fore.BLUE}Tool Result: {display_result}{Style.RESET_ALL}")
                    if callback:
                        callback("tool_result", {"name": event["tool_name"], "result": result})

                elif event_type == "log":
                    stop_md()
                    print(f"{Fore.YELLOW}{event['content']}{Style.RESET_ALL}")

                elif event_type == "error":
                    stop_md()
                    print(f"\n{Fore.RED}Error: {event['content']}{Style.RESET_ALL}")
                    if callback:
                        callback("error", event["content"])
                    return event["content"]

                elif event_type == "answer":
                    final_answer = event["content"]

            stop_md()
            print(Style.RESET_ALL)
            return final_answer

        except KeyboardInterrupt:
            stop_md()
            print(f"\n{Fore.YELLOW}[Interrupted by user]{Style.RESET_ALL}")
            return ""
