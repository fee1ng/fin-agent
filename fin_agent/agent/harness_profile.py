"""Build the pinned, finance-only Harness composition without embedding credentials."""
import hashlib
import json
from pathlib import Path
import sys


SDK_VERSION = "0.2.1rc2"
UPSTREAM_COMMIT = "687f282e8625c6b8d11ff58211a90524be2e4317"
PROVIDER = "finance-deepseek"


def status_for_finish(reason):
    # TurnEndReasonMap in the pinned upstream packages/core/session/src/types.ts.
    return {'completed': 'succeeded', 'aborted': 'cancelled', 'blocked': 'rejected',
            'error': 'failed', 'max-tokens': 'failed', 'interrupted': 'interrupted',
            'forked': 'interrupted'}.get(reason, 'interrupted')


def write_profile(directory, *, base_url, model, cwd, max_tokens=4096, tools="*"):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    disabled = (
        "persistent-bash", "persistent-pwsh", "tool-fs", "tool-working-directory",
        "session-log-deepseek", "plugin-package-inventory-deepseek",
        "deepseek-llm-api-extensions", "mcp-resources",
    )
    rows = [{"id": name, "disabled": True} for name in disabled]
    rows.append({'id': 'system-prompt', 'config': {
        'includeHarnessIdentity': False, 'includeRuntimeContext': False,
        'personaPrefix': (
            'You are a financial analysis assistant. Use the provided finance MCP tools for all '
            'market facts, news, portfolios, subscriptions, technical indicators and backtests. '
            'First call the current-time tool for time-sensitive questions. State the date and '
            'data source of your evidence. Never report an operation as successful before its '
            'tool result confirms success. Never retry a write whose outcome is unknown. '
            'Configuration credentials must be managed outside chat. A configuration_required '
            'result means the user must configure the host CLI. Respond in the user language.'
        )}})
    rows.append({"insert": [
        {"id": "finance-model-route", "name": "@deepseek-ai/dsh-llm-pi-ai", "config": {
            "providers": {PROVIDER: {
                "apiKeyEnv": "DEEPSEEK_API_KEY", "api": "openai-completions",
                "baseURL": base_url, "defaultMaxTokens": max_tokens,
                "retryPolicy": {"mode": "normal", "maxRetries": 0},
                "models": [{"id": model, "contextWindow": 65536, "reasoningEfforts": False}],
            }}}},
        {"id": "finance-checkpoint", "name": str(Path(__file__).with_name("harness_checkpoint.mjs")),
         "config": {"ackDirectory": str(directory / "acks")}},
        {"id": "finance-mcp", "name": "@deepseek-ai/dsh-mcp-client", "config": {
            "serverName": "finance", "transport": "stdio", "command": sys.executable,
            "args": ["-m", "fin_agent.agent.tool_bridge"], "cwd": str(cwd),
            "env": {"FIN_AGENT_HARNESS_TOOLS": tools},
            "failOnStartupError": True,
        }},
    ]})
    content = json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8")
    profile = directory / "finance.patch.yml"
    profile.write_bytes(content)
    profile.chmod(0o600)
    return profile, hashlib.sha256(content).hexdigest()
