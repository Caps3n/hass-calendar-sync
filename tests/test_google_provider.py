"""Google backend: request shapes for each operation."""

from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest

from homeassistant.core import HomeAssistant

from custom_components.calendar_sync.google_provider import GoogleProvider
from custom_components.calendar_sync.models import EventData, ProviderAuthError

UTC = timezone.utc


class FakeResponse:
    def __init__(self, status: int, data: Any = None) -> None:
        self.status = status
        self._data = data or {}

    async def json(self):
        return self._data

    async def text(self):
        return str(self._data)


class FakeSession:
    """Mimics OAuth2Session.async_request with canned responses."""

    def __init__(self, routes: dict[tuple[str, str], Any]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, Any, Any]] = []

    async def async_ensure_token_valid(self) -> None:
        return None

    async def async_request(self, method, url, params=None, json=None, **kwargs):
        path = url.split("/calendar/v3", 1)[1]
        self.calls.append((method, path, params, json))
        result = self.routes.get((method, path), {})
        if isinstance(result, FakeResponse):
            return result
        return FakeResponse(204 if method == "DELETE" else 200, result)


MASTER = {
    "id": "abc",
    "start": {"dateTime": "2026-11-10T10:00:00+01:00"},
    "end": {"dateTime": "2026-11-10T11:00:00+01:00"},
    "recurrence": ["RRULE:FREQ=DAILY;COUNT=5"],
    "summary": "Standup",
}
INSTANCE_3 = {
    "id": "abc_20261112T090000Z",
    "recurringEventId": "abc",
    "originalStartTime": {"dateTime": "2026-11-12T10:00:00+01:00"},
    "start": {"dateTime": "2026-11-12T10:00:00+01:00"},
    "end": {"dateTime": "2026-11-12T11:00:00+01:00"},
    "summary": "Standup",
}
INSTANCE_1 = {**INSTANCE_3, "id": "abc_20261110T090000Z",
              "originalStartTime": MASTER["start"], "start": MASTER["start"], "end": MASTER["end"]}

EV = "/calendars/primary/events"


@pytest.fixture(autouse=True)
async def tz(hass: HomeAssistant):
    await hass.config.async_set_time_zone("Europe/Berlin")


def _event(**kw):
    start = datetime(2026, 11, 12, 8, tzinfo=UTC)
    base = {"summary": "New", "start": start, "end": start + timedelta(hours=1)}
    base.update(kw)
    return EventData(**base)


async def test_list_calendars_marks_read_only(hass) -> None:
    session = FakeSession({("GET", "/users/me/calendarList"): {"items": [
        {"id": "primary", "summary": "Me", "accessRole": "owner"},
        {"id": "holidays", "summary": "Holidays", "accessRole": "reader"},
    ]}})
    cals = await GoogleProvider(session).async_list_calendars()
    assert [(c.id, c.writable) for c in cals] == [("primary", True), ("holidays", False)]


async def test_get_events_maps_series(hass) -> None:
    session = FakeSession({
        ("GET", EV): {"items": [
            INSTANCE_3,
            {"id": "x1", "start": {"date": "2026-11-20"}, "end": {"date": "2026-11-21"}, "summary": "Off"},
            {"id": "x2", "status": "cancelled"},
        ]},
        ("GET", f"{EV}/abc"): MASTER,
    })
    events = await GoogleProvider(session).async_get_events(
        "primary", datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 12, 1, tzinfo=UTC)
    )
    assert len(events) == 2
    standup, off = events
    assert standup.uid == "abc"
    assert standup.recurrence_id == "abc_20261112T090000Z"
    assert standup.rrule == "FREQ=DAILY;COUNT=5"
    assert off.uid == "x1" and off.recurrence_id is None and off.all_day


async def test_create_timed_and_all_day(hass) -> None:
    session = FakeSession({})
    provider = GoogleProvider(session)
    await provider.async_create_event("primary", _event(rrule="FREQ=WEEKLY"))
    await provider.async_create_event("primary", _event(start=date(2026, 11, 20), end=date(2026, 11, 21)))
    (_, path, _, body1), (_, _, _, body2) = session.calls
    assert path == EV
    assert body1["start"]["timeZone"] == "Europe/Berlin"
    assert body1["recurrence"] == ["RRULE:FREQ=WEEKLY"]
    assert body2["start"] == {"date": "2026-11-20"}


async def test_update_plain_and_single_instance(hass) -> None:
    session = FakeSession({})
    provider = GoogleProvider(session)
    await provider.async_update_event("primary", "x1", _event())
    await provider.async_update_event("primary", "abc", _event(), recurrence_id="abc_20261112T090000Z")
    assert session.calls[0][:2] == ("PATCH", f"{EV}/x1")
    assert session.calls[1][:2] == ("PATCH", f"{EV}/abc_20261112T090000Z")
    assert "recurrence" not in session.calls[1][3]


async def test_update_this_and_future_splits(hass) -> None:
    session = FakeSession({("GET", f"{EV}/abc"): MASTER, ("GET", f"{EV}/abc_20261112T090000Z"): INSTANCE_3})
    await GoogleProvider(session).async_update_event(
        "primary", "abc", _event(), recurrence_id="abc_20261112T090000Z", recurrence_range="THISANDFUTURE"
    )
    writes = [c for c in session.calls if c[0] != "GET"]
    assert writes[0][:2] == ("PATCH", f"{EV}/abc")
    assert writes[0][3] == {"recurrence": ["RRULE:FREQ=DAILY;UNTIL=20261112T085959Z"]}
    assert writes[1][:2] == ("POST", EV)
    assert writes[1][3]["recurrence"] == ["RRULE:FREQ=DAILY;COUNT=5"]


async def test_update_this_and_future_from_first_patches_master(hass) -> None:
    session = FakeSession({("GET", f"{EV}/abc"): MASTER, ("GET", f"{EV}/abc_20261110T090000Z"): INSTANCE_1})
    await GoogleProvider(session).async_update_event(
        "primary", "abc", _event(), recurrence_id="abc_20261110T090000Z", recurrence_range="THISANDFUTURE"
    )
    writes = [c for c in session.calls if c[0] != "GET"]
    assert len(writes) == 1 and writes[0][:2] == ("PATCH", f"{EV}/abc")
    assert "recurrence" not in writes[0][3]


async def test_delete_variants(hass) -> None:
    session = FakeSession({("GET", f"{EV}/abc"): MASTER, ("GET", f"{EV}/abc_20261112T090000Z"): INSTANCE_3})
    provider = GoogleProvider(session)
    await provider.async_delete_event("primary", "x1")
    await provider.async_delete_event("primary", "abc", recurrence_id="abc_20261112T090000Z")
    await provider.async_delete_event(
        "primary", "abc", recurrence_id="abc_20261112T090000Z", recurrence_range="THISANDFUTURE"
    )
    writes = [c[:2] for c in session.calls if c[0] != "GET"]
    assert writes == [
        ("DELETE", f"{EV}/x1"),
        ("DELETE", f"{EV}/abc_20261112T090000Z"),
        ("PATCH", f"{EV}/abc"),
    ]


async def test_unauthorized_raises_auth_error(hass) -> None:
    session = FakeSession({("GET", "/users/me/calendarList"): FakeResponse(401)})
    with pytest.raises(ProviderAuthError):
        await GoogleProvider(session).async_list_calendars()
