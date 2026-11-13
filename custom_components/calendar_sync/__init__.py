"""Calendar Sync: editable Google and iCloud calendars for Home Assistant."""

from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_URL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_entry_oauth2_flow

from .caldav_provider import CalDavProvider
from .const import CONF_CALENDARS, CONF_PROVIDER, PROVIDER_GOOGLE
from .coordinator import CalendarSyncCoordinator
from .google_provider import GoogleProvider
from .models import CalendarInfo, CalendarProvider, ProviderAuthError, ProviderError

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.CALENDAR]


@dataclass
class RuntimeData:
    """Objects shared by the platforms of one config entry."""

    provider: CalendarProvider
    calendars: list[CalendarInfo]
    coordinator: CalendarSyncCoordinator


type CalendarSyncConfigEntry = ConfigEntry[RuntimeData]


async def async_build_provider(
    hass: HomeAssistant, entry: ConfigEntry
) -> CalendarProvider:
    """Create the backend client for a config entry."""
    if entry.data[CONF_PROVIDER] == PROVIDER_GOOGLE:
        implementation = (
            await config_entry_oauth2_flow.async_get_config_entry_implementation(
                hass, entry
            )
        )
        session = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)
        return GoogleProvider(session)
    return CalDavProvider(
        hass,
        entry.data[CONF_URL],
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        entry.data.get(CONF_VERIFY_SSL, True),
    )


async def async_setup_entry(hass: HomeAssistant, entry: CalendarSyncConfigEntry) -> bool:
    """Set up one account."""
    provider = await async_build_provider(hass, entry)
    try:
        available = await provider.async_list_calendars()
    except ProviderAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except ProviderError as err:
        raise ConfigEntryNotReady(str(err)) from err

    selected = set(entry.options.get(CONF_CALENDARS, entry.data.get(CONF_CALENDARS, [])))
    calendars = [c for c in available if not selected or c.id in selected]

    coordinator = CalendarSyncCoordinator(hass, entry, provider, calendars)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = RuntimeData(provider, calendars, coordinator)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: CalendarSyncConfigEntry) -> bool:
    """Unload an account."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
