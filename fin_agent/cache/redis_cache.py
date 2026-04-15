"""
Redis cache layer for fin-agent financial data.

Key schema (per docs/redis-cache-design.md):
  fin:industry:stock:{code}     STRING   board_name (English)   TTL=7d
  fin:industry:map:built_at     STRING   unix timestamp         TTL=7d  (sentinel)
  fin:peer:board:{board}        HASH     {code: json_str}       TTL=1h
  fin:indicator:{ts_code}       HASH     {period: json_str}     TTL=24h
  fin:dividend:{code}           LIST     [json_str, ...]        TTL=7d
  fin:events:{date}:{type}      ZSET     score=seq, json_str    TTL=1d

All keys use the `fin:` namespace prefix.
Chinese board/type names are normalised to ASCII slugs before use as key segments.
"""

import json
import logging
import time
from typing import Optional

from fin_agent.config import Config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# TTLs (seconds)
# ---------------------------------------------------------------------------
_TTL_INDUSTRY = 7 * 24 * 3600   # 7 days
_TTL_PEER     = 3600             # 1 hour
_TTL_INDICATOR = 86400           # 24 hours
_TTL_DIVIDEND  = 7 * 24 * 3600  # 7 days
_TTL_EVENTS    = 86400           # 1 day

# Sentinel key that marks whether the full industry map has been built
_INDUSTRY_BUILT_KEY = "fin:industry:map:built_at"

# ---------------------------------------------------------------------------
# Board / event-type name → ASCII slug mapping
# (stored values in the industry STRING keys are English slugs, keeping keys
#  pure-ASCII and consistent with the design doc)
# ---------------------------------------------------------------------------
_BOARD_SLUG: dict[str, str] = {}   # populated lazily: cn_name → slug
_SLUG_BOARD: dict[str, str] = {}   # reverse: slug → cn_name


def _slug(name: str) -> str:
    """Return cached ASCII slug for a board / type name."""
    if name not in _BOARD_SLUG:
        import re, unicodedata, hashlib
        # Normalise Unicode, strip non-ASCII, collapse whitespace
        ascii_try = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", ascii_try).strip("_").lower()
        if not slug:
            # Fallback: short hex of the original name
            slug = "board_" + hashlib.md5(name.encode()).hexdigest()[:8]
        _BOARD_SLUG[name] = slug
        _SLUG_BOARD[slug] = name
    return _BOARD_SLUG[name]


def slug_to_board(slug: str) -> str:
    """Reverse lookup: ASCII slug → original Chinese board name."""
    return _SLUG_BOARD.get(slug, slug)


# ---------------------------------------------------------------------------
# RedisCache — singleton
# ---------------------------------------------------------------------------

class RedisCache:
    """
    Thin wrapper around redis.Redis that implements the fin-agent cache schema.

    Usage:
        cache = RedisCache.instance()
        if cache.available:
            board = cache.get_industry("000001")
    """

    _instance: Optional["RedisCache"] = None

    @classmethod
    def instance(cls) -> "RedisCache":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        self._client = None
        self._available = False
        self._connect()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _connect(self) -> None:
        try:
            import redis
        except ImportError:
            logger.warning("redis package not installed. Run: pip install redis>=5.0.0")
            return

        try:
            client = redis.Redis(
                host=Config.REDIS_HOST,
                port=Config.REDIS_PORT,
                db=Config.REDIS_DB,
                password=Config.REDIS_PASSWORD,
                socket_connect_timeout=2,
                socket_timeout=3,
                decode_responses=True,   # all keys/values are str
            )
            client.ping()
            self._client = client
            self._available = True
            logger.debug(
                "Redis connected: %s:%s db=%s",
                Config.REDIS_HOST, Config.REDIS_PORT, Config.REDIS_DB,
            )
        except Exception as exc:
            logger.warning("Redis unavailable (%s). Falling back to local cache.", exc)
            self._available = False

    @property
    def available(self) -> bool:
        return self._available

    # ------------------------------------------------------------------
    # 1. Industry mapping  (STRING per stock, TTL=7d)
    # ------------------------------------------------------------------

    def is_industry_map_fresh(self) -> bool:
        """True when the full industry → stock mapping has been built recently."""
        return bool(self._client.exists(_INDUSTRY_BUILT_KEY))

    def mark_industry_map_built(self) -> None:
        self._client.set(_INDUSTRY_BUILT_KEY, int(time.time()), ex=_TTL_INDUSTRY)

    def get_industry(self, code: str) -> Optional[str]:
        """Return English board slug for a stock code, or None on cache miss."""
        return self._client.get(f"fin:industry:stock:{code}")

    def set_industries_bulk(self, mapping: dict) -> None:
        """
        Persist {code: board_name} mapping.  board_name is auto-slugified.
        Uses pipeline for efficiency (~5000 stocks in one round-trip batch).
        """
        pipe = self._client.pipeline(transaction=False)
        for code, board in mapping.items():
            pipe.set(f"fin:industry:stock:{code}", _slug(board), ex=_TTL_INDUSTRY)
        pipe.execute()

    # ------------------------------------------------------------------
    # 2. Peer board data  (HASH per board, field=code, value=json, TTL=1h)
    # ------------------------------------------------------------------

    def get_peer_board(self, board: str) -> Optional[list]:
        """
        Return list of peer dicts for the given board, or None on cache miss.
        `board` may be Chinese name or ASCII slug.
        """
        key = f"fin:peer:board:{_slug(board)}"
        raw = self._client.hgetall(key)
        if not raw:
            return None
        try:
            return [json.loads(v) for v in raw.values()]
        except (json.JSONDecodeError, ValueError):
            return None

    def set_peer_board(self, board: str, rows: list) -> None:
        """Persist peer comparison rows for a board (overwrites stale data)."""
        key = f"fin:peer:board:{_slug(board)}"
        pipe = self._client.pipeline(transaction=False)
        pipe.delete(key)
        for row in rows:
            code = str(row.get("code", ""))
            pipe.hset(key, code, json.dumps(row, ensure_ascii=False, default=str))
        pipe.expire(key, _TTL_PEER)
        pipe.execute()

    # ------------------------------------------------------------------
    # 3. Financial indicators  (HASH per stock, field=period, value=json, TTL=24h)
    # ------------------------------------------------------------------

    def get_indicator(self, ts_code: str) -> Optional[list]:
        """Return list of indicator dicts ordered by period, or None on cache miss."""
        key = f"fin:indicator:{ts_code}"
        raw = self._client.hgetall(key)
        if not raw:
            return None
        try:
            return [json.loads(v) for v in raw.values()]
        except (json.JSONDecodeError, ValueError):
            return None

    def set_indicator(self, ts_code: str, rows: list) -> None:
        """
        Persist financial indicator rows.
        Each row must have a 'report_period' field used as the Hash field name.
        """
        key = f"fin:indicator:{ts_code}"
        pipe = self._client.pipeline(transaction=False)
        pipe.delete(key)
        for row in rows:
            period = str(row.get("report_period", "unknown"))
            pipe.hset(key, period, json.dumps(row, ensure_ascii=False, default=str))
        pipe.expire(key, _TTL_INDICATOR)
        pipe.execute()

    # ------------------------------------------------------------------
    # 4. Dividend history  (LIST per stock, prepend newest, TTL=7d)
    # ------------------------------------------------------------------

    def get_dividend(self, ts_code: str, limit: int = 30) -> Optional[list]:
        """Return up to `limit` dividend records (newest first), or None on cache miss."""
        code = ts_code.split(".")[0]
        key = f"fin:dividend:{code}"
        if not self._client.exists(key):
            return None
        raw = self._client.lrange(key, 0, limit - 1)
        try:
            return [json.loads(v) for v in raw]
        except (json.JSONDecodeError, ValueError):
            return None

    def set_dividend(self, ts_code: str, records: list) -> None:
        """Persist dividend records (list, newest first)."""
        code = ts_code.split(".")[0]
        key = f"fin:dividend:{code}"
        pipe = self._client.pipeline(transaction=False)
        pipe.delete(key)
        for rec in records:
            pipe.rpush(key, json.dumps(rec, ensure_ascii=False, default=str))
        pipe.expire(key, _TTL_DIVIDEND)
        pipe.execute()

    # ------------------------------------------------------------------
    # 5. Event calendar  (ZSET per date+type, score=seq, TTL=1d)
    # ------------------------------------------------------------------

    def get_events(self, date: str, symbol_type: str) -> Optional[list]:
        """Return event list for the given date and type, or None on cache miss."""
        key = f"fin:events:{date}:{_slug(symbol_type)}"
        if not self._client.exists(key):
            return None
        raw = self._client.zrange(key, 0, -1)
        try:
            return [json.loads(v) for v in raw]
        except (json.JSONDecodeError, ValueError):
            return None

    def set_events(self, date: str, symbol_type: str, records: list) -> None:
        """Persist event calendar records as a ZSET (score = insertion order)."""
        key = f"fin:events:{date}:{_slug(symbol_type)}"
        pipe = self._client.pipeline(transaction=False)
        pipe.delete(key)
        for seq, rec in enumerate(records, start=1):
            pipe.zadd(key, {json.dumps(rec, ensure_ascii=False, default=str): seq})
        pipe.expire(key, _TTL_EVENTS)
        pipe.execute()


# ---------------------------------------------------------------------------
# Module-level helper
# ---------------------------------------------------------------------------

def get_cache() -> Optional[RedisCache]:
    """
    Return the RedisCache singleton if Redis is enabled in config and reachable.
    Returns None otherwise — callers should fall back to local file cache.
    """
    if not Config.REDIS_ENABLED:
        return None
    cache = RedisCache.instance()
    return cache if cache.available else None
