"""Provider-neutral models and helpers shared by both backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from homeassistant.components.calendar import CalendarEvent
from homeassistant.util import dt as dt_util


class ProviderError(Exception):
    """A request to the calendar backend failed."""


class ProviderAuthError(ProviderError):
    """The credentials were rejected by the backend."""


@dataclass(frozen=True)
class CalendarInfo:
    """A calendar offered by an account."""

    id: str
    name: str
    writable: bool = True


@dataclass
class EventData:
    """Event fields as Home Assistant hands them to an entity.

    `start`/`end` are both `date` (all-day) or both aware `datetime`.
    """

    summary: str
    start: date | datetime
    end: date | datetime
    description: str | None = None
    location: str | None = None
    rrule: str | None = None

    @property
    def all_day(self) -> bool:
        return not isinstance(self.start, datetime)

    @classmethod
    def from_ha(cls, event: dict[str, Any]) -> EventData:
        """Build from the dict passed to async_create_event/async_update_event."""
        start = event["dtstart"]
        end = event["dtend"]
        if isinstance(start, datetime) and start.tzinfo is None:
            start = start.replace(tzinfo=dt_util.get_default_time_zone())
        if isinstance(end, datetime) and end.tzinfo is None:
            end = end.replace(tzinfo=dt_util.get_default_time_zone())
        return cls(
            summary=event.get("summary") or "",
            start=start,
            end=end,
            description=event.get("description") or None,
            location=event.get("location") or None,
            rrule=event.get("rrule") or None,
        )


class CalendarProvider(ABC):
    """Common interface for Google and CalDAV."""

    @abstractmethod
    async def async_list_calendars(self) -> list[CalendarInfo]:
        """Return all calendars of the account."""

    @abstractmethod
    async def async_get_events(
        self, calendar_id: str, start: datetime, end: datetime
    ) -> list[CalendarEvent]:
        """Return expanded events overlapping [start, end)."""

    @abstractmethod
    async def async_create_event(self, calendar_id: str, event: EventData) -> None:
        """Create a new event."""

    @abstractmethod
    async def async_update_event(
        self,
        calendar_id: str,
        uid: str,
        event: EventData,
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        """Update an event, one occurrence, or an occurrence and all after it."""

    @abstractmethod
    async def async_delete_event(
        self,
        calendar_id: str,
        uid: str,
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        """Delete an event, one occurrence, or an occurrence and all after it."""


# --- recurrence-id helpers -------------------------------------------------


def format_recurrence_id(value: date | datetime) -> str:
    """Encode an occurrence's original start as an RFC 5545 value string."""
    if not isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    if value.tzinfo is None:
        return value.strftime("%Y%m%dT%H%M%S")
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def parse_recurrence_id(value: str) -> date | datetime:
    """Inverse of format_recurrence_id."""
    value = value.strip()
    if len(value) == 8:
        return datetime.strptime(value, "%Y%m%d").date()
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    return datetime.strptime(value, "%Y%m%dT%H%M%S")


def same_instant(a: date | datetime, b: date | datetime) -> bool:
    """Compare two occurrence starts that may differ in type or zone."""
    a_dt = isinstance(a, datetime)
    b_dt = isinstance(b, datetime)
    if not a_dt and not b_dt:
        return a == b
    if a_dt != b_dt:
        a_date = a.date() if a_dt else a
        b_date = b.date() if b_dt else b
        return a_date == b_date
    if (a.tzinfo is None) != (b.tzinfo is None):
        tz = dt_util.get_default_time_zone()
        a = a if a.tzinfo else a.replace(tzinfo=tz)
        b = b if b.tzinfo else b.replace(tzinfo=tz)
    return a == b


def until_before(start: date | datetime) -> date | datetime:
    """UNTIL value that ends a series just before the given occurrence."""
    if not isinstance(start, datetime):
        return start - timedelta(days=1)
    if start.tzinfo is None:
        start = start.replace(tzinfo=dt_util.get_default_time_zone())
    return (start - timedelta(seconds=1)).astimezone(timezone.utc)


def rrule_with_until(rrule: str, until: date | datetime) -> str:
    """Replace COUNT/UNTIL in an RRULE string with the given UNTIL."""
    parts = [
        p
        for p in rrule.removeprefix("RRULE:").split(";")
        if p and not p.upper().startswith(("COUNT=", "UNTIL="))
    ]
    parts.append(f"UNTIL={format_recurrence_id(until)}")
    return ";".join(parts)


def normalize_event_times(
    start: date | datetime, end: date | datetime | None
) -> tuple[date | datetime, date | datetime]:
    """Make start/end valid for a Home Assistant CalendarEvent."""
    tz = dt_util.get_default_time_zone()
    if isinstance(start, datetime):
        if start.tzinfo is None:
            start = start.replace(tzinfo=tz)
        if end is None or not isinstance(end, datetime):
            end = start
        elif end.tzinfo is None:
            end = end.replace(tzinfo=tz)
        if end < start:
            end = start
        return dt_util.as_local(start), dt_util.as_local(end)
    if end is None or isinstance(end, datetime):
        end = start + timedelta(days=1)
    if end <= start:
        end = start + timedelta(days=1)
    return start, end
