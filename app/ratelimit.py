import logging
import os
from collections import OrderedDict
from collections.abc import Callable
from functools import lru_cache
from math import ceil
from threading import Lock
from time import monotonic

from fastapi import Request
from redis import Redis
from redis.exceptions import RedisError

from app.config import get_settings
from app.errors import AppError

logger = logging.getLogger(__name__)

RATE_LIMIT_ENABLED = os.environ.get("RATE_LIMIT_ENABLED", "true").lower() not in {
    "0",
    "false",
    "no",
}
# FIFO eviction resets old limits at capacity; use Redis for shared limits.
MAX_IDENTITIES = 10_000
_windows: OrderedDict[tuple[str, str], tuple[float, int]] = OrderedDict()
_next_prune = 0.0
_lock = Lock()


@lru_cache
def _redis_client(url: str) -> Redis:
    return Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)


def reset_rate_limits() -> None:
    global _next_prune
    with _lock:
        _windows.clear()
        _next_prune = 0.0


def rate_limit(key: str, limit: int, window_seconds: int) -> Callable[[Request], None]:
    if limit <= 0 or window_seconds <= 0:
        raise ValueError("limit and window_seconds must be positive")

    def check(request: Request) -> None:
        global _next_prune
        if not RATE_LIMIT_ENABLED:
            return
        identity = (key, request.client.host if request.client else "unknown")
        redis_url = get_settings().redis_url
        if redis_url:
            try:
                with _redis_client(redis_url).pipeline(transaction=True) as pipe:
                    redis_key = f"eve:ratelimit:{key}:{identity[1]}"
                    pipe.incr(redis_key)
                    pipe.expire(redis_key, window_seconds, nx=True)
                    pipe.ttl(redis_key)
                    count, _, retry_after = pipe.execute()
            except (RedisError, ValueError):
                logger.warning("Redis rate limit unavailable; using in-memory limits")
            else:
                if count > limit:
                    raise AppError(
                        429,
                        "RATE_LIMITED",
                        "Too many requests",
                        details={"retry_after": max(1, retry_after)},
                    )
                return
        with _lock:
            now = monotonic()
            if now >= _next_prune:
                for expired_identity in [
                    identity
                    for identity, (expires, _) in _windows.items()
                    if now >= expires
                ]:
                    del _windows[expired_identity]
                _next_prune = now + window_seconds
            expires, count = _windows.get(identity, (now + window_seconds, 0))
            if now >= expires:
                del _windows[identity]
                expires, count = now + window_seconds, 0
            if count >= limit:
                raise AppError(
                    429,
                    "RATE_LIMITED",
                    "Too many requests",
                    details={"retry_after": ceil(expires - now)},
                )
            if identity not in _windows and len(_windows) >= MAX_IDENTITIES:
                _windows.popitem(last=False)
            _windows[identity] = (expires, count + 1)

    return check
