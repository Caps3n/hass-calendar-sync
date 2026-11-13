"""Calendar entities with create, update and delete support."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import voluptuous as vol

from homeassistant.components.calendar import (
    CalendarEntity,
    CalendarEntityFeature,
    CalendarEvent,
)
from homeassistant.core import (
    HomeAssistant,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import CalendarSyncConfigEntry
from .const import (
    CONF_PROVIDER,
    DOMAIN,
    PROVIDER_GOOGLE,
    SERVICE_DELETE_EVENT,
    SERVICE_LIST_EVENTS,
    SERVICE_UPDATE_EVENT,
    THIS_AND_FUTURE,
)
from .coordinator import CalendarSyncCoordinator
from .models import CalendarInfo, EventData, ProviderError

ATTR_UID = "uid"
ATTR_RECURRENCE_ID = "recurrence_id"
ATTR_RECURRENCE_RANGE = "recurrence_range"
ATTR_SUMMARY = "summary"
ATTR_DESCRIPTION = "description"
ATTR_LOCATION = "location"
ATTR_START = "start"
ATTR_END = "end"
ATTR_RRULE = "rrule"

_RANGE = vol.In(["", THIS_AND_FUTURE])

UPDATE_SCHEMA: dict[Any, Any] = {
    vol.Required(ATTR_UID): cv.string,
    vol.Optional(ATTR_RECURRENCE_ID): cv.string,
    vol.Optional(ATTR_RECURRENCE_RANGE): _RANGE,
    vol.Optional(ATTR_SUMMARY): cv.string,
    vol.Optional(ATTR_DESCRIPTION): cv.string,
    vol.Optional(ATTR_LOCATION): cv.string,
    vol.Optional(ATTR_START): vol.Any(cv.datetime, cv.date),
    vol.Optional(ATTR_END): vol.Any(cv.datetime, cv.date),
    vol.Optional(ATTR_RRULE): cv.string,
}
DELETE_SCHEMA: dict[Any, Any] = {
    vol.Required(ATTR_UID): cv.string,
    vol.Optional(ATTR_RECURRENCE_ID): cv.string,
    vol.Optional(ATTR_RECURRENCE_RANGE): _RANGE,
}
LIST_SCHEMA: dict[Any, Any] = {
    vol.Optional(ATTR_START): cv.datetime,
    vol.Optional(ATTR_END): cv.datetime,
}

FULL_FEATURES = (
    CalendarEntityFeature.CREATE_EVENT
    | CalendarEntityFeature.UPDATE_EVENT
    | CalendarEntityFeature.DELETE_EVENT
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CalendarSyncConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one entity per selected calendar."""
    data = entry.runtime_data
    async_add_entities(
        SyncCalendarEntity(data.coordinator, entry, calendar) for calendar in data.calendars
    )

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_UPDATE_EVENT, UPDATE_SCHEMA, "async_service_update_event"
    )
    platform.async_register_entity_service(
        SERVICE_DELETE_EVENT, DELETE_SCHEMA, "async_service_delete_event"
    )
    platform.async_register_entity_service(
        SERVICE_LIST_EVENTS,
        LIST_SCHEMA,
        "async_service_list_events",
        supports_response=SupportsResponse.ONLY,
    )


def _as_local(value: date | datetime) -> date | datetime:
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=dt_util.get_default_time_zone())
    return value


class SyncCalendarEntity(CoordinatorEntity[CalendarSyncCoordinator], CalendarEntity):
    """One Google or CalDAV calendar."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: CalendarSyncCoordinator,
        entry: CalendarSyncConfigEntry,
        calendar: CalendarInfo,
    ) -> None:
        super().__init__(coordinator)
        self._calendar = calendar
        self._attr_name = calendar.name
        self._attr_unique_id = f"{entry.entry_id}_{calendar.id}"
        self._attr_supported_features = (
            FULL_FEATURES if calendar.writable else CalendarEntityFeature(0)
        )
        provider_name = (
            "Google Calendar" if entry.data[CONF_PROVIDER] == PROVIDER_GOOGLE else "CalDAV"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer=provider_name,
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def event(self) -> CalendarEvent | None:
        """The current or next upcoming event."""
        now = dt_util.now()
        for event in (self.coordinator.data or {}).get(self._calendar.id, []):
            if event.end_datetime_local > now:
                return event
        return None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        try:
            return await self.coordinator.provider.async_get_events(
                self._calendar.id, start_date, end_date
            )
        except ProviderError as err:
            raise HomeAssistantError(f"Could not load events: {err}") from err

    # -- write operations used by the dashboard and calendar.create_event --

    def _require_writable(self) -> None:
        if not self._calendar.writable:
            raise ServiceValidationError(f"Calendar {self._calendar.name} is read-only")

    async def async_create_event(self, **kwargs: Any) -> None:
        self._require_writable()
        try:
            await self.coordinator.provider.async_create_event(
                self._calendar.id, EventData.from_ha(kwargs)
            )
        except ProviderError as err:
            raise HomeAssistantError(f"Could not create event: {err}") from err
        await self.coordinator.async_request_refresh()

    async def async_update_event(
        self,
        uid: str,
        event: dict[str, Any],
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        self._require_writable()
        try:
            await self.coordinator.provider.async_update_event(
                self._calendar.id,
                uid,
                EventData.from_ha(event),
                recurrence_id or None,
                recurrence_range or None,
            )
        except ProviderError as err:
            raise HomeAssistantError(f"Could not update event: {err}") from err
        await self.coordinator.async_request_refresh()

    async def async_delete_event(
        self,
        uid: str,
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        self._require_writable()
        try:
            await self.coordinator.provider.async_delete_event(
                self._calendar.id, uid, recurrence_id or None, recurrence_range or None
            )
        except ProviderError as err:
            raise HomeAssistantError(f"Could not delete event: {err}") from err
        await self.coordinator.async_request_refresh()

    # -- entity services for automations ---------------------------------

    async def _find_event(self, uid: str, recurrence_id: str | None) -> CalendarEvent | None:
        for event in (self.coordinator.data or {}).get(self._calendar.id, []):
            if event.uid == uid and (recurrence_id is None or event.recurrence_id == recurrence_id):
                return event
        return None

    async def async_service_update_event(self, **kwargs: Any) -> None:
        uid = kwargs[ATTR_UID]
        recurrence_id = kwargs.get(ATTR_RECURRENCE_ID) or None
        current = await self._find_event(uid, recurrence_id)
        if current is None and not (ATTR_START in kwargs and ATTR_END in kwargs):
            raise ServiceValidationError(
                "Event not found in the next 60 days; pass start and end explicitly"
            )
        start = _as_local(kwargs.get(ATTR_START, current.start if current else None))
        end = _as_local(kwargs.get(ATTR_END, current.end if current else None))
        if isinstance(start, datetime) != isinstance(end, datetime):
            raise ServiceValidationError("start and end must both be dates or both datetimes")
        if end <= start:
            if isinstance(start, datetime):
                raise ServiceValidationError("end must be after start")
            end = start + timedelta(days=1)
        event = {
            "summary": kwargs.get(ATTR_SUMMARY, current.summary if current else ""),
            "dtstart": start,
            "dtend": end,
            "description": kwargs.get(ATTR_DESCRIPTION, current.description if current else None),
            "location": kwargs.get(ATTR_LOCATION, current.location if current else None),
            "rrule": kwargs.get(ATTR_RRULE, current.rrule if current else None),
        }
        await self.async_update_event(
            uid, event, recurrence_id, kwargs.get(ATTR_RECURRENCE_RANGE) or None
        )

    async def async_service_delete_event(self, **kwargs: Any) -> None:
        await self.async_delete_event(
            kwargs[ATTR_UID],
            kwargs.get(ATTR_RECURRENCE_ID) or None,
            kwargs.get(ATTR_RECURRENCE_RANGE) or None,
        )

    async def async_service_list_events(self, **kwargs: Any) -> ServiceResponse:
        start = kwargs.get(ATTR_START) or dt_util.now()
        end = kwargs.get(ATTR_END) or start + timedelta(days=7)
        start, end = dt_util.as_local(start), dt_util.as_local(end)
        events = await self.async_get_events(self.hass, start, end)
        return {
            "events": [
                {
                    "uid": e.uid,
                    "recurrence_id": e.recurrence_id,
                    "summary": e.summary,
                    "start": e.start.isoformat(),
                    "end": e.end.isoformat(),
                    "all_day": e.all_day,
                    "description": e.description,
                    "location": e.location,
                    "rrule": e.rrule,
                }
                for e in events
            ]
        }
