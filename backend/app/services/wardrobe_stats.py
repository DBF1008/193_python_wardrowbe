"""Shared wardrobe statistics.

Single source of truth for the type/color distribution computations used by both
the analytics endpoint (``app/api/analytics.py``) and the gap-analysis service
(``app/services/gap_analysis_service.py``). Keeping one implementation guarantees
that gap-analysis evidence stays consistent with the numbers reported by
``GET /analytics``.
"""

from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.item import ClothingItem, ItemStatus


class ColorDistribution(BaseModel):
    color: str
    count: int
    percentage: float


class TypeDistribution(BaseModel):
    type: str
    count: int
    percentage: float


def _percentage(count: int, ready_count: int) -> float:
    return round(count / ready_count * 100, 1) if ready_count > 0 else 0


async def compute_color_distribution(
    db: AsyncSession, user_id: UUID, ready_count: int
) -> list[ColorDistribution]:
    """Top-10 primary colors among ready items, with share of the ready wardrobe."""
    color_query = (
        select(
            ClothingItem.primary_color,
            func.count(ClothingItem.id).label("count"),
        )
        .where(
            and_(
                ClothingItem.user_id == user_id,
                ClothingItem.primary_color.isnot(None),
                ClothingItem.status == ItemStatus.ready,
            )
        )
        .group_by(ClothingItem.primary_color)
        .order_by(func.count(ClothingItem.id).desc())
        .limit(10)
    )
    result = await db.execute(color_query)
    return [
        ColorDistribution(
            color=row.primary_color,
            count=row.count,
            percentage=_percentage(row.count, ready_count),
        )
        for row in result.all()
    ]


async def compute_type_distribution(
    db: AsyncSession, user_id: UUID, ready_count: int
) -> list[TypeDistribution]:
    """Distribution of item types among ready items, with share of the ready wardrobe."""
    type_query = (
        select(
            ClothingItem.type,
            func.count(ClothingItem.id).label("count"),
        )
        .where(
            and_(
                ClothingItem.user_id == user_id,
                ClothingItem.status == ItemStatus.ready,
            )
        )
        .group_by(ClothingItem.type)
        .order_by(func.count(ClothingItem.id).desc())
    )
    result = await db.execute(type_query)
    return [
        TypeDistribution(
            type=row.type,
            count=row.count,
            percentage=_percentage(row.count, ready_count),
        )
        for row in result.all()
    ]
