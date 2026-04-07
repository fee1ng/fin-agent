"""
East Money (东方财富) web scraping tools for real-time A-share market data.
Uses public JSON APIs from eastmoney.com — no authentication required.
Covers Shanghai (SH) and Shenzhen (SZ) A-shares only.
"""
import requests
import json
from datetime import datetime

_REQUEST_TIMEOUT = 8  # seconds

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.eastmoney.com/",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ts_code_to_secid(ts_code: str) -> str:
    """
    Convert Tushare ts_code format to East Money secid format.

    600000.SH  ->  1.600000  (Shanghai, market prefix 1)
    000001.SZ  ->  0.000001  (Shenzhen, market prefix 0)
    688001.SH  ->  1.688001  (STAR Market, still Shanghai)
    300750.SZ  ->  0.300750  (ChiNext, still Shenzhen)
    """
    if '.' not in ts_code:
        raise ValueError(f"Invalid ts_code format: '{ts_code}'. Expected format: 'CODE.EXCHANGE'")
    code, exchange = ts_code.upper().split('.', 1)
    if exchange == 'SH':
        return f"1.{code}"
    elif exchange in ('SZ', 'BJ'):
        return f"0.{code}"
    else:
        raise ValueError(f"Unsupported exchange '{exchange}' in '{ts_code}'. Supported: SH, SZ, BJ.")


def _parse_price_field(value):
    """Parse East Money price field. With fltt=2 the API returns actual floats."""
    if value is None or value in ('-', ''):
        return None
    try:
        return round(float(value), 2)
    except (ValueError, TypeError):
        return None


def _parse_pct_field(value):
    """Parse East Money percentage field. With fltt=2 the API returns actual floats (e.g. 2.5 means 2.5%)."""
    if value is None or value in ('-', ''):
        return None
    try:
        return round(float(value), 2)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def get_realtime_quote_em(ts_code: str) -> str:
    """
    Fetch real-time quote for a single A-share stock from East Money (东方财富).

    :param ts_code: Stock code in Tushare format, e.g. '600000.SH' or '000001.SZ'
    :return: JSON string with price fields, or error string
    """
    try:
        secid = _ts_code_to_secid(ts_code)
    except ValueError as e:
        return f"Error: {e}"

    url = "http://push2.eastmoney.com/api/qt/stock/get"
    params = {
        "secid": secid,
        "fields": "f57,f58,f43,f44,f45,f46,f47,f48,f60,f169,f170,f171",
        "invt": "2",
        "fltt": "2",
    }

    try:
        resp = requests.get(url, params=params, headers=_HEADERS, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        d = data.get("data") or {}
        if not d:
            return f"Error: No data returned for '{ts_code}'. Stock may be delisted or code is incorrect."

        result = {
            "ts_code": ts_code,
            "code": d.get("f57"),
            "name": d.get("f58"),
            "current_price": _parse_price_field(d.get("f43")),
            "high": _parse_price_field(d.get("f44")),
            "low": _parse_price_field(d.get("f45")),
            "open": _parse_price_field(d.get("f46")),
            "prev_close": _parse_price_field(d.get("f60")),
            "change": _parse_price_field(d.get("f169")),
            "change_pct": _parse_pct_field(d.get("f170")),
            "amplitude_pct": _parse_pct_field(d.get("f171")),
            "volume_lots": d.get("f47"),
            "turnover_yuan": d.get("f48"),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        return json.dumps(result, ensure_ascii=False, indent=2)

    except requests.exceptions.Timeout:
        return f"Error: Request timed out fetching quote for '{ts_code}'."
    except requests.exceptions.RequestException as e:
        return f"Error: Network error fetching quote for '{ts_code}': {e}"
    except (KeyError, ValueError) as e:
        return f"Error: Failed to parse response for '{ts_code}': {e}"


def get_market_movers_em(category: str = "gainers", limit: int = 10) -> str:
    """
    Fetch top A-share market movers from East Money.

    :param category: 'gainers' (涨幅榜), 'losers' (跌幅榜),
                     'active_volume' (成交量榜), 'active_turnover' (成交额榜)
    :param limit: Number of results (1–50, default 10)
    :return: JSON string with stock list, or error string
    """
    category = (category or "gainers").lower().strip()
    category_config = {
        "gainers":          {"fid": "f3", "po": "1"},
        "losers":           {"fid": "f3", "po": "0"},
        "active_volume":    {"fid": "f5", "po": "1"},
        "active_turnover":  {"fid": "f6", "po": "1"},
    }

    if category not in category_config:
        return f"Error: Unknown category '{category}'. Valid options: {list(category_config.keys())}"

    limit = max(1, min(int(limit), 50))
    cfg = category_config[category]

    url = "http://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1",
        "pz": str(limit),
        "po": cfg["po"],
        "np": "1",
        "fltt": "2",
        "invt": "2",
        "fid": cfg["fid"],
        "fs": "m:0+t:6,m:0+t:13,m:1+t:2,m:1+t:23",
        "fields": "f1,f2,f3,f4,f5,f6,f12,f14,f15,f16,f17,f18,f20,f21,f23",
    }

    try:
        resp = requests.get(url, params=params, headers=_HEADERS, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        diff = (data.get("data") or {}).get("diff") or []
        if not diff:
            return f"Error: No market mover data returned for category '{category}'."

        stocks = []
        for item in diff:
            stocks.append({
                "code": item.get("f12"),
                "name": item.get("f14"),
                "current_price": _parse_price_field(item.get("f2")),
                "change_pct": _parse_pct_field(item.get("f3")),
                "change": _parse_price_field(item.get("f4")),
                "volume_lots": item.get("f5"),
                "turnover_yuan": item.get("f6"),
                "high": _parse_price_field(item.get("f15")),
                "low": _parse_price_field(item.get("f16")),
                "open": _parse_price_field(item.get("f17")),
                "prev_close": _parse_price_field(item.get("f18")),
            })

        output = {
            "category": category,
            "count": len(stocks),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "stocks": stocks,
        }
        return json.dumps(output, ensure_ascii=False, indent=2)

    except requests.exceptions.Timeout:
        return "Error: Request timed out fetching market movers."
    except requests.exceptions.RequestException as e:
        return f"Error: Network error fetching market movers: {e}"
    except (KeyError, ValueError) as e:
        return f"Error: Failed to parse market movers response: {e}"


def get_market_indices_em() -> str:
    """
    Fetch real-time data for the four major A-share indices from East Money:
    上证指数 (SSE Composite), 沪深300 (CSI 300), 深证成指 (SZSE Component), 创业板指 (ChiNext).

    :return: JSON string with index data, or error string
    """
    url = "http://push2.eastmoney.com/api/qt/ulist.np/get"
    params = {
        "fltt": "2",
        "invt": "2",
        "fields": "f1,f2,f3,f4,f12,f13,f14,f15,f16,f17,f18",
        "secids": "1.000001,1.000300,0.399001,0.399006",
    }

    try:
        resp = requests.get(url, params=params, headers=_HEADERS, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        diff = (data.get("data") or {}).get("diff") or []
        if not diff:
            return "Error: No index data returned from East Money."

        indices = []
        for item in diff:
            indices.append({
                "code": item.get("f12"),
                "name": item.get("f14"),
                "current": _parse_price_field(item.get("f2")),
                "change_pct": _parse_pct_field(item.get("f3")),
                "change": _parse_price_field(item.get("f4")),
                "high": _parse_price_field(item.get("f15")),
                "low": _parse_price_field(item.get("f16")),
                "open": _parse_price_field(item.get("f17")),
                "prev_close": _parse_price_field(item.get("f18")),
            })

        output = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "indices": indices,
        }
        return json.dumps(output, ensure_ascii=False, indent=2)

    except requests.exceptions.Timeout:
        return "Error: Request timed out fetching market indices."
    except requests.exceptions.RequestException as e:
        return f"Error: Network error fetching market indices: {e}"
    except (KeyError, ValueError) as e:
        return f"Error: Failed to parse market indices response: {e}"


def search_stock_em(keyword: str) -> str:
    """
    Search for A-share stocks by name or code using East Money's suggest API.

    :param keyword: Chinese name (e.g. '平安银行', '茅台') or code prefix (e.g. '600', '000001')
    :return: JSON string with matching stocks, or error string
    """
    if not keyword or not keyword.strip():
        return "Error: keyword must not be empty."

    url = "https://searchapi.eastmoney.com/api/suggest/get"
    params = {
        "input": keyword.strip(),
        "type": "14",   # 14 = A-share stocks
        "token": "D43BF722C8E33BDC906FB84D85E326AB",
        "count": "10",
    }

    try:
        resp = requests.get(url, params=params, headers=_HEADERS, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        items = (data.get("QuotationCodeTable") or {}).get("Data") or []
        if not items:
            return json.dumps({"keyword": keyword, "count": 0, "results": []}, ensure_ascii=False)

        results = []
        for item in items:
            mkt = item.get("MktNum", "")
            exchange = "SH" if str(mkt) == "1" else "SZ"
            code = item.get("Code", "")
            results.append({
                "code": code,
                "name": item.get("Name"),
                "market": exchange,
                "ts_code": f"{code}.{exchange}",
                "type": item.get("SecurityTypeName"),
            })

        output = {
            "keyword": keyword,
            "count": len(results),
            "results": results,
        }
        return json.dumps(output, ensure_ascii=False, indent=2)

    except requests.exceptions.Timeout:
        return f"Error: Request timed out searching for '{keyword}'."
    except requests.exceptions.RequestException as e:
        return f"Error: Network error searching for '{keyword}': {e}"
    except (KeyError, ValueError) as e:
        return f"Error: Failed to parse search response: {e}"


def get_sector_stocks_em(market: str = "ALL", limit: int = 20) -> str:
    """
    Get a snapshot of stocks from Shanghai (SH), Shenzhen (SZ), or all A-shares (ALL)
    with real-time prices from East Money.

    :param market: 'SH' for Shanghai, 'SZ' for Shenzhen, 'ALL' for all A-shares
    :param limit: Number of stocks to return (1–100, default 20)
    :return: JSON string with stock list, or error string
    """
    market = (market or "ALL").upper().strip()
    fs_map = {
        "SH":  "m:1+t:2,m:1+t:23",
        "SZ":  "m:0+t:6,m:0+t:13",
        "ALL": "m:0+t:6,m:0+t:13,m:1+t:2,m:1+t:23",
    }

    if market not in fs_map:
        return f"Error: Unknown market '{market}'. Valid options: SH, SZ, ALL."

    limit = max(1, min(int(limit), 100))

    url = "http://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1",
        "pz": str(limit),
        "po": "1",
        "np": "1",
        "fltt": "2",
        "invt": "2",
        "fid": "f3",
        "fs": fs_map[market],
        "fields": "f1,f2,f3,f4,f5,f6,f12,f14,f15,f16,f17,f18,f20,f21,f23",
    }

    try:
        resp = requests.get(url, params=params, headers=_HEADERS, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        diff = (data.get("data") or {}).get("diff") or []
        if not diff:
            return f"Error: No stock data returned for market '{market}'."

        stocks = []
        for item in diff:
            stocks.append({
                "code": item.get("f12"),
                "name": item.get("f14"),
                "current_price": _parse_price_field(item.get("f2")),
                "change_pct": _parse_pct_field(item.get("f3")),
                "change": _parse_price_field(item.get("f4")),
                "volume_lots": item.get("f5"),
                "turnover_yuan": item.get("f6"),
                "high": _parse_price_field(item.get("f15")),
                "low": _parse_price_field(item.get("f16")),
                "open": _parse_price_field(item.get("f17")),
                "prev_close": _parse_price_field(item.get("f18")),
                "total_mv": item.get("f20"),
                "float_mv": item.get("f21"),
                "pe_dynamic": item.get("f23"),
            })

        output = {
            "market": market,
            "count": len(stocks),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "stocks": stocks,
        }
        return json.dumps(output, ensure_ascii=False, indent=2)

    except requests.exceptions.Timeout:
        return f"Error: Request timed out fetching stocks for market '{market}'."
    except requests.exceptions.RequestException as e:
        return f"Error: Network error fetching stocks for market '{market}': {e}"
    except (KeyError, ValueError) as e:
        return f"Error: Failed to parse stock data for market '{market}': {e}"


# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------

EASTMONEY_TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "get_realtime_quote_em",
            "description": (
                "通过东方财富网获取单只A股的实时行情（无需Token）。"
                "返回最新价、涨跌幅、涨跌额、最高/最低/开盘价、昨收、成交量、成交额等。"
                "适用于无法使用Tushare实时数据时的替代方案。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ts_code": {
                        "type": "string",
                        "description": "Tushare格式股票代码，如 '600000.SH'（沪市）或 '000001.SZ'（深市）。"
                    }
                },
                "required": ["ts_code"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_market_movers_em",
            "description": (
                "通过东方财富网获取A股市场涨跌幅榜或成交排行（无需Token）。"
                "支持：涨幅榜(gainers)、跌幅榜(losers)、成交量榜(active_volume)、成交额榜(active_turnover)。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["gainers", "losers", "active_volume", "active_turnover"],
                        "description": "排行类型：'gainers'涨幅榜，'losers'跌幅榜，'active_volume'成交量榜，'active_turnover'成交额榜。默认gainers。"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回股票数量，1–50，默认10。"
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_market_indices_em",
            "description": (
                "通过东方财富网获取A股四大主要指数的实时数据（无需Token）："
                "上证指数、沪深300、深证成指、创业板指。适合快速了解大盘走势。"
            ),
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
            "name": "search_stock_em",
            "description": (
                "通过东方财富网按名称或代码搜索A股股票（无需Token）。"
                "返回匹配的股票代码、名称及Tushare格式ts_code。"
                "适用于用户提供模糊名称或代码前缀时的股票查找。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "搜索关键词：中文名称（如'平安银行'、'茅台'）或代码前缀（如'600'、'000001'）。"
                    }
                },
                "required": ["keyword"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_sector_stocks_em",
            "description": (
                "通过东方财富网获取沪市(SH)、深市(SZ)或全A股(ALL)的实时行情快照（无需Token）。"
                "返回指定数量股票的最新价、涨跌幅、成交量、市值、市盈率等数据。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "market": {
                        "type": "string",
                        "enum": ["SH", "SZ", "ALL"],
                        "description": "'SH'仅沪市，'SZ'仅深市，'ALL'全部A股。默认ALL。"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回股票数量，1–100，默认20。"
                    }
                },
                "required": []
            }
        }
    },
]


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def execute_eastmoney_tool(tool_name: str, arguments: dict) -> str:
    """
    Dispatcher for East Money tools. Called from tushare_tools.execute_tool_call().
    """
    if tool_name == "get_realtime_quote_em":
        return get_realtime_quote_em(**arguments)
    elif tool_name == "get_market_movers_em":
        return get_market_movers_em(**arguments)
    elif tool_name == "get_market_indices_em":
        return get_market_indices_em(**arguments)
    elif tool_name == "search_stock_em":
        return search_stock_em(**arguments)
    elif tool_name == "get_sector_stocks_em":
        return get_sector_stocks_em(**arguments)
    else:
        return f"Error: East Money tool '{tool_name}' not recognized."
