"""Local MCP transport for the existing finance tools (stdout is protocol only)."""
import asyncio
import contextlib
import json
import os
import sys
import re

from fin_agent.agent.redaction import configured_redactor


class SafeLog:
    def __init__(self, target, redactor):
        self.target, self.redactor = target, redactor
        self.buffer = ''

    def write(self, text):
        # Buffer complete lines so a secret split across writes stays detectable.
        self.buffer += text
        while '\n' in self.buffer:
            line, self.buffer = self.buffer.split('\n', 1)
            self.target.write(self.redactor.text(line) + '\n')
        return len(text)

    def flush(self):
        self.target.flush()

    def finish(self):
        if self.buffer:
            self.target.write(self.redactor.text(self.buffer))
            self.buffer = ''
        self.target.flush()


def is_business_error(result):
    if isinstance(result, dict):
        return bool(result.get('error')) or result.get('ok') is False or result.get('success') is False
    if isinstance(result, str):
        try:
            value = json.loads(result)
        except ValueError:
            return bool(re.match(r'^\s*(?:error\b|错误|失败|参数错误|无法)', result, re.I))
        return is_business_error(value) if isinstance(value, dict) else False
    return False


def allowed_schemas():
    from fin_agent.tools.tushare_tools import TOOLS_SCHEMA
    allowed = set(os.environ.get("FIN_AGENT_HARNESS_TOOLS", "*").split(","))
    catalog = [item['function'] for item in TOOLS_SCHEMA]
    names = [schema['name'] for schema in catalog]
    if len(names) != len(set(names)):
        raise ValueError('Duplicate finance tool names')
    return [schema for schema in catalog if '*' in allowed or schema['name'] in allowed]


def execute(name, arguments):
    from fin_agent.tools.tushare_tools import execute_tool_call
    if name not in {item["name"] for item in allowed_schemas()}:
        raise ValueError("Tool is not enabled in this Harness profile")
    if name in {"reset_core_config", "reset_email_config"}:
        return {"ok": False, "error": "configuration_required"}
    redactor = configured_redactor()
    safe_log = SafeLog(sys.stderr, redactor)
    try:
        with contextlib.redirect_stdout(safe_log), contextlib.redirect_stderr(safe_log):
            return redactor.clean(execute_tool_call(name, redactor.clean(arguments)))
    finally:
        safe_log.finish()


async def serve():
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool, TextContent, CallToolResult

    server = Server("fin-agent")

    @server.list_tools()
    async def list_tools():
        return [Tool(name=s["name"], description=s.get("description", ""),
                     inputSchema=s["parameters"]) for s in allowed_schemas()]

    @server.call_tool()
    async def call_tool(name, arguments):
        try:
            result = execute(name, arguments)
        except Exception as exc:
            result = {'ok': False, 'error': type(exc).__name__,
                      'message': configured_redactor().text(exc)}
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        return CallToolResult(content=[TextContent(type="text", text=text)], isError=is_business_error(result))

    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(serve())
