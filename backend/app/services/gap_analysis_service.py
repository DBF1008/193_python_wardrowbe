"""Wardrobe gap analysis.

Turns the existing analytics statistics (type/color distribution, wear data) into
structured, evidence-backed restocking advice. Crucially this is *not* static
counting: every learning-dependent detector folds in real signals derived from
feedback —

- ``UserLearningProfile.learned_color_scores`` (which colors the user actually likes),
- ``UserLearningProfile.learned_occasion_patterns[occ].success_rate`` (how well an
  occasion performs),
- ``ItemPairScore`` compatibility / ``occasion_performance`` (which pairs reliably work),
- per-item ``wear_count`` / ``last_worn_at`` (what actually gets worn),
- ``UserPreference.color_avoid`` (explicit user preference),

so the suggestions change as the user's behaviour and preferences change. When that
feedback is absent the service degrades gracefully to structural-only suggestions
flagged with low confidence.
"""

from collections import defaultdict
from datetime import date
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.item import ClothingItem, ItemStatus
from app.models.learning import ItemPairScore, UserLearningProfile
from app.models.outfit import Outfit
from app.models.preference import UserPreference
from app.services.wardrobe_stats import (
    compute_color_distribution,
    compute_type_distribution,
)
from app.utils.clothing import ITEM_ROLE
from app.utils.signed_urls import sign_image_url


class GapSuggestion(BaseModel):
    """A single restocking recommendation backed by structured evidence."""

    category: str  # type_shortage | color_overstock | occasion_gap | underused | no_data
    severity: str  # high | medium | low
    confidence: str  # high | medium | low — reflects how much feedback backs this
    title: str
    detail: str
    evidence: dict
    suggested_action: str


class GapAnalysisResult(BaseModel):
    has_sufficient_data: bool
    summary: dict
    suggestions: list[GapSuggestion]


_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}
_CATEGORY_RANK = {
    "type_shortage": 0,
    "occasion_gap": 1,
    "color_overstock": 2,
    "underused": 3,
    "no_data": 4,
}

# Roles that an everyday outfit needs; a full-body item (dress/jumpsuit) covers both
# the top and bottom slots.
_CORE_ROLE_LABELS = {"base_top": "tops", "bottom": "bottoms", "footwear": "shoes"}
_LIMITED_DATA_NOTE = "limited feedback data"


class GapAnalysisService:
    # A single colour occupying this share of the ready wardrobe is "over-stocked"...
    OVERSTOCK_SHARE = 35.0
    # ...but only once there are enough items to make the share meaningful.
    OVERSTOCK_MIN_COUNT = 3
    # A core role with fewer than this fraction of the dominant role is "short".
    SHORTAGE_RATIO = 0.34
    # If fewer than this fraction of a colour's items have ever been worn, usage is low.
    LOW_WORN_RATIO = 0.34
    # Items not worn in this many days count as "stale".
    UNDERUSED_STALE_DAYS = 90
    # An occasion needs at least this many positively-scored pairs to be "reliable".
    MIN_RELIABLE_PAIRS = 2
    # Occasions with a learned success rate below this are flagged.
    LOW_SUCCESS_RATE = 0.4

    def __init__(self, db: AsyncSession):
        self.db = db

    async def analyze(self, user_id: UUID) -> GapAnalysisResult:
        ready_items = list(
            (
                await self.db.execute(
                    select(ClothingItem).where(
                        ClothingItem.user_id == user_id,
                        ClothingItem.status == ItemStatus.ready,
                    )
                )
            )
            .scalars()
            .all()
        )
        ready_count = len(ready_items)

        if ready_count == 0:
            return GapAnalysisResult(
                has_sufficient_data=False,
                summary={"ready_items": 0, "feedback_count": 0, "has_learning": False},
                suggestions=[
                    GapSuggestion(
                        category="no_data",
                        severity="low",
                        confidence="low",
                        title="Add items to your wardrobe",
                        detail=(
                            "There are no ready-to-wear items yet, so there is nothing to "
                            "analyse. Add a few pieces to unlock gap analysis."
                        ),
                        evidence={"ready_items": 0},
                        suggested_action="Add a few tops, bottoms, and shoes to get started.",
                    )
                ],
            )

        color_distribution = await compute_color_distribution(self.db, user_id, ready_count)
        type_distribution = await compute_type_distribution(self.db, user_id, ready_count)

        profile = (
            await self.db.execute(
                select(UserLearningProfile).where(UserLearningProfile.user_id == user_id)
            )
        ).scalar_one_or_none()
        prefs = (
            await self.db.execute(
                select(UserPreference).where(UserPreference.user_id == user_id)
            )
        ).scalar_one_or_none()
        pair_scores = list(
            (
                await self.db.execute(
                    select(ItemPairScore).where(
                        ItemPairScore.user_id == user_id,
                        ItemPairScore.compatibility_score > 0,
                    )
                )
            )
            .scalars()
            .all()
        )
        used_occasions = await self._used_occasions(user_id, profile, prefs)

        has_learning = bool(
            profile and profile.last_computed_at and (profile.feedback_count or 0) > 0
        )

        suggestions: list[GapSuggestion] = []
        suggestions += self._detect_type_shortage(ready_items, profile, has_learning)
        suggestions += self._detect_color_overstock(
            ready_items, color_distribution, profile, prefs, has_learning
        )
        suggestions += self._detect_occasion_gap(
            ready_count, used_occasions, pair_scores, profile, has_learning
        )
        suggestions += self._detect_underused(ready_items)

        suggestions.sort(
            key=lambda s: (
                _SEVERITY_RANK.get(s.severity, 9),
                _CATEGORY_RANK.get(s.category, 9),
                s.title,
            )
        )

        return GapAnalysisResult(
            has_sufficient_data=has_learning,
            summary={
                "ready_items": ready_count,
                "feedback_count": (profile.feedback_count or 0) if profile else 0,
                "has_learning": has_learning,
                "distinct_colors": len(color_distribution),
                "distinct_types": len(type_distribution),
                "suggestion_count": len(suggestions),
            },
            suggestions=suggestions,
        )

    async def _used_occasions(
        self,
        user_id: UUID,
        profile: UserLearningProfile | None,
        prefs: UserPreference | None,
    ) -> set[str]:
        """Occasions the user actually engages with (worn, learned, or configured)."""
        occasions: set[str] = set()
        rows = await self.db.execute(
            select(Outfit.occasion).where(Outfit.user_id == user_id).distinct()
        )
        occasions.update(o for (o,) in rows.all() if o)
        if profile and profile.learned_occasion_patterns:
            occasions.update(profile.learned_occasion_patterns.keys())
        if prefs:
            if prefs.occasion_preferences:
                occasions.update(prefs.occasion_preferences.keys())
            if prefs.default_occasion:
                occasions.add(prefs.default_occasion)
        return occasions

    def _detect_type_shortage(
        self,
        ready_items: list[ClothingItem],
        profile: UserLearningProfile | None,
        has_learning: bool,
    ) -> list[GapSuggestion]:
        role_counts: dict[str, int] = defaultdict(int)
        for item in ready_items:
            role = ITEM_ROLE.get(item.type)
            if role:
                role_counts[role] += 1

        full_body = role_counts.get("full_body", 0)
        coverage = {
            "base_top": role_counts.get("base_top", 0) + full_body,
            "bottom": role_counts.get("bottom", 0) + full_body,
            "footwear": role_counts.get("footwear", 0),
        }
        max_cov = max(coverage.values())

        acceptance = (
            float(profile.overall_acceptance_rate)
            if profile and profile.overall_acceptance_rate is not None
            else None
        )
        confidence = "high" if has_learning else "low"

        suggestions: list[GapSuggestion] = []
        for role, cov in coverage.items():
            label = _CORE_ROLE_LABELS[role]
            if cov == 0:
                severity = "high"
                detail = (
                    f"You have no {label} ready to wear. Without this slot most outfits "
                    f"cannot be completed."
                )
                action = f"Add at least one or two versatile {label}."
            elif cov < self.SHORTAGE_RATIO * max_cov:
                severity = "medium"
                detail = (
                    f"You have far fewer {label} ({cov}) than your most-stocked slot "
                    f"({max_cov}), which limits how many outfits you can build."
                )
                action = f"Add a couple more {label} to balance your wardrobe."
            else:
                continue

            evidence: dict = {
                "role": role,
                "count": cov,
                "dominant_count": max_cov,
                "role_counts": dict(coverage),
            }
            if acceptance is not None:
                evidence["overall_acceptance_rate"] = round(acceptance, 3)
            if not has_learning:
                evidence["note"] = _LIMITED_DATA_NOTE

            suggestions.append(
                GapSuggestion(
                    category="type_shortage",
                    severity=severity,
                    confidence=confidence,
                    title=f"Short on {label}",
                    detail=detail,
                    evidence=evidence,
                    suggested_action=action,
                )
            )
        return suggestions

    def _detect_color_overstock(
        self,
        ready_items: list[ClothingItem],
        color_distribution: list,
        profile: UserLearningProfile | None,
        prefs: UserPreference | None,
        has_learning: bool,
    ) -> list[GapSuggestion]:
        worn_by_color: dict[str, int] = defaultdict(int)
        for item in ready_items:
            if item.primary_color and (item.wear_count or 0) > 0:
                worn_by_color[item.primary_color] += 1

        learned = (profile.learned_color_scores or {}) if profile else {}
        avoid = set((prefs.color_avoid or []) if prefs else [])

        suggestions: list[GapSuggestion] = []
        for cd in color_distribution:
            if cd.count < self.OVERSTOCK_MIN_COUNT or cd.percentage < self.OVERSTOCK_SHARE:
                continue

            worn_ratio = round(worn_by_color.get(cd.color, 0) / cd.count, 3)
            learned_score = learned.get(cd.color)
            in_avoid = cd.color in avoid

            # A colour you genuinely like *and* actually wear is a justified staple,
            # not a gap — suppress it. This is the clearest "non-static" behaviour:
            # the same count can be flagged or not depending on learned feedback.
            if learned_score is not None and learned_score > 0.2 and worn_ratio >= 0.5:
                continue

            disliked = (learned_score is not None and learned_score <= 0) or in_avoid
            low_usage = worn_ratio < self.LOW_WORN_RATIO
            severity = "high" if (disliked or low_usage) else "medium"
            confidence = "high" if learned_score is not None else "low"

            if disliked:
                detail = (
                    f"{cd.color.title()} makes up {cd.percentage}% of your wardrobe "
                    f"({cd.count} items) but your feedback shows little affinity for it."
                )
            elif low_usage:
                detail = (
                    f"{cd.color.title()} makes up {cd.percentage}% of your wardrobe "
                    f"({cd.count} items) yet only {int(worn_ratio * 100)}% of them get worn."
                )
            else:
                detail = (
                    f"{cd.color.title()} already makes up {cd.percentage}% of your "
                    f"wardrobe ({cd.count} items)."
                )

            evidence = {
                "color": cd.color,
                "count": cd.count,
                "percentage": cd.percentage,
                "worn_ratio": worn_ratio,
                "learned_score": learned_score,
                "in_avoid_list": in_avoid,
            }
            if not has_learning:
                evidence["note"] = _LIMITED_DATA_NOTE

            suggestions.append(
                GapSuggestion(
                    category="color_overstock",
                    severity=severity,
                    confidence=confidence,
                    title=f"Over-stocked on {cd.color}",
                    detail=detail,
                    evidence=evidence,
                    suggested_action=(
                        f"Hold off on buying more {cd.color}; prioritise colours that "
                        f"pair with what you already own."
                    ),
                )
            )
        return suggestions

    def _detect_occasion_gap(
        self,
        ready_count: int,
        used_occasions: set[str],
        pair_scores: list[ItemPairScore],
        profile: UserLearningProfile | None,
        has_learning: bool,
    ) -> list[GapSuggestion]:
        if not used_occasions or ready_count == 0:
            return []

        learned_occ = (profile.learned_occasion_patterns or {}) if profile else {}
        confidence = "high" if has_learning else "low"

        suggestions: list[GapSuggestion] = []
        for occ in sorted(used_occasions):
            reliable = 0
            for pair in pair_scores:
                entry = (pair.occasion_performance or {}).get(occ)
                if entry and (entry.get("count") or 0) >= 1:
                    reliable += 1

            success_rate = learned_occ.get(occ, {}).get("success_rate")
            low_success = success_rate is not None and success_rate < self.LOW_SUCCESS_RATE

            if reliable >= self.MIN_RELIABLE_PAIRS and not low_success:
                continue

            severity = "high" if reliable == 0 else "medium"
            if reliable == 0:
                detail = (
                    f"You use the '{occ}' occasion but have no item pairs that reliably "
                    f"work together for it yet."
                )
            elif low_success:
                detail = (
                    f"Outfits for '{occ}' have a low success rate "
                    f"({int(success_rate * 100)}%) and few reliable pairings."
                )
            else:
                detail = (
                    f"You have only {reliable} reliable pairing(s) for the '{occ}' "
                    f"occasion."
                )

            evidence = {
                "occasion": occ,
                "ready_item_count": ready_count,
                "reliable_pair_count": reliable,
                "success_rate": success_rate,
                "min_reliable_pairs": self.MIN_RELIABLE_PAIRS,
            }
            if not has_learning:
                evidence["note"] = _LIMITED_DATA_NOTE

            suggestions.append(
                GapSuggestion(
                    category="occasion_gap",
                    severity=severity,
                    confidence=confidence,
                    title=f"Few reliable outfits for {occ}",
                    detail=detail,
                    evidence=evidence,
                    suggested_action=(
                        f"Add a versatile staple that pairs with several pieces for {occ}."
                    ),
                )
            )
        return suggestions

    def _detect_underused(self, ready_items: list[ClothingItem]) -> list[GapSuggestion]:
        today = date.today()
        never = [item for item in ready_items if (item.wear_count or 0) == 0]
        stale = [
            item
            for item in ready_items
            if (item.wear_count or 0) > 0
            and item.last_worn_at
            and (today - item.last_worn_at).days > self.UNDERUSED_STALE_DAYS
        ]
        if not never and not stale:
            return []

        total = len(ready_items)
        any_worn = any((item.wear_count or 0) > 0 for item in ready_items)
        never_fraction = len(never) / total if total else 0.0

        if not any_worn:
            # Nothing has ever been worn — likely a brand-new wardrobe, not a real gap.
            severity = "low"
            confidence = "medium"
        elif never_fraction > 0.3 or stale:
            severity = "medium"
            confidence = "high"
        else:
            severity = "low"
            confidence = "high"

        sample = [
            {
                "id": str(item.id),
                "type": item.type,
                "name": item.name,
                "primary_color": item.primary_color,
                "thumbnail_url": sign_image_url(item.thumbnail_path)
                if item.thumbnail_path
                else None,
            }
            for item in (never or stale)[:5]
        ]

        return [
            GapSuggestion(
                category="underused",
                severity=severity,
                confidence=confidence,
                title="Underused items to style before buying more",
                detail=(
                    f"{len(never)} item(s) have never been worn and {len(stale)} haven't "
                    f"been worn in over {self.UNDERUSED_STALE_DAYS} days. Style these "
                    f"before adding similar pieces."
                ),
                evidence={
                    "never_worn_count": len(never),
                    "stale_count": len(stale),
                    "stale_threshold_days": self.UNDERUSED_STALE_DAYS,
                    "sample": sample,
                },
                suggested_action="Build outfits around these pieces before buying new ones.",
            )
        ]
