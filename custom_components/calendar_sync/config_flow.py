"""Config flow: add an iCloud/CalDAV or Google account and pick calendars."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import callback
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .caldav_provider import CalDavProvider
from .const import (
    CONF_CALENDARS,
    CONF_PROVIDER,
    DOMAIN,
    GOOGLE_API_BASE,
    GOOGLE_SCOPE,
    ICLOUD_CALDAV_URL,
    PROVIDER_CALDAV,
    PROVIDER_GOOGLE,
)
from .models import CalendarInfo, ProviderAuthError, ProviderError

_LOGGER = logging.getLogger(__name__)


def _calendar_schema(calendars: list[CalendarInfo], default: list[str]) -> vol.Schema:
    options = [
        SelectOptionDict(value=c.id, label=c.name if c.writable else f"{c.name} (read-only)")
        for c in calendars
    ]
    return vol.Schema(
        {
            vol.Required(CONF_CALENDARS, default=default): SelectSelector(
                SelectSelectorConfig(options=options, multiple=True)
            )
        }
    )


async def _google_calendars(
    hass, access_token: str
) -> tuple[list[CalendarInfo], str | None]:
    session = async_get_clientsession(hass)
    resp = await session.get(
        f"{GOOGLE_API_BASE}/users/me/calendarList",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"maxResults": 250},
    )
    resp.raise_for_status()
    data = await resp.json()
    return [
        CalendarInfo(
            id=item["id"],
            name=item.get("summaryOverride") or item.get("summary") or item["id"],
            writable=item.get("accessRole") in ("owner", "writer"),
        )
        for item in data.get("items", [])
    ], next((i["id"] for i in data.get("items", []) if i.get("primary")), None)


class CalendarSyncConfigFlow(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN
):
    """Handle the setup of one account."""

    DOMAIN = DOMAIN
    VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        self._data: dict[str, Any] = {}
        self._title = ""
        self._calendars: list[CalendarInfo] = []

    @property
    def logger(self) -> logging.Logger:
        return _LOGGER

    @property
    def extra_authorize_data(self) -> dict[str, Any]:
        return {"scope": GOOGLE_SCOPE, "access_type": "offline", "prompt": "consent"}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return CalendarSyncOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=["caldav", "google"])

    # -- Google ------------------------------------------------------------

    async def async_step_google(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_pick_implementation(user_input)

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> ConfigFlowResult:
        try:
            calendars, primary = await _google_calendars(
                self.hass, data["token"]["access_token"]
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Could not list Google calendars")
            return self.async_abort(reason="cannot_connect")

        unique_id = primary or data["token"]["access_token"][:16]
        await self.async_set_unique_id(f"google_{unique_id}")
        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data_updates=data
            )
        self._abort_if_unique_id_configured()

        self._data = {CONF_PROVIDER: PROVIDER_GOOGLE, **data}
        self._title = f"Google ({primary})" if primary else "Google"
        self._calendars = calendars
        return await self.async_step_calendars()

    # -- CalDAV / iCloud --------------------------------------------------

    async def async_step_caldav(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(
                f"caldav_{user_input[CONF_USERNAME]}@{user_input[CONF_URL]}"
            )
            self._abort_if_unique_id_configured()
            error, calendars = await self._test_caldav(user_input)
            if error is None:
                self._data = {CONF_PROVIDER: PROVIDER_CALDAV, **user_input}
                self._calendars = calendars
                is_icloud = "icloud.com" in user_input[CONF_URL]
                self._title = (
                    f"iCloud ({user_input[CONF_USERNAME]})"
                    if is_icloud
                    else f"CalDAV ({user_input[CONF_USERNAME]})"
                )
                return await self.async_step_calendars()
            errors["base"] = error

        defaults = user_input or {}
        schema = vol.Schema(
            {
                vol.Required(CONF_URL, default=defaults.get(CONF_URL, ICLOUD_CALDAV_URL)): str,
                vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "")): str,
                vol.Required(CONF_PASSWORD): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
                vol.Optional(CONF_VERIFY_SSL, default=defaults.get(CONF_VERIFY_SSL, True)): bool,
            }
        )
        return self.async_show_form(step_id="caldav", data_schema=schema, errors=errors)

    async def _test_caldav(
        self, data: Mapping[str, Any]
    ) -> tuple[str | None, list[CalendarInfo]]:
        provider = CalDavProvider(
            self.hass,
            data[CONF_URL],
            data[CONF_USERNAME],
            data[CONF_PASSWORD],
            data.get(CONF_VERIFY_SSL, True),
        )
        try:
            calendars = await provider.async_list_calendars()
        except ProviderAuthError:
            return "invalid_auth", []
        except ProviderError:
            _LOGGER.exception("CalDAV connection failed")
            return "cannot_connect", []
        if not calendars:
            return "no_calendars", []
        return None, calendars

    # -- shared ------------------------------------------------------------

    async def async_step_calendars(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title=self._title,
                data={**self._data, CONF_CALENDARS: user_input[CONF_CALENDARS]},
            )
        default = [c.id for c in self._calendars if c.writable]
        return self.async_show_form(
            step_id="calendars", data_schema=_calendar_schema(self._calendars, default)
        )

    # -- reauth ------------------------------------------------------------

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        if entry_data[CONF_PROVIDER] == PROVIDER_GOOGLE:
            return await self.async_step_reauth_confirm()
        return await self.async_step_reauth_caldav()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_pick_implementation()

    async def async_step_reauth_caldav(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
            error, _ = await self._test_caldav(data)
            if error is None:
                return self.async_update_reload_and_abort(entry, data=data)
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_caldav",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    )
                }
            ),
            description_placeholders={CONF_USERNAME: entry.data[CONF_USERNAME]},
            errors=errors,
        )


class CalendarSyncOptionsFlow(OptionsFlow):
    """Change which calendars become entities."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        entry = self.config_entry
        try:
            calendars = await entry.runtime_data.provider.async_list_calendars()
        except (ProviderError, AttributeError):
            return self.async_abort(reason="cannot_connect")
        current = entry.options.get(CONF_CALENDARS, entry.data.get(CONF_CALENDARS, []))
        return self.async_show_form(
            step_id="init", data_schema=_calendar_schema(calendars, current)
        )
