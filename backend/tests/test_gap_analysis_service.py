"""Tests for the wardrobe gap-analysis capability.

Covers three areas required by the feature:

1. Generating structured gap suggestions from the existing analytics statistics
   (type/color distribution + wear data) *enriched* with learned preferences and
   real feedback — proving the detectors are not static counting.
2. Graceful degradation when there is no history (empty wardrobe, no learning
   profile, profile that was never computed).
3. Consistency between ``GET /analytics`` and ``GET /analytics/gaps`` — the gap
   evidence must reconcile with the distribution / never-worn numbers reported by
   the analytics endpoint.
"""

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from app.models.item import ClothingItem, ItemStatus
from app.models.learning import ItemPairScore, UserLearningProfile
from app.models.outfit import Outfit, OutfitItem, OutfitSource, OutfitStatus
from app.models.preference import UserPreference
from app.services.gap_analysis_service import GapAnalysisService
from app.utils.clothing import ITEM_ROLE


def _ready_item(
    user_id,
    *,
    type="shirt",
    primary_color="blue",
    wear_count=0,
    last_worn_at=None,
    name=None,
    thumbnail_path=None,
):
    return ClothingItem(
        id=uuid4(),
        user_id=user_id,
        type=type,
        image_path="test.jpg",
        primary_color=primary_color,
        status=ItemStatus.ready,
        wear_count=wear_count,
        last_worn_at=last_worn_at,
        name=name,
        thumbnail_path=thumbnail_path,
    )


def _by_category(result, category):
    return [s for s in result.suggestions if s.category == category]


class TestGapGeneration:
    """Generating suggestions from existing stats, folding in learned signals."""

    @pytest.mark.asyncio
    async def test_type_shortage_detected(self, db_session, test_user):
        user_id = test_user.id
        # 6 tops, 1 bottom, 0 shoes.
        for _ in range(6):
            db_session.add(_ready_item(user_id, type="shirt"))
        db_session.add(_ready_item(user_id, type="pants"))
        await db_session.commit()

        result = await GapAnalysisService(db_session).analyze(user_id)

        shortages = {s.title: s for s in _by_category(result, "type_shortage")}
        assert "Short on shoes" in shortages
        assert "Short on bottoms" in shortages

        shoes = shortages["Short on shoes"]
        assert shoes.severity == "high"  # missing role
        assert shoes.evidence["count"] == 0
        assert shoes.evidence["dominant_count"] == 6
        assert shoes.evidence["role_counts"] == {"base_top": 6, "bottom": 1, "footwear": 0}

        bottoms = shortages["Short on bottoms"]
        assert bottoms.severity == "medium"  # present but far short
        assert bottoms.evidence["count"] == 1

        # No learning profile -> degraded confidence + note.
        assert shoes.confidence == "low"
        assert shoes.evidence["note"] == "limited feedback data"

    @pytest.mark.asyncio
    async def test_color_overstock_uses_learned_dislike(self, db_session, test_user):
        user_id = test_user.id
        for _ in range(5):
            db_session.add(_ready_item(user_id, type="shirt", primary_color="red"))
        for _ in range(2):
            db_session.add(_ready_item(user_id, type="shirt", primary_color="blue"))

        db_session.add(
            UserLearningProfile(
                user_id=user_id,
                learned_color_scores={"red": -0.6},
                learned_style_scores={},
                learned_occasion_patterns={},
                feedback_count=10,
                last_computed_at=datetime.now(UTC),
                overall_acceptance_rate=0.6,
            )
        )
        await db_session.commit()

        result = await GapAnalysisService(db_session).analyze(user_id)

        overstock = {s.evidence["color"]: s for s in _by_category(result, "color_overstock")}
        assert "red" in overstock
        red = overstock["red"]
        assert red.evidence["learned_score"] == -0.6
        assert red.severity == "high"  # disliked
        assert red.confidence == "high"  # learned score present
        assert result.has_sufficient_data is True

    @pytest.mark.asyncio
    async def test_color_not_overstocked_when_liked_and_worn(self, db_session, test_user):
        """Same dominant share as the dislike case, but liked + worn -> suppressed.

        This is the clearest proof the detector is non-static: an identical count
        is flagged or not depending entirely on learned feedback and real wear.
        """
        user_id = test_user.id
        recent = date.today() - timedelta(days=5)
        for _ in range(5):
            db_session.add(
                _ready_item(
                    user_id, type="shirt", primary_color="red", wear_count=3, last_worn_at=recent
                )
            )
        for _ in range(2):
            db_session.add(
                _ready_item(
                    user_id, type="shirt", primary_color="blue", wear_count=3, last_worn_at=recent
                )
            )

        db_session.add(
            UserLearningProfile(
                user_id=user_id,
                learned_color_scores={"red": 0.8},
                learned_style_scores={},
                learned_occasion_patterns={},
                feedback_count=10,
                last_computed_at=datetime.now(UTC),
            )
        )
        await db_session.commit()

        result = await GapAnalysisService(db_session).analyze(user_id)

        overstock_colors = {s.evidence["color"] for s in _by_category(result, "color_overstock")}
        assert "red" not in overstock_colors

    @pytest.mark.asyncio
    async def test_occasion_gap_drops_when_reliable_pairs_exist(self, db_session, test_user):
        user_id = test_user.id
        items = [_ready_item(user_id, type=t) for t in ("shirt", "pants", "shoes")]
        for item in items:
            db_session.add(item)

        outfit = Outfit(
            id=uuid4(),
            user_id=user_id,
            occasion="work",
            status=OutfitStatus.accepted,
            source=OutfitSource.on_demand,
            weather_data={"temperature": 20, "condition": "clear"},
        )
        db_session.add(outfit)
        await db_session.flush()
        db_session.add(OutfitItem(outfit_id=outfit.id, item_id=items[0].id, position=0))
        db_session.add(OutfitItem(outfit_id=outfit.id, item_id=items[1].id, position=1))
        await db_session.commit()

        # No reliable pairs yet -> work flagged as a gap.
        result = await GapAnalysisService(db_session).analyze(user_id)
        work_gaps = [s for s in _by_category(result, "occasion_gap") if s.evidence["occasion"] == "work"]
        assert len(work_gaps) == 1
        assert work_gaps[0].evidence["reliable_pair_count"] == 0
        assert work_gaps[0].severity == "high"

        # Add two positive, work-performing pairs -> gap should disappear.
        db_session.add(
            ItemPairScore(
                user_id=user_id,
                item1_id=items[0].id,
                item2_id=items[1].id,
                compatibility_score=0.5,
                times_paired=1,
                occasion_performance={"work": {"count": 2}},
            )
        )
        db_session.add(
            ItemPairScore(
                user_id=user_id,
                item1_id=items[0].id,
                item2_id=items[2].id,
                compatibility_score=0.5,
                times_paired=1,
                occasion_performance={"work": {"count": 1}},
            )
        )
        await db_session.commit()

        result2 = await GapAnalysisService(db_session).analyze(user_id)
        work_gaps2 = [
            s for s in _by_category(result2, "occasion_gap") if s.evidence["occasion"] == "work"
        ]
        assert work_gaps2 == []

    @pytest.mark.asyncio
    async def test_underused_items_flagged(self, db_session, test_user):
        user_id = test_user.id
        stale_date = date.today() - timedelta(days=120)
        db_session.add(_ready_item(user_id, type="shirt", wear_count=0))
        db_session.add(_ready_item(user_id, type="pants", wear_count=0))
        db_session.add(_ready_item(user_id, type="shoes", wear_count=2, last_worn_at=stale_date))
        await db_session.commit()

        result = await GapAnalysisService(db_session).analyze(user_id)

        underused = _by_category(result, "underused")
        assert len(underused) == 1
        ev = underused[0].evidence
        assert ev["never_worn_count"] == 2
        assert ev["stale_count"] == 1
        assert ev["stale_threshold_days"] == 90
        assert len(ev["sample"]) >= 1


class TestDegradation:
    """No-history degradation must never crash and must signal low confidence."""

    @pytest.mark.asyncio
    async def test_no_items_returns_no_data(self, db_session, test_user):
        result = await GapAnalysisService(db_session).analyze(test_user.id)

        assert result.has_sufficient_data is False
        assert len(result.suggestions) == 1
        assert result.suggestions[0].category == "no_data"
        assert result.summary["ready_items"] == 0

    @pytest.mark.asyncio
    async def test_no_learning_falls_back_to_static(self, db_session, test_user):
        user_id = test_user.id
        for _ in range(6):
            db_session.add(_ready_item(user_id, type="shirt"))
        db_session.add(_ready_item(user_id, type="pants"))
        await db_session.commit()

        result = await GapAnalysisService(db_session).analyze(user_id)

        assert result.has_sufficient_data is False
        assert result.suggestions  # static detectors still produce output
        type_shortages = _by_category(result, "type_shortage")
        assert type_shortages
        for s in type_shortages:
            assert s.confidence == "low"
            assert s.evidence["note"] == "limited feedback data"

    @pytest.mark.asyncio
    async def test_profile_exists_but_not_computed(self, db_session, test_user):
        user_id = test_user.id
        for _ in range(6):
            db_session.add(_ready_item(user_id, type="shirt"))
        db_session.add(_ready_item(user_id, type="pants"))
        db_session.add(
            UserLearningProfile(
                user_id=user_id,
                learned_color_scores={},
                learned_style_scores={},
                learned_occasion_patterns={},
                feedback_count=0,
                last_computed_at=None,
            )
        )
        await db_session.commit()

        result = await GapAnalysisService(db_session).analyze(user_id)

        # Profile present but never computed -> treated as no-learning.
        assert result.has_sufficient_data is False
        assert result.suggestions
        for s in _by_category(result, "type_shortage"):
            assert s.confidence == "low"

    @pytest.mark.asyncio
    async def test_color_avoid_used_without_learning(self, db_session, test_user):
        """Explicit ``color_avoid`` is honoured even with no learning profile."""
        user_id = test_user.id
        for _ in range(5):
            db_session.add(_ready_item(user_id, type="shirt", primary_color="orange"))
        for _ in range(2):
            db_session.add(_ready_item(user_id, type="shirt", primary_color="blue"))
        db_session.add(UserPreference(user_id=user_id, color_avoid=["orange"]))
        await db_session.commit()

        result = await GapAnalysisService(db_session).analyze(user_id)

        overstock = {s.evidence["color"]: s for s in _by_category(result, "color_overstock")}
        assert "orange" in overstock
        assert overstock["orange"].evidence["in_avoid_list"] is True
        assert overstock["orange"].severity == "high"


class TestConsistencyWithAnalytics:
    """Gap evidence must reconcile with the analytics distribution/wear numbers."""

    @pytest.mark.asyncio
    async def test_gaps_endpoint_empty_wardrobe(self, client, test_user, auth_headers):
        response = await client.get("/api/v1/analytics/gaps", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["has_sufficient_data"] is False
        assert data["suggestions"][0]["category"] == "no_data"

    @pytest.mark.asyncio
    async def test_gaps_consistent_with_analytics(
        self, client, db_session, test_user, auth_headers
    ):
        user_id = test_user.id
        # Deterministic wardrobe: <10 colors and <5 never-worn so neither the
        # color top-10 nor the never_worn top-5 list is truncated.
        for _ in range(4):
            db_session.add(_ready_item(user_id, type="shirt", primary_color="red"))  # never worn
        db_session.add(
            _ready_item(
                user_id,
                type="pants",
                primary_color="blue",
                wear_count=3,
                last_worn_at=date.today() - timedelta(days=2),
            )
        )
        db_session.add(
            _ready_item(
                user_id,
                type="shoes",
                primary_color="black",
                wear_count=5,
                last_worn_at=date.today() - timedelta(days=2),
            )
        )
        await db_session.commit()

        analytics = (await client.get("/api/v1/analytics", headers=auth_headers)).json()
        gaps = (await client.get("/api/v1/analytics/gaps", headers=auth_headers)).json()

        # 1) Overstock color count/percentage match analytics color_distribution.
        color_dist = {c["color"]: c for c in analytics["color_distribution"]}
        overstock = [s for s in gaps["suggestions"] if s["category"] == "color_overstock"]
        assert overstock, "expected red to be over-stocked"
        for s in overstock:
            ev = s["evidence"]
            cd = color_dist[ev["color"]]
            assert ev["count"] == cd["count"]
            assert ev["percentage"] == cd["percentage"]

        # 2) never-worn count in underused evidence == analytics never_worn list.
        underused = [s for s in gaps["suggestions"] if s["category"] == "underused"]
        assert underused
        assert underused[0]["evidence"]["never_worn_count"] == len(analytics["never_worn"])

        # 3) type_shortage role counts reconcile with analytics type_distribution.
        role_totals: dict[str, int] = defaultdict(int)
        for t in analytics["type_distribution"]:
            role = ITEM_ROLE.get(t["type"])
            if role:
                role_totals[role] += t["count"]
        shortages = [s for s in gaps["suggestions"] if s["category"] == "type_shortage"]
        assert shortages
        for s in shortages:
            rc = s["evidence"]["role_counts"]
            assert rc["base_top"] == role_totals.get("base_top", 0)
            assert rc["bottom"] == role_totals.get("bottom", 0)
            assert rc["footwear"] == role_totals.get("footwear", 0)
