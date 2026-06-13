import json
import logging

from app.utils.redis_lock import get_redis

logger = logging.getLogger(__name__)

CACHE_TTL = 3600
KEY_PREFIX = "suggest"


def _cache_key(user_id, occasion) -> str:
    return f"{KEY_PREFIX}:{user_id}:{occasion}"


async def push_suggestions(user_id, occasion, suggestions: list[dict]) -> None:
    try:
        redis = await get_redis()
        key = _cache_key(user_id, occasion)
        pipe = redis.pipeline()
        for s in suggestions:
            pipe.rpush(key, json.dumps(s))
        pipe.expire(key, CACHE_TTL)
        await pipe.execute()
    except Exception:
        logger.warning("Failed to push suggestions to cache", exc_info=True)


async def pop_suggestion(user_id, occasion) -> dict | None:
    try:
        redis = await get_redis()
        key = _cache_key(user_id, occasion)
        raw = await redis.lpop(key)
        if raw is None:
            return None
        return json.loads(raw)
    except Exception:
        logger.warning("Failed to pop suggestion from cache", exc_info=True)
        return None


async def clear_suggestions(user_id, occasion) -> None:
    try:
        redis = await get_redis()
        key = _cache_key(user_id, occasion)
        await redis.delete(key)
    except Exception:
        logger.warning("Failed to clear suggestion cache", exc_info=True)


async def has_cached(user_id, occasion) -> bool:
    try:
        redis = await get_redis()
        key = _cache_key(user_id, occasion)
        length = await redis.llen(key)
        return length > 0
    except Exception:
        logger.warning("Failed to check suggestion cache", exc_info=True)
        return False
