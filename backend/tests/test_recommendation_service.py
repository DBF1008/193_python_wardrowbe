import json
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.models.item import ClothingItem, ItemStatus
from app.models.outfit import Outfit, OutfitItem, OutfitSource, OutfitStatus
from app.models.user import User
from app.services.item_scorer import ScoredItem
from app.services.recommendation_service import (
    RecommendationService,
    get_time_of_day,
)


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


# ---------------------------------------------------------------------------
# Mandatory include_items regression tests
# ---------------------------------------------------------------------------


class TestDeduplicateByBodySlotMandatory:
    """deduplicate_by_body_slot must never remove mandatory items."""

    def test_mandatory_base_top_survives_full_body(self):
        from app.utils.clothing import deduplicate_by_body_slot

        dress_id = uuid4()
        shirt_id = uuid4()
        ids = [shirt_id, dress_id]
        type_map = {shirt_id: "shirt", dress_id: "dress"}
        result = deduplicate_by_body_slot(ids, type_map, mandatory_item_ids={shirt_id})
        assert shirt_id in result, "mandatory shirt must survive alongside a dress"
        assert dress_id in result

    def test_mandatory_bottom_survives_duplicate_role(self):
        from app.utils.clothing import deduplicate_by_body_slot

        pants_a = uuid4()
        pants_b = uuid4()
        ids = [pants_a, pants_b]
        type_map = {pants_a: "pants", pants_b: "jeans"}
        # pants_b is mandatory → must not be removed even though pants_a fills the slot first
        result = deduplicate_by_body_slot(ids, type_map, mandatory_item_ids={pants_b})
        assert pants_b in result

    def test_non_mandatory_still_deduped(self):
        from app.utils.clothing import deduplicate_by_body_slot

        pants_a = uuid4()
        pants_b = uuid4()
        ids = [pants_a, pants_b]
        type_map = {pants_a: "pants", pants_b: "jeans"}
        result = deduplicate_by_body_slot(ids, type_map)
        # without mandatory, the second bottom is removed
        assert pants_a in result
        assert pants_b not in result

    def test_mandatory_none_unchanged(self):
        from app.utils.clothing import deduplicate_by_body_slot

        shirt = uuid4()
        pants = uuid4()
        ids = [shirt, pants]
        type_map = {shirt: "shirt", pants: "pants"}
        result = deduplicate_by_body_slot(ids, type_map, mandatory_item_ids=None)
        assert result == [shirt, pants]


class TestMandatoryItemsValidation:
    """POST /outfits/suggest with invalid include_items must return 400."""

    @pytest.mark.asyncio
    async def test_nonexistent_item_returns_400(
        self, client, test_user, auth_headers, db_session
    ):
        # Create enough items so the wardrobe check isn't the failure reason
        for t in ["shirt", "pants", "sneakers"]:
            db_session.add(
                ClothingItem(
                    user_id=test_user.id,
                    type=t,
                    image_path=f"test/{uuid4()}.jpg",
                    status=ItemStatus.ready,
                    primary_color="blue",
                )
            )
        await db_session.commit()

        fake_id = str(uuid4())
        response = await client.post(
            "/api/v1/outfits/suggest",
            json={
                "occasion": "casual",
                "include_items": [fake_id],
                "weather_override": {"temperature": 20, "condition": "clear"},
            },
            headers=auth_headers,
        )
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert detail["error_code"] == "MANDATORY_ITEM_INVALID"
        assert fake_id in detail["message"]

    @pytest.mark.asyncio
    async def test_not_ready_item_returns_400(
        self, client, test_user, auth_headers, db_session
    ):
        processing_item = ClothingItem(
            user_id=test_user.id,
            type="shirt",
            image_path=f"test/{uuid4()}.jpg",
            status=ItemStatus.processing,
            primary_color="red",
        )
        for t in ["pants", "sneakers"]:
            db_session.add(
                ClothingItem(
                    user_id=test_user.id,
                    type=t,
                    image_path=f"test/{uuid4()}.jpg",
                    status=ItemStatus.ready,
                    primary_color="blue",
                )
            )
        db_session.add(processing_item)
        await db_session.commit()

        response = await client.post(
            "/api/v1/outfits/suggest",
            json={
                "occasion": "casual",
                "include_items": [str(processing_item.id)],
                "weather_override": {"temperature": 20, "condition": "clear"},
            },
            headers=auth_headers,
        )
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert detail["error_code"] == "MANDATORY_ITEM_INVALID"

    @pytest.mark.asyncio
    async def test_archived_item_returns_400(
        self, client, test_user, auth_headers, db_session
    ):
        archived_item = ClothingItem(
            user_id=test_user.id,
            type="shirt",
            image_path=f"test/{uuid4()}.jpg",
            status=ItemStatus.ready,
            is_archived=True,
            primary_color="green",
        )
        for t in ["pants", "sneakers"]:
            db_session.add(
                ClothingItem(
                    user_id=test_user.id,
                    type=t,
                    image_path=f"test/{uuid4()}.jpg",
                    status=ItemStatus.ready,
                    primary_color="blue",
                )
            )
        db_session.add(archived_item)
        await db_session.commit()

        response = await client.post(
            "/api/v1/outfits/suggest",
            json={
                "occasion": "casual",
                "include_items": [str(archived_item.id)],
                "weather_override": {"temperature": 20, "condition": "clear"},
            },
            headers=auth_headers,
        )
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert detail["error_code"] == "MANDATORY_ITEM_INVALID"

    @pytest.mark.asyncio
    async def test_other_users_item_returns_400(
        self, client, test_user, auth_headers, db_session
    ):
        from app.models.user import User

        other_user = User(
            id=uuid4(),
            external_id=f"other-{uuid4()}",
            email=f"other-{uuid4()}@example.com",
            display_name="Other",
            timezone="UTC",
            is_active=True,
        )
        db_session.add(other_user)
        await db_session.flush()

        other_item = ClothingItem(
            user_id=other_user.id,
            type="shirt",
            image_path=f"test/{uuid4()}.jpg",
            status=ItemStatus.ready,
            primary_color="blue",
        )
        for t in ["shirt", "pants", "sneakers"]:
            db_session.add(
                ClothingItem(
                    user_id=test_user.id,
                    type=t,
                    image_path=f"test/{uuid4()}.jpg",
                    status=ItemStatus.ready,
                    primary_color="blue",
                )
            )
        db_session.add(other_item)
        await db_session.commit()

        response = await client.post(
            "/api/v1/outfits/suggest",
            json={
                "occasion": "casual",
                "include_items": [str(other_item.id)],
                "weather_override": {"temperature": 20, "condition": "clear"},
            },
            headers=auth_headers,
        )
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert detail["error_code"] == "MANDATORY_ITEM_INVALID"


class TestMandatoryItemsEndToEnd:
    """Mandatory items must appear in the final outfit even when AI omits them."""

    @pytest.mark.asyncio
    async def test_mandatory_item_included_when_ai_omits(
        self, client, test_user, auth_headers, db_session
    ):
        """If the AI response doesn't reference a mandatory item, the pipeline
        must inject it into the materialised outfit."""
        from decimal import Decimal

        from app.services.ai_service import TextGenerationResult

        test_user.location_lat = Decimal("40.7128")
        test_user.location_lon = Decimal("-74.0060")

        shirt = ClothingItem(
            user_id=test_user.id,
            type="shirt",
            image_path=f"test/{uuid4()}.jpg",
            status=ItemStatus.ready,
            primary_color="blue",
        )
        pants = ClothingItem(
            user_id=test_user.id,
            type="pants",
            image_path=f"test/{uuid4()}.jpg",
            status=ItemStatus.ready,
            primary_color="navy",
        )
        sneakers = ClothingItem(
            user_id=test_user.id,
            type="sneakers",
            image_path=f"test/{uuid4()}.jpg",
            status=ItemStatus.ready,
            primary_color="white",
        )
        db_session.add_all([shirt, pants, sneakers])
        await db_session.commit()

        # AI omits the mandatory pants — only selects shirt [1] and sneakers [3].
        ai_json = json.dumps(
            {
                "outfits": [
                    {
                        "items": [1, 3],
                        "headline": "Casual look",
                        "highlights": ["Comfy", "Simple"],
                        "styling_tip": "Add a belt",
                    }
                ]
            }
        )

        async def mock_generate_text(prompt, **kwargs):
            return TextGenerationResult(
                content=ai_json, model="test", endpoint="test"
            )

        with patch(
            "app.services.recommendation_service.AIService.generate_text",
            side_effect=mock_generate_text,
        ):
            response = await client.post(
                "/api/v1/outfits/suggest",
                json={
                    "occasion": "casual",
                    "include_items": [str(pants.id)],
                    "weather_override": {"temperature": 20, "condition": "clear"},
                },
                headers=auth_headers,
            )

        assert response.status_code == 200
        data = response.json()
        returned_ids = {item["id"] for item in data["items"]}
        assert str(pants.id) in returned_ids, (
            "mandatory pants must appear in the outfit even though AI omitted it"
        )

    @pytest.mark.asyncio
    async def test_mandatory_item_survives_body_slot_conflict(
        self, client, test_user, auth_headers, db_session
    ):
        """A mandatory base_top must not be removed by body-slot dedup when a
        full_body item (dress) is also in the outfit."""
        from decimal import Decimal

        from app.services.ai_service import TextGenerationResult

        test_user.location_lat = Decimal("40.7128")
        test_user.location_lon = Decimal("-74.0060")

        shirt = ClothingItem(
            user_id=test_user.id,
            type="shirt",
            image_path=f"test/{uuid4()}.jpg",
            status=ItemStatus.ready,
            primary_color="white",
        )
        dress = ClothingItem(
            user_id=test_user.id,
            type="dress",
            image_path=f"test/{uuid4()}.jpg",
            status=ItemStatus.ready,
            primary_color="black",
        )
        sneakers = ClothingItem(
            user_id=test_user.id,
            type="sneakers",
            image_path=f"test/{uuid4()}.jpg",
            status=ItemStatus.ready,
            primary_color="white",
        )
        db_session.add_all([shirt, dress, sneakers])
        await db_session.commit()

        # AI selects dress and sneakers, omits the mandatory shirt.
        ai_json = json.dumps(
            {
                "outfits": [
                    {
                        "items": [1, 2, 3],
                        "headline": "Layered look",
                        "highlights": ["Layered", "Bold"],
                        "styling_tip": "Roll the sleeves",
                    }
                ]
            }
        )

        async def mock_generate_text(prompt, **kwargs):
            return TextGenerationResult(
                content=ai_json, model="test", endpoint="test"
            )

        with patch(
            "app.services.recommendation_service.AIService.generate_text",
            side_effect=mock_generate_text,
        ):
            response = await client.post(
                "/api/v1/outfits/suggest",
                json={
                    "occasion": "casual",
                    "include_items": [str(shirt.id)],
                    "weather_override": {"temperature": 20, "condition": "clear"},
                },
                headers=auth_headers,
            )

        assert response.status_code == 200
        data = response.json()
        returned_ids = {item["id"] for item in data["items"]}
        assert str(shirt.id) in returned_ids, (
            "mandatory shirt must survive body-slot dedup against dress"
        )
