import json
import logging
from functools import lru_cache
from typing import cast

from pydantic import BaseModel
from redis import Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry

from app.config import get_settings

logger = logging.getLogger(__name__)
TTL = 60
VERSION_KEY = "catalogue:version"


@lru_cache(maxsize=1)
def _warn_once() -> None:
    logger.warning("Catalogue cache unavailable; using the database")


@lru_cache(maxsize=1)
def _client(url: str) -> Redis:
    return Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=0.2,
        socket_timeout=0.2,
        retry=Retry(NoBackoff(), 0),
    )


def _redis() -> Redis | None:
    url = get_settings().redis_url
    if not url:
        _warn_once()
        return None
    return _client(url)


def key(*parts: str | int | None) -> str | None:
    try:
        client = _redis()
        if client is not None:
            # The client uses decode_responses=True, so GET returns text.
            version = cast(str | None, client.get(VERSION_KEY)) or "0"
            return f"catalogue:{version}:{json.dumps(parts, ensure_ascii=True)}"
    except (RedisError, ValueError):
        _warn_once()
    return None


def get_json[T: BaseModel](cache_key: str | None, model: type[T]) -> T | None:
    if cache_key is None:
        return None
    try:
        client = _redis()
        if client is not None:
            value = client.get(cache_key)
            if value is not None:
                return model.model_validate_json(value)
    except (RedisError, ValueError):
        _warn_once()
    return None


def set_json(cache_key: str | None, value: BaseModel) -> None:
    if cache_key is None:
        return
    try:
        client = _redis()
        if client is not None:
            client.set(cache_key, value.model_dump_json(), ex=TTL)
    except (RedisError, ValueError):
        _warn_once()


def invalidate() -> None:
    try:
        client = _redis()
        if client is not None:
            client.incr(VERSION_KEY)
    except (RedisError, ValueError):
        _warn_once()
