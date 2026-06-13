"""Weekly (7-day) outfit planning endpoints.

Read-only ``GET /plan`` returns the expanded week (no AI, no writes), built by
:class:`~app.services.plan_service.PlanService`. ``POST /plan/generate``
produces a single outfit for one day, reusing
:meth:`RecommendationService.generate_recommendation` (the same engine as
Suggest and the notification worker) so planning never forks the recommendation
logic.

``OutfitResponse`` / ``outfit_to_response`` are imported from
:mod:`app.api.outfits`; that module does not import this one, so there is no
import cycle.
"""

import calendar
import logging
from datetime import date, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.outfits import VALID_OCCASIONS, OutfitResponse, outfit_to_response
from app.database import get_db
from app.models.outfit import OutfitSource
from app.models.schedule import Schedule
from app.models.user import User
from app.services.plan_service import DayPlan, PlanService, WeekPlan
from app.services.recommendation_service import (
    AIRecommendationError,
    InsufficientWardrobeError,
    RecommendationService,
)
from app.services.weather_service import (
    WeatherService,
    WeatherServiceError,
    daily_forecast_to_weather,
)
from app.utils.auth import get_current_user
from app.utils.rate_limit import rate_limit_by_user
from app.utils.timezone import get_user_today

logger = logging.getLogger(__name__)

# Upper bound for both the GET window and how far ahead a day may be generated.
MAX_PLAN_DAYS = 14

router = APIRouter(prefix="/plan", tags=["Plan"])


class DailyForecastResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    date: str
    temp_min: float
    temp_max: float
    precipitation_chance: int
    condition: str
    condition_code: int


class RiskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str
    severity: str
    message: str


class PlanSlotResponse(BaseModel):
    occasion: str
    kind: Literal["fixed", "temporary"]
    notify_day_before: bool
    reminder_at: str | None = None
    outfit: OutfitResponse | None = None


class DayPlanResponse(BaseModel):
    date: date
    weekday: int
    weekday_name: str
    is_today: bool
    is_fixed: bool
    weather: DailyForecastResponse | None = None
    slots: list[PlanSlotResponse]
    risks: list[RiskResponse]


class WeekPlanResponse(BaseModel):
    start_date: date
    days: list[DayPlanResponse]
    location_set: bool
    location_name: str | None = None
    weather_available: bool
    available_item_count: int
    needs_wash_count: int


class GeneratePlanRequest(BaseModel):
    date: date
    occasion: str

    @field_validator("occasion")
    @classmethod
    def validate_occasion(cls, v: str) -> str:
        v = v.strip().lower()
        if not v:
            raise ValueError("Occasion is required")
        if len(v) > 50:
            raise ValueError("Occasion must be 50 characters or less")
        if v not in VALID_OCCASIONS:
            raise ValueError(
                f"Invalid occasion '{v}'. Must be one of: {', '.join(sorted(VALID_OCCASIONS))}"
            )
        return v


def _day_to_response(day: DayPlan) -> DayPlanResponse:
    return DayPlanResponse(
        date=day.date,
        weekday=day.weekday,
        weekday_name=calendar.day_name[day.weekday],
        is_today=day.is_today,
        is_fixed=day.is_fixed,
        weather=(
            DailyForecastResponse.model_validate(day.weather) if day.weather else None
        ),
        slots=[
            PlanSlotResponse(
                occasion=slot.occasion,
                kind=slot.kind,
                notify_day_before=slot.notify_day_before,
                reminder_at=slot.reminder_at.isoformat() if slot.reminder_at else None,
                outfit=outfit_to_response(slot.outfit) if slot.outfit else None,
            )
            for slot in day.slots
        ],
        risks=[RiskResponse.model_validate(risk) for risk in day.risks],
    )


def _plan_to_response(plan: WeekPlan) -> WeekPlanResponse:
    return WeekPlanResponse(
        start_date=plan.start_date,
        days=[_day_to_response(day) for day in plan.days],
        location_set=plan.location_set,
        location_name=plan.location_name,
        weather_available=plan.weather_available,
        available_item_count=plan.available_item_count,
        needs_wash_count=plan.needs_wash_count,
    )


@router.get("", response_model=WeekPlanResponse)
async def get_plan(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    days: Annotated[int, Query(ge=1, le=MAX_PLAN_DAYS)] = 7,
) -> WeekPlanResponse:
    """Return the expanded plan for the next ``days`` days. No AI, no writes."""
    plan = await PlanService(db).build_plan(current_user, days=days)
    return _plan_to_response(plan)


async def _is_schedule_backed(
    db: AsyncSession, user: User, target: date, occasion: str
) -> bool:
    """Whether an enabled schedule covers ``occasion`` on ``target``'s weekday."""
    result = await db.execute(
        select(Schedule.id).where(
            and_(
                Schedule.user_id == user.id,
                Schedule.enabled.is_(True),
                Schedule.day_of_week == target.weekday(),
                Schedule.occasion == occasion,
            )
        )
    )
    return result.first() is not None


async def _weather_override_for(user: User, target: date):
    """Daily-forecast-derived weather for a future ``target`` date, or ``None``.

    Today's date uses live current weather (``None`` -> generator fetches it),
    matching the Suggest flow. Future dates use the daily forecast so the outfit
    reflects that day, not today.
    """
    if user.location_lat is None or user.location_lon is None:
        return None
    try:
        forecasts = await WeatherService().get_daily_forecast(
            float(user.location_lat), float(user.location_lon), days=MAX_PLAN_DAYS
        )
    except WeatherServiceError:
        return None
    for forecast in forecasts:
        if forecast.date == target.isoformat():
            return daily_forecast_to_weather(forecast)
    return None


@router.post("/generate", response_model=OutfitResponse)
async def generate_plan_outfit(
    request: GeneratePlanRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> OutfitResponse:
    """Generate a single outfit for one planned day."""
    await rate_limit_by_user(
        str(current_user.id), "plan_generate", max_requests=10, window_seconds=60
    )

    today = get_user_today(current_user)
    last_allowed = today + timedelta(days=MAX_PLAN_DAYS - 1)
    if request.date < today or request.date > last_allowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Date must be between {today.isoformat()} and "
                f"{last_allowed.isoformat()}."
            ),
        )

    schedule_backed = await _is_schedule_backed(
        db, current_user, request.date, request.occasion
    )
    source = OutfitSource.scheduled if schedule_backed else OutfitSource.on_demand

    weather_override = None
    if request.date != today:
        weather_override = await _weather_override_for(current_user, request.date)

    service = RecommendationService(db)
    try:
        outfit = await service.generate_recommendation(
            user=current_user,
            occasion=request.occasion,
            weather_override=weather_override,
            source=source,
            single_outfit=True,
            scheduled_date=request.date,
        )
    except InsufficientWardrobeError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from None
    except AIRecommendationError as e:
        logger.error(f"AI recommendation error: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(e),
        ) from None
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from None

    return outfit_to_response(outfit)
