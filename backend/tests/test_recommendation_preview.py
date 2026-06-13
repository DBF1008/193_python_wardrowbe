from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models.item import ClothingItem, ItemStatus
from app.models.outfit import Outfit
from app.models.preference import UserPreference
from app.services.recommendation_service import (
    CandidateExclusionReason,
    classify_candidate_items,
)

PREVIEW_URL = "/api/v1/outfits/suggest/preview"


def _item(**kw) -> ClothingItem:
    defaults = dict(
        id=uuid4(),
        user_id=uuid4(),
        type="shirt",
        image_path="x.jpg",
        is_archived=False,
        needs_wash=False,
    )
    defaults.update(kw)
    return ClothingItem(**defaults)


class TestClassifyCandidateItems:
    def _classify(self, items, **kw):
        params = dict(
            exclude_request=set(),
            auto_rejected=set(),
            excluded_pref=set(),
            mandatory=set(),
        )
        params.update(kw)
        return classify_candidate_items(items, **params)

    def test_needs_wash(self):
        it = _item(needs_wash=True)
        kept, excluded = self._classify([it])
        assert kept == []
        assert len(excluded) == 1
        assert excluded[0].item.id == it.id
        assert excluded[0].reason == CandidateExclusionReason.NEEDS_WASH

    def test_archived(self):
        it = _item(is_archived=True)
        kept, excluded = self._classify([it])
        assert kept == []
        assert excluded[0].reason == CandidateExclusionReason.ARCHIVED

    def test_unknown_type(self):
        it = _item(type="unknown")
        kept, excluded = self._classify([it])
        assert kept == []
        assert excluded[0].reason == CandidateExclusionReason.UNKNOWN_TYPE

    def test_empty_type_is_unknown(self):
        it = _item(type="")
        kept, excluded = self._classify([it])
        assert kept == []
        assert excluded[0].reason == CandidateExclusionReason.UNKNOWN_TYPE

    def test_excluded_by_request(self):
        it = _item()
        kept, excluded = self._classify([it], exclude_request={it.id})
        assert kept == []
        assert excluded[0].reason == CandidateExclusionReason.EXCLUDED_BY_REQUEST

    def test_auto_rejected_today(self):
        it = _item()
        kept, excluded = self._classify([it], auto_rejected={it.id})
        assert kept == []
        assert excluded[0].reason == CandidateExclusionReason.AUTO_REJECTED_TODAY

    def test_excluded_by_preference(self):
        it = _item()
        kept, excluded = self._classify([it], excluded_pref={it.id})
        assert kept == []
        assert excluded[0].reason == CandidateExclusionReason.EXCLUDED_BY_PREFERENCE

    def test_mandatory_overrides_needs_wash_and_request(self):
        # A force-included item is kept even if it needs washing AND is in exclude_items,
        # mirroring production's force-include re-adding such items.
        it = _item(needs_wash=True)
        kept, excluded = self._classify(
            [it], exclude_request={it.id}, mandatory={it.id}
        )
        assert [i.id for i in kept] == [it.id]
        assert excluded == []

    def test_archived_beats_mandatory(self):
        # Archived wins even over mandatory, because production's force-include query
        # filters is_archived == False and so can never re-add archived items.
        it = _item(is_archived=True)
        kept, excluded = self._classify([it], mandatory={it.id})
        assert kept == []
        assert excluded[0].reason == CandidateExclusionReason.ARCHIVED


async def _seed(db, user, **kw) -> ClothingItem:
    defaults = dict(
        id=uuid4(),
        user_id=user.id,
        type="shirt",
        image_path=f"test/{uuid4()}.jpg",
        status=ItemStatus.ready,
        primary_color="blue",
    )
    defaults.update(kw)
    item = ClothingItem(**defaults)
    db.add(item)
    return item


async def _outfit_count(db, user) -> int:
    return await db.scalar(
        select(func.count()).select_from(Outfit).where(Outfit.user_id == user.id)
    )


class TestSuggestPreviewEndpoint:
    @pytest.mark.asyncio
    async def test_default_occasion_casual(self, client, test_user, auth_headers, db_session):
        await _seed(db_session, test_user, type="shirt")
        await _seed(db_session, test_user, type="pants")
        await db_session.commit()

        resp = await client.post(
            PREVIEW_URL,
            json={"weather_override": {"temperature": 20, "condition": "clear"}},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        assert resp.json()["occasion"] == "casual"

    @pytest.mark.asyncio
    async def test_default_occasion_from_preferences(
        self, client, test_user, auth_headers, db_session
    ):
        db_session.add(UserPreference(user_id=test_user.id, default_occasion="work"))
        await _seed(db_session, test_user, type="shirt")
        await _seed(db_session, test_user, type="pants")
        await db_session.commit()

        resp = await client.post(
            PREVIEW_URL,
            json={"weather_override": {"temperature": 20, "condition": "clear"}},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        assert resp.json()["occasion"] == "work"

    @pytest.mark.asyncio
    async def test_custom_weather_override(self, client, test_user, auth_headers, db_session):
        await _seed(db_session, test_user, type="shirt")
        await _seed(db_session, test_user, type="pants")
        await db_session.commit()

        resp = await client.post(
            PREVIEW_URL,
            json={
                "occasion": "casual",
                "weather_override": {
                    "temperature": 12.5,
                    "condition": "rain",
                    "precipitation_chance": 80,
                },
            },
            headers=auth_headers,
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["weather_source"] == "override"
        assert data["weather"]["temperature"] == 12.5
        assert data["weather"]["condition"] == "rain"
        # The preview must never pollute the official flow.
        assert await _outfit_count(db_session, test_user) == 0

    @pytest.mark.asyncio
    async def test_include_and_exclude_items(
        self, client, test_user, auth_headers, db_session
    ):
        excluded_item = await _seed(db_session, test_user, type="hat")
        mandatory_item = await _seed(db_session, test_user, type="jacket", needs_wash=True)
        await _seed(db_session, test_user, type="shirt")
        await _seed(db_session, test_user, type="pants")
        await db_session.commit()

        resp = await client.post(
            PREVIEW_URL,
            json={
                "occasion": "casual",
                "weather_override": {"temperature": 20, "condition": "clear"},
                "exclude_items": [str(excluded_item.id)],
                "include_items": [str(mandatory_item.id)],
            },
            headers=auth_headers,
        )

        assert resp.status_code == 200
        data = resp.json()

        candidate_ids = {c["id"] for c in data["candidates"]}
        excluded_map = {e["id"]: e["reason"] for e in data["excluded"]}

        # Excluded-by-request item is reported and absent from the candidate pool.
        assert excluded_map.get(str(excluded_item.id)) == "excluded_by_request"
        assert str(excluded_item.id) not in candidate_ids

        # A force-included needs-wash item is kept and flagged mandatory.
        mand = next(c for c in data["candidates"] if c["id"] == str(mandatory_item.id))
        assert mand["mandatory"] is True
        assert str(mandatory_item.id) in data["mandatory_item_ids"]

    @pytest.mark.asyncio
    async def test_filter_reasons_display(self, client, test_user, auth_headers, db_session):
        wash_item = await _seed(db_session, test_user, type="shirt", needs_wash=True)
        arch_item = await _seed(db_session, test_user, type="pants", is_archived=True)
        unknown_item = await _seed(db_session, test_user, type="unknown")
        await _seed(db_session, test_user, type="shoes")
        await _seed(db_session, test_user, type="jacket")
        await db_session.commit()

        resp = await client.post(
            PREVIEW_URL,
            json={
                "occasion": "casual",
                "weather_override": {"temperature": 20, "condition": "clear"},
            },
            headers=auth_headers,
        )

        assert resp.status_code == 200
        excluded = resp.json()["excluded"]
        excluded_map = {e["id"]: e["reason"] for e in excluded}
        assert excluded_map.get(str(wash_item.id)) == "needs_wash"
        assert excluded_map.get(str(arch_item.id)) == "archived"
        assert excluded_map.get(str(unknown_item.id)) == "unknown_type"
        # Each excluded entry carries a human-readable message.
        assert all(e["reason_message"] for e in excluded)

    @pytest.mark.asyncio
    async def test_no_ai_call_by_default(self, client, test_user, auth_headers, db_session):
        await _seed(db_session, test_user, type="shirt")
        await _seed(db_session, test_user, type="pants")
        await db_session.commit()

        with patch("app.services.recommendation_service.AIService") as mock_ai:
            resp = await client.post(
                PREVIEW_URL,
                json={
                    "occasion": "casual",
                    "weather_override": {"temperature": 20, "condition": "clear"},
                },
                headers=auth_headers,
            )

        assert resp.status_code == 200
        assert resp.json()["recommendation"] is None
        mock_ai.assert_not_called()

    @pytest.mark.asyncio
    async def test_opt_in_ai_reasoning(self, client, test_user, auth_headers, db_session):
        await _seed(db_session, test_user, type="shirt")
        await _seed(db_session, test_user, type="pants")
        await db_session.commit()

        content = (
            '{"outfits":[{"items":[1,2],"headline":"Crisp Casual",'
            '"highlights":["Balanced blues"],"styling_tip":"Tuck the shirt"}]}'
        )
        with patch("app.services.recommendation_service.AIService") as mock_ai:
            instance = mock_ai.return_value
            instance.generate_text = AsyncMock(
                return_value=SimpleNamespace(content=content, model="m", endpoint="e")
            )
            resp = await client.post(
                PREVIEW_URL,
                json={
                    "occasion": "casual",
                    "include_ai_reasoning": True,
                    "weather_override": {"temperature": 20, "condition": "clear"},
                },
                headers=auth_headers,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["recommendation"] is not None
        assert data["recommendation"]["headline"] == "Crisp Casual"
        assert len(data["recommendation"]["item_ids"]) >= 1
        instance.generate_text.assert_awaited_once()
        # AI reasoning still must not persist anything.
        assert await _outfit_count(db_session, test_user) == 0
