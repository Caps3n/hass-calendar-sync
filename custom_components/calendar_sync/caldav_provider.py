"""CalDAV backend (iCloud and any other CalDAV server)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timezone
import logging
import threading
from typing import Any, TypeVar
from urllib.parse import quote, urljoin
import uuid

import caldav
from caldav.lib import error as caldav_error
import icalendar
import recurring_ical_events
import requests

from homeassistant.components.calendar import CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import THIS_AND_FUTURE
from .models import (
    CalendarInfo,
    CalendarProvider,
    EventData,
    ProviderAuthError,
    ProviderError,
    calendar_key,
    format_recurrence_id,
    normalize_event_times,
    parse_recurrence_id,
    rrule_with_until,
    same_instant,
    until_before,
)

_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")

PRODID = "-//Calendar Sync for Home Assistant//EN"
# Seconds a single CalDAV request may take before it is given up.
REQUEST_TIMEOUT = 30


def _to_ical_value(value: date | datetime) -> date | datetime:
    """Write aware datetimes as UTC so no VTIMEZONE is required."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt_util.get_default_time_zone())
        return value.astimezone(timezone.utc)
    return value


def _match_type(value: date | datetime, like: date | datetime) -> date | datetime:
    """Convert an occurrence start to the value type used by the series DTSTART."""
    if not isinstance(like, datetime):
        return value.date() if isinstance(value, datetime) else value
    if not isinstance(value, datetime):
        value = datetime.combine(value, like.timetz() if like.tzinfo else like.time())
    if like.tzinfo is None:
        if value.tzinfo is not None:
            value = value.astimezone(dt_util.get_default_time_zone()).replace(tzinfo=None)
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt_util.get_default_time_zone())
    return value.astimezone(like.tzinfo)


def _rrule_str(component: icalendar.cal.Component) -> str | None:
    rrule = component.get("RRULE")
    if rrule is None:
        return None
    if isinstance(rrule, list):
        rrule = rrule[0]
    return rrule.to_ical().decode()


def _set_fields(vevent: icalendar.Event, event: EventData, *, keep_rrule: bool) -> None:
    """Write the editable fields of an event into a VEVENT."""
    for key in ("SUMMARY", "DTSTART", "DTEND", "DURATION", "DESCRIPTION", "LOCATION"):
        vevent.pop(key, None)
    vevent.add("SUMMARY", event.summary)
    vevent.add("DTSTART", _to_ical_value(event.start))
    vevent.add("DTEND", _to_ical_value(event.end))
    if event.description:
        vevent.add("DESCRIPTION", event.description)
    if event.location:
        vevent.add("LOCATION", event.location)
    if not keep_rrule:
        vevent.pop("RRULE", None)
        if event.rrule:
            vevent.add("RRULE", icalendar.vRecur.from_ical(event.rrule))
    _touch(vevent)


def _touch(vevent: icalendar.Event) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    for key in ("DTSTAMP", "LAST-MODIFIED"):
        vevent.pop(key, None)
        vevent.add(key, now)
    sequence = int(vevent.get("SEQUENCE", 0)) + 1
    vevent.pop("SEQUENCE", None)
    vevent.add("SEQUENCE", sequence)


def _new_vevent(event: EventData, uid: str | None = None) -> icalendar.Event:
    vevent = icalendar.Event()
    vevent.add("UID", uid or f"{uuid.uuid4()}@calendar-sync")
    _set_fields(vevent, event, keep_rrule=False)
    return vevent


def _new_calendar(*vevents: icalendar.Event) -> icalendar.Calendar:
    cal = icalendar.Calendar()
    cal.add("PRODID", PRODID)
    cal.add("VERSION", "2.0")
    for vevent in vevents:
        cal.add_component(vevent)
    return cal


def _split_components(
    cal: icalendar.Calendar, uid: str
) -> tuple[icalendar.Event | None, list[icalendar.Event]]:
    """Return the master VEVENT and the override VEVENTs for a UID."""
    master = None
    overrides = []
    for comp in cal.walk("VEVENT"):
        if str(comp.get("UID")) != uid:
            continue
        if comp.get("RECURRENCE-ID") is None:
            master = comp
        else:
            overrides.append(comp)
    return master, overrides


def _remove_overrides(
    cal: icalendar.Calendar, keep: Callable[[icalendar.Event], bool]
) -> None:
    cal.subcomponents = [
        comp
        for comp in cal.subcomponents
        if comp.name != "VEVENT" or comp.get("RECURRENCE-ID") is None or keep(comp)
    ]


def _end_series_before(master: icalendar.Event, occurrence: date | datetime) -> None:
    """Cut a series so it stops right before the given occurrence."""
    rrule = _rrule_str(master)
    if rrule is None:
        return
    series_start = master["DTSTART"].dt
    until = until_before(_match_type(occurrence, series_start))
    if isinstance(until, datetime) and isinstance(series_start, datetime):
        if series_start.tzinfo is None:
            # Floating series: UNTIL must be floating too.
            until = until.astimezone(dt_util.get_default_time_zone()).replace(tzinfo=None)
    master.pop("RRULE", None)
    master.add("RRULE", icalendar.vRecur.from_ical(rrule_with_until(rrule, until)))
    _touch(master)


def _occurrence_on_or_after(comp: icalendar.Event, occurrence: date | datetime) -> bool:
    rid = comp["RECURRENCE-ID"].dt
    occ = _match_type(occurrence, rid)
    return same_instant(rid, occ) or rid > occ


class CalDavProvider(CalendarProvider):
    """Talks to a CalDAV server with the `caldav` library."""

    def __init__(
        self,
        hass: HomeAssistant,
        url: str,
        username: str,
        password: str,
        verify_ssl: bool = True,
    ) -> None:
        self._hass = hass
        # Without a timeout a server that does not answer blocks the request
        # forever: setup then stalls Home Assistant's startup ("Bootstrap stage 2
        # timeout") instead of failing and being retried later.
        self._client = caldav.DAVClient(
            url=url,
            username=username,
            password=password,
            ssl_verify_cert=verify_ssl,
            timeout=REQUEST_TIMEOUT,
        )
        self._calendars: dict[str, caldav.Calendar] = {}
        # (calendar id, UID) -> object URL, learned while reading. iCloud rejects
        # REPORT queries that filter by UID (412), so edits load objects by URL.
        self._hrefs: dict[tuple[str, str], str] = {}
        # The dashboard asks for all calendars at once. One DAVClient (and its
        # HTTP session) is shared by all of them and is not thread safe, so
        # requests are made one after the other.
        self._lock = threading.Lock()

    def _locked(self, func: Callable[..., _T], *args: Any) -> _T:
        with self._lock:
            return func(*args)

    async def _run(self, func: Callable[..., _T], *args: Any) -> _T:
        try:
            return await self._hass.async_add_executor_job(self._locked, func, *args)
        except caldav_error.AuthorizationError as err:
            raise ProviderAuthError(str(err)) from err
        except ProviderError:
            raise
        except (caldav_error.DAVError, requests.RequestException, ValueError) as err:
            _LOGGER.debug("CalDAV request failed", exc_info=True)
            raise ProviderError(f"CalDAV request failed: {err}") from err
        except Exception as err:
            # caldav 2.x/3.x raise transport errors of other HTTP libraries
            # (niquests, httpx) and calendar data can trip icalendar or
            # recurring-ical-events. Never let those escape as a bare exception:
            # Home Assistant would only report an unknown error.
            _LOGGER.warning("CalDAV request failed unexpectedly", exc_info=True)
            raise ProviderError(
                f"CalDAV request failed: {type(err).__name__}: {err}"
            ) from err

    # -- discovery ---------------------------------------------------------

    def _list_calendars(self) -> list[CalendarInfo]:
        principal = self._client.principal()
        result = []
        for cal in principal.calendars():
            try:
                components = cal.get_supported_components()
            except Exception:  # noqa: BLE001 - not all servers report this
                components = ["VEVENT"]
            if "VEVENT" not in components:
                continue
            cal_id = str(cal.url)
            self._calendars[cal_id] = cal
            result.append(CalendarInfo(id=cal_id, name=cal.name or cal_id))
        return result

    async def async_list_calendars(self) -> list[CalendarInfo]:
        return await self._run(self._list_calendars)

    def _calendar(self, calendar_id: str) -> caldav.Calendar:
        if calendar_id not in self._calendars:
            key = calendar_key(calendar_id)
            found = next(
                (cal for cal_id, cal in self._calendars.items() if calendar_key(cal_id) == key),
                None,
            )
            self._calendars[calendar_id] = found or self._client.calendar(url=calendar_id)
        return self._calendars[calendar_id]

    # -- reading -----------------------------------------------------------

    def _get_events(
        self, calendar_id: str, start: datetime, end: datetime
    ) -> list[CalendarEvent]:
        calendar = self._calendar(calendar_id)
        objects = calendar.search(start=start, end=end, event=True, expand=False)
        events: list[CalendarEvent] = []
        for obj in objects:
            try:
                events.extend(self._object_events(calendar_id, obj, start, end))
            except Exception:  # noqa: BLE001 - one broken object must not hide the rest
                _LOGGER.warning(
                    "Skipping calendar object %s that could not be read",
                    getattr(obj, "url", None),
                    exc_info=True,
                )
        _LOGGER.debug(
            "Loaded %d events from %s (%d objects) between %s and %s",
            len(events),
            calendar_id,
            len(objects),
            start,
            end,
        )
        return events

    def _object_events(
        self, calendar_id: str, obj: Any, start: datetime, end: datetime
    ) -> list[CalendarEvent]:
        cal = obj.icalendar_instance
        if getattr(obj, "url", None) is not None:
            for comp in cal.walk("VEVENT"):
                self._hrefs[(calendar_id, str(comp.get("UID")))] = str(obj.url)
        rrules = {
            str(c.get("UID")): _rrule_str(c)
            for c in cal.walk("VEVENT")
            if c.get("RECURRENCE-ID") is None
        }
        events: list[CalendarEvent] = []
        for vevent in recurring_ical_events.of(cal).between(start, end):
            if str(vevent.get("STATUS", "")).upper() == "CANCELLED":
                continue
            event = self._to_ha_event(vevent, rrules)
            if event is not None:
                events.append(event)
        return events

    @staticmethod
    def _to_ha_event(
        vevent: icalendar.Event, rrules: dict[str, str | None]
    ) -> CalendarEvent | None:
        if "DTSTART" not in vevent:
            return None
        uid = str(vevent.get("UID", ""))
        try:
            start = vevent["DTSTART"].dt
            end = vevent["DTEND"].dt if "DTEND" in vevent else None
            if end is None and "DURATION" in vevent:
                end = start + vevent["DURATION"].dt
            start, end = normalize_event_times(start, end)
            recurrence_id = None
            rrule = rrules.get(uid)
            if rrule is not None and vevent.get("RECURRENCE-ID") is not None:
                recurrence_id = format_recurrence_id(vevent["RECURRENCE-ID"].dt)
            fields: dict[str, Any] = {
                "start": start,
                "end": end,
                "summary": str(vevent.get("SUMMARY", "")),
                "description": (
                    str(vevent["DESCRIPTION"]) if "DESCRIPTION" in vevent else None
                ),
                "location": str(vevent["LOCATION"]) if "LOCATION" in vevent else None,
                "uid": uid,
                "recurrence_id": recurrence_id,
            }
        except Exception as err:  # noqa: BLE001 - skip malformed server data
            _LOGGER.warning("Skipping event %s: %s", uid, err)
            return None
        if recurrence_id and rrule:
            try:
                return CalendarEvent(**fields, rrule=rrule)
            except Exception as err:  # noqa: BLE001
                # Home Assistant only accepts RRULEs that dateutil parses on
                # their own; show the occurrence anyway.
                _LOGGER.debug("Dropping RRULE %r of %s: %s", rrule, uid, err)
        try:
            return CalendarEvent(**fields)
        except Exception as err:  # noqa: BLE001 - skip malformed server data
            _LOGGER.warning("Skipping event %s: %s", uid, err)
            return None

    async def async_get_events(
        self, calendar_id: str, start: datetime, end: datetime
    ) -> list[CalendarEvent]:
        return await self._run(self._get_events, calendar_id, start, end)

    # -- writing -----------------------------------------------------------

    def _create(self, calendar_id: str, event: EventData) -> None:
        vevent = _new_vevent(event)
        obj = self._calendar(calendar_id).save_event(_new_calendar(vevent).to_ical().decode())
        if getattr(obj, "url", None) is not None:
            self._hrefs[(calendar_id, str(vevent["UID"]))] = str(obj.url)

    async def async_create_event(self, calendar_id: str, event: EventData) -> None:
        await self._run(self._create, calendar_id, event)

    def _load(self, calendar_id: str, uid: str) -> caldav.Event:
        """Find the object holding a UID, trying cheap lookups first."""
        calendar = self._calendar(calendar_id)

        def has_uid(obj: Any) -> bool:
            return any(
                str(c.get("UID")) == uid for c in obj.icalendar_instance.walk("VEVENT")
            )

        candidates: list[str] = []
        if href := self._hrefs.get((calendar_id, uid)):
            candidates.append(href)
        candidates.append(urljoin(calendar_id.rstrip("/") + "/", f"{quote(uid, safe='@')}.ics"))
        for href in candidates:
            try:
                obj = calendar.event_by_url(href)
                if has_uid(obj):
                    return obj
            except caldav_error.AuthorizationError:
                raise
            except (caldav_error.DAVError, ValueError):
                _LOGGER.debug("Lookup of %s by URL %s failed", uid, href)

        try:
            return calendar.event_by_uid(uid)
        except caldav_error.AuthorizationError:
            raise
        except caldav_error.DAVError:
            _LOGGER.debug("Server rejected UID search for %s, scanning calendar", uid)

        for obj in calendar.events():
            if has_uid(obj):
                if obj.url is not None:
                    self._hrefs[(calendar_id, uid)] = str(obj.url)
                return obj
        raise ValueError(f"Event {uid} not found")

    def _update(
        self,
        calendar_id: str,
        uid: str,
        event: EventData,
        recurrence_id: str | None,
        recurrence_range: str | None,
    ) -> None:
        obj = self._load(calendar_id, uid)
        cal = obj.icalendar_instance
        master, overrides = _split_components(cal, uid)
        if master is None:
            raise ValueError(f"Event {uid} has no master component")

        if recurrence_id is None or _rrule_str(master) is None:
            _set_fields(master, event, keep_rrule=False)
            obj.icalendar_instance = cal
            obj.save()
            return

        occurrence = parse_recurrence_id(recurrence_id)
        series_start = master["DTSTART"].dt

        if recurrence_range == THIS_AND_FUTURE:
            if same_instant(_match_type(occurrence, series_start), series_start):
                # Editing from the first occurrence edits the whole series.
                _set_fields(master, event, keep_rrule=not event.rrule)
                _remove_overrides(cal, keep=lambda _c: False)
                obj.icalendar_instance = cal
                obj.save()
                return
            rrule = event.rrule or _rrule_str(master)
            _end_series_before(master, occurrence)
            _remove_overrides(cal, keep=lambda c: not _occurrence_on_or_after(c, occurrence))
            obj.icalendar_instance = cal
            obj.save()
            new_event = EventData(
                summary=event.summary,
                start=event.start,
                end=event.end,
                description=event.description,
                location=event.location,
                rrule=rrule,
            )
            self._create(calendar_id, new_event)
            return

        # A single occurrence: write (or rewrite) an override component.
        rid_value = _match_type(occurrence, series_start)
        target = next(
            (c for c in overrides if same_instant(c["RECURRENCE-ID"].dt, rid_value)),
            None,
        )
        if target is None:
            target = icalendar.Event()
            target.add("UID", uid)
            target.add("RECURRENCE-ID", rid_value)
            cal.add_component(target)
        single = EventData(
            summary=event.summary,
            start=event.start,
            end=event.end,
            description=event.description,
            location=event.location,
        )
        _set_fields(target, single, keep_rrule=False)
        obj.icalendar_instance = cal
        obj.save()

    async def async_update_event(
        self,
        calendar_id: str,
        uid: str,
        event: EventData,
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        await self._run(self._update, calendar_id, uid, event, recurrence_id, recurrence_range)

    def _delete(
        self,
        calendar_id: str,
        uid: str,
        recurrence_id: str | None,
        recurrence_range: str | None,
    ) -> None:
        obj = self._load(calendar_id, uid)
        cal = obj.icalendar_instance
        master, _overrides = _split_components(cal, uid)

        if recurrence_id is None or master is None or _rrule_str(master) is None:
            obj.delete()
            return

        occurrence = parse_recurrence_id(recurrence_id)
        series_start = master["DTSTART"].dt
        rid_value = _match_type(occurrence, series_start)

        if recurrence_range == THIS_AND_FUTURE:
            if same_instant(rid_value, series_start):
                obj.delete()
                return
            _end_series_before(master, occurrence)
            _remove_overrides(cal, keep=lambda c: not _occurrence_on_or_after(c, occurrence))
        else:
            master.add("EXDATE", rid_value)
            _touch(master)
            _remove_overrides(
                cal, keep=lambda c: not same_instant(c["RECURRENCE-ID"].dt, rid_value)
            )
        obj.icalendar_instance = cal
        obj.save()

    async def async_delete_event(
        self,
        calendar_id: str,
        uid: str,
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        await self._run(self._delete, calendar_id, uid, recurrence_id, recurrence_range)
