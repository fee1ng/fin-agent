"""
Stock news tools for A-share market analysis.

Sources:
  - stock_news_em      : individual stock news via East Money (东方财富)
  - stock_news_main_cx : market-wide headlines via Cailianshe (财联社)
"""
import json
from datetime import datetime


def get_stock_news_em(ts_code: str, limit: int = 20) -> str:
    """
    通过 AKShare stock_news_em 接口获取单只A股的最新新闻资讯（东方财富）。

    :param ts_code: 股票代码，Tushare格式，如 '600000.SH' 或 '000001.SZ'
    :param limit: 返回条数，默认20，最多50
    :return: JSON string with news list, or error string
    """
    try:
        import akshare as ak
    except ImportError:
        return "Error: akshare is not installed. Run: pip install akshare"

    code = ts_code.split('.')[0]
    limit = max(1, min(int(limit), 50))

    try:
        df = ak.stock_news_em(symbol=code)
    except Exception as e:
        return f"Error: AKShare fetch failed for '{ts_code}': {e}"

    if df is None or df.empty:
        return f"Error: No news returned for '{ts_code}'."

    df = df.head(limit)

    rename_map = {
        "关键词": "keyword",
        "新闻标题": "title",
        "新闻内容": "content",
        "发布时间": "pub_time",
        "文章来源": "source",
        "新闻链接": "url",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})
    df = df.astype(str)

    result = {
        "ts_code": ts_code,
        "count": len(df),
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "news": df.to_dict(orient="records"),
    }
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)


def get_stock_news_main_cx(limit: int = 20) -> str:
    """
    通过 AKShare stock_news_main_cx 接口获取财联社实时财经要闻。

    :param limit: 返回条数，默认20，最多50
    :return: JSON string with news list, or error string
    """
    try:
        import akshare as ak
    except ImportError:
        return "Error: akshare is not installed. Run: pip install akshare"

    limit = max(1, min(int(limit), 50))

    try:
        df = ak.stock_news_main_cx()
    except Exception as e:
        return f"Error: AKShare fetch failed for stock_news_main_cx: {e}"

    if df is None or df.empty:
        return "Error: No news returned from 财联社."

    df = df.head(limit)

    rename_map = {
        "标题": "title",
        "内容": "content",
        "发布时间": "pub_time",
        "时间": "pub_time",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})
    df = df.astype(str)

    result = {
        "source": "财联社(cailianshe)",
        "count": len(df),
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "news": df.to_dict(orient="records"),
    }
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)


def get_stock_info_cjzc_em(limit: int = 20) -> str:
    """
    通过 AKShare stock_info_cjzc_em 接口获取东方财富财经早餐资讯。

    :param limit: 返回条数，默认20，最多50
    :return: JSON string with news list, or error string
    """
    try:
        import akshare as ak
    except ImportError:
        return "Error: akshare is not installed. Run: pip install akshare"

    limit = max(1, min(int(limit), 50))

    try:
        df = ak.stock_info_cjzc_em()
    except Exception as e:
        return f"Error: AKShare fetch failed for stock_info_cjzc_em: {e}"

    if df is None or df.empty:
        return "Error: No data returned from 东方财富财经早餐."

    df = df.head(limit)

    rename_map = {
        "标题": "title",
        "摘要": "summary",
        "发布时间": "pub_time",
        "链接": "url",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})
    df = df.astype(str)

    result = {
        "source": "东方财富财经早餐(eastmoney)",
        "count": len(df),
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "news": df.to_dict(orient="records"),
    }
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)


# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------

NEWS_TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "get_stock_news_em",
            "description": (
                "通过东方财富（AKShare stock_news_em）获取单只A股的最新新闻资讯。"
                "返回新闻标题、内容摘要、发布时间、来源等字段。"
                "适用于分析特定股票的最新舆情、公告、事件对走势的影响。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ts_code": {
                        "type": "string",
                        "description": "股票代码，Tushare格式，如 '600000.SH' 或 '000001.SZ'",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回新闻条数，1–50，默认20",
                    },
                },
                "required": ["ts_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_stock_news_main_cx",
            "description": (
                "通过财联社（AKShare stock_news_main_cx）获取A股市场实时财经要闻。"
                "返回最新宏观/行业/市场头条新闻，包含标题、内容、发布时间。"
                "适用于了解市场整体资讯动态，辅助判断大盘或板块走势。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "返回新闻条数，1–50，默认20",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_stock_info_cjzc_em",
            "description": (
                "通过东方财富获取财经早餐资讯（AKShare stock_info_cjzc_em）。"
                "返回近期财经早餐标题、摘要、发布时间、链接。"
                "适用于获取每日财经综合资讯，了解宏观市场动态。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "返回条数，1–50，默认20",
                    },
                },
                "required": [],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def execute_news_tool(tool_name: str, arguments: dict) -> str:
    """Dispatcher for news tools. Called from tushare_tools.execute_tool_call()."""
    if tool_name == "get_stock_news_em":
        return get_stock_news_em(**arguments)
    elif tool_name == "get_stock_news_main_cx":
        return get_stock_news_main_cx(**arguments)
    elif tool_name == "get_stock_info_cjzc_em":
        return get_stock_info_cjzc_em(**arguments)
    else:
        return f"Error: News tool '{tool_name}' not recognized."
