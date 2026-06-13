"""Read-only 7-day outfit planning.

:class:`PlanService` assembles a forward-looking view of the upcoming days
*without* running the AI or writing anything. It deliberately reuses the same
building blocks the rest of the app relies on so the plan can never drift from
what the scheduler / Suggest flow actually does:

- schedule weekday + ``notify_day_before`` -> wear date / reminder time comes
  from :mod:`app.services.schedule_planner` (shared with the notification
  worker),
- "available items" counting uses
  :meth:`RecommendationService.get_available_items` (the same filter that feeds
  recommendation generation),
- per-day weather uses :meth:`WeatherService.get_daily_forecast`, and
- existing outfits for each day are looked up via
  :meth:`OutfitService.list_with_filters`.

Outfit *generation* for a day is intentionally not done here — it is an explicit
on-demand action in the API layer.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.item import ClothingItem
from app.models.outfit import Outfit
from app.models.schedule import Schedule
from app.models.user import User
from app.services.outfit_service import OutfitListFilters, OutfitService
from app.services.recommendation_service import RecommendationService
from app.services.schedule_planner import upcoming_schedule_map
from app.services.weather_service import (
    DailyForecast,
    WeatherService,
    WeatherServiceError,
)
from app.utils.timezone import get_user_timezone, get_user_today

# Outfits in these statuses are treated as "the plan for that day". Rejected /
# skipped / expired outfits are ignored so a discarded suggestion does not keep
# filling its slot.
PLAN_OUTFIT_STATUSES = "pending,sent,viewed,accepted"

DEFAULT_OCCASION = "casual"

# Fewer than this many available items cannot form an outfit (mirrors the
# InsufficientWardrobeError threshold in generate_recommendation).
MIN_ITEMS_FOR_OUTFIT = 2


@dataclass
class Risk:
    """A surfaced planning problem for a given day."""

    code: str  # location_not_set | weather_unavailable | insufficient_items | needs_wash
    severity: str  # error | warning | info
    message: str


@dataclass
class PlanSlot:
    """One occasion to dress for on a day.

    ``kind`` is ``"fixed"`` when backed by an enabled :class:`Schedule` and
    ``"temporary"`` when it is the auto-filled default occasion.
    """

    occasion: str
    kind: str  # "fixed" | "temporary"
    notify_day_before: bool
    reminder_at: datetime | None
    outfit: Outfit | None


@dataclass
class DayPlan:
    date: date
    weekday: int  # 0=Monday
    is_today: bool
    is_fixed: bool
    weather: DailyForecast | None
    slots: list[PlanSlot]
    risks: list[Risk]


@dataclass
class WeekPlan:
    start_date: date
    days: list[DayPlan]
    location_set: bool
    location_name: str | None
    weather_available: bool
    available_item_count: int
    needs_wash_count: int


class PlanService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def _load_schedules(self, user: User) -> list[Schedule]:
        result = await self.db.execute(
            select(Schedule).where(Schedule.user_id == user.id)
        )
        return list(result.scalars().all())

    async def _forecast_by_date(
        self, user: User, days: int
    ) -> tuple[bool, dict[date, DailyForecast]]:
        """Return ``(weather_available, {date: forecast})``.

        ``weather_available`` is ``False`` when the location is unset or the
        forecast request fails; in that case the map is empty.
        """
        if user.location_lat is None or user.location_lon is None:
            return False, {}
        try:
            forecasts = await WeatherService().get_daily_forecast(
                float(user.location_lat), float(user.location_lon), days=days
            )
        except WeatherServiceError:
            return False, {}

        by_date: dict[date, DailyForecast] = {}
        for forecast in forecasts:
            try:
                parsed = date.fromisoformat(forecast.date)
            except ValueError:
                continue
            by_date[parsed] = forecast
        return True, by_date

    async def _needs_wash_count(self, user: User) -> int:
        result = await self.db.execute(
            select(func.count())
            .select_from(ClothingItem)
            .where(
                and_(
                    ClothingItem.user_id == user.id,
                    ClothingItem.needs_wash.is_(True),
                    ClothingItem.is_archived.is_(False),
                )
            )
        )
        return int(result.scalar_one())

    async def _outfits_by_slot(
        self, user: User, start: date, end: date
    ) -> dict[tuple[date, str], Outfit]:
        """Existing plan outfits keyed by ``(scheduled_for, occasion)``.

        Results are ordered newest-first by the underlying query, so the first
        outfit seen for a slot is the most recent one and later (older)
        duplicates are skipped.
        """
        filters = OutfitListFilters(
            user_id=user.id,
            status_filter=PLAN_OUTFIT_STATUSES,
            date_from=start,
            date_to=end,
        )
        outfits, _ = await OutfitService(self.db).list_with_filters(
            filters, page=1, page_size=200
        )
        by_slot: dict[tuple[date, str], Outfit] = {}
        for outfit in outfits:
            if outfit.scheduled_for is None:
                continue
            key = (outfit.scheduled_for, outfit.occasion)
            by_slot.setdefault(key, outfit)
        return by_slot

    def _build_risks(
        self,
        *,
        location_set: bool,
        weather: DailyForecast | None,
        available_count: int,
        needs_wash_count: int,
    ) -> list[Risk]:
        risks: list[Risk] = []
        if not location_set:
            risks.append(
                Risk(
                    code="location_not_set",
                    severity="error",
                    message=(
                        "Set your location in settings to get weather-based "
                        "recommendations."
                    ),
                )
            )
        elif weather is None:
            risks.append(
                Risk(
                    code="weather_unavailable",
                    severity="warning",
                    message="Weather forecast is not available for this day.",
                )
            )
        if available_count < MIN_ITEMS_FOR_OUTFIT:
            risks.append(
                Risk(
                    code="insufficient_items",
                    severity="error",
                    message=(
                        "Not enough available items to build an outfit. "
                        "Add more items to your wardrobe."
                    ),
                )
            )
        if needs_wash_count > 0:
            risks.append(
                Risk(
                    code="needs_wash",
                    severity="info",
                    message=f"{needs_wash_count} item(s) need a wash.",
                )
            )
        return risks

    async def build_plan(self, user: User, *, days: int = 7) -> WeekPlan:
        today = get_user_today(user)
        tz = get_user_timezone(user)
        end = today + timedelta(days=days - 1)

        schedules = await self._load_schedules(user)
        occ_map = upcoming_schedule_map(schedules, today=today, tz=tz, days=days)

        location_set = user.location_lat is not None and user.location_lon is not None
        weather_available, forecast_by_date = await self._forecast_by_date(user, days)

        available_items = await RecommendationService(self.db).get_available_items(user)
        available_count = len(available_items)
        needs_wash_count = await self._needs_wash_count(user)

        outfit_by_slot = await self._outfits_by_slot(user, today, end)

        default_occasion = DEFAULT_OCCASION
        if user.preferences and user.preferences.default_occasion:
            default_occasion = user.preferences.default_occasion

        day_plans: list[DayPlan] = []
        for offset in range(days):
            current = today + timedelta(days=offset)
            weather = forecast_by_date.get(current)
            occurrences = occ_map.get(current, [])

            slots: list[PlanSlot] = []
            if occurrences:
                for occ in occurrences:
                    occasion = occ.schedule.occasion
                    slots.append(
                        PlanSlot(
                            occasion=occasion,
                            kind="fixed",
                            notify_day_before=occ.notify_day_before,
                            reminder_at=occ.reminder_at,
                            outfit=outfit_by_slot.get((current, occasion)),
                        )
                    )
                is_fixed = True
            else:
                slots.append(
                    PlanSlot(
                        occasion=default_occasion,
                        kind="temporary",
                        notify_day_before=False,
                        reminder_at=None,
                        outfit=outfit_by_slot.get((current, default_occasion)),
                    )
                )
                is_fixed = False

            day_plans.append(
                DayPlan(
                    date=current,
                    weekday=current.weekday(),
                    is_today=current == today,
                    is_fixed=is_fixed,
                    weather=weather,
                    slots=slots,
                    risks=self._build_risks(
                        location_set=location_set,
                        weather=weather,
                        available_count=available_count,
                        needs_wash_count=needs_wash_count,
                    ),
                )
            )

        return WeekPlan(
            start_date=today,
            days=day_plans,
            location_set=location_set,
            location_name=user.location_name,
            weather_available=weather_available,
            available_item_count=available_count,
            needs_wash_count=needs_wash_count,
        )
