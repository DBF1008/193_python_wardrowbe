from datetime import date
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.family import Family
from app.models.item import ClothingItem, ItemStatus
from app.models.outfit import (
    Outfit,
    OutfitItem,
    OutfitSource,
    OutfitStatus,
    UserFeedback,
)
from app.models.user import User


def _make_item(user_id, item_type="shirt", **kwargs) -> ClothingItem:
    return ClothingItem(
        user_id=user_id,
        type=item_type,
        image_path=f"test/{uuid4()}.jpg",
        status=ItemStatus.ready,
        **kwargs,
    )


def _make_worn_outfit(user_id, items: list[ClothingItem], **kwargs) -> Outfit:
    outfit = Outfit(
        user_id=user_id,
        occasion="casual",
        scheduled_for=date.today(),
        status=OutfitStatus.accepted,
        source=OutfitSource.manual,
        **kwargs,
    )
    for i, item in enumerate(items):
        outfit.items.append(OutfitItem(item_id=item.id, position=i))
    return outfit


def _make_family(creator_id) -> Family:
    return Family(
        name="Test Family",
        invite_code=f"FAM{uuid4().hex[:6]}",
        created_by=creator_id,
    )


def _make_family_member(family_id) -> User:
    uid = uuid4()
    return User(
        id=uid,
        external_id=f"member-{uid}",
        email=f"member-{uid}@example.com",
        display_name="Family Member",
        timezone="UTC",
        is_active=True,
        family_id=family_id,
    )


class TestWoreInsteadResolution:
    """Regression coverage for resolving an outfit's "wore instead" replacement items.

    The list endpoint must resolve replacement items against the owner of the
    outfits being listed, so the self view and the family-member view both return
    the correct items (and thumbnails) instead of silently filtering them out.
    """

    @pytest.mark.asyncio
    async def test_self_view_resolves_wore_instead_items(
        self,
        client: AsyncClient,
        test_user: User,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
    ):
        worn_item = _make_item(test_user.id, "shirt")
        replacement = _make_item(
            test_user.id,
            "tshirt",
            name="Comfy Tee",
            thumbnail_path=f"thumbs/{uuid4()}.jpg",
        )
        db_session.add_all([worn_item, replacement])
        await db_session.flush()

        outfit = _make_worn_outfit(test_user.id, [worn_item])
        db_session.add(outfit)
        await db_session.flush()

        feedback = UserFeedback(
            outfit_id=outfit.id,
            actually_worn=False,
            worn_at=date.today(),
            wore_instead_items=[str(replacement.id)],
        )
        db_session.add(feedback)
        await db_session.commit()

        response = await client.get("/api/v1/outfits", headers=auth_headers)
        assert response.status_code == 200

        data = response.json()
        outfit_resp = next(o for o in data["outfits"] if o["id"] == str(outfit.id))
        assert outfit_resp["feedback"] is not None

        wore = outfit_resp["feedback"]["wore_instead_items"]
        assert wore is not None
        assert [w["id"] for w in wore] == [str(replacement.id)]
        assert wore[0]["name"] == "Comfy Tee"
        assert wore[0]["thumbnail_url"] is not None

    @pytest.mark.asyncio
    async def test_family_member_view_resolves_members_wore_instead_items(
        self,
        client: AsyncClient,
        test_user: User,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
    ):
        family = _make_family(test_user.id)
        db_session.add(family)
        await db_session.flush()
        test_user.family_id = family.id

        member = _make_family_member(family.id)
        db_session.add(member)
        await db_session.flush()

        # Both the worn item and the replacement belong to the family MEMBER,
        # not the viewing user.
        member_worn = _make_item(member.id, "dress")
        member_replacement = _make_item(
            member.id,
            "skirt",
            name="Member Skirt",
            thumbnail_path=f"thumbs/{uuid4()}.jpg",
        )
        db_session.add_all([member_worn, member_replacement])
        await db_session.flush()

        member_outfit = _make_worn_outfit(member.id, [member_worn])
        db_session.add(member_outfit)
        await db_session.flush()

        feedback = UserFeedback(
            outfit_id=member_outfit.id,
            actually_worn=False,
            worn_at=date.today(),
            wore_instead_items=[str(member_replacement.id)],
        )
        db_session.add(feedback)
        await db_session.commit()

        response = await client.get(
            f"/api/v1/outfits?family_member_id={member.id}", headers=auth_headers
        )
        assert response.status_code == 200

        data = response.json()
        outfit_resp = next(o for o in data["outfits"] if o["id"] == str(member_outfit.id))
        assert outfit_resp["feedback"] is not None

        wore = outfit_resp["feedback"]["wore_instead_items"]
        # Regression: previously these were resolved against the viewer's wardrobe,
        # so a family member's own replacement items were filtered out, leaving the
        # list and thumbnails empty.
        assert wore is not None
        assert [w["id"] for w in wore] == [str(member_replacement.id)]
        assert wore[0]["name"] == "Member Skirt"
        assert wore[0]["thumbnail_url"] is not None

    @pytest.mark.asyncio
    async def test_family_member_view_excludes_items_not_owned_by_member(
        self,
        client: AsyncClient,
        test_user: User,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
    ):
        family = _make_family(test_user.id)
        db_session.add(family)
        await db_session.flush()
        test_user.family_id = family.id

        member = _make_family_member(family.id)
        db_session.add(member)
        await db_session.flush()

        member_worn = _make_item(member.id, "dress")
        member_replacement = _make_item(
            member.id,
            "skirt",
            name="Member Skirt",
            thumbnail_path=f"thumbs/{uuid4()}.jpg",
        )
        # Belongs to the viewer, not the member — must never surface in the member's
        # replacement list. This guards the ownership boundary: resolution is scoped
        # to the outfit owner, not a global lookup.
        viewer_item = _make_item(
            test_user.id,
            "jacket",
            name="Viewer Jacket",
            thumbnail_path=f"thumbs/{uuid4()}.jpg",
        )
        db_session.add_all([member_worn, member_replacement, viewer_item])
        await db_session.flush()

        member_outfit = _make_worn_outfit(member.id, [member_worn])
        db_session.add(member_outfit)
        await db_session.flush()

        feedback = UserFeedback(
            outfit_id=member_outfit.id,
            actually_worn=False,
            worn_at=date.today(),
            wore_instead_items=[str(member_replacement.id), str(viewer_item.id)],
        )
        db_session.add(feedback)
        await db_session.commit()

        response = await client.get(
            f"/api/v1/outfits?family_member_id={member.id}", headers=auth_headers
        )
        assert response.status_code == 200

        data = response.json()
        outfit_resp = next(o for o in data["outfits"] if o["id"] == str(member_outfit.id))
        wore = outfit_resp["feedback"]["wore_instead_items"]
        assert wore is not None

        returned_ids = {w["id"] for w in wore}
        assert str(member_replacement.id) in returned_ids
        assert str(viewer_item.id) not in returned_ids
