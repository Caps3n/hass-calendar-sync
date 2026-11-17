"""Polls each selected calendar so the entity state stays current."""

from __future__ import annotations

import logging

from homeassistant.components.calendar import CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import DOMAIN, LOOKAHEAD, LOOKBACK, UPDATE_INTERVAL
from .models import CalendarInfo, CalendarProvider, ProviderAuthError, ProviderError

_LOGGER = logging.getLogger(__name__)


class CalendarSyncCoordinator(DataUpdateCoordinator[dict[str, list[CalendarEvent]]]):
    """Holds the upcoming events of every calendar of one account."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        provider: CalendarProvider,
        calendars: list[CalendarInfo],
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=UPDATE_INTERVAL,
        )
        self.provider = provider
        self.calendars = calendars

    async def _async_update_data(self) -> dict[str, list[CalendarEvent]]:
        now = dt_util.now()
        start, end = now - LOOKBACK, now + LOOKAHEAD
        previous = self.data or {}
        data: dict[str, list[CalendarEvent]] = {}
        errors: list[str] = []
        for calendar in self.calendars:
            try:
                events = await self.provider.async_get_events(calendar.id, start, end)
            except ProviderAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except ProviderError as err:
                # One failing calendar must not take the others down with it.
                _LOGGER.warning("Could not load events of %s: %s", calendar.name, err)
                errors.append(f"{calendar.name}: {err}")
                data[calendar.id] = previous.get(calendar.id, [])
                continue
            data[calendar.id] = sorted(events, key=lambda e: e.start_datetime_local)
        if self.calendars and len(errors) == len(self.calendars):
            raise UpdateFailed("; ".join(errors))
        return data
