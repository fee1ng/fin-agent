import json
from datetime import datetime
from fin_agent.config import Config
from fin_agent.tools.portfolio_tools import (
    PORTFOLIO_TOOLS_SCHEMA,
    add_portfolio_position,
    remove_portfolio_position,
    get_portfolio_status,
    clear_portfolio
)
from fin_agent.tools.scheduler_tools import (
    SCHEDULER_TOOLS_SCHEMA,
    add_price_alert,
    list_alerts,
    remove_alert,
    update_alert,
    reset_email_config
)
from fin_agent.tools.profile_tools import (
    PROFILE_TOOLS_SCHEMA,
    update_user_profile,
    get_user_profile
)
from fin_agent.tools.eastmoney_tools import (
    EASTMONEY_TOOLS_SCHEMA,
    execute_eastmoney_tool,
)
from fin_agent.tools.news import (
    NEWS_TOOLS_SCHEMA,
    execute_news_tool,
)


def get_current_time():
    """Get current date and time."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def reset_core_config():
    """Reset core configuration (LLM provider & API keys) interactively."""
    try:
        print("Initiating core configuration reset...")
        Config.setup()
        return "Core configuration wizard finished. New settings are applied."
    except Exception as e:
        return f"Error resetting core config: {str(e)}"


# Tool definitions for LLM
BASE_TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "reset_core_config",
            "description": "Reset or update core configuration (LLM Provider, API Keys) interactively. Use this when the user wants to change API keys or providers.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "Get the current system date and time. Use this when the user asks about 'today', 'now', or relative dates.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
]

# Combine all tool schemas
TOOLS_SCHEMA = BASE_TOOLS_SCHEMA + PORTFOLIO_TOOLS_SCHEMA + SCHEDULER_TOOLS_SCHEMA + PROFILE_TOOLS_SCHEMA + EASTMONEY_TOOLS_SCHEMA + NEWS_TOOLS_SCHEMA


def execute_tool_call(tool_name, arguments):
    if isinstance(arguments, str):
        if not arguments.strip():
            arguments = {}
        else:
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                try:
                    import ast
                    val = ast.literal_eval(arguments)
                    if isinstance(val, dict):
                        arguments = val
                    else:
                        return "Error: Invalid JSON arguments (not a dict)."
                except:
                    return "Error: Invalid JSON arguments."

    if arguments is None:
        arguments = {}

    if tool_name == "get_current_time":
        return get_current_time()
    elif tool_name == "reset_core_config":
        return reset_core_config()
    elif tool_name == "add_portfolio_position":
        return add_portfolio_position(**arguments)
    elif tool_name == "remove_portfolio_position":
        return remove_portfolio_position(**arguments)
    elif tool_name == "get_portfolio_status":
        return get_portfolio_status(**arguments)
    elif tool_name == "clear_portfolio":
        return clear_portfolio(**arguments)
    elif tool_name == "add_price_alert":
        return add_price_alert(**arguments)
    elif tool_name == "list_alerts":
        return list_alerts(**arguments)
    elif tool_name == "remove_alert":
        return remove_alert(**arguments)
    elif tool_name == "update_alert":
        return update_alert(**arguments)
    elif tool_name == "reset_email_config":
        return reset_email_config()
    elif tool_name == "update_user_profile":
        return update_user_profile(**arguments)
    elif tool_name == "get_user_profile":
        return get_user_profile(**arguments)
    elif tool_name in {
        "get_realtime_quote_em",
        "get_market_movers_em",
        "get_market_indices_em",
        "search_stock_em",
        "get_sector_stocks_em",
        "get_hist_data_em",
    }:
        return execute_eastmoney_tool(tool_name, arguments)
    elif tool_name in {
        "get_stock_news_em",
        "get_stock_news_main_cx",
    }:
        return execute_news_tool(tool_name, arguments)
    else:
        return f"Error: Tool '{tool_name}' not found."
