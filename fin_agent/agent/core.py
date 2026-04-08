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
        elif provider in ("openai", "local", "openrouter"):
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
        llm_with_tools = self.llm.bind_tools(TOOLS_SCHEMA)

        def agent_node(state: AgentState) -> dict:
            response = llm_with_tools.invoke(state["messages"])
            return {"messages": [response], "step_count": state.get("step_count", 0) + 1}

        def tools_node(state: AgentState) -> dict:
            last = state["messages"][-1]
            results = []
            for tc in last.tool_calls:
                try:
                    result = execute_tool_call(tc["name"], json.dumps(tc["args"]))
                except Exception as exc:
                    result = f"Error executing tool: {exc}"
                results.append(ToolMessage(
                    content=str(result),
                    tool_call_id=tc["id"],
                    name=tc["name"],
                ))
            return {"messages": results}

        def wrap_up_node(state: AgentState) -> dict:
            messages = list(state["messages"]) + [HumanMessage(content=FORCED_STOP_MESSAGE)]
            response = self.llm.invoke(messages)
            return {"messages": [response]}

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

    def _get_system_content(self):
        user_profile_summary = get_profile_manager().get_profile_summary()

        return (
            "You are a financial assistant focused on A-share (沪深) market data.\\n"
            "You can help users check real-time stock prices, market indices, movers, and manage their portfolio.\\n\\n"
            "### CRITICAL PROTOCOL ###\\n"
            "1. **CHECK TIME FIRST**: When the user mentions relative dates ('today', 'this week', 'recent', 'latest', '最近', '今天', '本周'), call 'get_current_time' first to get the exact date, then compute the required date range.\\n"
            "2. **ONE FETCH, THEN ANALYZE**: Gather ALL required data before writing any analysis. When multiple tools are needed, call them ALL in the same turn and wait for every result — do NOT produce any analysis after a partial result, analyze ONCE with all data in hand.\\n"
            "3. **USE TOOLS**: All market data MUST be obtained via tools. Do not use internal knowledge for prices or market data.\\n\\n"
            "### TOOL SELECTION RULES ###\\n"
            "**Data source is East Money (东方财富), covering A-shares (沪深) only.**\\n\\n"
            "**Rule 1 — Trend / Historical analysis (走势、涨跌、近N天/周/月表现、K线)**\\n"
            "MANDATORY: issue BOTH tool calls simultaneously (in the same turn), wait for BOTH results, then write ONE analysis. Never analyze after only one result arrives.\\n"
            "  - 'get_hist_data_em' with the full date range for OHLCV data\\n"
            "  - 'get_stock_news_em' for the same stock (default limit=20)\\n"
            "DO NOT call 'get_realtime_quote_em'; historical data already covers recent trading days.\\n"
            "Example: '最近一周走势' → get_current_time → {get_hist_data_em + get_stock_news_em} → single combined analysis.\\n\\n"
            "**Rule 2 — Spot price only (当前价、实时价、最新价)**\\n"
            "Use 'get_realtime_quote_em' for a single stock's current price snapshot. "
            "Only use this when the user explicitly asks for the current/real-time price, NOT for trend or historical analysis.\\n\\n"
            "**Rule 3 — Market overview**\\n"
            "Indices (上证指数, 沪深300, etc.) → 'get_market_indices_em'.\\n"
            "Gainers/losers/volume boards (涨幅榜, 跌幅榜, etc.) → 'get_market_movers_em'.\\n"
            "Broad market snapshot → 'get_sector_stocks_em'.\\n\\n"
            "**Rule 4 — News & sentiment**\\n"
            "Individual stock news (舆情、公告、事件) → 'get_stock_news_em(ts_code)'. "
            "**REQUIRED whenever Rule 1 applies** — always fetch alongside historical price data.\\n"
            "Market-wide financial headlines (宏观要闻、市场资讯) → 'get_stock_news_main_cx()'. "
            "Call this when the user asks about macro news or broad market context.\\n\\n"
            "**Rule 5 — Other operations**\\n"
            "Search stock by name/code → 'search_stock_em'.\\n"
            "Portfolio queries ('my portfolio', '我的持仓') → 'get_portfolio_status'.\\n"
            "Add/remove position → 'add_portfolio_position' / 'remove_portfolio_position'.\\n"
            "Price alerts with % threshold → first get current price via 'get_realtime_quote_em', compute absolute target, then set alert.\\n"
            "Config (email, LLM) → 'reset_email_config' / 'reset_core_config' (INTERACTIVE, call directly, do not ask user for details).\\n\\n"
            "When analyzing, EXPLICITLY state the date range and data source of the data you are using.\\n"
            "When you have enough information, answer the user's question directly.\\n\\n"
            "### RESPONSE DISCIPLINE ###\\n"
            "**STRICT**: Answer ONLY what the user explicitly asked. Do NOT add unrequested analysis.\\n"
            "- If the user asks for multiple stocks' trends individually, present each stock's data separately. Do NOT add a comparison section unless the user explicitly asks to compare (e.g., '对比', '哪个更好', 'compare').\\n"
            "- Do NOT add investment advice, buy/sell recommendations, or risk warnings unless the user asks.\\n"
            "- Do NOT add summary conclusions that go beyond the scope of the question.\\n\\n"
            "### OUTPUT FORMATTING ###\\n"
            "**CRITICAL**: If you need to present a list of items (stocks, companies, data points, etc.) and the count is 3 or more, you MUST format it as a Markdown table or a structured list. "
            "Do NOT present 3+ items as plain text paragraphs. Use tables for structured data (e.g., stock lists with columns like code, name, price) or numbered/bulleted lists for simple items. "
            "For example, if listing 3+ stocks, use a table format with columns. If listing 3+ simple items, use a numbered or bulleted list.\\n\\n"
            "### USER CONTEXT & MEMORY ###\\n"
            "You have access to a long-term memory of the user's investment preferences. "
            "Use the 'update_user_profile' tool to SAVE new preferences when the user explicitly states them or when you infer them (e.g., 'I prefer low risk', 'I only buy tech stocks'). "
            "The current user profile is:\\n"
            f"{user_profile_summary}\\n\\n"
            "Tailor your responses and recommendations based on this profile. "
            "If the user asks for recommendations without specifying criteria, refer to their profile (e.g. 'Based on your preference for low risk...')."
        )

    # ------------------------------------------------------------------ #
    # History management  (dict format for save/load compat)
    # ------------------------------------------------------------------ #

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

    def save_session(self, filename="last_session.json"):
        config_dir = Config.get_config_dir()
        filepath = os.path.join(config_dir, "sessions", filename)
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(self.history, f, ensure_ascii=False, indent=2)
            return f"Session saved to {filepath}"
        except Exception as exc:
            return f"Error saving session: {exc}"

    def load_session(self, filename="last_session.json"):
        config_dir = Config.get_config_dir()
        filepath = os.path.join(config_dir, "sessions", filename)
        if not os.path.exists(filepath):
            return "No saved session found."
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                self.history = json.load(f)
            if self.history and self.history[0].get("role") == "system":
                self.history[0]["content"] = self._get_system_content()
            return f"Session loaded from {filepath}"
        except Exception as exc:
            return f"Error loading session: {exc}"

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

                    # Tools node: ToolMessage (complete, not chunked)
                    elif node == "tools" and isinstance(chunk, ToolMessage):
                        # Reload LLM if core config was just reset
                        if chunk.name == "reset_core_config":
                            yield {"type": "log", "content": "Reloading LLM configuration..."}
                            try:
                                self.llm = self._create_llm()
                                self.graph = self._build_graph()
                                yield {"type": "log", "content": "LLM re-initialized successfully."}
                            except Exception as exc:
                                yield {"type": "error", "content": f"Error re-initializing LLM: {exc}"}

                        yield {
                            "type": "tool_result",
                            "tool_name": chunk.name or "tool",
                            "result": chunk.content,
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
                                # If this round had tool calls, reset for next round
                                if msg.tool_calls:
                                    final_content = ""
                                    buffer = ""
                                    thinking_state = False

                        elif node_name == "tools":
                            for msg in msgs:
                                if isinstance(msg, ToolMessage):
                                    self.history.append(self._tool_msg_to_dict(msg))

                        elif node_name == "wrap_up":
                            for msg in msgs:
                                if isinstance(msg, AIMessage):
                                    self.history.append(self._ai_msg_to_dict(msg))

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
