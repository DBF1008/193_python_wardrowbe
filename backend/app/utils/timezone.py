from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from app.models import User


def get_user_timezone(user: User) -> ZoneInfo:
    try:
        return ZoneInfo(user.timezone or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def get_user_today(user: User) -> date:
    user_tz = get_user_timezone(user)
    return datetime.now(UTC).astimezone(user_tz).date()


def get_user_now(user: User) -> datetime:
    user_tz = get_user_timezone(user)
    return datetime.now(UTC).astimezone(user_tz)
