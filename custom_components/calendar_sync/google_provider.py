"""Google Calendar backend (REST API v3 over Home Assistant's OAuth2 session)."""

from __future__ import annotations

from datetime import date, datetime
from http import HTTPStatus
import logging
from typing import Any
from urllib.parse import quote

from aiohttp import ClientError, ClientResponseError

from homeassistant.components.calendar import CalendarEvent
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.util import dt as dt_util

from .const import GOOGLE_API_BASE, THIS_AND_FUTURE
from .models import (
    CalendarInfo,
    CalendarProvider,
    EventData,
    ProviderAuthError,
    ProviderError,
    normalize_event_times,
    rrule_with_until,
    same_instant,
    until_before,
)

_LOGGER = logging.getLogger(__name__)

WRITE_ROLES = {"owner", "writer"}


def _parse_time(value: dict[str, Any]) -> date | datetime:
    if "dateTime" in value:
        parsed = dt_util.parse_datetime(value["dateTime"])
        if parsed is None:
            raise ValueError(f"Bad dateTime {value}")
        return parsed
    return date.fromisoformat(value["date"])


def _format_time(value: date | datetime) -> dict[str, str]:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt_util.get_default_time_zone())
        return {"dateTime": value.isoformat(), "timeZone": str(dt_util.get_default_time_zone())}
    return {"date": value.isoformat()}


def _body(event: EventData, *, include_recurrence: bool = True) -> dict[str, Any]:
    body: dict[str, Any] = {
        "summary": event.summary,
        "description": event.description or "",
        "location": event.location or "",
        "start": _format_time(event.start),
        "end": _format_time(event.end),
    }
    if include_recurrence:
        body["recurrence"] = [f"RRULE:{event.rrule}"] if event.rrule else []
    return body


def _rrule_from(recurrence: list[str] | None) -> str | None:
    for line in recurrence or []:
        if line.upper().startswith("RRULE:"):
            return line[6:]
    return None


class GoogleProvider(CalendarProvider):
    """Uses the Google Calendar REST API."""

    def __init__(self, session: config_entry_oauth2_flow.OAuth2Session) -> None:
        self._session = session

    async def _request(
        self, method: str, path: str, *, params: dict | None = None, json: Any = None
    ) -> dict[str, Any]:
        try:
            await self._session.async_ensure_token_valid()
            resp = await self._session.async_request(
                method, f"{GOOGLE_API_BASE}{path}", params=params, json=json
            )
        except ClientResponseError as err:
            if err.status in (HTTPStatus.BAD_REQUEST, HTTPStatus.UNAUTHORIZED):
                raise ProviderAuthError(str(err)) from err
            raise ProviderError(str(err)) from err
        except ClientError as err:
            raise ProviderError(f"Google request failed: {err}") from err

        if resp.status == HTTPStatus.UNAUTHORIZED:
            raise ProviderAuthError("Google rejected the access token")
        if resp.status == HTTPStatus.NO_CONTENT:
            return {}
        if resp.status >= 400:
            text = await resp.text()
            raise ProviderError(f"Google API error {resp.status}: {text[:300]}")
        return await resp.json()

    async def async_list_calendars(self) -> list[CalendarInfo]:
        result: list[CalendarInfo] = []
        params: dict[str, Any] = {"maxResults": 250}
        while True:
            data = await self._request("GET", "/users/me/calendarList", params=params)
            for item in data.get("items", []):
                result.append(
                    CalendarInfo(
                        id=item["id"],
                        name=item.get("summaryOverride") or item.get("summary") or item["id"],
                        writable=item.get("accessRole") in WRITE_ROLES,
                    )
                )
            if not (token := data.get("nextPageToken")):
                return result
            params["pageToken"] = token

    def _path(self, calendar_id: str, event_id: str | None = None) -> str:
        path = f"/calendars/{quote(calendar_id, safe='')}/events"
        if event_id:
            path += f"/{quote(event_id, safe='')}"
        return path

    async def async_get_events(
        self, calendar_id: str, start: datetime, end: datetime
    ) -> list[CalendarEvent]:
        params: dict[str, Any] = {
            "timeMin": dt_util.as_utc(start).isoformat(),
            "timeMax": dt_util.as_utc(end).isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": 2500,
        }
        items: list[dict[str, Any]] = []
        while True:
            data = await self._request("GET", self._path(calendar_id), params=params)
            items.extend(data.get("items", []))
            if not (token := data.get("nextPageToken")):
                break
            params["pageToken"] = token

        rrules: dict[str, str | None] = {}
        for series_id in {i["recurringEventId"] for i in items if "recurringEventId" in i}:
            try:
                master = await self._request("GET", self._path(calendar_id, series_id))
                rrules[series_id] = _rrule_from(master.get("recurrence"))
            except ProviderError:
                rrules[series_id] = None

        events = []
        for item in items:
            if item.get("status") == "cancelled" or "start" not in item:
                continue
            try:
                start_v, end_v = normalize_event_times(
                    _parse_time(item["start"]), _parse_time(item["end"]) if "end" in item else None
                )
                series_id = item.get("recurringEventId")
                events.append(
                    CalendarEvent(
                        start=start_v,
                        end=end_v,
                        summary=item.get("summary", ""),
                        description=item.get("description"),
                        location=item.get("location"),
                        uid=series_id or item["id"],
                        # The instance id identifies one occurrence of a series.
                        recurrence_id=item["id"] if series_id else None,
                        rrule=rrules.get(series_id) if series_id else None,
                    )
                )
            except Exception as err:  # noqa: BLE001 - skip malformed items
                _LOGGER.debug("Skipping Google event %s: %s", item.get("id"), err)
        return events

    async def async_create_event(self, calendar_id: str, event: EventData) -> None:
        await self._request("POST", self._path(calendar_id), json=_body(event))

    async def _get(self, calendar_id: str, event_id: str) -> dict[str, Any]:
        return await self._request("GET", self._path(calendar_id, event_id))

    async def _is_first_instance(self, master: dict, instance: dict) -> bool:
        original = instance.get("originalStartTime") or instance["start"]
        return same_instant(_parse_time(original), _parse_time(master["start"]))

    async def _end_series_before(self, calendar_id: str, master: dict, instance: dict) -> None:
        rrule = _rrule_from(master.get("recurrence"))
        if rrule is None:
            return
        original = _parse_time(instance.get("originalStartTime") or instance["start"])
        until = until_before(original)
        others = [r for r in master.get("recurrence", []) if not r.upper().startswith("RRULE:")]
        await self._request(
            "PATCH",
            self._path(calendar_id, master["id"]),
            json={"recurrence": [f"RRULE:{rrule_with_until(rrule, until)}", *others]},
        )

    async def async_update_event(
        self,
        calendar_id: str,
        uid: str,
        event: EventData,
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        if recurrence_id is None:
            await self._request("PATCH", self._path(calendar_id, uid), json=_body(event))
            return

        if recurrence_range != THIS_AND_FUTURE:
            await self._request(
                "PATCH",
                self._path(calendar_id, recurrence_id),
                json=_body(event, include_recurrence=False),
            )
            return

        master = await self._get(calendar_id, uid)
        instance = await self._get(calendar_id, recurrence_id)
        if await self._is_first_instance(master, instance):
            body = _body(event, include_recurrence=bool(event.rrule))
            await self._request("PATCH", self._path(calendar_id, uid), json=body)
            return

        rrule = event.rrule or _rrule_from(master.get("recurrence"))
        await self._end_series_before(calendar_id, master, instance)
        new_event = EventData(
            summary=event.summary,
            start=event.start,
            end=event.end,
            description=event.description,
            location=event.location,
            rrule=rrule,
        )
        await self.async_create_event(calendar_id, new_event)

    async def async_delete_event(
        self,
        calendar_id: str,
        uid: str,
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        if recurrence_id is None:
            await self._request("DELETE", self._path(calendar_id, uid))
            return
        if recurrence_range != THIS_AND_FUTURE:
            await self._request("DELETE", self._path(calendar_id, recurrence_id))
            return
        master = await self._get(calendar_id, uid)
        instance = await self._get(calendar_id, recurrence_id)
        if await self._is_first_instance(master, instance):
            await self._request("DELETE", self._path(calendar_id, uid))
            return
        await self._end_series_before(calendar_id, master, instance)

