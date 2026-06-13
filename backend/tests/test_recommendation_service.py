from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.models.item import ClothingItem, ItemStatus
from app.models.outfit import Outfit, OutfitItem, OutfitSource, OutfitStatus
from app.models.user import User
from app.services.ai_service import TextGenerationResult
from app.services.item_scorer import ScoredItem
from app.services.recommendation_service import (
    InvalidMandatoryItemError,
    RecommendationService,
    get_time_of_day,
)
from app.services.weather_service import WeatherData


def _make_user(timezone: str = "UTC") -> User:
    uid = uuid4()
    return User(
        id=uid,
        external_id=f"test-{uid}",
        email=f"test-{uid}@example.com",
        display_name="Test",
        timezone=timezone,
        is_active=True,
    )


class TestGetTimeOfDay:
    @pytest.mark.parametrize(
        "hour,expected",
        [
            (6, "morning"),
            (9, "morning"),
            (11, "morning"),
            (12, "afternoon"),
            (14, "afternoon"),
            (16, "afternoon"),
            (17, "evening"),
            (19, "evening"),
            (20, "evening"),
            (21, "night"),
            (23, "night"),
            (0, "night"),
            (3, "night"),
            (5, "night"),
        ],
    )
    def test_time_buckets(self, hour, expected):
        user = _make_user("UTC")
        mock_dt = datetime(2026, 3, 8, hour, 30, 0, tzinfo=UTC)
        with patch("app.services.recommendation_service.datetime") as mock_datetime:
            mock_datetime.now.return_value = mock_dt
            mock_datetime.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = get_time_of_day(user)
        assert result == expected

    def test_respects_user_timezone(self):
        user = _make_user("Asia/Kolkata")
        mock_dt = datetime(2026, 3, 8, 13, 30, 0, tzinfo=UTC)
        with patch("app.services.recommendation_service.datetime") as mock_datetime:
            mock_datetime.now.return_value = mock_dt
            mock_datetime.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = get_time_of_day(user)
        assert result == "evening"

    def test_invalid_timezone_falls_back_to_utc(self):
        user = _make_user("Invalid/Timezone")
        mock_dt = datetime(2026, 3, 8, 9, 0, 0, tzinfo=UTC)
        with patch("app.services.recommendation_service.datetime") as mock_datetime:
            mock_datetime.now.return_value = mock_dt
            mock_datetime.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = get_time_of_day(user)
        assert result == "morning"

    def test_none_timezone_falls_back_to_utc(self):
        user = _make_user()
        user.timezone = None
        mock_dt = datetime(2026, 3, 8, 22, 0, 0, tzinfo=UTC)
        with patch("app.services.recommendation_service.datetime") as mock_datetime:
            mock_datetime.now.return_value = mock_dt
            mock_datetime.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = get_time_of_day(user)
        assert result == "night"


class TestPromptTemplate:
    def test_prompt_contains_fashion_principles(self):
        from app.services.recommendation_service import RECOMMENDATION_PROMPT

        prompt = RECOMMENDATION_PROMPT
        assert "Color coordination" in prompt
        assert "Monochrome" in prompt
        assert "Neutral base" in prompt
        assert "Analogous" in prompt
        assert "Texture and fabric" in prompt
        assert "Proportion and silhouette" in prompt
        assert "Time of day" in prompt
        assert "Full day" in prompt
        assert "{time_of_day}" in prompt

    def test_prompt_format_accepts_time_of_day(self):
        from app.services.recommendation_service import RECOMMENDATION_PROMPT

        formatted = RECOMMENDATION_PROMPT.format(
            occasion="casual",
            time_of_day="evening",
            temperature=22,
            feels_like=20,
            condition="clear",
            precipitation_chance=10,
            preferences_text="",
            items_text="[1] shirt | blue | cotton",
            mandatory_items_section="",
        )
        assert "evening" in formatted
        assert "casual" in formatted
        assert "22" in formatted

    def test_prompt_format_all_time_of_day_values(self):
        from app.services.recommendation_service import RECOMMENDATION_PROMPT

        for tod in ["morning", "afternoon", "evening", "night", "full day"]:
            formatted = RECOMMENDATION_PROMPT.format(
                occasion="work",
                time_of_day=tod,
                temperature=15,
                feels_like=13,
                condition="cloudy",
                precipitation_chance=30,
                preferences_text="",
                items_text="[1] shirt",
                mandatory_items_section="",
            )
            assert tod in formatted


class TestSuggestRequestTimeOfDay:
    @pytest.mark.asyncio
    async def test_suggest_accepts_time_of_day(self, client, test_user, auth_headers, db_session):
        from app.models.item import ClothingItem, ItemStatus

        for item_type in ["shirt", "pants", "sneakers"]:
            item = ClothingItem(
                user_id=test_user.id,
                type=item_type,
                image_path=f"test/{uuid4()}.jpg",
                status=ItemStatus.ready,
                primary_color="blue",
            )
            db_session.add(item)
        await db_session.commit()

        response = await client.post(
            "/api/v1/outfits/suggest",
            json={
                "occasion": "casual",
                "time_of_day": "evening",
                "weather_override": {
                    "temperature": 20,
                    "condition": "clear",
                },
            },
            headers=auth_headers,
        )
        assert response.status_code != 422

    @pytest.mark.asyncio
    async def test_suggest_rejects_invalid_time_of_day(self, client, test_user, auth_headers):
        response = await client.post(
            "/api/v1/outfits/suggest",
            json={
                "occasion": "casual",
                "time_of_day": "brunch",
            },
            headers=auth_headers,
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_suggest_accepts_full_day(self, client, test_user, auth_headers):
        response = await client.post(
            "/api/v1/outfits/suggest",
            json={
                "occasion": "casual",
                "time_of_day": "full day",
                "weather_override": {"temperature": 20, "condition": "clear"},
            },
            headers=auth_headers,
        )
        assert response.status_code != 422

    @pytest.mark.asyncio
    async def test_suggest_allows_null_time_of_day(self, client, test_user, auth_headers):
        response = await client.post(
            "/api/v1/outfits/suggest",
            json={
                "occasion": "casual",
                "time_of_day": None,
                "weather_override": {
                    "temperature": 20,
                    "condition": "clear",
                },
            },
            headers=auth_headers,
        )
        assert response.status_code != 422


class TestSuggestEndpointRuntime:
    @pytest.mark.asyncio
    async def test_suggest_reaches_ready_item_count(
        self, client, test_user, auth_headers, db_session
    ):
        item = ClothingItem(
            user_id=test_user.id,
            type="shirt",
            image_path=f"test/{uuid4()}.jpg",
            status=ItemStatus.ready,
            primary_color="blue",
        )
        outfit = Outfit(
            user_id=test_user.id,
            occasion="casual",
            status=OutfitStatus.pending,
            source=OutfitSource.on_demand,
        )
        outfit.feedback = None
        outfit.family_ratings = []
        outfit.items = [OutfitItem(item=item, position=0, layer_type=None)]

        db_session.add_all([item, outfit])
        await db_session.commit()

        with patch(
            "app.api.outfits.RecommendationService.generate_recommendation",
            new_callable=AsyncMock,
            return_value=outfit,
        ):
            response = await client.post(
                "/api/v1/outfits/suggest",
                json={
                    "occasion": "casual",
                    "weather_override": {
                        "temperature": 20,
                        "condition": "clear",
                    },
                },
                headers=auth_headers,
            )

        assert response.status_code == 200
        data = response.json()
        assert data["is_starter_suggestion"] is True


def _make_item(**kwargs) -> ClothingItem:
    defaults = {
        "id": uuid4(),
        "user_id": uuid4(),
        "type": "shirt",
        "image_path": "test.jpg",
        "primary_color": "blue",
        "colors": ["blue"],
        "formality": "casual",
        "season": [],
        "style": [],
        "last_worn_at": None,
    }
    defaults.update(kwargs)
    return ClothingItem(**defaults)


class TestMultiOutfitParse:
    def test_three_outfits(self):
        service = RecommendationService.__new__(RecommendationService)
        content = '{"outfits": [{"items": [1, 2]}, {"items": [3, 4]}, {"items": [5, 6]}]}'
        result = service._parse_multi_outfit_response(content)
        assert len(result) == 3
        assert result[0]["items"] == [1, 2]

    def test_single_fallback(self):
        service = RecommendationService.__new__(RecommendationService)
        content = '{"items": [1, 2], "headline": "Test"}'
        result = service._parse_multi_outfit_response(content)
        assert len(result) == 1
        assert result[0]["items"] == [1, 2]

    def test_array_format(self):
        service = RecommendationService.__new__(RecommendationService)
        content = '[{"items": [1, 2]}, {"items": [3, 4]}]'
        result = service._parse_multi_outfit_response(content)
        assert len(result) >= 1
        assert "items" in result[0]


class TestFormatItemsEnriched:
    def test_recency_annotation(self):
        service = RecommendationService.__new__(RecommendationService)
        item1 = _make_item(last_worn_at=date(2026, 3, 5))
        item2 = _make_item(last_worn_at=None)
        scored = [ScoredItem(item=item1), ScoredItem(item=item2)]
        today = date(2026, 3, 8)

        text, _ = service._format_items_for_prompt(scored, {}, today)
        assert "worn 3 days ago" in text
        assert "never worn" in text

    def test_pair_annotation(self):
        service = RecommendationService.__new__(RecommendationService)
        item1 = _make_item()
        item2 = _make_item()
        scored = [ScoredItem(item=item1), ScoredItem(item=item2)]
        pairs = {item1.id: [item2.id]}
        today = date(2026, 3, 8)

        text, _ = service._format_items_for_prompt(scored, pairs, today)
        assert "pairs well with:" in text


class TestFormatPrefsOccasion:
    def test_occasion_insights(self):
        service = RecommendationService.__new__(RecommendationService)
        learned = {
            "occasion_insights": {
                "work": {"preferred_colors": ["blue", "gray"], "success_rate": 0.3}
            }
        }
        text = service._format_preferences_for_prompt(None, learned, None, None, occasion="work")
        assert "For work, user prefers: blue, gray" in text
        assert "Low success rate" in text


class TestPromptPreRanking:
    def test_pre_ranking_hint_present(self):
        from app.services.recommendation_service import RECOMMENDATION_PROMPT

        assert "pre-ranked" in RECOMMENDATION_PROMPT


def _other_user() -> User:
    uid = uuid4()
    return User(
        id=uid,
        external_id=f"other-{uid}",
        email=f"other-{uid}@example.com",
        display_name="Other",
        timezone="UTC",
        is_active=True,
    )


def _ready_item(user_id, item_type: str = "shirt", **kwargs) -> ClothingItem:
    defaults = {
        "id": uuid4(),
        "user_id": user_id,
        "type": item_type,
        "image_path": f"test/{uuid4()}.jpg",
        "status": ItemStatus.ready,
        "primary_color": "blue",
    }
    defaults.update(kwargs)
    return ClothingItem(**defaults)


def _weather_data() -> WeatherData:
    return WeatherData(
        temperature=20.0,
        feels_like=20.0,
        humidity=50,
        precipitation_chance=0,
        precipitation_mm=0,
        wind_speed=0,
        condition="clear",
        condition_code=0,
        is_day=True,
        uv_index=0,
        timestamp=datetime(2026, 6, 13, 12, 0, 0),
    )


class TestMandatoryItemValidation:
    @pytest.mark.asyncio
    async def test_accepts_valid_mandatory(self, db_session, test_user):
        shirt = _ready_item(test_user.id, "shirt")
        db_session.add(shirt)
        await db_session.commit()

        service = RecommendationService(db_session)
        result = await service._validate_mandatory_items(test_user, [shirt.id])

        assert [i.id for i in result] == [shirt.id]

    @pytest.mark.asyncio
    async def test_overrides_needs_wash_soft_filter(self, db_session, test_user):
        # needs_wash is a soft filter for candidates, but an explicit mandatory
        # request must still be honoured.
        item = _ready_item(test_user.id, "shirt", needs_wash=True)
        db_session.add(item)
        await db_session.commit()

        service = RecommendationService(db_session)
        result = await service._validate_mandatory_items(test_user, [item.id])

        assert item.id in {i.id for i in result}

    @pytest.mark.asyncio
    async def test_rejects_unowned_item(self, db_session, test_user):
        other = _other_user()
        db_session.add(other)
        await db_session.commit()
        foreign = _ready_item(other.id, "shirt")
        db_session.add(foreign)
        await db_session.commit()

        service = RecommendationService(db_session)
        with pytest.raises(InvalidMandatoryItemError) as exc_info:
            await service._validate_mandatory_items(test_user, [foreign.id])

        assert foreign.id in exc_info.value.item_ids

    @pytest.mark.asyncio
    async def test_rejects_not_ready_item(self, db_session, test_user):
        item = _ready_item(test_user.id, "shirt", status=ItemStatus.processing)
        db_session.add(item)
        await db_session.commit()

        service = RecommendationService(db_session)
        with pytest.raises(InvalidMandatoryItemError) as exc_info:
            await service._validate_mandatory_items(test_user, [item.id])

        assert item.id in exc_info.value.item_ids

    @pytest.mark.asyncio
    async def test_rejects_archived_item(self, db_session, test_user):
        item = _ready_item(test_user.id, "shirt", is_archived=True)
        db_session.add(item)
        await db_session.commit()

        service = RecommendationService(db_session)
        with pytest.raises(InvalidMandatoryItemError) as exc_info:
            await service._validate_mandatory_items(test_user, [item.id])

        assert item.id in exc_info.value.item_ids

    @pytest.mark.asyncio
    async def test_rejects_nonexistent_item(self, db_session, test_user):
        ghost = uuid4()
        service = RecommendationService(db_session)
        with pytest.raises(InvalidMandatoryItemError) as exc_info:
            await service._validate_mandatory_items(test_user, [ghost])

        assert ghost in exc_info.value.item_ids

    @pytest.mark.asyncio
    async def test_reports_only_invalid_ids(self, db_session, test_user):
        good = _ready_item(test_user.id, "shirt")
        archived = _ready_item(test_user.id, "pants", is_archived=True)
        db_session.add_all([good, archived])
        await db_session.commit()

        service = RecommendationService(db_session)
        with pytest.raises(InvalidMandatoryItemError) as exc_info:
            await service._validate_mandatory_items(test_user, [good.id, archived.id])

        assert archived.id in exc_info.value.item_ids
        assert good.id not in exc_info.value.item_ids


class TestMaterializeMandatory:
    @pytest.mark.asyncio
    async def test_injects_mandatory_omitted_by_ai(self, db_session, test_user):
        shirt = _ready_item(test_user.id, "shirt")
        pants = _ready_item(test_user.id, "pants")
        db_session.add_all([shirt, pants])
        await db_session.commit()

        service = RecommendationService(db_session)
        number_map = {1: shirt.id, 2: pants.id}
        # AI selects only pants (2), omitting the mandatory shirt (1).
        outfit = await service._materialize_outfit(
            {"items": [2], "headline": "Test"},
            test_user,
            _weather_data(),
            "casual",
            OutfitSource.on_demand,
            number_map,
            mandatory_item_ids={shirt.id},
        )

        item_ids = {oi.item_id for oi in outfit.items}
        assert shirt.id in item_ids
        assert pants.id in item_ids

    @pytest.mark.asyncio
    async def test_mandatory_wins_body_slot_conflict(self, db_session, test_user):
        dress = _ready_item(test_user.id, "dress")
        shirt = _ready_item(test_user.id, "shirt")
        shoes = _ready_item(test_user.id, "sneakers")
        db_session.add_all([dress, shirt, shoes])
        await db_session.commit()

        service = RecommendationService(db_session)
        number_map = {1: dress.id, 2: shirt.id, 3: shoes.id}
        # AI picks a dress (full_body) alongside the mandatory shirt; dedup must
        # drop the dress, not the mandatory top.
        outfit = await service._materialize_outfit(
            {"items": [1, 2, 3], "headline": "Test"},
            test_user,
            _weather_data(),
            "casual",
            OutfitSource.on_demand,
            number_map,
            mandatory_item_ids={shirt.id},
        )

        item_ids = {oi.item_id for oi in outfit.items}
        assert shirt.id in item_ids
        assert dress.id not in item_ids
        assert shoes.id in item_ids


class TestSuggestMandatoryEndToEnd:
    @pytest.mark.asyncio
    async def test_invalid_include_items_returns_422(self, client, test_user, auth_headers):
        response = await client.post(
            "/api/v1/outfits/suggest",
            json={
                "occasion": "casual",
                "weather_override": {"temperature": 20, "condition": "clear"},
                "include_items": [str(uuid4())],
            },
            headers=auth_headers,
        )

        assert response.status_code == 422
        detail = response.json()["detail"]
        assert detail["error_code"] == "INVALID_INCLUDE_ITEMS"

    @pytest.mark.asyncio
    async def test_mandatory_item_present_in_result(
        self, client, test_user, auth_headers, db_session
    ):
        shirt = _ready_item(test_user.id, "shirt")
        pants = _ready_item(test_user.id, "pants")
        shoes = _ready_item(test_user.id, "sneakers")
        db_session.add_all([shirt, pants, shoes])
        await db_session.commit()

        # AI deliberately omits the mandatory item (sorted to number [1]);
        # enforcement must still place it in the final outfit.
        ai_result = TextGenerationResult(
            content='{"outfits": [{"items": [2, 3], "headline": "X"}]}',
            model="test-model",
            endpoint="test-endpoint",
        )
        with patch("app.services.recommendation_service.AIService") as mock_ai_cls:
            mock_ai_cls.return_value.generate_text = AsyncMock(return_value=ai_result)
            response = await client.post(
                "/api/v1/outfits/suggest",
                json={
                    "occasion": "casual",
                    "weather_override": {"temperature": 20, "condition": "clear"},
                    "include_items": [str(shirt.id)],
                },
                headers=auth_headers,
            )

        assert response.status_code == 200, response.text
        item_ids = {item["id"] for item in response.json()["items"]}
        assert str(shirt.id) in item_ids
