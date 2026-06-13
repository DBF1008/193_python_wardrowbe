import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.services.suggestion_cache import (
    _cache_key,
    clear_suggestions,
    has_cached,
    pop_suggestion,
    push_suggestions,
)


@pytest.fixture
def user_id():
    return uuid4()


@pytest.fixture
def mock_redis():
    redis = MagicMock()
    redis.lpop = AsyncMock(return_value=None)
    redis.llen = AsyncMock(return_value=0)
    redis.delete = AsyncMock()

    pipe = MagicMock()
    pipe.execute = AsyncMock()
    redis.pipeline.return_value = pipe
    return redis, pipe


class TestSuggestionCache:
    @pytest.mark.asyncio
    async def test_push_and_pop_fifo(self, user_id, mock_redis):
        redis, pipe = mock_redis

        with patch("app.services.suggestion_cache.get_redis", AsyncMock(return_value=redis)):
            suggestions = [
                {"items": [1, 2], "headline": "First"},
                {"items": [3, 4], "headline": "Second"},
            ]
            await push_suggestions(user_id, "casual", suggestions)

            assert pipe.rpush.call_count == 2
            pipe.expire.assert_called_once()
            pipe.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_pop_empty_returns_none(self, user_id, mock_redis):
        redis, _ = mock_redis
        redis.lpop.return_value = None

        with patch("app.services.suggestion_cache.get_redis", AsyncMock(return_value=redis)):
            result = await pop_suggestion(user_id, "casual")
            assert result is None

    @pytest.mark.asyncio
    async def test_pop_returns_dict(self, user_id, mock_redis):
        redis, _ = mock_redis
        redis.lpop.return_value = json.dumps({"items": [1, 2], "headline": "Test"})

        with patch("app.services.suggestion_cache.get_redis", AsyncMock(return_value=redis)):
            result = await pop_suggestion(user_id, "casual")
            assert result == {"items": [1, 2], "headline": "Test"}

    @pytest.mark.asyncio
    async def test_clear_removes_all(self, user_id, mock_redis):
        redis, _ = mock_redis

        with patch("app.services.suggestion_cache.get_redis", AsyncMock(return_value=redis)):
            await clear_suggestions(user_id, "casual")
            key = _cache_key(user_id, "casual")
            redis.delete.assert_called_once_with(key)

    @pytest.mark.asyncio
    async def test_has_cached_true(self, user_id, mock_redis):
        redis, _ = mock_redis
        redis.llen.return_value = 2

        with patch("app.services.suggestion_cache.get_redis", AsyncMock(return_value=redis)):
            assert await has_cached(user_id, "casual") is True

    @pytest.mark.asyncio
    async def test_redis_error_degrades_gracefully(self, user_id):
        async def failing_redis():
            raise ConnectionError("Redis down")

        with patch("app.services.suggestion_cache.get_redis", side_effect=failing_redis):
            result = await pop_suggestion(user_id, "casual")
            assert result is None

            cached = await has_cached(user_id, "casual")
            assert cached is False

            await push_suggestions(user_id, "casual", [{"items": [1]}])
            await clear_suggestions(user_id, "casual")
