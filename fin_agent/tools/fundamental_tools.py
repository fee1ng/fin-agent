"""
Fundamental analysis tools for A-share market.

Covers:
  - Financial indicators (ROE, ROA, margins, debt ratio, etc.)
  - Peer valuation comparison (same-industry PE/PB ranking)
  - Dividend history (cash dividends, bonus shares, ex-dividend dates)
  - Event calendar (corporate announcements & disclosures)

Data sources: AKShare (东方财富, 巨潮资讯)

Cache strategy (two-tier):
  L1 — Redis  (enabled via REDIS_ENABLED=true in config)
  L2 — Local JSON files in Config.get_config_dir()  (always available as fallback)
"""

import json
import os
import time
from datetime import datetime
from typing import Optional

from fin_agent.config import Config
from fin_agent.cache.redis_cache import get_cache


# ---------------------------------------------------------------------------
# Local file cache  (L2 fallback)
#
# industry_map.json  — {code: board_slug, "_built_at": timestamp}   TTL=7d
# peer_cache.json    — {board_slug: {"_fetched_at": ts, "data": []}} TTL=1h
# indicator_cache.json — {ts_code: {"_fetched_at": ts, "data": []}} TTL=24h
# dividend_cache.json  — {code: {"_fetched_at": ts, "data": []}}    TTL=7d
# events_cache.json    — {"{date}:{type}": {"_fetched_at": ts, "data": []}} TTL=1d
# ---------------------------------------------------------------------------

_LOCAL_TTL = {
    "industry": 7 * 24 * 3600,
    "peer":     3600,
    "indicator": 86400,
    "dividend": 7 * 24 * 3600,
    "events":   86400,
}


def _local_path(filename: str) -> str:
    d = Config.get_config_dir()
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, filename)


def _local_load(filename: str) -> dict:
    path = _local_path(filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _local_save(filename: str, data: dict) -> None:
    try:
        with open(_local_path(filename), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    except Exception:
        pass


def _local_fresh(store: dict, key: str, ttl: int) -> bool:
    entry = store.get(key)
    return bool(entry and time.time() - entry.get("_fetched_at", 0) < ttl)


# ---------------------------------------------------------------------------
# Industry mapping helpers
# ---------------------------------------------------------------------------

def _board_slug(name: str) -> str:
    """Delegate to redis_cache._slug for consistent slugification."""
    from fin_agent.cache.redis_cache import _slug
    return _slug(name)


def _fetch_industry_map_from_akshare() -> dict:
    """
    Build {code: board_slug} by scanning all industry boards.
    Prints progress because this takes ~60-120 s on first run.
    """
    import akshare as ak
    print("[fin-agent] 首次构建行业-股票映射缓存，约需 60-120 秒，请稍候...")
    try:
        board_df = ak.stock_board_industry_name_em()
    except Exception as e:
        print(f"[fin-agent] 获取行业板块列表失败: {e}")
        return {}

    mapping: dict[str, str] = {}
    for _, row in board_df.iterrows():
        board_name = row.get("板块名称", "")
        if not board_name:
            continue
        try:
            cons_df = ak.stock_board_industry_cons_em(symbol=board_name)
            if cons_df is not None and not cons_df.empty and "代码" in cons_df.columns:
                slug = _board_slug(board_name)
                for code in cons_df["代码"].values:
                    mapping[str(code)] = slug
        except Exception:
            continue

    print(f"[fin-agent] 行业映射缓存已构建，覆盖 {len(mapping)} 只股票。")
    return mapping


def _get_industry_slug(code: str) -> Optional[str]:
    """
    Return the industry board slug for `code`.

    Read path:
      1. Redis (if enabled)  → L2 local file → AKShare rebuild
    Write path mirrors the same priority.
    """
    cache = get_cache()

    # --- Redis path ---
    if cache:
        if cache.is_industry_map_fresh():
            return cache.get_industry(code)
        # Map stale/missing in Redis → rebuild and populate Redis
        mapping = _fetch_industry_map_from_akshare()
        if mapping:
            cache.set_industries_bulk(mapping)
            cache.mark_industry_map_built()
        return cache.get_industry(code)

    # --- Local file fallback ---
    store = _local_load("industry_map.json")
    if store and time.time() - store.get("_built_at", 0) < _LOCAL_TTL["industry"]:
        return store.get(code)

    mapping = _fetch_industry_map_from_akshare()
    if mapping:
        mapping["_built_at"] = time.time()
        _local_save("industry_map.json", mapping)
    return mapping.get(code)


def _fetch_peer_data(board_slug: str) -> list:
    """Fetch live peer data for a board from AKShare and return as list of dicts."""
    import akshare as ak
    from fin_agent.cache.redis_cache import slug_to_board
    board_name = slug_to_board(board_slug)
    try:
        df = ak.stock_board_industry_cons_em(symbol=board_name)
    except Exception:
        return []
    if df is None or df.empty:
        return []
    rename_map = {
        "代码": "code", "名称": "name", "最新价": "current_price",
        "涨跌幅": "change_pct", "成交额": "turnover",
        "市盈率-动态": "pe_dynamic", "市净率": "pb", "换手率": "turnover_rate",
    }
    available = {k: v for k, v in rename_map.items() if k in df.columns}
    return df[list(available.keys())].rename(columns=available).to_dict(orient="records")


def _get_peer_data(board_slug: str) -> list:
    """
    Return peer comparison rows for a board (list of dicts).

    Read path: Redis → local file → AKShare
    """
    cache = get_cache()

    # --- Redis path ---
    if cache:
        rows = cache.get_peer_board(board_slug)
        if rows is not None:
            return rows
        rows = _fetch_peer_data(board_slug)
        if rows:
            cache.set_peer_board(board_slug, rows)
        return rows

    # --- Local file fallback ---
    store = _local_load("peer_cache.json")
    entry = store.get(board_slug)
    if entry and time.time() - entry.get("_fetched_at", 0) < _LOCAL_TTL["peer"]:
        return entry.get("data", [])

    rows = _fetch_peer_data(board_slug)
    store[board_slug] = {"_fetched_at": time.time(), "data": rows}
    _local_save("peer_cache.json", store)
    return rows


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def get_financial_indicators(ts_code: str, indicator_type: str = "按报告期",
                             limit: int = 8) -> str:
    """
    获取个股核心财务分析指标（东方财富源，带缓存）。

    :param ts_code: 股票代码，Tushare格式，如 '000001.SZ'
    :param indicator_type: '按报告期' 或 '按单季度'
    :param limit: 返回期数，默认8
    :return: JSON string with financial indicators
    """
    try:
        import akshare as ak
    except ImportError:
        return "Error: akshare is not installed. Run: pip install akshare"

    indicator_type = indicator_type if indicator_type in ("按报告期", "按单季度") else "按报告期"
    limit = max(1, min(int(limit), 20))

    cache = get_cache()
    cache_key = f"{ts_code}:{indicator_type}"

    # --- Redis path ---
    if cache:
        rows = cache.get_indicator(cache_key)
        if rows is not None:
            trimmed = rows[:limit]
            return json.dumps({
                "ts_code": ts_code, "indicator_type": indicator_type,
                "count": len(trimmed), "cached": True,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "data": trimmed,
            }, ensure_ascii=False, indent=2, default=str)

    # --- Local file fallback check ---
    if not cache:
        store = _local_load("indicator_cache.json")
        if _local_fresh(store, cache_key, _LOCAL_TTL["indicator"]):
            rows = store[cache_key]["data"][:limit]
            return json.dumps({
                "ts_code": ts_code, "indicator_type": indicator_type,
                "count": len(rows), "cached": True,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "data": rows,
            }, ensure_ascii=False, indent=2, default=str)

    # --- AKShare fetch ---
    try:
        df = ak.stock_financial_analysis_indicator_em(
            symbol=ts_code, indicator=indicator_type
        )
    except Exception as e:
        return f"Error: AKShare fetch failed for '{ts_code}': {e}"

    if df is None or df.empty:
        return f"Error: No financial indicator data for '{ts_code}'."

    column_map = {
        "SECURITY_NAME_ABBR": "name", "REPORT_DATE_NAME": "report_period",
        "EPSJB": "eps", "BPS": "bps", "ROEJQ": "roe", "ZZCJLL": "roa",
        "XSMLL": "gross_margin", "XSJLL": "net_margin", "ZCFZL": "debt_ratio",
        "LD": "current_ratio", "SD": "quick_ratio",
        "TOTALOPERATEREVE": "revenue", "PARENTNETPROFIT": "net_profit",
        "TOTALOPERATEREVETZ": "revenue_yoy", "PARENTNETPROFITTZ": "net_profit_yoy",
        "MGJYXJJE": "ocf_per_share", "MGWFPLR": "retained_eps",
    }
    available = {k: v for k, v in column_map.items() if k in df.columns}
    df_out = df[list(available.keys())].rename(columns=available)
    if "report_period" in df_out.columns:
        df_out["report_period"] = df_out["report_period"].astype(str)

    all_rows = df_out.to_dict(orient="records")

    # Write to cache
    if cache:
        cache.set_indicator(cache_key, all_rows)
    else:
        store = _local_load("indicator_cache.json")
        store[cache_key] = {"_fetched_at": time.time(), "data": all_rows}
        _local_save("indicator_cache.json", store)

    result = {
        "ts_code": ts_code, "indicator_type": indicator_type,
        "count": len(all_rows[:limit]), "cached": False,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data": all_rows[:limit],
    }
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)


def get_peer_comparison(ts_code: str) -> str:
    """
    获取同行业个股估值对比（带两级缓存）。

    行业映射 TTL 7 天，同行业 PE/PB 数据 TTL 1 小时。
    首次调用会构建全量行业映射（约 60-120 秒），后续调用秒级返回。

    :param ts_code: 股票代码，Tushare格式，如 '000001.SZ'
    :return: JSON string with peer valuation data
    """
    try:
        import akshare as ak  # noqa: F401
    except ImportError:
        return "Error: akshare is not installed. Run: pip install akshare"

    import pandas as pd
    code = ts_code.split('.')[0]

    board_slug = _get_industry_slug(code)
    if not board_slug:
        return (
            f"Error: Could not find industry board for '{ts_code}'. "
            "If this is a newly listed stock, the industry map may need a rebuild — "
            f"delete '{_local_path('industry_map.json')}' to force one."
        )

    peers = _get_peer_data(board_slug)
    if not peers:
        return f"Error: No peer data available for board '{board_slug}'."

    df_out = pd.DataFrame(peers)
    df_out["is_target"] = df_out["code"].astype(str) == code

    avg_pe = df_out["pe_dynamic"].dropna().mean() if "pe_dynamic" in df_out.columns else None
    avg_pb = df_out["pb"].dropna().mean() if "pb" in df_out.columns else None

    result = {
        "ts_code": ts_code,
        "industry_slug": board_slug,
        "peer_count": len(df_out),
        "industry_avg_pe": round(float(avg_pe), 2) if avg_pe is not None else None,
        "industry_avg_pb": round(float(avg_pb), 2) if avg_pb is not None else None,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "peers": df_out.to_dict(orient="records"),
    }
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)


def get_stock_dividend_history(ts_code: str, limit: int = 10) -> str:
    """
    获取个股历年分红送转记录（巨潮资讯源，带缓存）。

    :param ts_code: 股票代码，Tushare格式，如 '000001.SZ'
    :param limit: 返回条数，默认10
    :return: JSON string with dividend history
    """
    try:
        import akshare as ak
    except ImportError:
        return "Error: akshare is not installed. Run: pip install akshare"

    code = ts_code.split('.')[0]
    limit = max(1, min(int(limit), 30))
    cache = get_cache()

    # --- Redis path ---
    if cache:
        rows = cache.get_dividend(ts_code, limit=limit)
        if rows is not None:
            return json.dumps({
                "ts_code": ts_code, "count": len(rows), "cached": True,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "data": rows,
            }, ensure_ascii=False, indent=2, default=str)

    # --- Local file fallback check ---
    if not cache:
        store = _local_load("dividend_cache.json")
        if _local_fresh(store, code, _LOCAL_TTL["dividend"]):
            rows = store[code]["data"][:limit]
            return json.dumps({
                "ts_code": ts_code, "count": len(rows), "cached": True,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "data": rows,
            }, ensure_ascii=False, indent=2, default=str)

    # --- AKShare fetch ---
    try:
        df = ak.stock_dividend_cninfo(symbol=code)
    except Exception as e:
        return f"Error: AKShare fetch failed for dividend data of '{ts_code}': {e}"

    if df is None or df.empty:
        return f"Error: No dividend data for '{ts_code}'."

    rename_map = {
        "实施方案公告日期": "announce_date", "分红类型": "dividend_type",
        "送股比例": "bonus_share_ratio", "转增比例": "conversion_ratio",
        "派息比例": "cash_dividend_ratio", "股权登记日": "record_date",
        "除权日": "ex_date", "派息日": "payment_date",
        "实施方案分红说明": "description", "报告时间": "report_period",
    }
    available = {k: v for k, v in rename_map.items() if k in df.columns}
    df = df[list(available.keys())].rename(columns=available)
    if "announce_date" in df.columns:
        df = df.sort_values("announce_date", ascending=False)

    all_rows = df.to_dict(orient="records")

    # Write to cache
    if cache:
        cache.set_dividend(ts_code, all_rows)
    else:
        store = _local_load("dividend_cache.json")
        store[code] = {"_fetched_at": time.time(), "data": all_rows}
        _local_save("dividend_cache.json", store)

    result = {
        "ts_code": ts_code, "count": len(all_rows[:limit]), "cached": False,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data": all_rows[:limit],
    }
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)


def get_event_calendar(symbol_type: str = "全部", date: str = "",
                       limit: int = 30) -> str:
    """
    获取A股公告事件日历（东方财富源，带缓存）。

    :param symbol_type: 公告类型，支持 '全部','财务报告','重大事项','融资公告',
                        '风险提示','资产重组','信息变更','持股变动'
    :param date: 日期，格式 YYYYMMDD，默认今天
    :param limit: 返回条数，默认30
    :return: JSON string with event list
    """
    try:
        import akshare as ak
    except ImportError:
        return "Error: akshare is not installed. Run: pip install akshare"

    valid_types = ["全部", "财务报告", "重大事项", "融资公告", "风险提示",
                   "资产重组", "信息变更", "持股变动"]
    if symbol_type not in valid_types:
        symbol_type = "全部"
    if not date:
        date = datetime.now().strftime("%Y%m%d")
    limit = max(1, min(int(limit), 100))

    cache = get_cache()
    local_key = f"{date}:{symbol_type}"

    # --- Redis path ---
    if cache:
        rows = cache.get_events(date, symbol_type)
        if rows is not None:
            return json.dumps({
                "source": "eastmoney(cached)", "symbol_type": symbol_type,
                "date": date, "count": len(rows[:limit]), "cached": True,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "data": rows[:limit],
            }, ensure_ascii=False, indent=2, default=str)

    # --- Local file fallback check ---
    if not cache:
        store = _local_load("events_cache.json")
        if _local_fresh(store, local_key, _LOCAL_TTL["events"]):
            rows = store[local_key]["data"][:limit]
            return json.dumps({
                "source": "eastmoney(cached)", "symbol_type": symbol_type,
                "date": date, "count": len(rows), "cached": True,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "data": rows,
            }, ensure_ascii=False, indent=2, default=str)

    # --- AKShare fetch ---
    try:
        df = ak.stock_notice_report(symbol=symbol_type, date=date)
    except Exception as e:
        return f"Error: AKShare fetch failed for event calendar: {e}"

    if df is None or df.empty:
        return f"Error: No announcements for type='{symbol_type}' on {date}."

    rename_map = {
        "代码": "code", "名称": "name", "公告标题": "title",
        "公告类型": "notice_type", "公告日期": "date", "网址": "url",
    }
    available = {k: v for k, v in rename_map.items() if k in df.columns}
    df = df[list(available.keys())].rename(columns=available).astype(str)
    all_rows = df.to_dict(orient="records")

    # Write to cache
    if cache:
        cache.set_events(date, symbol_type, all_rows)
    else:
        store = _local_load("events_cache.json")
        store[local_key] = {"_fetched_at": time.time(), "data": all_rows}
        _local_save("events_cache.json", store)

    result = {
        "source": "eastmoney", "symbol_type": symbol_type, "date": date,
        "count": len(all_rows[:limit]), "cached": False,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data": all_rows[:limit],
    }
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)


# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------

FUNDAMENTAL_TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "get_financial_indicators",
            "description": (
                "获取个股核心财务分析指标（东方财富源，AKShare stock_financial_analysis_indicator_em，带缓存）。"
                "返回ROE、ROA、毛利率、净利率、资产负债率、EPS、每股净资产、营收及净利润同比增速等。"
                "适用于分析个股的盈利能力、偿债能力、成长性，以及做财务健康诊断。"
                "支持按报告期（年报/半年报/季报）或按单季度查看。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ts_code": {
                        "type": "string",
                        "description": "股票代码，Tushare格式，如 '000001.SZ' 或 '600000.SH'",
                    },
                    "indicator_type": {
                        "type": "string",
                        "enum": ["按报告期", "按单季度"],
                        "description": "指标维度：'按报告期'（默认，累计值）或 '按单季度'（单季拆分）",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回最近几期数据，1–20，默认8",
                    },
                },
                "required": ["ts_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_peer_comparison",
            "description": (
                "获取个股所在行业板块的同行估值对比（东方财富行业板块成分股数据，带两级缓存）。"
                "自动识别目标股所属行业，返回同行业所有个股的最新价、涨跌幅、动态市盈率(PE)、市净率(PB)等。"
                "同时计算行业平均PE和PB，方便判断目标股估值在行业中的位置。"
                "适用于回答'估值高不高'、'和同行比怎么样'、'行业对比'等问题。"
                "行业映射缓存7天，PE/PB数据缓存1小时，首次调用后响应速度显著提升。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ts_code": {
                        "type": "string",
                        "description": "股票代码，Tushare格式，如 '000001.SZ' 或 '600000.SH'",
                    },
                },
                "required": ["ts_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_stock_dividend_history",
            "description": (
                "获取个股历年分红送转记录（巨潮资讯源，AKShare stock_dividend_cninfo，带缓存）。"
                "返回每次分红的送股比例、转增比例、每股派息金额、股权登记日、除权日等。"
                "适用于分析股息率、分红稳定性、高分红选股，以及回答'分红情况'、'股息率'等问题。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ts_code": {
                        "type": "string",
                        "description": "股票代码，Tushare格式，如 '000001.SZ' 或 '600519.SH'",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回最近几次分红记录，1–30，默认10",
                    },
                },
                "required": ["ts_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_event_calendar",
            "description": (
                "获取A股市场公告事件日历（东方财富源，AKShare stock_notice_report，带缓存）。"
                "返回指定日期的上市公司公告列表，包括公告标题、类型、公司代码等。"
                "支持按类型筛选：全部、财务报告、重大事项、融资公告、风险提示、资产重组、持股变动等。"
                "适用于了解某日有哪些重要公告、跟踪持仓股的事件动态。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol_type": {
                        "type": "string",
                        "enum": ["全部", "财务报告", "重大事项", "融资公告",
                                 "风险提示", "资产重组", "信息变更", "持股变动"],
                        "description": "公告类型筛选，默认'全部'",
                    },
                    "date": {
                        "type": "string",
                        "description": "日期，格式YYYYMMDD，如'20260415'，默认今天",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回条数，1–100，默认30",
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

def execute_fundamental_tool(tool_name: str, arguments: dict) -> str:
    """Dispatcher for fundamental analysis tools."""
    if tool_name == "get_financial_indicators":
        return get_financial_indicators(**arguments)
    elif tool_name == "get_peer_comparison":
        return get_peer_comparison(**arguments)
    elif tool_name == "get_stock_dividend_history":
        return get_stock_dividend_history(**arguments)
    elif tool_name == "get_event_calendar":
        return get_event_calendar(**arguments)
    else:
        return f"Error: Fundamental tool '{tool_name}' not recognized."
