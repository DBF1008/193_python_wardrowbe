"""Regression tests for wore-instead item resolution in own vs family-member outfit views.

Bug: GET /outfits?family_member_id=<uuid> resolved wore_instead items against the
*current* user's wardrobe instead of the *target* member's wardrobe, causing replacement
items to be silently dropped from the response.

These tests cover both paths:
  1. Own view (no family_member_id) -- wore_instead items resolve from own wardrobe.
  2. Family-member view (family_member_id set) -- wore_instead items resolve from the
     target member's wardrobe, NOT the current user's.
"""

from datetime import date
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import create_access_token
from app.models import (
    ClothingItem,
    Family,
    Outfit,
    OutfitItem,
    User,
    UserFeedback,
)
from app.models.item import ItemStatus
from app.models.outfit import OutfitSource, OutfitStatus


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def family(db_session: AsyncSession) -> Family:
    """Create a family row that both users will share."""
    fam = Family(
        id=uuid4(),
        name="Test Family",
        created_by=uuid4(),  # placeholder; real FK not enforced in test
        invite_code="TESTCODE123",
    )
    # We need a real creator user for the FK, so create one first.
    creator = User(
        id=fam.created_by,
        external_id=f"family-creator-{uuid4()}",
        email=f"creator-{uuid4()}@example.com",
        display_name="Creator",
        timezone="UTC",
        is_active=True,
        onboarding_completed=True,
    )
    db_session.add(creator)
    db_session.add(fam)
    await db_session.commit()
    await db_session.refresh(fam)
    return fam


@pytest_asyncio.fixture
async def user_a(db_session: AsyncSession, family: Family) -> User:
    """The 'viewer' -- the user who is logged in and looks at family member's outfits."""
    user = User(
        id=uuid4(),
        external_id=f"user-a-{uuid4()}",
        email=f"user-a-{uuid4()}@example.com",
        display_name="User A",
        timezone="UTC",
        is_active=True,
        onboarding_completed=True,
        family_id=family.id,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def user_b(db_session: AsyncSession, family: Family) -> User:
    """The 'family member' whose outfits are being viewed."""
    user = User(
        id=uuid4(),
        external_id=f"user-b-{uuid4()}",
        email=f"user-b-{uuid4()}@example.com",
        display_name="User B",
        timezone="UTC",
        is_active=True,
        onboarding_completed=True,
        family_id=family.id,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def _make_item(user: User, item_type: str, name: str) -> ClothingItem:
    return ClothingItem(
        id=uuid4(),
        user_id=user.id,
        type=item_type,
        name=name,
        image_path=f"test/{item_type}.jpg",
        thumbnail_path=f"test/thumb_{item_type}.jpg",
        status=ItemStatus.ready,
        primary_color="blue",
        colors=["blue"],
    )


@pytest_asyncio.fixture
async def user_a_items(db_session: AsyncSession, user_a: User) -> list[ClothingItem]:
    """Clothing items belonging to user A (the viewer)."""
    items = [
        _make_item(user_a, "shirt", "A's Shirt"),
        _make_item(user_a, "pants", "A's Pants"),
    ]
    for item in items:
        db_session.add(item)
    await db_session.commit()
    for item in items:
        await db_session.refresh(item)
    return items


@pytest_asyncio.fixture
async def user_b_items(db_session: AsyncSession, user_b: User) -> list[ClothingItem]:
    """Clothing items belonging to user B (the family member being viewed)."""
    items = [
        _make_item(user_b, "shirt", "B's Shirt"),
        _make_item(user_b, "pants", "B's Pants"),
        _make_item(user_b, "jacket", "B's Jacket"),
        _make_item(user_b, "sneakers", "B's Sneakers"),
    ]
    for item in items:
        db_session.add(item)
    await db_session.commit()
    for item in items:
        await db_session.refresh(item)
    return items


@pytest_asyncio.fixture
async def user_b_outfit_with_wore_instead(
    db_session: AsyncSession,
    user_b: User,
    user_b_items: list[ClothingItem],
) -> Outfit:
    """An outfit owned by user B that has feedback with wore_instead_items.

    The outfit uses items [shirt, pants] as original, and the feedback records
    that user B actually wore [jacket, sneakers] instead.
    """
    shirt, pants, jacket, sneakers = user_b_items

    outfit = Outfit(
        id=uuid4(),
        user_id=user_b.id,
        occasion="casual",
        scheduled_for=date.today(),
        status=OutfitStatus.accepted,
        source=OutfitSource.manual,
        name="B's Casual Outfit",
    )
    db_session.add(outfit)
    await db_session.flush()

    # Original outfit items
    db_session.add_all(
        [
            OutfitItem(outfit_id=outfit.id, item_id=shirt.id, position=0, layer_type="top"),
            OutfitItem(outfit_id=outfit.id, item_id=pants.id, position=1, layer_type="bottom"),
        ]
    )

    # Feedback with wore_instead items pointing to B's jacket and sneakers
    feedback = UserFeedback(
        id=uuid4(),
        outfit_id=outfit.id,
        accepted=False,
        rating=3,
        actually_worn=False,
        wore_instead_items=[str(jacket.id), str(sneakers.id)],
    )
    db_session.add(feedback)
    await db_session.commit()
    await db_session.refresh(outfit)
    return outfit


@pytest_asyncio.fixture
async def user_a_outfit_with_wore_instead(
    db_session: AsyncSession,
    user_a: User,
    user_a_items: list[ClothingItem],
) -> Outfit:
    """An outfit owned by user A that also has wore_instead feedback."""
    shirt, pants = user_a_items

    outfit = Outfit(
        id=uuid4(),
        user_id=user_a.id,
        occasion="office",
        scheduled_for=date.today(),
        status=OutfitStatus.accepted,
        source=OutfitSource.manual,
        name="A's Office Outfit",
    )
    db_session.add(outfit)
    await db_session.flush()

    db_session.add_all(
        [
            OutfitItem(outfit_id=outfit.id, item_id=shirt.id, position=0, layer_type="top"),
            OutfitItem(outfit_id=outfit.id, item_id=pants.id, position=1, layer_type="bottom"),
        ]
    )

    feedback = UserFeedback(
        id=uuid4(),
        outfit_id=outfit.id,
        accepted=False,
        rating=2,
        actually_worn=False,
        # A says they wore B's items instead -- these IDs won't be in A's wardrobe
        wore_instead_items=[str(pants.id)],
    )
    db_session.add(feedback)
    await db_session.commit()
    await db_session.refresh(outfit)
    return outfit


@pytest.fixture
def user_a_auth(user_a: User) -> dict[str, str]:
    token = create_access_token(user_a.external_id)
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_own_view_resolves_wore_instead_from_own_wardrobe(
    client: AsyncClient,
    user_a_auth: dict[str, str],
    user_a_outfit_with_wore_instead: Outfit,
    user_a_items: list[ClothingItem],
):
    """Own outfit list (no family_member_id): wore_instead items should be resolved
    against the current user's own wardrobe.
    """
    resp = await client.get("/api/v1/outfits", headers=user_a_auth, params={"page_size": 50})
    assert resp.status_code == 200

    data = resp.json()
    assert data["total"] >= 1

    # Find our specific outfit
    outfit_data = next(
        (o for o in data["outfits"] if o["id"] == str(user_a_outfit_with_wore_instead.id)),
        None,
    )
    assert outfit_data is not None, "User A's outfit should appear in their own list"
    assert outfit_data["feedback"] is not None
    assert outfit_data["feedback"]["actually_worn"] is False

    wore_instead = outfit_data["feedback"]["wore_instead_items"]
    assert wore_instead is not None, "wore_instead_items must not be None for own view"
    assert len(wore_instead) == 1
    # The replaced item is user A's pants -- should resolve correctly
    assert wore_instead[0]["id"] == str(user_a_items[1].id)
    assert wore_instead[0]["type"] == "pants"


@pytest.mark.asyncio
async def test_family_member_view_resolves_wore_instead_from_target_wardrobe(
    client: AsyncClient,
    user_a_auth: dict[str, str],
    user_b: User,
    user_b_outfit_with_wore_instead: Outfit,
    user_b_items: list[ClothingItem],
):
    """Family-member view (family_member_id=user_b): wore_instead items must be
    resolved from user B's wardrobe, not user A's.

    This is the primary regression test for the bug.
    """
    resp = await client.get(
        "/api/v1/outfits",
        headers=user_a_auth,
        params={"family_member_id": str(user_b.id), "page_size": 50},
    )
    assert resp.status_code == 200

    data = resp.json()
    assert data["total"] >= 1

    # Find user B's outfit in the response
    outfit_data = next(
        (o for o in data["outfits"] if o["id"] == str(user_b_outfit_with_wore_instead.id)),
        None,
    )
    assert outfit_data is not None, "User B's outfit should appear in family member view"
    assert outfit_data["feedback"] is not None
    assert outfit_data["feedback"]["actually_worn"] is False

    wore_instead = outfit_data["feedback"]["wore_instead_items"]
    assert wore_instead is not None, (
        "REGRESSION: wore_instead_items must NOT be None/empty when viewing a "
        "family member's outfits. Previously the items were filtered against the "
        "current user's wardrobe and dropped silently."
    )
    assert len(wore_instead) == 2, (
        "Should see both of user B's replacement items (jacket + sneakers)"
    )

    # Verify the correct items are returned
    wore_ids = {w["id"] for w in wore_instead}
    expected_ids = {str(user_b_items[2].id), str(user_b_items[3].id)}  # jacket + sneakers
    assert wore_ids == expected_ids

    # Verify item details are populated (not just IDs)
    types_in_response = {w["type"] for w in wore_instead}
    assert types_in_response == {"jacket", "sneakers"}

    # Verify names are populated
    names_in_response = {w["name"] for w in wore_instead}
    assert "B's Jacket" in names_in_response
    assert "B's Sneakers" in names_in_response


@pytest.mark.asyncio
async def test_family_member_view_does_not_leak_other_user_items(
    client: AsyncClient,
    user_a_auth: dict[str, str],
    user_b: User,
    user_a_items: list[ClothingItem],
    db_session: AsyncSession,
):
    """Security boundary: even if user B's feedback references an item ID from
    user A's wardrobe, that item must NOT be returned (it doesn't belong to B).
    """
    # Create an outfit for user B whose feedback references user A's shirt
    shirt_a = user_a_items[0]  # belongs to user A

    outfit = Outfit(
        id=uuid4(),
        user_id=user_b.id,
        occasion="casual",
        scheduled_for=date.today(),
        status=OutfitStatus.accepted,
        source=OutfitSource.manual,
        name="B's Outfit With A's Item Ref",
    )
    db_session.add(outfit)
    await db_session.flush()

    feedback = UserFeedback(
        id=uuid4(),
        outfit_id=outfit.id,
        actually_worn=False,
        wore_instead_items=[str(shirt_a.id)],  # points to user A's item
    )
    db_session.add(feedback)
    await db_session.commit()

    resp = await client.get(
        "/api/v1/outfits",
        headers=user_a_auth,
        params={"family_member_id": str(user_b.id), "page_size": 50},
    )
    assert resp.status_code == 200

    data = resp.json()
    outfit_data = next(
        (o for o in data["outfits"] if o["id"] == str(outfit.id)),
        None,
    )
    assert outfit_data is not None

    # The feedback exists but the item reference should NOT resolve because
    # shirt_a belongs to user A, not user B (the target member).
    feedback_data = outfit_data["feedback"]
    assert feedback_data is not None
    wore_instead = feedback_data["wore_instead_items"]
    # Either None or empty list -- the cross-user item must not leak
    assert not wore_instead, (
        "Items from user A's wardrobe must NOT appear when viewing user B's outfits, "
        "even if B's feedback references them. The user_id scope must be enforced."
    )


@pytest.mark.asyncio
async def test_family_member_view_outfit_without_feedback(
    client: AsyncClient,
    user_a_auth: dict[str, str],
    user_b: User,
    user_b_items: list[ClothingItem],
    db_session: AsyncSession,
):
    """Family-member view of an outfit that has NO feedback should not error
    and should return feedback=None.
    """
    shirt = user_b_items[0]
    outfit = Outfit(
        id=uuid4(),
        user_id=user_b.id,
        occasion="work",
        scheduled_for=date.today(),
        status=OutfitStatus.pending,
        source=OutfitSource.scheduled,
        name="B's Plain Outfit",
    )
    db_session.add(outfit)
    await db_session.flush()
    db_session.add(
        OutfitItem(outfit_id=outfit.id, item_id=shirt.id, position=0, layer_type="top")
    )
    await db_session.commit()

    resp = await client.get(
        "/api/v1/outfits",
        headers=user_a_auth,
        params={"family_member_id": str(user_b.id), "page_size": 50},
    )
    assert resp.status_code == 200

    data = resp.json()
    outfit_data = next(
        (o for o in data["outfits"] if o["id"] == str(outfit.id)),
        None,
    )
    assert outfit_data is not None
    assert outfit_data["feedback"] is None
