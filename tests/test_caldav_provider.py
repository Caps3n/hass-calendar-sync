"""CalDAV backend: create, read, update and delete against an in-memory calendar."""

from datetime import date, datetime, timedelta, timezone

import icalendar
import pytest

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.calendar_sync.caldav_provider import CalDavProvider
from custom_components.calendar_sync.models import EventData

from .fake_caldav import FakeCalendar

CAL = "https://caldav.example.com/cal/home/"
UTC = timezone.utc

SERIES = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:test
BEGIN:VEVENT
UID:series-1
DTSTART:20261110T090000Z
DTEND:20261110T100000Z
RRULE:FREQ=DAILY;COUNT=5
SUMMARY:Standup
END:VEVENT
END:VCALENDAR
"""

SINGLE = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:test
BEGIN:VEVENT
UID:single-1
DTSTART:20261112T150000Z
DTEND:20261112T160000Z
SUMMARY:Dentist
LOCATION:Town
END:VEVENT
END:VCALENDAR
"""

WINDOW = (datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 12, 1, tzinfo=UTC))


@pytest.fixture
async def setup(hass: HomeAssistant):
    await hass.config.async_set_time_zone("Europe/Berlin")
    fake = FakeCalendar()
    fake.add(SERIES)
    fake.add(SINGLE)
    provider = CalDavProvider(hass, "https://caldav.example.com/", "u", "p")
    provider._calendars[CAL] = fake
    return provider, fake


def _ev(summary, start, end, rrule=None, location=None):
    return EventData(summary=summary, start=start, end=end, rrule=rrule, location=location)


async def _events(provider):
    events = await provider.async_get_events(CAL, *WINDOW)
    return sorted(events, key=lambda e: (e.start_datetime_local, e.summary))


async def test_read_expands_series(setup) -> None:
    provider, _ = setup
    events = await _events(provider)
    standups = [e for e in events if e.summary == "Standup"]
    assert len(standups) == 5
    assert all(e.uid == "series-1" for e in standups)
    assert standups[0].recurrence_id == "20261110T090000Z"
    assert standups[0].rrule == "FREQ=DAILY;COUNT=5"
    dentist = next(e for e in events if e.summary == "Dentist")
    assert dentist.recurrence_id is None
    assert dentist.location == "Town"


async def test_create_and_delete_single(setup) -> None:
    provider, fake = setup
    tz = dt_util.get_default_time_zone()
    await provider.async_create_event(
        CAL, _ev("Party", datetime(2026, 11, 20, 19, tzinfo=tz), datetime(2026, 11, 20, 23, tzinfo=tz))
    )
    party = next(e for e in await _events(provider) if e.summary == "Party")
    assert party.start == datetime(2026, 11, 20, 19, tzinfo=tz)
    await provider.async_delete_event(CAL, party.uid)
    assert all(e.summary != "Party" for e in await _events(provider))


async def test_create_all_day(setup) -> None:
    provider, _ = setup
    await provider.async_create_event(CAL, _ev("Holiday", date(2026, 11, 25), date(2026, 11, 26)))
    holiday = next(e for e in await _events(provider) if e.summary == "Holiday")
    assert holiday.start == date(2026, 11, 25)
    assert holiday.all_day


async def test_update_single_event(setup) -> None:
    provider, _ = setup
    start = datetime(2026, 11, 12, 16, tzinfo=UTC)
    await provider.async_update_event(
        CAL, "single-1", _ev("Dentist (moved)", start, start + timedelta(hours=1))
    )
    dentist = next(e for e in await _events(provider) if e.uid == "single-1")
    assert dentist.summary == "Dentist (moved)"
    assert dentist.start == start
    assert dentist.location is None


async def test_update_one_occurrence(setup) -> None:
    provider, _ = setup
    start = datetime(2026, 11, 12, 11, tzinfo=UTC)
    await provider.async_update_event(
        CAL,
        "series-1",
        _ev("Standup (late)", start, start + timedelta(hours=1)),
        recurrence_id="20261112T090000Z",
    )
    standups = [e for e in await _events(provider) if e.uid == "series-1"]
    assert len(standups) == 5
    moved = [e for e in standups if e.summary == "Standup (late)"]
    assert len(moved) == 1
    assert moved[0].start == start
    assert moved[0].recurrence_id == "20261112T090000Z"


async def test_update_this_and_future_splits_series(setup) -> None:
    provider, _ = setup
    start = datetime(2026, 11, 12, 8, tzinfo=UTC)
    await provider.async_update_event(
        CAL,
        "series-1",
        _ev("Standup v2", start, start + timedelta(minutes=30), rrule="FREQ=DAILY;COUNT=3"),
        recurrence_id="20261112T090000Z",
        recurrence_range="THISANDFUTURE",
    )
    events = await _events(provider)
    old = [e for e in events if e.uid == "series-1"]
    new = [e for e in events if e.summary == "Standup v2"]
    assert [e.start.day for e in old] == [10, 11]
    assert [e.start.day for e in new] == [12, 13, 14]
    assert all(e.start.astimezone(UTC).hour == 8 for e in new)


async def test_update_this_and_future_from_first_edits_series(setup) -> None:
    provider, fake = setup
    start = datetime(2026, 11, 10, 7, tzinfo=UTC)
    await provider.async_update_event(
        CAL,
        "series-1",
        _ev("Early standup", start, start + timedelta(hours=1)),
        recurrence_id="20261110T090000Z",
        recurrence_range="THISANDFUTURE",
    )
    standups = [e for e in await _events(provider) if e.uid == "series-1"]
    assert len(standups) == 5
    assert all(e.summary == "Early standup" and e.start.astimezone(UTC).hour == 7 for e in standups)


async def test_delete_one_occurrence(setup) -> None:
    provider, fake = setup
    await provider.async_delete_event(CAL, "series-1", recurrence_id="20261111T090000Z")
    standups = [e for e in await _events(provider) if e.uid == "series-1"]
    assert [e.start.day for e in standups] == [10, 12, 13, 14]
    master = icalendar.Calendar.from_ical(fake.objects["series-1"]).walk("VEVENT")[0]
    assert "EXDATE" in master


async def test_delete_this_and_future(setup) -> None:
    provider, _ = setup
    await provider.async_delete_event(
        CAL, "series-1", recurrence_id="20261112T090000Z", recurrence_range="THISANDFUTURE"
    )
    standups = [e for e in await _events(provider) if e.uid == "series-1"]
    assert [e.start.day for e in standups] == [10, 11]


async def test_delete_this_and_future_from_first_removes_series(setup) -> None:
    provider, fake = setup
    await provider.async_delete_event(
        CAL, "series-1", recurrence_id="20261110T090000Z", recurrence_range="THISANDFUTURE"
    )
    assert "series-1" not in fake.objects


async def test_override_survives_then_deleted(setup) -> None:
    provider, fake = setup
    start = datetime(2026, 11, 13, 12, tzinfo=UTC)
    await provider.async_update_event(
        CAL, "series-1", _ev("Moved", start, start + timedelta(hours=1)),
        recurrence_id="20261113T090000Z",
    )
    await provider.async_delete_event(CAL, "series-1", recurrence_id="20261113T090000Z")
    standups = [e for e in await _events(provider) if e.uid == "series-1"]
    assert [e.start.day for e in standups] == [10, 11, 12, 14]
    assert all(e.summary == "Standup" for e in standups)


BERLIN_SERIES = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:test
BEGIN:VTIMEZONE
TZID:Europe/Berlin
BEGIN:STANDARD
DTSTART:19701025T030000
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:19700329T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU
TZOFFSETFROM:+0100
TZOFFSETTO:+0200
END:DAYLIGHT
END:VTIMEZONE
BEGIN:VEVENT
UID:berlin-1
DTSTART;TZID=Europe/Berlin:20261116T180000
DTEND;TZID=Europe/Berlin:20261116T190000
RRULE:FREQ=WEEKLY
SUMMARY:Training
END:VEVENT
END:VCALENDAR
"""


async def test_tzid_series_delete_and_cut(setup) -> None:
    provider, fake = setup
    fake.add(BERLIN_SERIES)
    trainings = [e for e in await _events(provider) if e.uid == "berlin-1"]
    assert [e.start.day for e in trainings] == [16, 23, 30]
    assert trainings[0].start_datetime_local.hour == 18
    await provider.async_delete_event(CAL, "berlin-1", recurrence_id=trainings[1].recurrence_id)
    trainings = [e for e in await _events(provider) if e.uid == "berlin-1"]
    assert [e.start.day for e in trainings] == [16, 30]
    await provider.async_delete_event(
        CAL, "berlin-1", recurrence_id=trainings[1].recurrence_id, recurrence_range="THISANDFUTURE"
    )
    trainings = [e for e in await _events(provider) if e.uid == "berlin-1"]
    assert [e.start.day for e in trainings] == [16]
